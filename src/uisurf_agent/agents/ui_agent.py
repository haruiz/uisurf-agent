from __future__ import annotations

import inspect
import logging
import sys
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

load_dotenv(verbose=True)

SafetyPromptDecision = tuple[bool, bool]
SafetyPromptHandler = Callable[[Any, bool], Awaitable[SafetyPromptDecision] | SafetyPromptDecision]


async def default_safety_prompt_handler(
    function_call: Any,
    auto_confirm: bool,
) -> SafetyPromptDecision:
    """Resolve a safety-gated action using the default terminal prompt flow.

    Args:
        function_call: Model-emitted function call that may include a
            `safety_decision` request in its arguments.
        auto_confirm: Whether confirmation should be granted automatically.

    Returns:
        A tuple of `(safety_acknowledged, allowed)`. The first element indicates
        whether the action required and received explicit acknowledgement. The
        second indicates whether execution should proceed.
    """
    safety_decision = function_call.args.get("safety_decision")
    if not (
        safety_decision
        and safety_decision.get("decision") == "require_confirmation"
    ):
        return False, True

    explanation = safety_decision.get("explanation", "No explanation provided.")
    logger.debug("Safety prompt: %s", explanation)
    if auto_confirm:
        return True, True

    if not sys.stdin or not sys.stdin.isatty():
        logger.warning(
            "Safety confirmation requested for '%s' without an interactive stdin. Denying the action.",
            function_call.name,
        )
        return False, False

    try:
        user_input = input(f"Allow the agent to execute '{function_call.name}'? (y/n): ")
    except EOFError:
        logger.warning(
            "Safety confirmation requested for '%s' but stdin returned EOF. Denying the action.",
            function_call.name,
        )
        return False, False

    allowed = user_input.strip().lower() in {"y", "yes"}
    if not allowed:
        logger.debug("Action denied by user.")
        return False, False

    return True, True


async def resolve_safety_prompt(
    handler: SafetyPromptHandler,
    function_call: Any,
    auto_confirm: bool,
) -> SafetyPromptDecision:
    """Execute a safety prompt handler and await it when necessary.

    Args:
        handler: Callback supplied by the caller or the default prompt handler.
        function_call: Model-emitted function call under review.
        auto_confirm: Whether confirmation should be granted automatically when
            the active handler chooses to honor that flag.

    Returns:
        A tuple of `(safety_acknowledged, allowed)`.
    """
    result = handler(function_call, auto_confirm)
    if inspect.isawaitable(result):
        return await result
    return result


@dataclass
class AgentStepResult:
    """Structured result returned by a single agent step.

    Concrete agents use this object to tell the shared `UIAgent.run()` loop what
    happened after the latest reasoning and action phase. `done` signals whether
    the loop should terminate, `message` carries any user-visible output produced
    by the step, and `observation` contains the next piece of model input to
    append to the conversation history before the following iteration.
    """

    done: bool = False
    message: str | None = None
    observation: Any | None = None
    events: list["AgentEvent"] = field(default_factory=list)


class AgentEvent(BaseModel):
    """Structured event emitted while the agent is running.

    These events are designed to be streamed to external consumers such as a
    frontend. `eventType` identifies the category of event, `payload` contains
    event-specific data in a serializable form, and `isFinal` indicates whether
    this is the terminal event for the current agent run.
    """

    model_config = ConfigDict(extra="forbid")

    eventType: str
    payload: dict[str, Any]
    isFinal: bool = False


class UIAgent(ABC):
    """Abstract base class for agents that observe interfaces, reason, and act.

    This class owns the high-level orchestration loop while delegating the
    environment-specific details to subclasses. A concrete implementation is
    expected to define how the current UI state is observed, how a model is asked
    to reason about the next step, how model responses are stored in history, and
    how proposed actions are executed against the target environment.
    """

    def __init__(
        self,
        auto_confirm: bool = False,
        client: genai.Client | None = None,
        config: genai.types.GenerateContentConfig | None = None,
        max_observation_images: int = 2,
    ) -> None:
        """Initialize shared agent dependencies and behavior flags.

        Args:
            auto_confirm: Whether safety-gated actions should be approved
                automatically instead of prompting the user.
            client: Optional preconfigured Google GenAI client. When omitted, a
                default client instance is created.
            config: Optional model configuration stored for subclasses that want
                to reuse or extend a shared config object.
            max_observation_images: Maximum number of screenshot-bearing
                observations to keep with image payloads in model history.
        """
        if max_observation_images < 1:
            raise ValueError("max_observation_images must be greater than or equal to 1.")
        self.auto_confirm = auto_confirm
        self.client = client or genai.Client()
        self.config = config
        self._max_observation_images = max_observation_images
        self._safety_prompt_handler: SafetyPromptHandler = default_safety_prompt_handler

    def get_agent_event_name(self) -> str:
        """Return the stable agent identifier attached to streamed events."""
        return self.__class__.__name__

    def _with_agent_event_metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Attach agent metadata to an event payload when it is missing."""
        if "agent_name" in payload or "agentName" in payload:
            return payload
        return {
            **payload,
            "agent_name": self.get_agent_event_name(),
        }

    def _build_agent_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        is_final: bool = False,
    ) -> AgentEvent:
        """Create a structured event stamped with the emitting agent name."""
        return AgentEvent(
            eventType=event_type,
            payload=self._with_agent_event_metadata(payload),
            isFinal=is_final,
        )

    async def __aenter__(self) -> "UIAgent":
        """Enter the async context manager and initialize agent resources.

        Returns:
            The current agent instance so callers can use `async with` naturally.
        """
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the async context manager and release agent resources.

        Args:
            exc_type: Exception type raised inside the context, if any.
            exc_val: Exception instance raised inside the context, if any.
            exc_tb: Traceback associated with the exception, if any.
        """
        await self.cleanup()

    async def initialize(self) -> None:
        """Allocate resources required by the agent session.

        Subclasses can override this hook to start browsers, connect to remote
        services, or allocate any other runtime resources needed before the first
        observation occurs. The default implementation does nothing.
        """

    async def cleanup(self) -> None:
        """Release resources allocated by the agent session.

        Subclasses can override this hook to close browser instances, stop
        background tasks, or otherwise dispose of resources created during
        `initialize()`. The default implementation does nothing.
        """

    @abstractmethod
    async def observe(self, task: str, history: list[Any]) -> Any:
        """Capture the current environment state for the next model call.

        Args:
            task: The original user task being executed.
            history: The conversation or observation history accumulated so far.

        Returns:
            An object representing the latest observation, ready to be appended to
            the model conversation history.
        """

    @abstractmethod
    async def reason(self, task: str, history: list[Any]) -> Any:
        """Ask the underlying model to decide what should happen next.

        Args:
            task: The original user task being executed.
            history: The full interaction history, including prior observations
                and model turns.

        Returns:
            The raw model response object that will later be recorded and acted on.
        """

    @abstractmethod
    async def record_model_response(self, response: Any, history: list[Any]) -> None:
        """Persist the latest model response into the shared history structure.

        Args:
            response: The raw response returned by `reason()`.
            history: The mutable history list used by the shared run loop.
        """

    @abstractmethod
    async def act(self, response: Any, history: list[Any]) -> AgentStepResult:
        """Execute the action implied by the model response.

        Args:
            response: The raw response returned by `reason()`.
            history: The mutable history list used by the shared run loop.

        Returns:
            An `AgentStepResult` describing whether the run should continue,
            whether any user-visible message should be emitted, and whether a new
            observation should be appended for the next cycle.
        """

    async def run(
        self,
        task: str,
        max_steps: int = 10,
        safety_prompt_handler: SafetyPromptHandler | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Run the shared observe/reason/act loop for up to `max_steps` steps.

        The loop begins by capturing an initial observation, then repeatedly:
        1. asks the model to reason about the next step,
        2. records the model response in history,
        3. executes the selected action, and
        4. appends any resulting observation for the next iteration.

        Args:
            task: The high-level instruction the agent should complete.
            max_steps: Maximum number of reasoning/action iterations to run.
            safety_prompt_handler: Optional callback used to resolve safety-gated
                tool calls. The agent awaits this callback before continuing, so
                a UI can block execution until the user approves or denies.

        Yields:
            Structured `AgentEvent` objects describing streamed reasoning,
            actions, responses, and user-visible messages.
        """
        previous_safety_prompt_handler = self._safety_prompt_handler
        self._safety_prompt_handler = safety_prompt_handler or default_safety_prompt_handler
        try:
            history: list[Any] = [await self.observe(task, history=[])]

            for step in range(max_steps):
                logger.debug("Step %d/%d: %s", step + 1, max_steps, task)
                response = await self.reason(
                    task,
                    self.prepare_history_for_reasoning(task, history),
                )
                await self.record_model_response(response, history)
                result = await self.act(response, history)

                if result.observation is not None:
                    history.append(result.observation)

                for index, event in enumerate(result.events):
                    is_last_result_event = index == len(result.events) - 1
                    yield event.model_copy(
                        update={"isFinal": bool(result.done and not result.message and is_last_result_event)}
                    )

                if result.message:
                    yield self._build_agent_event(
                        "message",
                        {"text": result.message},
                        is_final=result.done,
                    )

                if result.done:
                    return
        except Exception as exc:
            logger.exception("An unexpected error occurred during agent execution: %s", exc)
            yield self._build_agent_event(
                "message",
                {"text": f"An unexpected error occurred during agent execution: {exc!s}"},
                is_final=True,
            )
        finally:
            self._safety_prompt_handler = previous_safety_prompt_handler

    def prepare_history_for_reasoning(self, task: str, history: list[Any]) -> list[Any]:
        """Return the model history view used for the next reasoning call."""
        del task
        if self._max_observation_images < 1:
            return history

        image_indexes = [
            index for index, item in enumerate(history) if self._content_has_image_payload(item)
        ]
        if len(image_indexes) <= self._max_observation_images:
            return history

        image_indexes_to_keep = set(image_indexes[-self._max_observation_images :])
        prepared_history: list[Any] = []
        for index, item in enumerate(history):
            if index in image_indexes_to_keep or index not in image_indexes:
                prepared_history.append(item)
                continue

            stripped_item = self._strip_images_from_content(item)
            if stripped_item is not None:
                prepared_history.append(stripped_item)

        return prepared_history

    def _content_has_image_payload(self, item: Any) -> bool:
        """Return whether a history item contains image data."""
        for part in getattr(item, "parts", None) or []:
            if self._part_has_image_payload(part):
                return True
        return False

    def _part_has_image_payload(self, part: Any) -> bool:
        """Return whether a model part or function response contains an image."""
        inline_data = getattr(part, "inline_data", None)
        mime_type = getattr(inline_data, "mime_type", None)
        if isinstance(mime_type, str) and mime_type.startswith("image/"):
            return True

        function_response = getattr(part, "function_response", None)
        if function_response is None:
            return False

        for response_part in getattr(function_response, "parts", None) or []:
            response_blob = getattr(response_part, "inline_data", None)
            response_mime_type = getattr(response_blob, "mime_type", None)
            if isinstance(response_mime_type, str) and response_mime_type.startswith("image/"):
                return True
        return False

    def _strip_images_from_content(self, item: Any) -> Any | None:
        """Return a content item with image payloads removed, when possible."""
        if not hasattr(item, "parts"):
            return item

        stripped_parts = []
        for part in getattr(item, "parts", None) or []:
            stripped_part = self._strip_images_from_part(part)
            if stripped_part is not None:
                stripped_parts.append(stripped_part)

        if not stripped_parts:
            return None

        return self._copy_model(item, parts=stripped_parts)

    def _strip_images_from_part(self, part: Any) -> Any | None:
        """Return a part without image payloads."""
        if self._part_has_direct_inline_image(part):
            return None

        function_response = getattr(part, "function_response", None)
        if function_response is None:
            return part

        original_response_parts = list(getattr(function_response, "parts", None) or [])
        stripped_response_parts = [
            response_part
            for response_part in original_response_parts
            if not self._response_part_has_inline_image(response_part)
        ]
        if len(stripped_response_parts) == len(original_response_parts):
            return part

        return self._copy_model(
            part,
            function_response=self._copy_model(
                function_response,
                parts=stripped_response_parts,
            ),
        )

    def _part_has_direct_inline_image(self, part: Any) -> bool:
        """Return whether a part directly embeds an image payload."""
        inline_data = getattr(part, "inline_data", None)
        mime_type = getattr(inline_data, "mime_type", None)
        return isinstance(mime_type, str) and mime_type.startswith("image/")

    def _response_part_has_inline_image(self, response_part: Any) -> bool:
        """Return whether a function response part embeds an image payload."""
        inline_data = getattr(response_part, "inline_data", None)
        mime_type = getattr(inline_data, "mime_type", None)
        return isinstance(mime_type, str) and mime_type.startswith("image/")

    def _copy_model(self, model: Any, **updates: Any) -> Any:
        """Clone a pydantic-style model with selected updates."""
        if hasattr(model, "model_copy"):
            return model.model_copy(update=updates)
        if hasattr(model, "copy"):
            return model.copy(update=updates)
        raise TypeError(f"Unsupported model copy for type {type(model)!r}")
