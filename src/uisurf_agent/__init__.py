import logging

from rich.logging import RichHandler

from .agents.browser_agent import BrowserAgent
from .agents.desktop_agent import DesktopAgent


logger = logging.getLogger("uisurf_agent")
logger.setLevel(logging.INFO)
if not logger.handlers:
    rich_handler = RichHandler(show_path=False, markup=True)
    rich_handler.setLevel(logging.INFO)
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(rich_handler)
    logger.propagate = False

__all__ = ["BrowserAgent", "DesktopAgent", "logger"]
