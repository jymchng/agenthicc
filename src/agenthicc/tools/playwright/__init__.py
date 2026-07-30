"""Optional Microsoft Playwright browser integration.

Importing this package never imports Playwright or launches a browser.  The
optional dependency is loaded only after the Playwright backend is selected and
its manager is used.
"""

from .agent_tools import PLAYWRIGHT_AGENT_TOOLS, make_playwright_tools
from .client import PlaywrightBrowserClient
from .session import PLAYWRIGHT_TOOL_NAMES, create_playwright_session, is_playwright_tool

__all__ = [
    "PLAYWRIGHT_AGENT_TOOLS",
    "PLAYWRIGHT_TOOL_NAMES",
    "PlaywrightBrowserClient",
    "create_playwright_session",
    "is_playwright_tool",
    "make_playwright_tools",
]
