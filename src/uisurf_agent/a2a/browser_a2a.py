from __future__ import annotations

"""A2A server for the browser automation agent.

This module adapts ``uisurf_agent.BrowserAgent`` to the A2A server protocol so the
browser automation workflow can be reached through a standard HTTP agent endpoint.

Runtime flow:
1. ``start_a2a_server`` loads configuration and publishes browser-agent metadata.
2. ``BrowserAgentExecutor.execute`` receives each inbound task request.
3. The executor ensures a ``BrowserAgent`` instance is initialized.
4. Streamed browser-agent events are forwarded to A2A clients as task updates.
5. The final event is attached to the task as the ``result`` artifact.

The module's responsibility is protocol translation and lifecycle wiring; browser
navigation, page interaction, and reasoning remain inside ``BrowserAgent``.
"""

import os
import logging

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TaskState, Part, TextPart
from a2a.utils import (
    new_agent_text_message,
    new_task,
)
from dotenv import load_dotenv

from uisurf_agent.agents import BrowserAgent


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class BrowserAgentExecutor(AgentExecutor):
    """Bridge ``BrowserAgent`` to the A2A executor contract.

    The executor receives A2A requests and translates them into browser-agent runs.
    It manages the A2A task lifecycle and forwards the underlying agent's streamed
    events back to clients using ``TaskUpdater``.

    A single ``BrowserAgent`` instance is cached on the executor and initialized on
    demand. That keeps server startup light while still reusing the agent runtime
    across requests.
    """

    def __init__(self) -> None:
        """Create executor state with deferred agent construction."""
        self.agent: BrowserAgent | None = None

    async def _ensure_initialized(self) -> None:
        """Create and initialize the browser agent if it is not ready.

        ``BrowserAgent.initialize`` is called for every request after ensuring the
        object exists. This matches the current implementation expectation that the
        browser agent may need per-request readiness even when the instance is reused.
        """
        if self.agent is None:
            self.agent = BrowserAgent()
        await self.agent.initialize()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Handle one browser-agent A2A request.

        Args:
            context: Request wrapper containing the user message and any existing
                task state.
            event_queue: A2A event sink used to publish task progress and results.

        Behavior:
        - ensures the browser agent is initialized
        - creates a new task when the request does not already reference one
        - marks the task as ``working`` before running the agent
        - emits intermediate agent events as serialized JSON text updates
        - stores the final event as the ``result`` artifact and completes the task
        - marks the task as failed if the agent raises an exception

        The serialized JSON format is preserved so A2A consumers can reconstruct the
        full browser-agent event payload rather than only receiving extracted text.
        """
        await self._ensure_initialized()
        assert self.agent is not None

        query = context.get_user_input()
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        try:
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    "Starting browser agent execution...",
                    task.context_id,
                    task.id,
                ),
            )
            async for event in self.agent.run(query, max_steps=20):
                is_final = event.isFinal
                json_str = event.model_dump_json()

                if not is_final:
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(
                            json_str, task.context_id, task.id
                        ),
                    )
                    continue

                await updater.add_artifact(
                    [Part(root=TextPart(text=json_str))], name='result'
                )
                await updater.complete()
                await self.agent.cleanup()
                break

        except Exception as e:
            await updater.update_status(
                TaskState.failed,
                new_agent_text_message(f"Error: {e!s}", task.context_id, task.id),
                final=True,
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Accept cancellation requests without interrupting background work.

        The current executor does not maintain per-task cancellation handles or a
        detached worker pool, so there is nothing additional to stop here.
        """
        del context, event_queue
        return


def start_a2a_server(host: str | None = None, port: int | None = None) -> None:
    """Configure and start the browser-agent A2A HTTP server.

    Environment:
        ``AGENT_HOST``: Default host interface to bind. Defaults to ``localhost``.
        ``BROWSER_AGENT_PORT``: Default HTTP port for this server. Defaults to ``8001``.

    Args:
        host: Optional host override. When omitted, ``AGENT_HOST`` is used.
        port: Optional port override. When omitted, ``BROWSER_AGENT_PORT`` is used.

    Setup performed by this function:
    - load ``.env`` values
    - define advertised browser capabilities and skill metadata
    - construct the A2A ``AgentCard``
    - wire the executor and in-memory task store into the request handler
    - start the Starlette application with ``uvicorn``
    """
    load_dotenv()

    host = host or os.environ.get("AGENT_HOST", "localhost")
    port = port or int(os.environ.get("BROWSER_AGENT_PORT", "8001"))
    public_url = os.environ.get("BROWSER_AGENT_PUBLIC_URL", f"http://{host}:{port}/")
    capabilities = AgentCapabilities(streaming=True, push_notifications=True)
    skill = AgentSkill(
        id="browser_operator",
        name="Browser Operator",
        description=(
            "Controls a browser, observes screenshots, navigates websites, and "
            "answers questions based on the current page and prior browser history."
        ),
        tags=["browser", "automation", "web", "comparison", "screenshots"],
        examples=[
            "Go to Amazon and Best Buy, compare the MacBook Pro prices, and tell me which is cheaper.",
            "Open the product page and summarize the details visible in the screenshots.",
        ],
    )

    agent_card = AgentCard(
        name="BrowserOperatorAgent",
        description="A browser automation and web observation agent powered by Gemini computer use.",
        url=public_url,
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=capabilities,
        skills=[skill],
    )

    request_handler = DefaultRequestHandler(
        agent_executor=BrowserAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )

    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    uvicorn.run(server.build(), host=host, port=port)


if __name__ == "__main__":
    start_a2a_server()
