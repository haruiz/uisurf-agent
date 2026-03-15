from __future__ import annotations

"""A2A server for the desktop automation agent.

This module exposes a small A2A-compatible HTTP server that wraps
``uisurf_agent.DesktopAgent`` behind the A2A server interfaces.

Runtime flow:
1. ``start_a2a_server`` loads environment variables and publishes an agent card.
2. ``DesktopAgentExecutor.execute`` receives each inbound A2A request.
3. The executor lazily initializes ``DesktopAgent`` on first use.
4. Agent events are streamed back through the A2A ``EventQueue`` as task updates.
5. The final agent event is stored as an A2A artifact named ``result``.

The implementation is intentionally thin: it delegates nearly all desktop control
and reasoning to ``DesktopAgent`` and focuses on translating between the agent's
event stream and A2A task lifecycle updates.
"""

import os

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TaskState, Part, TextPart
from a2a.utils import new_agent_text_message, new_task
from dotenv import load_dotenv

from uisurf_agent.agents import DesktopAgent


class DesktopAgentExecutor(AgentExecutor):
    """Bridge ``DesktopAgent`` to the A2A executor interface.

    The A2A framework calls this executor for each incoming task. The executor is
    responsible for:
    - ensuring the underlying desktop agent exists and is initialized
    - creating or resuming the A2A task record
    - forwarding streamed agent events to A2A clients as task status updates
    - publishing the final structured agent response as an A2A artifact

    The executor keeps a single agent instance on ``self.agent`` and initializes it
    lazily so server startup stays cheap.
    """

    def __init__(self) -> None:
        """Create the executor state.

        The wrapped ``DesktopAgent`` instance is not constructed at import time or
        server startup time. That avoids paying the cost of desktop automation setup
        until the first real request arrives.
        """
        self.agent: DesktopAgent | None = None

    async def _ensure_initialized(self) -> None:
        """Create and initialize the desktop agent if needed.

        ``DesktopAgent`` owns the expensive runtime setup for desktop automation.
        This helper ensures that setup only happens once per executor instance.
        Subsequent requests reuse the same in-memory agent object.
        """
        if self.agent is None:
            self.agent = DesktopAgent(auto_confirm=True)
            await self.agent.initialize()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Handle one A2A task from request to completion.

        Args:
            context: A2A request wrapper containing the incoming message, any current
                task state, and helper methods such as ``get_user_input``.
            event_queue: Queue used by the A2A framework to publish task lifecycle
                events back to connected clients.

        Behavior:
        - Initializes the desktop agent if this is the first request.
        - Creates a new task when the request is not already tied to one.
        - Marks the task as ``working`` before invoking the agent.
        - Streams non-final agent events back as serialized JSON text messages.
        - Stores the final event as an artifact named ``result`` and completes the task.
        - Marks the task as failed if any exception escapes the agent loop.

        Notes:
        - The streamed payload is the agent event serialized with ``model_dump_json``.
          That preserves the full event structure for A2A consumers.
        - The agent is explicitly cleaned up after a final event is emitted.
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
        """Acknowledge cancellation requests without extra cleanup.

        This executor processes requests inline and does not currently track a
        separately cancellable background coroutine per task. As a result, the A2A
        cancellation hook is a no-op.
        """
        del context, event_queue
        return


async def _run_agent_to_text(agent: DesktopAgent, prompt: str, max_steps: int = 30) -> str:
    """Run the desktop agent and flatten message events into plain text.

    Args:
        agent: Initialized ``DesktopAgent`` instance to execute.
        prompt: Natural-language instruction to send to the agent.
        max_steps: Upper bound on the number of reasoning/action iterations.

    Returns:
        A newline-joined string containing every ``message`` event text emitted by
        the agent. If the agent finishes without any text-bearing message events, a
        fallback string is returned instead.

    This helper is useful when a caller wants a simple text transcript rather than
    the richer structured A2A task/event model used by ``execute``.
    """
    messages: list[str] = []
    async for event in agent.run(prompt, max_steps=max_steps):
        if event.eventType == "message":
            text = event.payload.get("text")
            if text:
                messages.append(text)
    return "\n".join(messages) if messages else "The desktop agent completed without a text response."


def start_a2a_server(host: str | None = None, port: int | None = None) -> None:
    """Configure and launch the desktop-agent A2A HTTP server.

    Environment:
        ``AGENT_HOST``: Default host interface to bind. Defaults to ``localhost``.
        ``DESKTOP_AGENT_PORT``: Default HTTP port for this server. Defaults to ``8002``.

    Args:
        host: Optional host override. When omitted, ``AGENT_HOST`` is used.
        port: Optional port override. When omitted, ``DESKTOP_AGENT_PORT`` is used.

    Server setup performed here:
    - load environment variables from ``.env``
    - define the A2A agent capabilities and advertised skill metadata
    - construct the A2A ``AgentCard`` shown to clients
    - wire the ``DesktopAgentExecutor`` into the default request handler
    - launch the Starlette application through ``uvicorn``
    """
    load_dotenv()

    host = host or os.environ.get("AGENT_HOST", "localhost")
    port = port or int(os.environ.get("DESKTOP_AGENT_PORT", "8002"))
    public_url = os.environ.get("DESKTOP_AGENT_PUBLIC_URL", f"http://{host}:{port}/")
    capabilities = AgentCapabilities(streaming=True, push_notifications=True)
    skill = AgentSkill(
        id="desktop_operator",
        name="Desktop Operator",
        description=(
            "Controls desktop applications, observes screenshots, opens tools like "
            "Terminal or editors, and answers questions about what was seen on screen."
        ),
        tags=["desktop", "automation", "terminal", "applications", "screenshots"],
        examples=[
            "Open Terminal and run `pwd`, then tell me the result shown on screen.",
            "Open a text editor, write a note, and describe what is visible afterward.",
        ],
    )

    agent_card = AgentCard(
        name="DesktopOperatorAgent",
        description="A desktop automation and screenshot observation agent powered by Gemini computer use.",
        url=public_url,
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=capabilities,
        skills=[skill],
    )

    request_handler = DefaultRequestHandler(
        agent_executor=DesktopAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )

    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    uvicorn.run(server.build(), host=host, port=port)


if __name__ == "__main__":
    start_a2a_server()
