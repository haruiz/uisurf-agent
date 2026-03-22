from __future__ import annotations

"""Browser-specific UI agent backed by Gemini computer-use and Playwright.

This module adapts the shared `UIAgent` lifecycle to a browser environment. It
captures screenshot observations from Playwright, configures the Gemini browser
computer-use tool, executes browser actions through `BrowserController`, and
returns protocol-compliant function responses after each step.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from google import genai
from google.genai import types
from google.genai.types import (
    ComputerUse,
    Content,
    Environment,
    FinishReason,
    FunctionResponse,
    FunctionResponseBlob,
    GenerateContentConfig,
    Part,
    ThinkingConfig,
    Tool,
    FunctionResponsePart,
)

from uisurf_agent.utils.browser_controller import BrowserController
from .ui_agent import AgentEvent, AgentStepResult, UIAgent, resolve_safety_prompt


logger = logging.getLogger(__name__)

MODEL_ID = os.environ.get("MODEL_ID", "gemini-3-flash-preview")
BROWSER_EXCLUDED_PREDEFINED_FUNCTIONS = ["drag_and_drop"]
BROWSER_SYSTEM_INSTRUCTION = (
    "You are controlling a web browser and can inspect screenshots and the full "
    "interaction history. Use the screenshots, current page state, and prior "
    "tool results to answer user questions whenever the information is already "
    "visible or has already been gathered. "
    "When the task requires comparing information across pages or sites, keep "
    "track of what you observed earlier in the run and produce a direct textual "
    "answer once you have enough evidence. "
    "Use clear_text_input before retyping into a form field when the field "
    "already contains text that should be replaced. "
    "If the browser window, dialog, or visible page area appears clipped so key "
    "controls are outside the visible region, first enlarge the active browser "
    "window, maximize/fullscreen it when available, or otherwise bring the "
    "required controls into view before continuing. "
    "Do not continue clicking or navigating if the answer can already be derived "
    "from the screenshots and history. "
    "Only use browser actions when more evidence is needed."
)


def clear_text_input(x: int | None = None, y: int | None = None) -> dict[str, Any]:
    """Declare a request to clear a browser text input.

    Args:
        x: Optional normalized horizontal coordinate (0-1000) to click before
            clearing.
        y: Optional normalized vertical coordinate (0-1000) to click before
            clearing.

    Returns:
        A payload echoing the requested clear-input action.
    """
    return {"x": x, "y": y}


BROWSER_CUSTOM_FUNCTIONS = (clear_text_input,)


def build_browser_custom_function_declarations(
    client: genai.Client,
) -> list[types.FunctionDeclaration]:
    """Build custom browser function declarations for the Gemini config.

    Args:
        client: GenAI client used by `from_callable()` to construct declarations.

    Returns:
        Function declarations describing browser-specific custom tools.
    """
    return [
        types.FunctionDeclaration.from_callable(client=client, callable=function)
        for function in BROWSER_CUSTOM_FUNCTIONS
    ]


def build_browser_generate_content_config(client: genai.Client) -> dict[str, Any]:
    """Build the base Gemini config payload for browser tool use.

    The browser config combines Gemini's predefined browser computer-use tool
    with browser-specific custom function declarations.

    Args:
        client: GenAI client used to build callable-based custom declarations.

    Returns:
        A dictionary of keyword arguments used to create
        `GenerateContentConfig` for browser computer-use sessions.
    """
    return {
        "tools": [
            Tool(
                computer_use=ComputerUse(
                    environment=Environment.ENVIRONMENT_BROWSER,
                    excluded_predefined_functions=BROWSER_EXCLUDED_PREDEFINED_FUNCTIONS,
                )
            ),
            Tool(function_declarations=build_browser_custom_function_declarations(client)),
        ],
        "system_instruction": BROWSER_SYSTEM_INSTRUCTION,
    }


def maybe_add_thinking_config(
    config_kwargs: dict[str, Any],
    model_id: str,
    include_thoughts: bool,
) -> None:
    """Enable Gemini thought streaming when the selected model supports it.

    Args:
        config_kwargs: Mutable config dictionary that may be updated in place.
        model_id: Model identifier used to determine whether thoughts are
            supported for this run.
        include_thoughts: Whether thought streaming should be enabled when the
            model supports it.
    """
    if not include_thoughts:
        return

    model_parts = model_id.split("-")
    if len(model_parts) <= 1:
        return

    try:
        if float(model_parts[1]) >= 3:
            config_kwargs["thinking_config"] = ThinkingConfig(include_thoughts=True)
    except ValueError:
        logger.debug("Skipping thinking config for unrecognized model id: %s", model_id)


def normalize_x(x: int, screen_width: int) -> int:
    """Convert a Gemini-normalized horizontal coordinate into browser pixels.

    Gemini browser tool calls express pointer coordinates on a 0-1000 scale.
    This helper maps that normalized value onto the configured browser viewport
    width so Playwright can target the intended horizontal position.

    Args:
        x: Horizontal coordinate on Gemini's 0-1000 grid.
        screen_width: Browser viewport width in pixels.

    Returns:
        The corresponding horizontal coordinate in viewport pixels.
    """
    return int(x / 1000 * screen_width)


def normalize_y(y: int, screen_height: int) -> int:
    """Convert a Gemini-normalized vertical coordinate into browser pixels.

    Gemini browser tool calls express pointer coordinates on a 0-1000 scale.
    This helper maps that normalized value onto the configured browser viewport
    height so Playwright can target the intended vertical position.

    Args:
        y: Vertical coordinate on Gemini's 0-1000 grid.
        screen_height: Browser viewport height in pixels.

    Returns:
        The corresponding vertical coordinate in viewport pixels.
    """
    return int(y / 1000 * screen_height)


class BrowserAgent(UIAgent):
    """Concrete UI agent that drives a web browser via Gemini computer-use tools.

    The class implements the abstract `UIAgent` lifecycle for a browser
    environment. It captures screenshot-based observations from Playwright, sends
    them to Gemini with the browser computer-use tool enabled, interprets the
    resulting function calls, executes them in the browser, and returns the
    post-action state back to the model to continue the loop.
    """

    def __init__(
        self,
        auto_confirm: bool = False,
        viewport_width: int = 1024,
        viewport_height: int = 760,
        headless: bool = False,
        animate_actions: bool = True,
        fast_mode: bool = False,
        include_thoughts: bool = True,
        max_observation_images: int = 2,
        observation_scale: float = 1.0,
        client: genai.Client | None = None,
    ) -> None:
        """Create a browser agent with a dedicated Playwright controller.

        Args:
            auto_confirm: Whether safety-gated model actions should be approved
                automatically.
            viewport_width: Browser viewport width used for screenshots and
                coordinate conversion.
            viewport_height: Browser viewport height used for screenshots and
                coordinate conversion.
            headless: Whether the browser should run without a visible window.
            animate_actions: Whether pointer movements and clicks should be
                visually animated for debugging/demo purposes.
            fast_mode: Whether browser settling should favor speed over
                completeness.
            include_thoughts: Whether model thought streaming should be enabled
                when supported by the selected model.
            max_observation_images: Maximum number of screenshot-bearing
                observations to keep with image payloads in model history.
            observation_scale: Scale factor applied to screenshots before they
                are sent to the model. Coordinates still use the full viewport.
            client: Optional preconfigured GenAI client instance.
        """
        super().__init__(
            auto_confirm=auto_confirm,
            client=client,
            max_observation_images=max_observation_images,
        )
        self._include_thoughts = include_thoughts
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self._browser_controller = BrowserController(
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            headless=headless,
            animate_actions=animate_actions,
            fast_mode=fast_mode,
            observation_scale=observation_scale,
        )
        self._client_config = self._build_client_config()



    def _build_client_config(self) -> GenerateContentConfig:
        """Build the model configuration used for browser computer-use requests.

        The config enables Gemini's predefined browser tool environment,
        browser-specific custom functions, and, when the selected model version
        supports it, thought streaming so the agent can log intermediate
        reasoning text alongside function calls.

        Returns:
            A `GenerateContentConfig` configured for browser-based tool use.
        """
        config_kwargs = build_browser_generate_content_config(self.client)
        maybe_add_thinking_config(config_kwargs, MODEL_ID, self._include_thoughts)
        logger.info("Client config: %s", config_kwargs)
        logger.info("Model: %s", MODEL_ID)
        return GenerateContentConfig(**config_kwargs)

    async def initialize(self) -> None:
        """Start the browser controller and prepare the initial browser page."""
        logger.debug("Setting up browser controller...")
        await self._browser_controller.setup()

    async def cleanup(self) -> None:
        """Close the browser controller and release Playwright resources."""
        logger.debug("Cleaning up browser controller...")
        await asyncio.sleep(0.3)
        await self._browser_controller.cleanup()

    async def observe(self, task: str, history: list[Any]) -> Content:
        """Capture the current browser state as model input.

        The first observation seeds the conversation with both the user task and a
        screenshot. Later observations are produced after actions and contain only
        the post-action state needed for the next reasoning step.

        Args:
            task: The original task requested by the user.
            history: Prior conversation items already sent to the model.

        Returns:
            A `Content` object containing the screenshot and, for the first turn,
            the original task text.
        """
        screenshot = await self._browser_controller.capture_screenshot()
        parts = [Part.from_bytes(data=screenshot, mime_type="image/png")]
        if not history:
            parts.insert(0, Part(text=task))
        return Content(role="user", parts=parts)

    async def reason(self, task: str, history: list[Content]) -> Any:
        """Send the current browser conversation history to Gemini.

        Args:
            task: The original user task. It is not used directly here because
                the task has already been embedded into the first observation, but
                it remains part of the common agent interface.
            history: The complete browser conversation history up to this point.

        Returns:
            The raw response from `client.models.generate_content()`.
        """
        return await asyncio.to_thread(
            self.client.models.generate_content,
            model=MODEL_ID,
            contents=history,
            config=self._client_config,
        )

    async def record_model_response(self, response: Any, history: list[Content]) -> None:
        """Append the selected model candidate content to the browser history.

        Args:
            response: The raw Gemini response returned by `reason()`.
            history: Mutable list of `Content` objects representing the running
                conversation with the model.
        """
        candidate = self._get_candidate(response)
        if candidate is not None:
            history.append(candidate.content)

    async def act(self, response: Any, history: list[Content]) -> AgentStepResult:
        """Interpret the model response and execute the next browser step.

        This method is the main bridge between model output and environment
        behavior. It handles empty responses, safety refusals, plain-text final
        answers, and executable function calls. When tool calls are present, it
        executes them and captures a post-action observation for the next loop.

        Args:
            response: The raw Gemini response returned by `reason()`.
            history: Mutable browser conversation history.

        Returns:
            An `AgentStepResult` describing whether execution is complete and what
            observation or message should be propagated to the shared loop.
        """
        candidate = self._get_candidate(response)

        if candidate is None:
            return AgentStepResult(
                done=True,
                message="Model returned no candidates. Terminating.",
            )

        if candidate.finish_reason == FinishReason.SAFETY:
            logger.debug("Safety details: %s", candidate.safety_ratings)
            return AgentStepResult(
                done=True,
                message="Model refused to perform an action due to safety concerns. Terminating.",
            )

        events: list[AgentEvent] = []
        thoughts = self._extract_thoughts(candidate)
        self._log_thoughts(thoughts)
        events.extend(
            self._build_agent_event("thought", {"text": thought})
            for thought in thoughts
        )

        function_calls = self._extract_function_calls(candidate)
        events.extend(
            self._build_agent_event(
                "function_call",
                {"name": function_call.name, "args": dict(function_call.args)},
            )
            for function_call in function_calls
        )
        if not function_calls:
            final_text = self._extract_final_text(candidate)
            if final_text:
                logger.debug("Agent finished: %s", final_text)
            return AgentStepResult(done=True, message=final_text, events=events)

        execution_results = await self._execute_function_calls(function_calls)
        logger.debug("Execution results: %s", execution_results)
        if any(result == "user_denied" for _, result, _ in execution_results):
            return AgentStepResult(
                done=True,
                message="The requested action was denied by the user. Terminating.",
                events=events,
            )
        observation, response_events = await self._build_action_observation(execution_results)
        events.extend(response_events)
        return AgentStepResult(done=False, observation=observation, events=events)

    def _get_candidate(self, response: Any) -> Any | None:
        """Extract the primary model candidate from a Gemini response object.

        Args:
            response: Raw response returned by the GenAI client.

        Returns:
            The first candidate when present, otherwise `None`.
        """
        if not response.candidates:
            return None
        return response.candidates[0]

    def _extract_thoughts(self, candidate: Any) -> list[str]:
        """Extract free-form reasoning text emitted alongside browser tool calls.

        Gemini responses may include both natural-language thoughts and function
        calls in the same candidate.

        Args:
            candidate: The chosen Gemini candidate object.

        Returns:
            A list of thought strings emitted by the model.
        """
        return [part.text for part in candidate.content.parts if getattr(part, "text", None)]

    def _log_thoughts(self, thoughts: list[str]) -> None:
        """Log extracted reasoning text for local debugging visibility.

        Args:
            thoughts: Ordered thought strings extracted from the model response.
        """
        if thoughts:
            logger.debug("Model reasoning: %s", " ".join(thoughts))

    def _extract_function_calls(self, candidate: Any) -> list[Any]:
        """Collect all browser function calls from a model candidate.

        Args:
            candidate: The chosen Gemini candidate object.

        Returns:
            A list of function-call payloads in the order they were emitted.
        """
        return [
            part.function_call
            for part in candidate.content.parts
            if getattr(part, "function_call", None)
        ]

    def _extract_final_text(self, candidate: Any) -> str | None:
        """Extract the final plain-text answer when the model stops using tools.

        Args:
            candidate: The chosen Gemini candidate object.

        Returns:
            Concatenated text emitted by the candidate, or `None` when no plain
            text response is present.
        """
        final_text = "".join(
            part.text
            for part in candidate.content.parts
            if getattr(part, "text", None) is not None
        )
        return final_text or None

    async def _execute_function_calls(
        self, function_calls: list[Any]
    ) -> list[tuple[str, str, bool]]:
        """Execute all tool calls emitted for the current model turn.

        Each function call is safety-checked, executed against the browser when
        allowed, and summarized into a compact tuple consumed by
        `_build_action_observation()`.

        Args:
            function_calls: Ordered function calls emitted by Gemini.

        Returns:
            A list of `(function_name, result, safety_acknowledged)` tuples.
        """
        await asyncio.sleep(0.1)
        results: list[tuple[str, str, bool]] = []
        for function_call in function_calls:
            safety_acknowledged, allowed = await resolve_safety_prompt(
                self._safety_prompt_handler,
                function_call,
                self.auto_confirm,
            )
            if not allowed:
                results.append((function_call.name, "user_denied", safety_acknowledged))
                continue

            result = await self._execute_function_call(function_call)
            results.append((function_call.name, result, safety_acknowledged))
        return results

    async def _execute_function_call(self, function_call: Any) -> str:
        """Execute one browser function against Playwright.

        Supported functions are mapped onto the `BrowserController` or direct page
        keyboard interactions. Unknown functions are reported instead of raising,
        and exceptions are caught and converted into error strings so the agent can
        continue reporting state to the model.

        Args:
            function_call: The Gemini function call payload to execute.

        Returns:
            A short status string such as `success`, `unknown_function`, or
            `error: ...`.
        """
        action_name = function_call.name
        args = dict(function_call.args)
        args.pop("safety_decision", None)

        try:
            handler = self._get_function_handler(action_name)
            if handler is not None:
                await handler(args)
                return "success"

            controller_callable = getattr(self._browser_controller, action_name, None)
            if controller_callable is not None and callable(controller_callable):
                await controller_callable(**args)
                return "success"

            logger.warning("Unknown function call from model: %s", action_name)
            return "unknown_function"
        except Exception as exc:
            logger.exception("Error executing %s", action_name)
            return f"error: {exc!s}"

    def _get_predefined_browser_handlers(
        self,
    ) -> dict[str, Callable[[dict[str, Any]], Awaitable[None]]]:
        """Return handlers for Gemini predefined browser tool actions.

        Returns:
            A mapping from predefined Gemini browser function names to local
            coroutine handlers.
        """
        return {
            "open_web_browser": self._handle_open_web_browser,
            "navigate": self._handle_navigate,
            "click_at": self._handle_click_at,
            "type_text_at": self._handle_type_text_at,
            "wait_5_seconds": self._handle_wait_5_seconds,
            "go_back": self._handle_go_back,
            "go_forward": self._handle_go_forward,
            "search": self._handle_search,
            "scroll_document": self._handle_scroll_document,
            "key_combination": self._handle_key_combination,
            "scroll_at": self._handle_scroll_at,
            "hover_at": self._handle_hover_at,
        }

    def _get_function_handler(
        self, action_name: str
    ) -> Callable[[dict[str, Any]], Awaitable[None]] | None:
        """Return the async handler for a model-emitted browser action.

        Args:
            action_name: Name of the predefined Gemini browser function.

        Returns:
            An async callable that accepts the function-call args dictionary, or
            `None` if the action is not supported by this agent.
        """
        return self._get_predefined_browser_handlers().get(action_name)

    def _normalize_coordinates(self, x: int, y: int) -> tuple[int, int]:
        """Convert Gemini-normalized coordinates into viewport pixel coordinates.

        Args:
            x: Horizontal coordinate on Gemini's 0-1000 grid.
            y: Vertical coordinate on Gemini's 0-1000 grid.

        Returns:
            A tuple of `(x, y)` viewport pixel coordinates.
        """
        return (
            normalize_x(x, self.viewport_width),
            normalize_y(y, self.viewport_height),
        )

    async def _handle_open_web_browser(self, args: dict[str, Any]) -> None:
        """Acknowledge the browser-open action when the browser is already running."""
        del args

    async def _handle_navigate(self, args: dict[str, Any]) -> None:
        """Navigate the active page to the URL supplied by the model."""
        await self._browser_controller.navigate(args["url"])

    async def _handle_click_at(self, args: dict[str, Any]) -> None:
        """Click the model-selected location after converting coordinates."""
        actual_x, actual_y = self._normalize_coordinates(args["x"], args["y"])
        await self._browser_controller.click_coords(actual_x, actual_y)

    async def _handle_type_text_at(self, args: dict[str, Any]) -> None:
        """Focus a target location, type text, and optionally submit with Enter."""
        actual_x, actual_y = self._normalize_coordinates(args["x"], args["y"])
        await self._browser_controller.click_coords(actual_x, actual_y)
        await asyncio.sleep(0.1)
        await self._browser_controller.page.keyboard.type(args["text"])
        if args.get("press_enter", False):
            await self._browser_controller.page.keyboard.press("Enter")

    async def _handle_wait_5_seconds(self, args: dict[str, Any]) -> None:
        """Wait for the page to settle for up to five seconds."""
        del args
        await self._browser_controller.wait_until_loaded(timeout_ms=5000)

    async def _handle_go_back(self, args: dict[str, Any]) -> None:
        """Navigate backward in browser history."""
        del args
        await self._browser_controller.go_back()

    async def _handle_go_forward(self, args: dict[str, Any]) -> None:
        """Navigate forward in browser history."""
        del args
        await self._browser_controller.go_forward()

    async def _handle_search(self, args: dict[str, Any]) -> None:
        """Open the default search page used by the current browser workflow."""
        del args
        await self._browser_controller.navigate("https://www.google.com/")

    async def _handle_scroll_document(self, args: dict[str, Any]) -> None:
        """Scroll the page in the direction requested by the model."""
        await self._browser_controller.scroll_document(direction=args["direction"])

    async def _handle_key_combination(self, args: dict[str, Any]) -> None:
        """Press a key combination such as `Control+C` or `Enter`."""
        await self._browser_controller.key_combination(args["keys"])

    async def _handle_scroll_at(self, args: dict[str, Any]) -> None:
        """Scroll from a specific normalized location using the requested magnitude."""
        await self._browser_controller.scroll_at(
            x=args["x"],
            y=args["y"],
            direction=args["direction"],
            magnitude=args["magnitude"],
        )

    async def _handle_hover_at(self, args: dict[str, Any]) -> None:
        """Hover the pointer at the normalized location supplied by the model."""
        await self._browser_controller.hover_at(
            x=args["x"],
            y=args["y"],
        )

    async def _build_action_observation(
        self, execution_results: list[tuple[str, str, bool]]
    ) -> tuple[Content, list[AgentEvent]]:
        """Build the follow-up observation returned to Gemini after actions run.

        For each executed tool call, this method captures a fresh screenshot and
        current URL, wraps them in a `function_response`, and returns the set of
        responses as a `Content` object. This mirrors the computer-use protocol
        expected by Gemini so the model can observe the consequences of its last
        action before deciding what to do next.

        Args:
            execution_results: Summaries produced by `_execute_function_calls()`.

        Returns:
            A tuple containing the `Content` object for the next model turn and
            streamed function-response events for external consumers.
        """
        function_response_parts = []
        events: list[AgentEvent] = []
        for name, result, safety_acknowledged in execution_results:
            screenshot = await self._browser_controller.capture_screenshot()
            current_url = await self._browser_controller.get_current_url()
            response_payload: dict[str, Any] = {"url": current_url}

            if result == "user_denied":
                response_payload["error"] = "user_denied"
            elif safety_acknowledged:
                response_payload["safety_acknowledgement"] = True

            function_response_parts.append(
                Part(
                    function_response=FunctionResponse(
                        name=name,
                        response=response_payload,
                        parts=[
                            FunctionResponsePart(
                                inline_data=FunctionResponseBlob(
                                    mime_type="image/png",
                                    data=screenshot,
                                )
                            )
                        ],
                    )
                )
            )
            events.append(
                self._build_agent_event(
                    "function_response",
                    {
                        "name": name,
                        "result": result,
                        "url": current_url,
                        "safety_acknowledged": safety_acknowledged,
                    },
                )
            )

        return Content(role="user", parts=function_response_parts), events
