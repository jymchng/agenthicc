"""Optional CloakBrowser integration.

Importing this module never imports ``cloakbrowser`` or Playwright.  The
optional dependency is loaded only when an enabled session performs a local
or CDP operation.
"""

from .agent_tools import CLOAKBROWSER_AGENT_TOOLS, make_cloakbrowser_tools
from .artifacts import BrowserArtifact, BrowserArtifactStore
from .client import (
    BrowserClient,
    BrowserHealth,
    CdpCloakBrowserClient,
    LocalCloakBrowserClient,
    PageSnapshot,
    PageState,
    ScreenshotData,
    UnavailableBrowserClient,
)
from .errors import BrowserErrorKind, BrowserToolError
from .policy import BrowserPolicy, redact_link, redact_url
from .session import (
    CLOAKBROWSER_TOOL_NAMES,
    BrowserSessionManager,
    create_browser_session,
    is_browser_tool,
    is_cloakbrowser_tool,
)

__all__ = [
    "BrowserArtifact",
    "BrowserArtifactStore",
    "BrowserClient",
    "BrowserErrorKind",
    "BrowserHealth",
    "BrowserPolicy",
    "BrowserSessionManager",
    "BrowserToolError",
    "CLOAKBROWSER_AGENT_TOOLS",
    "CLOAKBROWSER_TOOL_NAMES",
    "CdpCloakBrowserClient",
    "LocalCloakBrowserClient",
    "PageSnapshot",
    "PageState",
    "ScreenshotData",
    "UnavailableBrowserClient",
    "create_browser_session",
    "is_browser_tool",
    "is_cloakbrowser_tool",
    "make_cloakbrowser_tools",
    "redact_url",
    "redact_link",
]
