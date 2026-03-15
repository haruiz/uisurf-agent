from __future__ import annotations

"""Desktop-specific UI agent built on Gemini computer-use plus custom tools.

The module defines a set of custom desktop function declarations exposed to the
model, configures the Gemini client for desktop-oriented tool use, and maps the
resulting tool calls onto `DesktopController` operations.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Dict
from typing import Any
from typing import Optional

from google import genai
from google.genai import types
from google.genai.types import (
    ComputerUse,
    Content,
    Environment,
    FinishReason,
    FunctionResponse,
    FunctionResponseBlob,
    FunctionResponsePart,
    GenerateContentConfig,
    Part,
    ThinkingConfig,
    Tool,
)

from uisurf_agent.utils.desktop_controller import DesktopController
from .ui_agent import AgentEvent, AgentStepResult, UIAgent, resolve_safety_prompt


logger = logging.getLogger(__name__)

MODEL_ID = os.environ.get("MODEL_ID", "gemini-3-flash-preview")
SYSTEM_INSTRUCTION = (
    "You are controlling a desktop computer, not a web page. "
    "Prefer desktop-specific functions such as open_app, open_terminal, "
    "run_terminal_command, click, type_text, move_cursor, scroll, long_press_at, "
    "go_home, close_window, minimize_window, maximize_window, and switch_application. "
    "Use open_app to launch applications like Terminal, Finder, VS Code, or Chrome. "
    "Use run_terminal_command after opening Terminal when shell access is needed. "
    "Coordinates for click, move_cursor, and scroll are normalized on a 0-1000 grid. "
    "Use the screenshots and the full interaction history to answer questions "
    "about what is visible on screen or what was observed earlier in the run. "
    "If you already have enough information from prior screenshots or prior "
    "observations, respond directly instead of taking more actions. "
    "Do not close windows, quit applications, or clean up the desktop unless the "
    "user explicitly asks for that behavior. "
    "Only finish when the task is actually completed on the desktop."
)


def open_app(app_name: str, intent: Optional[str] = None) -> Dict[str, Any]:
    """Declare a request to open an application by name.

    Args:
        app_name: Name of the application to open.
        intent: Optional follow-up deep link, path, or URL to open once the app
            is launched.

    Returns:
        A small acknowledgement payload that mirrors the requested action.
    """
    return {"status": "requested_open", "app_name": app_name, "intent": intent}


def long_press_at(x: int, y: int, duration_ms: int = 500) -> Dict[str, int]:
    """Declare a long-press at a screen coordinate.

    Args:
        x: Absolute horizontal screen coordinate in pixels.
        y: Absolute vertical screen coordinate in pixels.
        duration_ms: Press duration in milliseconds.

    Returns:
        A payload echoing the requested press parameters.
    """
    return {"x": x, "y": y, "duration_ms": duration_ms}


def go_home() -> Dict[str, str]:
    """Declare a request to navigate to the desktop home surface.

    Returns:
        A minimal acknowledgement payload.
    """
    return {"status": "home_requested"}


def open_terminal() -> Dict[str, str]:
    """Declare a request to open the system terminal application.

    Returns:
        A minimal acknowledgement payload.
    """
    return {"status": "terminal_requested"}


def run_terminal_command(command: str, press_enter: bool = True) -> Dict[str, Any]:
    """Declare a terminal command to run or type.

    Args:
        command: Shell command to execute or type.
        press_enter: Whether the command should be submitted immediately.

    Returns:
        A payload echoing the requested terminal action.
    """
    return {"status": "terminal_command_requested", "command": command, "press_enter": press_enter}


def click(x: int, y: int) -> Dict[str, int]:
    """Declare a click at a normalized screen coordinate.

    Args:
        x: Horizontal coordinate on a 0-1000 grid.
        y: Vertical coordinate on a 0-1000 grid.

    Returns:
        A payload echoing the requested click coordinates.
    """
    return {"x": x, "y": y}


def type_text(text: str, x: Optional[int] = None, y: Optional[int] = None, press_enter: bool = False) -> Dict[str, Any]:
    """Declare a text-entry request for the desktop.

    Args:
        text: Text to type.
        x: Optional normalized horizontal coordinate to click before typing.
        y: Optional normalized vertical coordinate to click before typing.
        press_enter: Whether Enter should be pressed after typing.

    Returns:
        A payload echoing the requested typing action.
    """
    return {"text": text, "x": x, "y": y, "press_enter": press_enter}


def move_cursor(x: int, y: int) -> Dict[str, int]:
    """Declare a cursor move to a normalized screen coordinate.

    Args:
        x: Horizontal coordinate on a 0-1000 grid.
        y: Vertical coordinate on a 0-1000 grid.

    Returns:
        A payload echoing the requested cursor movement.
    """
    return {"x": x, "y": y}


def scroll(x: int, y: int, direction: str, magnitude: int = 800) -> Dict[str, Any]:
    """Declare a scroll request from a normalized screen coordinate.

    Args:
        x: Horizontal coordinate on a 0-1000 grid.
        y: Vertical coordinate on a 0-1000 grid.
        direction: One of `up`, `down`, `left`, or `right`.
        magnitude: Scroll distance on a 0-1000 scale.

    Returns:
        A payload echoing the requested scroll action.
    """
    return {"x": x, "y": y, "direction": direction, "magnitude": magnitude}


EXCLUDED_PREDEFINED_FUNCTIONS = [
    "open_web_browser",
    "search",
    "navigate",
    "hover_at",
    "scroll_document",
    "go_forward",
    "go_back",
    "key_combination",
    "drag_and_drop",
    "click_at",
    "type_text_at",
    "scroll_at",
]
CUSTOM_FUNCTIONS = (
    open_app,
    long_press_at,
    go_home,
    open_terminal,
    run_terminal_command,
    click,
    type_text,
    move_cursor,
    scroll,
)


def normalize_x(x: int, screen_width: int) -> int:
    """Convert a Gemini-normalized horizontal coordinate into screen pixels.

    Args:
        x: Horizontal coordinate on a 0-1000 grid.
        screen_width: Desktop width in pixels.

    Returns:
        Absolute horizontal screen coordinate in pixels.
    """
    return int(x / 1000 * screen_width)


def normalize_y(y: int, screen_height: int) -> int:
    """Convert a Gemini-normalized vertical coordinate into screen pixels.

    Args:
        y: Vertical coordinate on a 0-1000 grid.
        screen_height: Desktop height in pixels.

    Returns:
        Absolute vertical screen coordinate in pixels.
    """
    return int(y / 1000 * screen_height)


def build_custom_function_declarations(client: genai.Client) -> list[types.FunctionDeclaration]:
    """Build custom desktop function declarations for the Gemini config.

    Args:
        client: GenAI client used by `from_callable()` to construct declarations.

    Returns:
        Function declarations describing the desktop-specific custom tools.
    """
    return [
        types.FunctionDeclaration.from_callable(client=client, callable=function)
        for function in CUSTOM_FUNCTIONS
    ]


class DesktopAgent(UIAgent):
    """Concrete UI agent that drives a local desktop using Gemini computer-use.

    The agent mirrors the shared `UIAgent` lifecycle while tailoring model
    configuration, observation capture, and tool-call execution to a desktop
    environment backed by `DesktopController`.
    """

    def __init__(
        self,
        auto_confirm: bool = False,
        screen_width: int | None = None,
        screen_height: int | None = None,
        client: genai.Client | None = None,
    ) -> None:
        """Create a desktop agent and its backing controller.

        Args:
            auto_confirm: Whether safety-gated tool calls should be approved
                automatically.
            screen_width: Optional fixed desktop width override.
            screen_height: Optional fixed desktop height override.
            client: Optional preconfigured GenAI client instance.
        """
        super().__init__(auto_confirm=auto_confirm, client=client)
        self._desktop_controller = DesktopController(
            screen_width=screen_width,
            screen_height=screen_height,
        )
        self._client_config = self._build_client_config()

    @property
    def screen_width(self) -> int:
        """Expose the controller's current desktop width in pixels."""
        return self._desktop_controller.screen_width

    @property
    def screen_height(self) -> int:
        """Expose the controller's current desktop height in pixels."""
        return self._desktop_controller.screen_height

    def _build_client_config(self) -> GenerateContentConfig:
        """Build the model configuration used for desktop computer-use requests.

        Returns:
            A `GenerateContentConfig` that combines Gemini computer-use with the
            desktop-specific custom function declarations defined in this module.
        """
        config_kwargs: dict[str, Any] = {
            "tools": [
                Tool(
                    computer_use=ComputerUse(
                        environment=Environment.ENVIRONMENT_UNSPECIFIED,
                        excluded_predefined_functions=EXCLUDED_PREDEFINED_FUNCTIONS,
                    )
                ),
                Tool(function_declarations=build_custom_function_declarations(self.client)),
            ],
            "system_instruction": SYSTEM_INSTRUCTION,
        }

        model_parts = MODEL_ID.split("-")
        if len(model_parts) > 1:
            try:
                if float(model_parts[1]) >= 3:
                    config_kwargs["thinking_config"] = ThinkingConfig(
                        include_thoughts=True
                    )
            except ValueError:
                logger.debug("Skipping thinking config for unrecognized model id: %s", MODEL_ID)
        logger.info("Client config: %s", config_kwargs)
        logger.info("Model: %s", MODEL_ID)
        return GenerateContentConfig(**config_kwargs)

    async def initialize(self) -> None:
        """Initialize the desktop controller before the first observation."""
        logger.debug("Setting up desktop controller...")
        await self._desktop_controller.setup()

    async def cleanup(self) -> None:
        """Tear down the desktop controller when the agent run completes."""
        logger.debug("Cleaning up desktop controller...")
        await asyncio.sleep(0.3)
        await self._desktop_controller.cleanup()

    async def observe(self, task: str, history: list[Any]) -> Content:
        """Capture the current desktop screenshot as the next model observation.

        Args:
            task: Original user task for this run.
            history: Existing model conversation history.

        Returns:
            A `Content` object containing the desktop screenshot and, on the first
            turn, the original task text.
        """
        screenshot = await self._desktop_controller.capture_screenshot()
        parts = [Part.from_bytes(data=screenshot, mime_type="image/png")]
        if not history:
            parts.insert(0, Part(text=task))
        return Content(role="user", parts=parts)

    async def reason(self, task: str, history: list[Content]) -> Any:
        """Ask Gemini to decide the next desktop action.

        Args:
            task: Original user task. The task is already included in the initial
                observation, so it is unused here.
            history: Complete model conversation history so far.

        Returns:
            The raw Gemini response object for the current turn.
        """
        del task
        return await asyncio.to_thread(
            self.client.models.generate_content,
            model=MODEL_ID,
            contents=history,
            config=self._client_config,
        )

    async def record_model_response(self, response: Any, history: list[Content]) -> None:
        """Append the primary model candidate into the shared history.

        Args:
            response: Raw model response returned by `reason()`.
            history: Mutable conversation history for the current agent run.
        """
        candidate = self._get_candidate(response)
        if candidate is not None:
            history.append(candidate.content)

    async def act(self, response: Any, history: list[Content]) -> AgentStepResult:
        """Execute the next desktop step implied by the model response.

        Args:
            response: Raw model response returned by `reason()`.
            history: Shared conversation history. It is not mutated here.

        Returns:
            An `AgentStepResult` describing the next observation or terminal
            completion state for the shared run loop.
        """
        del history
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
            AgentEvent(eventType="thought", payload={"text": thought})
            for thought in thoughts
        )

        function_calls = self._extract_function_calls(candidate)
        events.extend(
            AgentEvent(
                eventType="function_call",
                payload={"name": function_call.name, "args": dict(function_call.args)},
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
        """Return the primary Gemini candidate from a response object."""
        if not response.candidates:
            return None
        return response.candidates[0]

    def _extract_thoughts(self, candidate: Any) -> list[str]:
        """Extract free-form thought text parts from a model candidate."""
        return [part.text for part in candidate.content.parts if getattr(part, "text", None)]

    def _log_thoughts(self, thoughts: list[str]) -> None:
        """Log model reasoning text for local debugging visibility."""
        if thoughts:
            logger.debug("Model reasoning: %s", " ".join(thoughts))

    def _extract_function_calls(self, candidate: Any) -> list[Any]:
        """Collect function calls emitted by the model in the current turn."""
        return [
            part.function_call
            for part in candidate.content.parts
            if getattr(part, "function_call", None)
        ]

    def _extract_final_text(self, candidate: Any) -> str | None:
        """Extract final plain text from a candidate when no more tools are used."""
        final_text = "".join(
            part.text
            for part in candidate.content.parts
            if getattr(part, "text", None) is not None
        )
        return final_text or None

    async def _execute_function_calls(
        self, function_calls: list[Any]
    ) -> list[tuple[str, str, bool]]:
        """Execute all function calls emitted for the current model turn.

        Args:
            function_calls: Ordered list of model-emitted function calls.

        Returns:
            Tuples of `(name, result, safety_acknowledged)` for each attempted
            function call.
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
        """Execute one desktop function call against either handlers or controller.

        Args:
            function_call: Model-emitted function call payload.

        Returns:
            A compact execution result string such as `success` or
            `unknown_function`.
        """
        action_name = function_call.name
        args = dict(function_call.args)
        args.pop("safety_decision", None)

        try:
            handler = self._get_function_handler(action_name)
            if handler is not None:
                await handler(args)
                return "success"

            controller_callable = getattr(self._desktop_controller, action_name, None)
            if controller_callable is not None and callable(controller_callable):
                await controller_callable(**args)
                return "success"

            logger.warning("Unknown function call from model: %s", action_name)
            return "unknown_function"
        except Exception as exc:
            logger.exception("Error executing %s", action_name)
            return f"error: {exc!s}"

    def _get_function_handler(
        self, action_name: str
    ) -> Callable[[dict[str, Any]], Awaitable[None]] | None:
        """Return a compatibility handler for predefined desktop/browser actions.

        Args:
            action_name: Function name emitted by the model.

        Returns:
            A coroutine handler when the action requires argument adaptation,
            otherwise `None` so controller fallback dispatch can be attempted.
        """
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
            "launch_application": self._handle_launch_application,
            "open_item": self._handle_open_item,
            "click_at": self._handle_click,
            "double_click": self._handle_double_click,
            "right_click": self._handle_right_click,
            "type_text_at": self._handle_type_text,
            "wait_5_seconds": self._handle_wait_5_seconds,
            "close_window": self._handle_close_window,
            "minimize_window": self._handle_minimize_window,
            "maximize_window": self._handle_maximize_window,
            "switch_application": self._handle_switch_application,
            "key_combination": self._handle_key_combination,
            "scroll_at": self._handle_scroll,
            "hover_at": self._handle_move_cursor,
            "drag_and_drop": self._handle_drag_and_drop,
        }
        return handlers.get(action_name)

    def _normalize_coordinates(self, x: int, y: int) -> tuple[int, int]:
        """Convert normalized model coordinates into screen pixels."""
        return (
            normalize_x(x, self.screen_width),
            normalize_y(y, self.screen_height),
        )

    async def _handle_launch_application(self, args: dict[str, Any]) -> None:
        """Handle the predefined `launch_application` action."""
        await self._desktop_controller.launch_application(args["application"])

    async def _handle_open_item(self, args: dict[str, Any]) -> None:
        """Handle the predefined `open_item` action."""
        await self._desktop_controller.open_item(args["target"])

    async def _handle_click(self, args: dict[str, Any]) -> None:
        """Handle the predefined normalized click action."""
        actual_x, actual_y = self._normalize_coordinates(args["x"], args["y"])
        await self._desktop_controller.click_coords(actual_x, actual_y)

    async def _handle_double_click(self, args: dict[str, Any]) -> None:
        """Handle the predefined normalized double-click action."""
        actual_x, actual_y = self._normalize_coordinates(args["x"], args["y"])
        await self._desktop_controller.double_click_coords(actual_x, actual_y)

    async def _handle_right_click(self, args: dict[str, Any]) -> None:
        """Handle the predefined normalized right-click action."""
        actual_x, actual_y = self._normalize_coordinates(args["x"], args["y"])
        await self._desktop_controller.right_click_coords(actual_x, actual_y)

    async def _handle_type_text(self, args: dict[str, Any]) -> None:
        """Handle the predefined typing action with optional focus coordinates."""
        await self._desktop_controller.type_text(
            text=args["text"],
            x=args.get("x"),
            y=args.get("y"),
            press_enter=args.get("press_enter", False),
        )

    async def _handle_wait_5_seconds(self, args: dict[str, Any]) -> None:
        """Handle the predefined five-second wait action."""
        del args
        await self._desktop_controller.wait_5_seconds()

    async def _handle_close_window(self, args: dict[str, Any]) -> None:
        """Handle the predefined close-window action."""
        del args
        await self._desktop_controller.close_window()

    async def _handle_minimize_window(self, args: dict[str, Any]) -> None:
        """Handle the predefined minimize-window action."""
        del args
        await self._desktop_controller.minimize_window()

    async def _handle_maximize_window(self, args: dict[str, Any]) -> None:
        """Handle the predefined maximize-window action."""
        del args
        await self._desktop_controller.maximize_window()

    async def _handle_switch_application(self, args: dict[str, Any]) -> None:
        """Handle the predefined app-switch action."""
        del args
        await self._desktop_controller.switch_application()

    async def _handle_key_combination(self, args: dict[str, Any]) -> None:
        """Handle the predefined key-combination action."""
        await self._desktop_controller.key_combination(args["keys"])

    async def _handle_scroll(self, args: dict[str, Any]) -> None:
        """Handle the predefined normalized scroll action."""
        await self._desktop_controller.scroll(
            x=args["x"],
            y=args["y"],
            direction=args["direction"],
            magnitude=args["magnitude"],
        )

    async def _handle_move_cursor(self, args: dict[str, Any]) -> None:
        """Handle the predefined normalized pointer-move action."""
        await self._desktop_controller.move_cursor(
            x=args["x"],
            y=args["y"],
        )

    async def _handle_drag_and_drop(self, args: dict[str, Any]) -> None:
        """Handle the predefined normalized drag-and-drop action."""
        await self._desktop_controller.drag_and_drop(
            x=args["x"],
            y=args["y"],
            destination_x=args["destination_x"],
            destination_y=args["destination_y"],
        )

    async def _build_action_observation(
        self, execution_results: list[tuple[str, str, bool]]
    ) -> tuple[Content, list[AgentEvent]]:
        """Build Gemini function responses for the post-action desktop state.

        Args:
            execution_results: Results returned by `_execute_function_calls()`.

        Returns:
            A tuple of model input content for the next turn and streamed
            `AgentEvent` records describing each function response.
        """
        function_response_parts = []
        events: list[AgentEvent] = []
        for name, result, safety_acknowledged in execution_results:
            screenshot = await self._desktop_controller.capture_screenshot()
            current_state = await self._desktop_controller.get_state()
            response_payload: dict[str, Any] = {
                "url": current_state,
                "current_url": current_state,
                "desktop_state": current_state,
            }

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
                AgentEvent(
                    eventType="function_response",
                    payload={
                        "name": name,
                        "result": result,
                        "url": current_state,
                        "desktop_state": current_state,
                        "safety_acknowledged": safety_acknowledged,
                    },
                )
            )

        return Content(role="user", parts=function_response_parts), events
