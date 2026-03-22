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
import json
import asyncio
from dataclasses import dataclass
from typing import Any

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TaskState, Part, TextPart
from a2a.utils import new_agent_text_message, new_task
from dotenv import load_dotenv

from uisurf_agent.agents import DesktopAgent
from uisurf_agent.agents.ui_agent import SafetyPromptDecision
from uisurf_agent.a2a.confirmation_request_handler import ConfirmationRequestHandler
from uisurf_agent.utils.config_utils import resolve_bool_config, resolve_int_config
from uisurf_agent.utils.screenshot_utils import resolve_observation_scale


@dataclass
class PendingSafetyPrompt:
    """One outstanding user confirmation request for a task."""

    future: asyncio.Future[SafetyPromptDecision]
    function_name: str
    explanation: str


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

    def __init__(
        self,
        observation_delay_ms: int = 1500,
        include_thoughts: bool = True,
        max_observation_images: int = 2,
        observation_scale: float = 1.0,
        auto_confirm: bool = False,
    ) -> None:
        """Create the executor state.

        The wrapped ``DesktopAgent`` instance is not constructed at import time or
        server startup time. That avoids paying the cost of desktop automation setup
        until the first real request arrives.
        """
        self.agent: DesktopAgent | None = None
        self._observation_delay_ms = observation_delay_ms
        self._include_thoughts = include_thoughts
        self._max_observation_images = max_observation_images
        self._observation_scale = observation_scale
        self._auto_confirm = auto_confirm
        self._pending_prompts: dict[str, PendingSafetyPrompt] = {}

    async def _ensure_initialized(self) -> None:
        """Create and initialize the desktop agent if needed.

        ``DesktopAgent`` owns the expensive runtime setup for desktop automation.
        This helper ensures that setup only happens once per executor instance.
        Subsequent requests reuse the same in-memory agent object.
        """
        if self.agent is None:
            self.agent = DesktopAgent(
                auto_confirm=self._auto_confirm,
                observation_delay_ms=self._observation_delay_ms,
                include_thoughts=self._include_thoughts,
                max_observation_images=self._max_observation_images,
                observation_scale=self._observation_scale,
            )
        await self.agent.initialize()

    def has_pending_prompt(self, task_id: str) -> bool:
        """Return whether the task is currently waiting on user confirmation."""
        return task_id in self._pending_prompts

    def build_pending_prompt_message(self, task_id: str) -> str:
        """Build the structured A2A payload shown while confirmation is pending."""
        prompt = self._pending_prompts[task_id]
        return json.dumps(
            {
                "eventType": "approval_required",
                "taskId": task_id,
                "status": {"state": "input_required"},
                "payload": {
                    "agent_name": "desktop_agent",
                    "name": prompt.function_name,
                    "args": {
                        "safety_decision": {
                            "decision": "require_confirmation",
                            "explanation": prompt.explanation,
                        }
                    },
                },
                "isFinal": False,
            }
        )

    def _parse_confirmation_reply(
        self,
        user_input: str,
    ) -> SafetyPromptDecision | None:
        """Parse a follow-up user reply into the two-bool safety decision."""
        normalized = user_input.strip().lower()
        if normalized in {"y", "yes", "allow", "approve", "continue"}:
            return True, True
        if normalized in {"n", "no", "deny", "reject", "cancel"}:
            return False, False
        return None

    def resolve_pending_prompt(self, task_id: str, user_input: str) -> bool:
        """Resolve the pending confirmation when the user reply is valid."""
        prompt = self._pending_prompts.get(task_id)
        if prompt is None:
            return False

        decision = self._parse_confirmation_reply(user_input)
        if decision is None:
            return False

        if not prompt.future.done():
            prompt.future.set_result(decision)
        return True

    def _build_safety_prompt_handler(
        self,
        updater: TaskUpdater,
        task_id: str,
        context_id: str,
    ):
        """Create an A2A-aware safety prompt callback for this task run."""

        async def handler(
            function_call: Any,
            auto_confirm: bool,
        ) -> SafetyPromptDecision:
            safety_decision = function_call.args.get("safety_decision")
            if not (
                safety_decision
                and safety_decision.get("decision") == "require_confirmation"
            ):
                return False, True

            if auto_confirm:
                return True, True

            explanation = safety_decision.get("explanation", "No explanation provided.")
            prompt = PendingSafetyPrompt(
                future=asyncio.get_running_loop().create_future(),
                function_name=function_call.name,
                explanation=explanation,
            )
            self._pending_prompts[task_id] = prompt

            await updater.update_status(
                TaskState.input_required,
                new_agent_text_message(
                    self.build_pending_prompt_message(task_id),
                    context_id,
                    task_id,
                ),
            )

            try:
                return await prompt.future
            finally:
                if self._pending_prompts.get(task_id) is prompt:
                    self._pending_prompts.pop(task_id, None)

        return handler

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
                    "Starting desktop agent execution...",
                    task.context_id,
                    task.id,
                ),
            )
            async for event in self.agent.run(
                query,
                max_steps=20,
                safety_prompt_handler=self._build_safety_prompt_handler(
                    updater,
                    task.id,
                    task.context_id,
                ),
            ):
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
        if context.task_id:
            prompt = self._pending_prompts.pop(context.task_id, None)
            if prompt is not None and not prompt.future.done():
                prompt.future.cancel()
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


def start_a2a_server(
    host: str | None = None,
    port: int | None = None,
    observation_delay_ms: int | None = None,
    include_thoughts: bool | None = None,
    max_observation_images: int | None = None,
    observation_scale: float | None = None,
    auto_confirm: bool | None = None,
) -> None:
    """Configure and launch the desktop-agent A2A HTTP server.

    Environment:
        ``AGENT_HOST``: Default host interface to bind. Defaults to ``localhost``.
        ``DESKTOP_AGENT_PORT``: Default HTTP port for this server. Defaults to ``8002``.
        ``DESKTOP_OBSERVATION_DELAY_MS``: Delay before each desktop screenshot
        capture in milliseconds.
        ``DESKTOP_INCLUDE_THOUGHTS``: Desktop-only override for model thought
        streaming. Falls back to ``INCLUDE_THOUGHTS``.
        ``MAX_OBSERVATION_IMAGES``: Maximum number of screenshot observations to
        keep with image payloads in model history.
        ``DESKTOP_OBSERVATION_SCALE``: Optional screenshot scale override for the
        desktop agent. Falls back to ``OBSERVATION_SCALE`` or ``1.0``.
        ``DESKTOP_AUTO_CONFIRM``: Whether safety-gated desktop actions should be
        auto-approved. Falls back to ``AUTO_CONFIRM`` and defaults to ``false``.

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
    resolved_observation_delay_ms = resolve_int_config(
        observation_delay_ms,
        "DESKTOP_OBSERVATION_DELAY_MS",
        default=1500,
        minimum=0,
    )
    resolved_include_thoughts = resolve_bool_config(
        include_thoughts,
        "DESKTOP_INCLUDE_THOUGHTS",
        default=resolve_bool_config(None, "INCLUDE_THOUGHTS", default=True),
    )
    resolved_max_observation_images = resolve_int_config(
        max_observation_images,
        "MAX_OBSERVATION_IMAGES",
        default=2,
        minimum=1,
    )
    resolved_observation_scale = resolve_observation_scale(
        observation_scale,
        "DESKTOP_OBSERVATION_SCALE",
    )
    resolved_auto_confirm = resolve_bool_config(
        auto_confirm,
        "DESKTOP_AUTO_CONFIRM",
        default=resolve_bool_config(None, "AUTO_CONFIRM", default=False),
    )
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

    request_handler = ConfirmationRequestHandler(
        agent_executor=DesktopAgentExecutor(
            observation_delay_ms=resolved_observation_delay_ms,
            include_thoughts=resolved_include_thoughts,
            max_observation_images=resolved_max_observation_images,
            observation_scale=resolved_observation_scale,
            auto_confirm=resolved_auto_confirm,
        ),
        task_store=InMemoryTaskStore(),
    )

    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    uvicorn.run(server.build(), host=host, port=port)


if __name__ == "__main__":
    start_a2a_server()
