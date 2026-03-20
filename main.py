from __future__ import annotations

"""Small local runner for manually testing the browser or desktop agent.

Edit the module-level configuration variables below and run this file directly to
exercise either agent without CLI argument parsing.
"""

import asyncio
import logging

from uisurf_agent.agents import BrowserAgent, DesktopAgent
from uisurf_agent.agents.ui_agent import SafetyPromptDecision

logger = logging.getLogger("ui_operator_agent")

# Select which agent implementation to exercise from this file.
AGENT = "browser"
# Natural-language task passed to the chosen agent.
#TASK = "Find my images folder and open the first file found, use the terminal for it"
TASK = "Find images of pomeranian in Google"

# Maximum number of reasoning/action iterations.
MAX_STEPS = 20
# Whether the browser agent should hide the browser window.
HEADLESS = False
# Whether safety confirmations should be auto-approved.
AUTO_CONFIRM = False
# Whether browser settling should favor speed over completeness.
BROWSER_FAST_MODE = True
# Whether model thought streaming should be enabled when supported.
INCLUDE_THOUGHTS = True
# Delay before each desktop screenshot in milliseconds.
DESKTOP_OBSERVATION_DELAY_MS = 1000
# Number of screenshot-bearing observations to keep in model history.
MAX_OBSERVATION_IMAGES = 2
# Scale screenshots before sending them to the model. Use 1.0 for full resolution.
OBSERVATION_SCALE = 1.0


async def main_safety_prompt_handler(function_call, auto_confirm: bool) -> SafetyPromptDecision:
    """Resolve safety-gated actions for the local manual runner.

    This callback mirrors the UI integration shape used by `UIAgent.run(...)`.
    It pauses execution until a decision is available, which makes it suitable
    for later replacement with a real UI-driven approval flow.

    Args:
        function_call: Model-emitted function call that may contain a
            `safety_decision` block.
        auto_confirm: Whether approvals should be granted automatically.

    Returns:
        A tuple of `(safety_acknowledged, allowed)`.
    """
    safety_decision = function_call.args.get("safety_decision")
    if not (
        safety_decision
        and safety_decision.get("decision") == "require_confirmation"
    ):
        return False, True

    explanation = safety_decision.get("explanation", "No explanation provided.")
    logger.info("safety_prompt: %s", explanation)
    logger.info("pending_action: %s %s", function_call.name, dict(function_call.args))

    if auto_confirm:
        return True, True

    user_input = await asyncio.to_thread(
        input,
        f"Allow the agent to execute '{function_call.name}'? (y/n): ",
    )
    allowed = user_input.strip().lower() in {"y", "yes"}
    return True, allowed


async def run_browser_agent(
    task: str = "Please check the price of the Mac at Best Buy and Amazon, and let me know which website offers the lower price.",
    max_steps: int = 30,
    headless: bool = False,
    auto_confirm: bool = False,
    fast_mode: bool = False,
    include_thoughts: bool = True,
    max_observation_images: int = 2,
    observation_scale: float = 1.0,
) -> None:
    """Run a standalone browser-agent session and log streamed events.

    Args:
        task: Natural-language instruction for the browser agent.
        max_steps: Maximum number of observe/reason/act iterations.
        headless: Whether to launch the browser without a visible UI.
        auto_confirm: Whether safety prompts should be auto-approved.
        fast_mode: Whether browser settling should favor speed over completeness.
        include_thoughts: Whether model thought streaming should be enabled when
            supported by the selected model.
        max_observation_images: Number of screenshot-bearing observations to keep
            in model history.
        observation_scale: Scale factor applied to screenshots before sending
            them to the model.
    """
    async with BrowserAgent(
        auto_confirm=auto_confirm,
        headless=headless,
        fast_mode=fast_mode,
        include_thoughts=include_thoughts,
        max_observation_images=max_observation_images,
        observation_scale=observation_scale,
    ) as agent:
        async for event in agent.run(
            task,
            max_steps=max_steps,
            safety_prompt_handler=main_safety_prompt_handler,
        ):
            logger.info("%s: %s", event.eventType, event.payload)


async def run_desktop_agent(
    task: str = "Open a text editor, create a new file, and write 'Hello World' in it.",
    max_steps: int = 10,
    auto_confirm: bool = False,
    observation_delay_ms: int = 1500,
    include_thoughts: bool = True,
    max_observation_images: int = 2,
    observation_scale: float = 1.0,
) -> None:
    """Run a standalone desktop-agent session and log streamed events.

    Args:
        task: Natural-language instruction for the desktop agent.
        max_steps: Maximum number of observe/reason/act iterations.
        auto_confirm: Whether safety prompts should be auto-approved.
        observation_delay_ms: Delay before each desktop screenshot capture.
        include_thoughts: Whether model thought streaming should be enabled when
            supported by the selected model.
        max_observation_images: Number of screenshot-bearing observations to keep
            in model history.
        observation_scale: Scale factor applied to screenshots before sending
            them to the model.
    """
    async with DesktopAgent(
        auto_confirm=auto_confirm,
        observation_delay_ms=observation_delay_ms,
        include_thoughts=include_thoughts,
        max_observation_images=max_observation_images,
        observation_scale=observation_scale,
    ) as agent:
        async for event in agent.run(
            task,
            max_steps=max_steps,
            safety_prompt_handler=main_safety_prompt_handler,
        ):
            logger.info("%s: %s", event.eventType, event.payload)


def main() -> None:
    """Entrypoint for launching the standalone test runner."""
    if AGENT == "desktop":
        asyncio.run(
            run_desktop_agent(
                task=TASK,
                max_steps=MAX_STEPS,
                auto_confirm=AUTO_CONFIRM,
                observation_delay_ms=DESKTOP_OBSERVATION_DELAY_MS,
                include_thoughts=INCLUDE_THOUGHTS,
                max_observation_images=MAX_OBSERVATION_IMAGES,
                observation_scale=OBSERVATION_SCALE,
            )
        )
        return

    asyncio.run(
        run_browser_agent(
            task=TASK,
            max_steps=MAX_STEPS,
            headless=HEADLESS,
            auto_confirm=AUTO_CONFIRM,
            fast_mode=BROWSER_FAST_MODE,
            include_thoughts=INCLUDE_THOUGHTS,
            max_observation_images=MAX_OBSERVATION_IMAGES,
            observation_scale=OBSERVATION_SCALE,
        )
    )


if __name__ == "__main__":
    main()
