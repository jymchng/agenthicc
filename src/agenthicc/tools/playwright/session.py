"""Session construction for the optional Playwright browser backend."""

from __future__ import annotations

from pathlib import Path

from agenthicc.config import PlaywrightSettings
from agenthicc.tools.cloakbrowser.client import BrowserClient
from agenthicc.tools.cloakbrowser.policy import BrowserPolicy
from agenthicc.tools.cloakbrowser.session import (
    BrowserSessionManager,
    UnavailableBrowserClient,
)
from agenthicc.tools.cloakbrowser.errors import BrowserErrorKind

from .client import PlaywrightBrowserClient

__all__ = [
    "PLAYWRIGHT_TOOL_NAMES",
    "create_playwright_session",
    "is_playwright_tool",
]

PLAYWRIGHT_TOOL_NAMES = frozenset(
    {
        "playwright_status",
        "playwright_open",
        "playwright_snapshot",
        "playwright_click",
        "playwright_fill",
        "playwright_press",
        "playwright_wait_for",
        "playwright_screenshot",
        "playwright_close",
    }
)


def is_playwright_tool(tool: object) -> bool:
    """Return whether a callable is one of the built-in Playwright tools."""
    return str(getattr(tool, "__name__", "")) in PLAYWRIGHT_TOOL_NAMES


def create_playwright_session(
    settings: PlaywrightSettings,
    conversation_id: str,
    workspace_root: Path,
) -> BrowserSessionManager:
    """Construct a Playwright-backed manager without importing Playwright."""
    try:
        policy = BrowserPolicy(
            tuple(settings.allowed_domains),
            allow_all_domains=settings.allow_all_domains,
        )
    except ValueError:
        # Match CloakBrowser's fail-closed startup behaviour: an invalid
        # operator allow-list makes this backend unavailable rather than
        # preventing the whole agent session from starting.
        return BrowserSessionManager(
            settings,
            conversation_id,
            workspace_root,
            client=UnavailableBrowserClient(
                "local",
                BrowserErrorKind.POLICY_DENIED,
                "Playwright browser policy configuration is invalid.",
            ),
            backend_name="Playwright",
        )
    client: BrowserClient
    if settings.enabled:
        client = PlaywrightBrowserClient(
            settings,
            policy,
            workspace_root,
        )
    else:
        client = UnavailableBrowserClient(
            "local",
            BrowserErrorKind.DISABLED,
            "Playwright is disabled; enable the selected browser backend.",
        )
    return BrowserSessionManager(
        settings,
        conversation_id,
        workspace_root,
        client=client,
        policy=policy,
        backend_name="Playwright",
    )
