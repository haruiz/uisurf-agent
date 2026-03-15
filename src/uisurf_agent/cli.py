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
) -> None:
    """Run the browser agent locally and print streamed events."""
    async with BrowserAgent(
        auto_confirm=auto_confirm,
        headless=headless,
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
) -> None:
    """Run the desktop agent locally and print streamed events."""
    async with DesktopAgent(auto_confirm=auto_confirm) as agent:
        async for event in agent.run(
            task,
            max_steps=max_steps,
            safety_prompt_handler=_safety_prompt_handler,
        ):
            logger.info("%s: %s", event.eventType, event.payload)


def _run_a2a_server(agent: AgentName, host: str, port: int | None) -> None:
    """Start the requested agent in A2A server mode."""
    if agent is AgentName.browser_agent:
        start_browser_a2a_server(host=host, port=port or 8001)
        return

    start_desktop_a2a_server(host=host, port=port or 8002)


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
) -> None:
    """Run an agent interactively or expose it through a server protocol."""
    if mode is RunMode.a2a:
        _run_a2a_server(agent=agent, host=host, port=port)
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
        )
    )


def main() -> None:
    """Execute the Typer app."""
    app()


if __name__ == "__main__":
    main()
