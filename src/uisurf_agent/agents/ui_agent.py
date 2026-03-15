from __future__ import annotations

import inspect
import logging
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

    user_input = input(f"Allow the agent to execute '{function_call.name}'? (y/n): ")
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
    ) -> None:
        """Initialize shared agent dependencies and behavior flags.

        Args:
            auto_confirm: Whether safety-gated actions should be approved
                automatically instead of prompting the user.
            client: Optional preconfigured Google GenAI client. When omitted, a
                default client instance is created.
            config: Optional model configuration stored for subclasses that want
                to reuse or extend a shared config object.
        """
        self.auto_confirm = auto_confirm
        self.client = client or genai.Client()
        self.config = config
        self._safety_prompt_handler: SafetyPromptHandler = default_safety_prompt_handler

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
                response = await self.reason(task, history)
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
                    yield AgentEvent(
                        eventType="message",
                        payload={"text": result.message},
                        isFinal=result.done,
                    )

                if result.done:
                    return
        except Exception as exc:
            logger.exception("An unexpected error occurred during agent execution: %s", exc)
            yield AgentEvent(
                eventType="message",
                payload={"text": f"An unexpected error occurred during agent execution: {exc!s}"},
                isFinal=True,
            )
        finally:
            self._safety_prompt_handler = previous_safety_prompt_handler
