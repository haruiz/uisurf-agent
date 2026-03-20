from __future__ import annotations

"""Desktop automation primitives used by the desktop agent.

This module wraps screenshot capture, mouse control, keyboard input, window
management, and application launching behind an async-friendly controller API.
The methods are intentionally small and composable so the agent can map model
tool calls onto concrete desktop operations with minimal glue code.
"""

import asyncio
import io
import os
import platform
import subprocess
import webbrowser
from typing import Literal

import mss
import pyautogui
from PIL import Image

from .screenshot_utils import scale_image, validate_observation_scale


PYAUTOGUI_KEY_ALIASES = {
    "/": "/",
    "\\": "\\",
    "alt": "alt",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "arrowup": "up",
    "backspace": "backspace",
    "capslock": "capslock",
    "cmd": "command",
    "ctrl": "ctrl",
    "delete": "delete",
    "end": "end",
    "enter": "enter",
    "esc": "esc",
    "escape": "esc",
    "home": "home",
    "insert": "insert",
    "meta": "command",
    "option": "alt",
    "pagedown": "pagedown",
    "pageup": "pageup",
    "shift": "shift",
    "space": "space",
    "super": "win",
    "tab": "tab",
    "win": "win",
}


class DesktopController:
    """Async wrapper around screenshot and desktop automation primitives.

    The controller owns all environment-facing side effects for the desktop
    agent. It exposes both low-level pointer helpers and higher-level desktop
    actions such as opening an application or running a terminal command.
    """

    def __init__(
        self,
        screen_width: int | None = None,
        screen_height: int | None = None,
        sleep_after_action: int | float = 0.1,
        observation_delay_ms: int = 1500,
        observation_scale: float = 1.0,
    ) -> None:
        """Initialize controller state and default automation timings.

        Args:
            screen_width: Optional fixed screen width. When omitted, the value is
                discovered from the host desktop during `setup()`.
            screen_height: Optional fixed screen height. When omitted, the value
                is discovered from the host desktop during `setup()`.
            sleep_after_action: Delay inserted after mutating actions so the
                desktop can visually settle before the next observation.
            observation_delay_ms: Delay before each screenshot capture so the
                desktop has time to visually settle after actions.
            observation_scale: Scale factor applied to screenshots before they
                are sent to the model. Coordinates still map to the full desktop.
        """
        if observation_delay_ms < 0:
            raise ValueError("observation_delay_ms must be greater than or equal to 0.")
        self._sleep_after_action = sleep_after_action
        self._observation_delay_ms = observation_delay_ms
        self._screen_width = screen_width
        self._screen_height = screen_height
        self._observation_scale = validate_observation_scale(observation_scale)
        self._last_target = "desktop://local"
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0

    async def setup(self) -> None:
        """Initialize screen geometry from the active desktop.

        This method should be called before any coordinate-based actions are
        executed so normalized coordinates can be translated into real screen
        pixels.
        """
        if self._screen_width is None or self._screen_height is None:
            width, height = await asyncio.to_thread(pyautogui.size)
            self._screen_width = width
            self._screen_height = height

    async def cleanup(self) -> None:
        """Release controller resources.

        The current controller does not hold long-lived OS handles, so cleanup is
        presently a no-op. The method exists to preserve a symmetric lifecycle.
        """

    @property
    def screen_width(self) -> int:
        """Return the active screen width in pixels.

        Raises:
            RuntimeError: If `setup()` has not populated the geometry yet.
        """
        if self._screen_width is None:
            raise RuntimeError("Call setup() first.")
        return self._screen_width

    @property
    def screen_height(self) -> int:
        """Return the active screen height in pixels.

        Raises:
            RuntimeError: If `setup()` has not populated the geometry yet.
        """
        if self._screen_height is None:
            raise RuntimeError("Call setup() first.")
        return self._screen_height

    async def capture_screenshot(self) -> bytes:
        """Capture the primary display as PNG bytes for model observation.

        Returns:
            Raw PNG bytes representing the current desktop state.
        """
        await self.wait_until_loaded()
        return await asyncio.to_thread(self._capture_primary_monitor)

    def _capture_primary_monitor(self) -> bytes:
        """Synchronously capture the primary monitor and encode it as PNG bytes."""
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            screenshot = sct.grab(monitor)
            image = scale_image(
                Image.frombytes("RGB", screenshot.size, screenshot.rgb),
                self._observation_scale,
            )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

    async def wait_until_loaded(self, timeout_ms: int | None = None) -> None:
        """Wait briefly for the desktop to visually settle.

        Args:
            timeout_ms: Number of milliseconds to pause before the next
                observation or action.
        """
        delay_ms = self._observation_delay_ms if timeout_ms is None else timeout_ms
        await asyncio.sleep(delay_ms / 1000.0)

    def _normalize_point(self, x: int, y: int) -> tuple[int, int]:
        """Convert normalized 0-1000 coordinates into absolute screen pixels.

        Args:
            x: Horizontal coordinate on a 0-1000 grid.
            y: Vertical coordinate on a 0-1000 grid.

        Returns:
            A tuple of `(x, y)` coordinates in desktop pixels.
        """
        return (
            int((x / 1000.0) * self.screen_width),
            int((y / 1000.0) * self.screen_height),
        )

    async def click_coords(
        self, x: float, y: float, button: Literal["left", "right"] = "left"
    ) -> None:
        """Click absolute screen coordinates with the requested mouse button.

        Args:
            x: Absolute horizontal coordinate in screen pixels.
            y: Absolute vertical coordinate in screen pixels.
            button: Mouse button to press.
        """
        await asyncio.to_thread(pyautogui.click, x=x, y=y, button=button)
        await self._sleep_after_action_if_needed()

    async def double_click_coords(self, x: float, y: float) -> None:
        """Double-click absolute screen coordinates.

        Args:
            x: Absolute horizontal coordinate in screen pixels.
            y: Absolute vertical coordinate in screen pixels.
        """
        await asyncio.to_thread(pyautogui.doubleClick, x=x, y=y)
        await self._sleep_after_action_if_needed()

    async def right_click_coords(self, x: float, y: float) -> None:
        """Right-click absolute screen coordinates.

        Args:
            x: Absolute horizontal coordinate in screen pixels.
            y: Absolute vertical coordinate in screen pixels.
        """
        await self.click_coords(x=x, y=y, button="right")

    async def hover_coords(self, x: float, y: float) -> None:
        """Move the pointer to absolute screen coordinates.

        Args:
            x: Absolute horizontal coordinate in screen pixels.
            y: Absolute vertical coordinate in screen pixels.
        """
        await asyncio.to_thread(pyautogui.moveTo, x, y)
        await self._sleep_after_action_if_needed()

    async def _write_text(self, text: str) -> None:
        """Type raw text into the currently focused desktop control.

        Args:
            text: Literal text to type.
        """
        await asyncio.to_thread(pyautogui.write, text, interval=0.01)
        await self._sleep_after_action_if_needed()

    async def keypress(self, keys: list[str]) -> None:
        """Press one key or hotkey combination.

        Args:
            keys: Ordered key names using the agent's abstract key vocabulary.
        """
        normalized = [self._normalize_key(key) for key in keys if str(key).strip()]
        if not normalized:
            return
        if len(normalized) == 1:
            await asyncio.to_thread(pyautogui.press, normalized[0])
        else:
            await asyncio.to_thread(pyautogui.hotkey, *normalized)
        await self._sleep_after_action_if_needed()

    async def key_combination(self, keys: str) -> None:
        """Press a `+`-delimited keyboard combination string.

        Args:
            keys: String such as `command+c` or `ctrl+shift+t`.
        """
        parts = [part.strip() for part in keys.split("+") if part.strip()]
        await self.keypress(parts)

    async def scroll(self, y: int, x: int, direction: str, magnitude: int = 800) -> None:
        """Scroll from a normalized point in the requested direction.

        Args:
            y: Vertical coordinate on a 0-1000 grid.
            x: Horizontal coordinate on a 0-1000 grid.
            direction: One of `up`, `down`, `left`, or `right`.
            magnitude: Scroll distance on the same 0-1000 scale used by the
                model's desktop function declarations.
        """
        target_x, target_y = self._normalize_point(x=x, y=y)
        await asyncio.to_thread(pyautogui.moveTo, target_x, target_y)

        vertical_amount = max(100, int((magnitude / 1000.0) * self.screen_height))
        horizontal_amount = max(100, int((magnitude / 1000.0) * self.screen_width))
        direction_lower = direction.lower()
        if direction_lower == "down":
            await asyncio.to_thread(pyautogui.scroll, -vertical_amount)
        elif direction_lower == "up":
            await asyncio.to_thread(pyautogui.scroll, vertical_amount)
        elif direction_lower == "left":
            await asyncio.to_thread(pyautogui.hscroll, -horizontal_amount)
        elif direction_lower == "right":
            await asyncio.to_thread(pyautogui.hscroll, horizontal_amount)
        await self._sleep_after_action_if_needed()

    async def move_cursor(self, x: int, y: int) -> None:
        """Move the pointer to a normalized point on the screen.

        Args:
            x: Horizontal coordinate on a 0-1000 grid.
            y: Vertical coordinate on a 0-1000 grid.
        """
        target_x, target_y = self._normalize_point(x=x, y=y)
        await self.hover_coords(target_x, target_y)

    async def click(self, x: int, y: int) -> None:
        """Click a normalized point on the screen.

        Args:
            x: Horizontal coordinate on a 0-1000 grid.
            y: Vertical coordinate on a 0-1000 grid.
        """
        actual_x, actual_y = self._normalize_point(x=x, y=y)
        await self.click_coords(actual_x, actual_y)

    async def type_text(self, text: str, x: int | None = None, y: int | None = None, press_enter: bool = False) -> None:
        """Type text, optionally focusing a normalized point first.

        Args:
            text: Text to type.
            x: Optional normalized horizontal coordinate to click before typing.
            y: Optional normalized vertical coordinate to click before typing.
            press_enter: Whether to press Enter after typing finishes.
        """
        if x is not None and y is not None:
            await self.click(x=x, y=y)
            await asyncio.sleep(0.1)
        await self._write_text(text)
        if press_enter:
            await self.keypress(["enter"])

    async def drag_and_drop(
        self, y: int, x: int, destination_y: int, destination_x: int
    ) -> None:
        """Drag from one normalized point to another.

        Args:
            y: Start vertical coordinate on a 0-1000 grid.
            x: Start horizontal coordinate on a 0-1000 grid.
            destination_y: Destination vertical coordinate on a 0-1000 grid.
            destination_x: Destination horizontal coordinate on a 0-1000 grid.
        """
        start_x, start_y = self._normalize_point(x=x, y=y)
        end_x, end_y = self._normalize_point(x=destination_x, y=destination_y)
        await asyncio.to_thread(pyautogui.moveTo, start_x, start_y)
        await asyncio.to_thread(pyautogui.dragTo, end_x, end_y, duration=0.2, button="left")
        await self._sleep_after_action_if_needed()

    async def wait_5_seconds(self) -> None:
        """Pause execution for five seconds."""
        await asyncio.sleep(5)

    async def launch_application(self, application: str) -> None:
        """Launch an application by executable or application name.

        Args:
            application: Platform-specific application identifier.
        """
        self._last_target = f"app:{application}"
        await asyncio.to_thread(self._launch_application_sync, application)
        await self.wait_until_loaded(timeout_ms=1200)

    async def open_app(self, app_name: str, intent: str | None = None) -> None:
        """Open an application and optionally follow it with a deep-link target.

        Args:
            app_name: Human-readable or executable application name.
            intent: Optional path or URL to open after the application launches.
        """
        await self.launch_application(app_name)
        if intent:
            await self.open_item(intent)

    async def open_terminal(self) -> None:
        """Open the host operating system's default terminal application.

        Raises:
            RuntimeError: If no supported terminal application can be located.
        """
        system = platform.system()
        if system == "Darwin":
            await self.open_app("Terminal")
            return
        if system == "Windows":
            await self.launch_application("cmd")
            return
        for candidate in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            try:
                await self.launch_application(candidate)
                return
            except Exception:
                continue
        raise RuntimeError("No supported terminal application found.")

    async def run_terminal_command(self, command: str, press_enter: bool = True) -> None:
        """Run or type a command in the active terminal session.

        Args:
            command: Shell command to execute or type.
            press_enter: Whether to submit the command immediately.
        """
        system = platform.system()
        self._last_target = f"terminal:{command}"
        if system == "Darwin":
            await asyncio.to_thread(self._run_terminal_command_macos_sync, command, press_enter)
            await self.wait_until_loaded(timeout_ms=700)
            return

        await self._write_text(command)
        if press_enter:
            await self.keypress(["enter"])

    def _run_terminal_command_macos_sync(self, command: str, press_enter: bool) -> None:
        """Run a terminal command on macOS via AppleScript.

        Args:
            command: Shell command to execute or type.
            press_enter: Whether to submit the command immediately.
        """
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        if press_enter:
            script = f'tell application "Terminal" to do script "{escaped}"'
        else:
            script = (
                'tell application "Terminal" to activate\n'
                f'tell application "System Events" to keystroke "{escaped}"'
            )
        subprocess.run(["osascript", "-e", script], check=True)

    def _launch_application_sync(self, application: str) -> None:
        """Launch an application using platform-specific OS commands.

        Args:
            application: Platform-specific application identifier.
        """
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", "-a", application])
            return
        if system == "Windows":
            os.startfile(application)  # type: ignore[attr-defined]
            return
        subprocess.Popen([application])

    async def open_item(self, target: str) -> None:
        """Open a file path or URL with the host operating system.

        Args:
            target: Local path or URL to open.
        """
        self._last_target = target
        if "://" in target:
            await asyncio.to_thread(webbrowser.open, target)
        else:
            await asyncio.to_thread(self._open_item_sync, target)
        await self.wait_until_loaded(timeout_ms=1200)

    async def long_press_at(self, x: int, y: int, duration_ms: int = 500) -> None:
        """Long-press absolute screen coordinates.

        Args:
            x: Absolute horizontal coordinate in screen pixels.
            y: Absolute vertical coordinate in screen pixels.
            duration_ms: Duration of the press in milliseconds.
        """
        actual_x = max(0, min(int(x), self.screen_width - 1))
        actual_y = max(0, min(int(y), self.screen_height - 1))
        await asyncio.to_thread(pyautogui.moveTo, actual_x, actual_y)
        await asyncio.to_thread(pyautogui.mouseDown, x=actual_x, y=actual_y, button="left")
        await asyncio.sleep(duration_ms / 1000.0)
        await asyncio.to_thread(pyautogui.mouseUp, x=actual_x, y=actual_y, button="left")
        await self._sleep_after_action_if_needed()

    def _open_item_sync(self, target: str) -> None:
        """Open a local item synchronously using the host operating system.

        Args:
            target: Local file system path to open.
        """
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", target])
            return
        if system == "Windows":
            os.startfile(target)  # type: ignore[attr-defined]
            return
        subprocess.Popen(["xdg-open", target])

    async def close_window(self) -> None:
        """Close the active desktop window using the platform shortcut."""
        if platform.system() == "Darwin":
            await self.keypress(["command", "w"])
        else:
            await self.keypress(["alt", "f4"])

    async def minimize_window(self) -> None:
        """Minimize the active window using the platform shortcut."""
        system = platform.system()
        if system == "Darwin":
            await self.keypress(["command", "m"])
        elif system == "Windows":
            await self.keypress(["win", "down"])
        else:
            await self.keypress(["alt", "space"])
            await self.keypress(["n"])

    async def maximize_window(self) -> None:
        """Maximize or full-screen the active window using the platform shortcut."""
        system = platform.system()
        if system == "Darwin":
            await self.keypress(["command", "ctrl", "f"])
        elif system == "Windows":
            await self.keypress(["win", "up"])
        else:
            await self.keypress(["alt", "f10"])

    async def switch_application(self) -> None:
        """Switch to the next application using the platform shortcut."""
        if platform.system() == "Darwin":
            await self.keypress(["command", "tab"])
        else:
            await self.keypress(["alt", "tab"])

    async def go_home(self) -> None:
        """Show the desktop or home surface for the host operating system."""
        self._last_target = "desktop://home"
        system = platform.system()
        if system == "Darwin":
            await self.keypress(["f11"])
        else:
            await self.keypress(["win", "d"])

    async def get_state(self) -> str:
        """Return the controller's last known high-level desktop target.

        Returns:
            A string describing the most recent opened app, path, URL, or other
            synthetic desktop state marker.
        """
        return self._last_target

    async def _sleep_after_action_if_needed(self) -> None:
        """Sleep for the configured post-action delay when non-zero."""
        if self._sleep_after_action > 0:
            await asyncio.sleep(self._sleep_after_action)

    def _normalize_key(self, key: str) -> str:
        """Translate an abstract key token into a PyAutoGUI-compatible key name.

        Args:
            key: Key name emitted by the agent or supplied by a caller.

        Returns:
            A platform-appropriate key string compatible with PyAutoGUI.
        """
        normalized = PYAUTOGUI_KEY_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        if normalized == "command" and platform.system() != "Darwin":
            return "ctrl"
        if normalized == "win" and platform.system() == "Darwin":
            return "command"
        return normalized
