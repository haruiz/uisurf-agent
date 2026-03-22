"""Playwright-backed browser controller used by the browser agent.

This module owns Playwright lifecycle management, browser observations, pointer
and keyboard actions, page-helper injection, and browser-specific convenience
methods such as scrolling and history navigation.
"""

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Union

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .animation_utils import AnimationUtilsPlaywright
from .screenshot_utils import resize_png_bytes, validate_observation_scale


@dataclass
class InteractiveRegion:
    """Geometry and metadata for an interactive element in the current document.

    Instances of this dataclass are created from the injected `WebSurfer` helper
    script and represent clickable or otherwise actionable regions detected on the
    page. The coordinates are expressed in viewport space.
    """

    x: float
    y: float
    width: float
    height: float
    tag: str
    text: str


@dataclass
class VisualViewport:
    """Viewport and scroll metrics returned by the injected page helper script.

    This structure mirrors the browser-side payload returned by
    `WebSurfer.getVisualViewport()` and provides enough information for agents to
    reason about the currently visible portion of the page and the overall scroll
    position.
    """

    x: float
    y: float
    width: float
    height: float
    pageTop: float
    scrollHeight: float


def interactiveregion_from_dict(d: Dict[str, Any]) -> InteractiveRegion:
    """Convert a raw browser-side dictionary into an `InteractiveRegion`.

    Args:
        d: Mapping returned by the page helper script for a single interactive
            region.

    Returns:
        A populated `InteractiveRegion` instance.
    """
    return InteractiveRegion(**d)


def visualviewport_from_dict(d: Dict[str, Any]) -> VisualViewport:
    """Convert a raw browser-side dictionary into a `VisualViewport`.

    Args:
        d: Mapping returned by the page helper script for the visual viewport.

    Returns:
        A populated `VisualViewport` instance.
    """
    return VisualViewport(**d)


CUA_KEY_TO_PLAYWRIGHT_KEY = {
    "/": "Divide",
    "\\": "Backslash",
    "alt": "Alt",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "arrowup": "ArrowUp",
    "backspace": "Backspace",
    "capslock": "CapsLock",
    "cmd": "Meta",
    "command": "Meta",
    "control": "Control",
    "ctrl": "Control",
    "delete": "Delete",
    "end": "End",
    "enter": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "home": "Home",
    "insert": "Insert",
    "meta": "Meta",
    "option": "Alt",
    "pagedown": "PageDown",
    "pageup": "PageUp",
    "shift": "Shift",
    "space": " ",
    "super": "Meta",
    "tab": "Tab",
    "win": "Meta",
    "browserback": "Alt+Left",
    "browserforward": "Alt+Right",
    "back": "Alt+Left",
    "forward": "Alt+Right",
}

class BrowserController:
    """Thin async wrapper around Playwright tailored for UI-agent interactions.

    The controller is responsible for owning the Playwright lifecycle, exposing
    common browser actions in agent-friendly methods, injecting helper scripts
    used to extract page structure, and optionally rendering action animations to
    make debugging easier.
    """

    DEFAULT_CDP_URL = os.environ.get("BROWSER_CDP_URL", "http://127.0.0.1:9222")

    def __init__(
        self,
        downloads_folder: Optional[str] = None,
        animate_actions: bool = False,
        viewport_width: int = 1440,
        viewport_height: int = 1440,
        headless: bool = True,
        timeout_load: Union[int, float] = 1,
        sleep_after_action: Union[int, float] = 0.1,
        channel: str = "chrome",
        fast_mode: bool = False,
        observation_scale: float = 1.0,
    ) -> None:
        """Configure the browser controller and preload page helper assets.

        Args:
            downloads_folder: Optional downloads directory for future browser
                interactions.
            animate_actions: Whether cursor movement and action effects should be
                animated in the page.
            viewport_width: Width of the Playwright viewport in CSS pixels.
            viewport_height: Height of the Playwright viewport in CSS pixels.
            headless: Whether Chromium should run without a visible UI.
            timeout_load: Reserved load timeout configuration for future use.
            sleep_after_action: Delay inserted after actions to allow the page to
                respond before the next observation.
            channel: Browser channel passed to Playwright when launching Chromium.
            fast_mode: Whether page stabilization should favor speed over
                completeness.
            observation_scale: Scale factor applied to screenshots before they
                are sent to the model. Coordinates still map to the full viewport.
        """
        self.animate_actions = animate_actions
        self.downloads_folder = downloads_folder
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self._timeout_load = timeout_load
        self._channel = channel
        self._headless = headless
        self._sleep_after_action = sleep_after_action
        self.fast_mode = fast_mode
        self._observation_scale = validate_observation_scale(observation_scale)
        self._cdp_url = self.DEFAULT_CDP_URL
        self._connected_over_cdp = False
        self._owns_context = False
        self._owns_page = False
        
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        self._animation = AnimationUtilsPlaywright()

        # Load page script
        script_path = os.path.join(os.path.dirname(__file__), "page_script.js")
        with open(script_path, "r") as f:
            self._page_script = f.read()

    async def setup(self) -> Page:
        """Start Playwright and prepare a browser page for interaction.

        The controller prefers attaching to an already-running Chromium instance
        over CDP. That is the desired mode inside the Docker container, where
        Chromium is started separately and exposed on ``BROWSER_CDP_URL``.

        If the CDP endpoint is unavailable, the controller falls back to
        launching its own Chromium instance for local development workflows.

        Returns:
            The Playwright `Page` instance ready for interaction.
        """
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(self._cdp_url)
            self._connected_over_cdp = True
        except Exception:
            self.browser = await self.playwright.chromium.launch(
                headless=self._headless,
                channel=self._channel,
            )
            self._connected_over_cdp = False

        if self._connected_over_cdp and self.browser.contexts:
            self.context = self.browser.contexts[0]
            self._owns_context = False
        else:
            self.context = await self.browser.new_context(
                viewport={"width": self.viewport_width, "height": self.viewport_height}
            )
            self._owns_context = True

        if self.context.pages:
            self.page = self.context.pages[0]
            self._owns_page = False
        else:
            self.page = await self.context.new_page()
            self._owns_page = True

        await self._ensure_page_ready(self.page)
        return self.page

    def _normalize_point(self, x: int, y: int) -> tuple[float, float]:
        """Convert normalized 0-1000 browser coordinates into viewport pixels.

        Args:
            x: Horizontal coordinate on Gemini's 0-1000 grid.
            y: Vertical coordinate on Gemini's 0-1000 grid.

        Returns:
            A tuple of `(x, y)` coordinates expressed in viewport pixels.
        """
        return (
            (x / 1000.0) * self.viewport_width,
            (y / 1000.0) * self.viewport_height,
        )

    async def _sleep_after_action_if_needed(self) -> None:
        """Apply the configured post-action delay when non-zero."""
        if self._sleep_after_action > 0:
            await asyncio.sleep(self._sleep_after_action)

    async def _ensure_page_ready(self, page: Page) -> None:
        """Ensure the current page has the injected helper API available.

        Args:
            page: The Playwright page that should be prepared for interaction.
        """
        # Ensure future navigations always have WebSurfer helpers.
        await page.add_init_script(script=self._page_script)
        try:
            await page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        # Also ensure the current document has helpers available now.
        await self._ensure_websurfer_api(page)

    async def _ensure_websurfer_api(self, page: Page) -> None:
        """Inject the `WebSurfer` helper API into the current page if needed.

        The helper may disappear during navigation because the execution context
        is recreated. This method retries through transient navigation failures
        and performs a final best-effort injection after the page settles.

        Args:
            page: The Playwright page that should expose `window.WebSurfer`.
        """
        for _ in range(3):
            try:
                has_api = await page.evaluate("typeof window.WebSurfer !== 'undefined'")
                if not has_api:
                    await page.evaluate(self._page_script)
                return
            except Exception as exc:
                # Common during navigation: "Execution context was destroyed".
                message = str(exc).lower()
                if "execution context was destroyed" in message or "cannot find context" in message:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=2000)
                    except Exception:
                        pass
                    continue
                raise
        # Final best effort after transient navigation races.
        try:
            await page.wait_for_load_state("load", timeout=3000)
        except Exception:
            pass
        has_api = await page.evaluate("typeof window.WebSurfer !== 'undefined'")
        if not has_api:
            await page.evaluate(self._page_script)

    async def navigate(self, url: str):
        """Navigate the active page to a new URL and restore helper scripts.

        Args:
            url: Destination URL to open in the current page.
        """
        if not self.page: raise RuntimeError("Call setup() first.")
        await self.page.goto(url)
        await self._ensure_page_ready(self.page)

    async def get_current_url(self) -> str:
        """Return the current URL of the active page.

        Returns:
            The current `page.url` value.
        """
        if not self.page: raise RuntimeError("No page active.")
        return self.page.url

    async def capture_screenshot(self, full_page: bool = False) -> bytes:
        """Capture a PNG screenshot after waiting for the page to settle.

        Args:
            full_page: Whether the screenshot should include the full scrollable
                page instead of only the viewport.

        Returns:
            Raw PNG bytes suitable for passing back to the model.
        """
        if not self.page: raise RuntimeError("No page active.")
        await self.wait_until_loaded()
        screenshot = await self.page.screenshot(full_page=full_page, type="png")
        return resize_png_bytes(screenshot, self._observation_scale)

    async def wait_until_loaded(self, timeout_ms: int = 15000) -> None:
        """Wait until the active page is loaded and visually stable.

        This method combines Playwright load-state waits with a custom render
        stabilization pass so screenshots are more likely to reflect a settled UI.

        Args:
            timeout_ms: Maximum time to wait for load states and stabilization.
        """
        if not self.page:
            raise RuntimeError("No page active.")
        if self.fast_mode:
            timeout_ms = min(timeout_ms, 5000)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception:
            pass
        if not self.fast_mode:
            try:
                await self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                # Some sites keep long-polling requests open forever.
                pass
        await self._wait_for_render_stable(
            timeout_ms=timeout_ms if not self.fast_mode else 2500,
            check_interval_ms=250 if not self.fast_mode else 120,
            required_stable_checks=3 if not self.fast_mode else 1,
        )

    async def _wait_for_render_stable(
        self,
        timeout_ms: int = 15000,
        check_interval_ms: int = 250,
        required_stable_checks: int = 3,
    ) -> None:
        """Wait for a best-effort stable rendering state before observing the page.

        This mirrors the intent of Magentic-UI's observation loop where each
        action is followed by an observation screenshot. We wait for readiness
        signals so screenshots represent a settled page.

        Args:
            timeout_ms: Maximum time to spend checking for stability.
            check_interval_ms: Delay between stability checks.
            required_stable_checks: Number of consecutive stable checks required
                before the page is considered settled.
        """
        if not self.page:
            raise RuntimeError("No page active.")

        deadline = time.monotonic() + (timeout_ms / 1000.0)
        stable_count = 0

        while time.monotonic() < deadline:
            try:
                state = await self.page.evaluate(
                    """() => {
                        const pendingImages = Array.from(document.images)
                          .filter((img) => !img.complete).length;
                        const fontsReady = document.fonts ? document.fonts.status === "loaded" : true;
                        const spinnerSelectors = [
                          '[aria-busy="true"]',
                          '[role="progressbar"]',
                          '.loading',
                          '.spinner',
                          '[data-testid*="loading"]'
                        ];
                        const busyIndicators = spinnerSelectors
                          .map((s) => document.querySelectorAll(s).length)
                          .reduce((a, b) => a + b, 0);
                        return {
                          readyState: document.readyState,
                          pendingImages,
                          fontsReady,
                          busyIndicators
                        };
                    }"""
                )
                is_stable = (
                    state["readyState"] == "complete"
                    and state["pendingImages"] == 0
                    and state["fontsReady"] is True
                    and state["busyIndicators"] == 0
                )
                if is_stable:
                    stable_count += 1
                    if stable_count >= required_stable_checks:
                        break
                else:
                    stable_count = 0
            except Exception:
                stable_count = 0

            await asyncio.sleep(check_interval_ms / 1000.0)

        # Small final settle helps with CSS transitions finishing.
        await asyncio.sleep(0.15)

    async def get_window_info(self) -> Dict[str, int]:
        """Return browser window geometry used for coordinate calculations.

        Returns:
            A dictionary containing screen position, outer size, inner size, and
            device-pixel-ratio information from the active browser window.
        """
        if not self.page: raise RuntimeError("No page active.")
        return await self.page.evaluate("""
            () => ({
                screenX: window.screenX,
                screenY: window.screenY,
                outerWidth: window.outerWidth,
                outerHeight: window.outerHeight,
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight,
                devicePixelRatio: window.devicePixelRatio
            })
        """)

    async def get_element_coordinates(self, selector: str) -> Dict[str, Dict[str, float]]:
        """Return both viewport and screen coordinates for a selected element.

        Args:
            selector: CSS selector used to locate the target element.

        Returns:
            A dictionary with `viewport` coordinates from Playwright and derived
            `screen` coordinates that account for browser chrome offsets.
        """
        if not self.page: raise RuntimeError("No page active.")
        element = await self.page.wait_for_selector(selector)
        box = await element.bounding_box()
        if not box: raise ValueError("Element not visible.")
        
        win = await self.get_window_info()
        chrome_h = (win['outerWidth'] - win['innerWidth']) / 2
        chrome_v = win['outerHeight'] - win['innerHeight'] - chrome_h
        
        return {
            "viewport": box,
            "screen": {
                "x": win['screenX'] + chrome_h + box['x'],
                "y": win['screenY'] + chrome_v + box['y'],
                "width": box['width'],
                "height": box['height']
            }
        }

    async def click_coords(self, x: float, y: float, button: Literal["left", "right"] = "left"):
        """Click the page at the specified viewport coordinates.

        Args:
            x: Horizontal coordinate in viewport pixels.
            y: Vertical coordinate in viewport pixels.
            button: Mouse button to click with.
        """
        if not self.page: raise RuntimeError("No page active.")
        if self.animate_actions:
            start_x, start_y = self._animation.last_cursor_position
            await self._animation.gradual_cursor_animation(self.page, start_x, start_y, x, y)
            await self._animation.click_ripple(self.page, x, y)
        await self.page.mouse.click(x, y, button=button)
        await self._sleep_after_action_if_needed()

    async def hover_coords(self, x: float, y: float):
        """Move the pointer to the specified viewport coordinates.

        Args:
            x: Horizontal coordinate in viewport pixels.
            y: Vertical coordinate in viewport pixels.
        """
        if not self.page: raise RuntimeError("No page active.")
        if self.animate_actions:
            start_x, start_y = self._animation.last_cursor_position
            await self._animation.gradual_cursor_animation(self.page, start_x, start_y, x, y)
        await self.page.mouse.move(x, y)
        await self._sleep_after_action_if_needed()

    async def type_text(self, text: str):
        """Type text into the currently focused element.

        Args:
            text: Text to send through the browser keyboard interface.
        """
        if not self.page: raise RuntimeError("No page active.")
        if self.animate_actions:
            x, y = self._animation.last_cursor_position
            await self._animation.type_pulse(self.page, x, y)
        await self.page.keyboard.type(text)
        await self._sleep_after_action_if_needed()

    async def clear_text_input(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> None:
        """Clear the focused text input, optionally focusing it first.

        Args:
            x: Optional normalized horizontal coordinate (0-1000) to click
                before clearing.
            y: Optional normalized vertical coordinate (0-1000) to click before
                clearing.
        """
        if not self.page:
            raise RuntimeError("No page active.")
        if (x is None) != (y is None):
            raise ValueError("Both x and y must be provided together.")

        if x is not None and y is not None:
            target_x, target_y = self._normalize_point(x=x, y=y)
            await self.click_coords(target_x, target_y)
            await asyncio.sleep(0.1)

        has_editable_focus = await self.page.evaluate(
            """() => {
                const active = document.activeElement;
                if (!active) {
                    return false;
                }

                const nonTextInputTypes = new Set([
                    "button",
                    "checkbox",
                    "color",
                    "date",
                    "datetime-local",
                    "file",
                    "hidden",
                    "image",
                    "month",
                    "radio",
                    "range",
                    "reset",
                    "submit",
                    "time",
                    "week"
                ]);

                if (active instanceof HTMLTextAreaElement) {
                    return !active.disabled && !active.readOnly;
                }

                if (active instanceof HTMLInputElement) {
                    const type = (active.type || "text").toLowerCase();
                    return !active.disabled && !active.readOnly && !nonTextInputTypes.has(type);
                }

                return active instanceof HTMLElement && active.isContentEditable;
            }"""
        )
        if not has_editable_focus:
            raise RuntimeError("No focused text input to clear.")

        if self.animate_actions:
            cursor_x, cursor_y = self._animation.last_cursor_position
            await self._animation.type_pulse(self.page, cursor_x, cursor_y)

        modifier_key = "Meta" if sys.platform == "darwin" else "Control"
        await self.page.keyboard.press(f"{modifier_key}+A")
        await self.page.keyboard.press("Backspace")

        cleared = await self.page.evaluate(
            """() => {
                const active = document.activeElement;
                if (!active) {
                    return false;
                }

                if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
                    return active.value === "";
                }

                if (active instanceof HTMLElement && active.isContentEditable) {
                    return active.textContent === "";
                }

                return false;
            }"""
        )
        if not cleared:
            cleared = await self.page.evaluate(
                """() => {
                    const active = document.activeElement;
                    if (!active) {
                        return false;
                    }

                    const dispatchInputEvent = (element) => {
                        const view = element.ownerDocument.defaultView;
                        if (view && typeof view.InputEvent === "function") {
                            element.dispatchEvent(
                                new view.InputEvent("input", {
                                    bubbles: true,
                                    composed: true,
                                    inputType: "deleteContentBackward",
                                    data: null
                                })
                            );
                            return;
                        }
                        element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
                    };

                    if (active instanceof HTMLInputElement) {
                        const descriptor = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype,
                            "value",
                        );
                        if (descriptor && descriptor.set) {
                            descriptor.set.call(active, "");
                        } else {
                            active.value = "";
                        }
                        dispatchInputEvent(active);
                        return active.value === "";
                    }

                    if (active instanceof HTMLTextAreaElement) {
                        const descriptor = Object.getOwnPropertyDescriptor(
                            HTMLTextAreaElement.prototype,
                            "value",
                        );
                        if (descriptor && descriptor.set) {
                            descriptor.set.call(active, "");
                        } else {
                            active.value = "";
                        }
                        dispatchInputEvent(active);
                        return active.value === "";
                    }

                    if (active instanceof HTMLElement && active.isContentEditable) {
                        active.textContent = "";
                        dispatchInputEvent(active);
                        return active.textContent === "";
                    }

                    return false;
                }"""
            )

        if not cleared:
            raise RuntimeError("Failed to clear focused text input.")

        await self._sleep_after_action_if_needed()

    async def keypress(self, keys: List[str]):
        """Press one or more keys using computer-use to Playwright key mappings.

        Args:
            keys: Ordered list of key names produced by the model.
        """
        if not self.page: raise RuntimeError("No page active.")
        if self.animate_actions:
            x, y = self._animation.last_cursor_position
            await self._animation.type_pulse(self.page, x, y)
        for key in keys:
            normalized = str(key).strip()
            mapped = CUA_KEY_TO_PLAYWRIGHT_KEY.get(normalized.lower(), normalized)
            try:
                await self.page.keyboard.press(mapped)
            except Exception:
                # Skip unsupported keys from model output instead of crashing the run.
                continue
        await self._sleep_after_action_if_needed()

    async def scroll_by(self, dx: float = 0, dy: float = 0):
        """Scroll the page by mouse-wheel deltas in viewport space.

        Args:
            dx: Horizontal wheel delta.
            dy: Vertical wheel delta.
        """
        if not self.page: raise RuntimeError("No page active.")
        await self.page.mouse.wheel(dx, dy)
        await self._sleep_after_action_if_needed()

    async def get_visual_viewport(self) -> VisualViewport:
        """Read the current visual viewport metadata from the helper API.

        Returns:
            A `VisualViewport` describing the visible portion of the page.
        """
        if not self.page: raise RuntimeError("No page active.")
        for _ in range(3):
            try:
                await self._ensure_websurfer_api(self.page)
                res = await self.page.evaluate("WebSurfer.getVisualViewport();")
                return visualviewport_from_dict(res)
            except Exception as exc:
                message = str(exc).lower()
                if "execution context was destroyed" in message or "cannot find context" in message:
                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=2000)
                    except Exception:
                        pass
                    continue
                raise
        raise RuntimeError("Failed to read visual viewport after navigation retries.")

    async def get_interactive_regions(self) -> Dict[str, InteractiveRegion]:
        """Read the currently detected interactive regions from the helper API.

        Returns:
            A mapping from region identifiers to `InteractiveRegion` instances.
        """
        if not self.page: raise RuntimeError("No page active.")
        for _ in range(3):
            try:
                await self._ensure_websurfer_api(self.page)
                res = await self.page.evaluate("WebSurfer.getInteractiveRects();")
                return {k: interactiveregion_from_dict(v) for k, v in res.items()}
            except Exception as exc:
                message = str(exc).lower()
                if "execution context was destroyed" in message or "cannot find context" in message:
                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=2000)
                    except Exception:
                        pass
                    continue
                raise
        return {}

    async def cleanup(self) -> None:
        """Close controller-owned resources and stop the Playwright runtime.

        The method attempts to remove any visual debug artifacts first, then
        disposes of browser resources owned by this controller, and finally
        shuts down Playwright itself.

        When connected to an externally managed Chromium instance over CDP, the
        controller avoids closing the shared browser process.
        """
        if self.page and self.animate_actions:
            try:
                await self._animation.cleanup_animations(self.page)
            except Exception:
                pass
        if self._owns_page and self.page:
            try:
                await self.page.close()
            except Exception:
                pass
        if self._owns_context and self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.browser and not self._connected_over_cdp:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def wait_5_seconds(self) -> None:
        """Pause execution for five seconds before continuing."""
        await asyncio.sleep(5)

    async def go_back(self) -> None:
        """Navigate back in browser history using the predefined back shortcut."""
        await self._ensure_websurfer_api(self.page)
        await self.page.keyboard.type(CUA_KEY_TO_PLAYWRIGHT_KEY["browserback"])

    async def go_forward(self) -> None:
        """Navigate forward in browser history using the predefined shortcut."""
        await self._ensure_websurfer_api(self.page)
        await self.page.keyboard.type(CUA_KEY_TO_PLAYWRIGHT_KEY["browserforward"])

    async def scroll_down(self) -> None:
        """Trigger a page-down style keyboard scroll."""
        await self._ensure_websurfer_api(self.page)
        await self.page.keyboard.press("scrolldown")

    async def scroll_up(self) -> None:
        """Trigger a page-up style keyboard scroll."""
        await self._ensure_websurfer_api(self.page)
        await self.page.keyboard.press("scrollup")

    def _map_key_combination(self, keys: str) -> str:
        """Translate a `+`-delimited key combination into Playwright syntax.

        Args:
            keys: Combination string such as `control+c`.

        Returns:
            Playwright-compatible key combination string.
        """
        parts = keys.split("+")
        mapped_parts = []
        for part in parts:
            part = part.strip()
            mapped_parts.append(CUA_KEY_TO_PLAYWRIGHT_KEY.get(part.lower(), part))
        return "+".join(mapped_parts)

    async def key_combination(self, keys: str):
        """Press keyboard keys or combinations, such as 'Control+C' or 'Enter'.

        Args:
            keys: String of keys to press (e.g., 'control+c').
        """
        if not self.page: raise RuntimeError("No page active.")
        final_key = self._map_key_combination(keys)

        if self.animate_actions:
            x, y = self._animation.last_cursor_position
            await self._animation.type_pulse(self.page, x, y)
        await self.page.keyboard.press(final_key)
        await self._sleep_after_action_if_needed()

    async def scroll_document(self, direction: str) -> None:
        """Scrolls the entire webpage.

        Args:
            direction: 'up', 'down', 'left', or 'right'.
        """
        if not self.page: raise RuntimeError("No page active.")
        
        direction = direction.lower()
        delta_x = 0
        delta_y = 0
        scroll_amount_y = self.viewport_height * 0.8
        scroll_amount_x = self.viewport_width * 0.8
        
        if direction == "down":
            delta_y = scroll_amount_y
        elif direction == "up":
            delta_y = -scroll_amount_y
        elif direction == "right":
            delta_x = scroll_amount_x
        elif direction == "left":
            delta_x = -scroll_amount_x
        await self.page.mouse.wheel(delta_x, delta_y)
        await self._sleep_after_action_if_needed()

    async def scroll_at(self, y: int, x: int, direction: str, magnitude: int = 800) -> None:
        """Scrolls a specific element or area.

        Args:
            y: Vertical coordinate (0-999).
            x: Horizontal coordinate (0-999).
            direction: 'up', 'down', 'left', or 'right'.
            magnitude: Scroll amount (0-999, default 800).
        """
        if not self.page: raise RuntimeError("No page active.")
        viewport_x, viewport_y = self._normalize_point(x=x, y=y)
        if self.animate_actions:
            start_x, start_y = self._animation.last_cursor_position
            await self._animation.gradual_cursor_animation(self.page, start_x, start_y, viewport_x, viewport_y)
        await self.page.mouse.move(viewport_x, viewport_y)
        direction = direction.lower()
        delta_x = 0
        delta_y = 0
        
        # Magnitude is on 1000x1000 grid
        pixel_magnitude_x = (magnitude / 1000.0) * self.viewport_width
        pixel_magnitude_y = (magnitude / 1000.0) * self.viewport_height
        
        if direction == "down":
            delta_y = pixel_magnitude_y
        elif direction == "up":
            delta_y = -pixel_magnitude_y
        elif direction == "right":
            delta_x = pixel_magnitude_x
        elif direction == "left":
            delta_x = -pixel_magnitude_x
        await self.page.mouse.wheel(delta_x, delta_y)
        await self._sleep_after_action_if_needed()

    async def drag_and_drop(self, y: int, x: int, destination_y: int, destination_x: int) -> None:
        """Drags an element from a starting coordinate and drops it at a destination.

        Args:
            y: Start vertical coordinate (0-999).
            x: Start horizontal coordinate (0-999).
            destination_y: End vertical coordinate (0-999).
            destination_x: End horizontal coordinate (0-999).
        """
        if not self.page: raise RuntimeError("No page active.")
        start_x, start_y = self._normalize_point(x=x, y=y)
        end_x, end_y = self._normalize_point(x=destination_x, y=destination_y)
        if self.animate_actions:
            cur_x, cur_y = self._animation.last_cursor_position
            await self._animation.gradual_cursor_animation(self.page, cur_x, cur_y, start_x, start_y)
        await self.page.mouse.move(start_x, start_y)
        await self.page.mouse.down()
        if self.animate_actions:
            await self._animation.gradual_cursor_animation(self.page, start_x, start_y, end_x, end_y)
        await self.page.mouse.move(end_x, end_y, steps=10)
        await self.page.mouse.up()
        await self._sleep_after_action_if_needed()

    async def hover_at(self, y: int, x: int) -> None:
        """Hovers the mouse at a specific coordinate on the webpage.

        Args:
            y: Vertical coordinate (0-999).
            x: Horizontal coordinate (0-999).
        """
        if not self.page: raise RuntimeError("No page active.")
        target_x, target_y = self._normalize_point(x=x, y=y)
        if self.animate_actions:
            cur_x, cur_y = self._animation.last_cursor_position
            await self._animation.gradual_cursor_animation(self.page, cur_x, cur_y, target_x, target_y)
        await self.page.mouse.move(target_x, target_y)
        await self._sleep_after_action_if_needed()
