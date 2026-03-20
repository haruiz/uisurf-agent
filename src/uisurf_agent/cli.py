from __future__ import annotations

"""Command-line interface for launching UI surf agents."""

import asyncio
import logging
from enum import Enum
from typing import Annotated

import typer

from uisurf_agent.a2a.browser_a2a import start_a2a_server as start_browser_a2a_server
from uisurf_agent.a2a.desktop_a2a import start_a2a_server as start_desktop_a2a_server
from uisurf_agent.agents import BrowserAgent, DesktopAgent
from uisurf_agent.agents.ui_agent import SafetyPromptDecision
from uisurf_agent.utils.config_utils import resolve_bool_config, resolve_int_config
from uisurf_agent.utils.screenshot_utils import resolve_observation_scale


logger = logging.getLogger("uisurf_agent.cli")
app = typer.Typer(
    name="uisurf_agent",
    help="Run UI surf agents interactively or expose them as servers.",
    no_args_is_help=True,
)


class AgentName(str, Enum):
    browser_agent = "browser_agent"
    desktop_agent = "desktop_agent"


class RunMode(str, Enum):
    interactive = "interactive"
    a2a = "a2a"
    mcp = "mcp"


BROWSER_DEFAULT_TASK = (
    "Please check the price of the Mac at Best Buy and Amazon, and let me know "
    "which website offers the lower price."
)
DESKTOP_DEFAULT_TASK = "Open a text editor, create a new file, and write 'Hello World' in it."


@app.callback()
def callback() -> None:
    """CLI entrypoint for uisurf-agent commands."""
    return


async def _safety_prompt_handler(function_call, auto_confirm: bool) -> SafetyPromptDecision:
    """Resolve safety confirmation requests for interactive CLI runs."""
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


async def _run_browser_agent_interactive(
    task: str,
    max_steps: int,
    headless: bool,
    auto_confirm: bool,
    fast_mode: bool,
    include_thoughts: bool,
    max_observation_images: int,
    observation_scale: float,
) -> None:
    """Run the browser agent locally and print streamed events."""
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
            safety_prompt_handler=_safety_prompt_handler,
        ):
            logger.info("%s: %s", event.eventType, event.payload)


async def _run_desktop_agent_interactive(
    task: str,
    max_steps: int,
    auto_confirm: bool,
    observation_delay_ms: int,
    include_thoughts: bool,
    max_observation_images: int,
    observation_scale: float,
) -> None:
    """Run the desktop agent locally and print streamed events."""
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
            safety_prompt_handler=_safety_prompt_handler,
        ):
            logger.info("%s: %s", event.eventType, event.payload)


def _resolve_cli_observation_scale(
    agent: AgentName,
    observation_scale: float | None,
) -> float:
    """Resolve screenshot scale for the selected CLI agent."""
    env_var = (
        "BROWSER_OBSERVATION_SCALE"
        if agent is AgentName.browser_agent
        else "DESKTOP_OBSERVATION_SCALE"
    )
    return resolve_observation_scale(observation_scale, env_var)


def _resolve_cli_fast_mode(
    agent: AgentName,
    fast_mode: bool | None,
) -> bool:
    """Resolve browser fast mode for the selected CLI agent."""
    if agent is not AgentName.browser_agent:
        return False
    return resolve_bool_config(fast_mode, "BROWSER_FAST_MODE", default=False)


def _resolve_cli_include_thoughts(
    agent: AgentName,
    include_thoughts: bool | None,
) -> bool:
    """Resolve whether model thought streaming should be enabled."""
    env_var = (
        "BROWSER_INCLUDE_THOUGHTS"
        if agent is AgentName.browser_agent
        else "DESKTOP_INCLUDE_THOUGHTS"
    )
    return resolve_bool_config(
        include_thoughts,
        env_var,
        default=resolve_bool_config(None, "INCLUDE_THOUGHTS", default=True),
    )


def _resolve_cli_observation_delay_ms(
    agent: AgentName,
    observation_delay_ms: int | None,
) -> int:
    """Resolve desktop observation delay for the selected CLI agent."""
    if agent is not AgentName.desktop_agent:
        return 0
    return resolve_int_config(
        observation_delay_ms,
        "DESKTOP_OBSERVATION_DELAY_MS",
        default=1500,
        minimum=0,
    )


def _resolve_cli_max_observation_images(
    max_observation_images: int | None,
) -> int:
    """Resolve how many screenshot observations keep image payloads."""
    return resolve_int_config(
        max_observation_images,
        "MAX_OBSERVATION_IMAGES",
        default=2,
        minimum=1,
    )


def _run_a2a_server(
    agent: AgentName,
    host: str,
    port: int | None,
    fast_mode: bool,
    observation_delay_ms: int,
    include_thoughts: bool,
    max_observation_images: int,
    observation_scale: float,
) -> None:
    """Start the requested agent in A2A server mode."""
    if agent is AgentName.browser_agent:
        start_browser_a2a_server(
            host=host,
            port=port or 8001,
            fast_mode=fast_mode,
            include_thoughts=include_thoughts,
            max_observation_images=max_observation_images,
            observation_scale=observation_scale,
        )
        return

    start_desktop_a2a_server(
        host=host,
        port=port or 8002,
        observation_delay_ms=observation_delay_ms,
        include_thoughts=include_thoughts,
        max_observation_images=max_observation_images,
        observation_scale=observation_scale,
    )


@app.command()
def run(
    agent: Annotated[
        AgentName,
        typer.Argument(help="Agent implementation to launch."),
    ],
    mode: Annotated[
        RunMode,
        typer.Option(help="Execution mode."),
    ] = RunMode.interactive,
    host: Annotated[
        str,
        typer.Option(help="Host interface for server-based modes."),
    ] = "127.0.0.1",
    port: Annotated[
        int | None,
        typer.Option(help="Port for server-based modes. Defaults depend on the selected agent."),
    ] = None,
    task: Annotated[
        str,
        typer.Option(help="Task for interactive runs."),
    ] = BROWSER_DEFAULT_TASK,
    max_steps: Annotated[
        int,
        typer.Option("--max-steps", help="Maximum observe/reason/act iterations for interactive runs."),
    ] = 30,
    headless: Annotated[
        bool,
        typer.Option(help="Run browser-agent interactive mode without a visible browser window."),
    ] = False,
    auto_confirm: Annotated[
        bool,
        typer.Option("--auto-confirm", help="Automatically approve safety-gated actions in interactive mode."),
    ] = False,
    fast_mode: Annotated[
        bool | None,
        typer.Option(
            "--fast-mode/--no-fast-mode",
            help="Use faster but less conservative browser settling. Browser agent only.",
        ),
    ] = None,
    include_thoughts: Annotated[
        bool | None,
        typer.Option(
            "--include-thoughts/--no-include-thoughts",
            help="Enable or disable model thought streaming when supported.",
        ),
    ] = None,
    desktop_observation_delay_ms: Annotated[
        int | None,
        typer.Option(
            "--desktop-observation-delay-ms",
            help="Delay before each desktop screenshot in milliseconds. Desktop agent only.",
        ),
    ] = None,
    max_observation_images: Annotated[
        int | None,
        typer.Option(
            "--max-observation-images",
            help="Keep only this many screenshot observations with image payloads in model history.",
        ),
    ] = None,
    observation_scale: Annotated[
        float | None,
        typer.Option(
            "--observation-scale",
            help="Scale screenshots before sending them to the model. Use 1.0 for full resolution.",
        ),
    ] = None,
) -> None:
    """Run an agent interactively or expose it through a server protocol."""
    try:
        resolved_observation_scale = _resolve_cli_observation_scale(
            agent=agent,
            observation_scale=observation_scale,
        )
        resolved_fast_mode = _resolve_cli_fast_mode(
            agent=agent,
            fast_mode=fast_mode,
        )
        resolved_include_thoughts = _resolve_cli_include_thoughts(
            agent=agent,
            include_thoughts=include_thoughts,
        )
        resolved_desktop_observation_delay_ms = _resolve_cli_observation_delay_ms(
            agent=agent,
            observation_delay_ms=desktop_observation_delay_ms,
        )
        resolved_max_observation_images = _resolve_cli_max_observation_images(
            max_observation_images=max_observation_images,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if mode is RunMode.a2a:
        _run_a2a_server(
            agent=agent,
            host=host,
            port=port,
            fast_mode=resolved_fast_mode,
            observation_delay_ms=resolved_desktop_observation_delay_ms,
            include_thoughts=resolved_include_thoughts,
            max_observation_images=resolved_max_observation_images,
            observation_scale=resolved_observation_scale,
        )
        return

    if mode is RunMode.mcp:
        raise typer.BadParameter(
            "MCP mode is not implemented yet. Use --mode a2a or --mode interactive.",
            param_hint="mode",
        )

    if agent is AgentName.browser_agent:
        asyncio.run(
            _run_browser_agent_interactive(
                task=task,
                max_steps=max_steps,
                headless=headless,
                auto_confirm=auto_confirm,
                fast_mode=resolved_fast_mode,
                include_thoughts=resolved_include_thoughts,
                max_observation_images=resolved_max_observation_images,
                observation_scale=resolved_observation_scale,
            )
        )
        return

    if task == BROWSER_DEFAULT_TASK:
        task = DESKTOP_DEFAULT_TASK
    asyncio.run(
        _run_desktop_agent_interactive(
            task=task,
            max_steps=max_steps,
            auto_confirm=auto_confirm,
            observation_delay_ms=resolved_desktop_observation_delay_ms,
            include_thoughts=resolved_include_thoughts,
            max_observation_images=resolved_max_observation_images,
            observation_scale=resolved_observation_scale,
        )
    )


def main() -> None:
    """Execute the Typer app."""
    app()


if __name__ == "__main__":
    main()
