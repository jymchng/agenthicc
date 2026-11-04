"""Agent-facing tools for a session-scoped CloakBrowser manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lauren_ai._tools import tool

from agenthicc.tools.capabilities import (
    tool_network_read,
    tool_network_write,
    tool_read,
    tool_write,
)

if TYPE_CHECKING:
    from agenthicc.tools.base import ToolLike

    from .session import BrowserSessionManager

CLOAKBROWSER_AGENT_TOOLS: tuple[str, ...] = (
    "cloakbrowser_status",
    "cloakbrowser_open",
    "cloakbrowser_snapshot",
    "cloakbrowser_click",
    "cloakbrowser_fill",
    "cloakbrowser_press",
    "cloakbrowser_wait_for",
    "cloakbrowser_screenshot",
    "cloakbrowser_close",
)

__all__ = ["CLOAKBROWSER_AGENT_TOOLS", "make_cloakbrowser_tools"]


def make_cloakbrowser_tools(manager: "BrowserSessionManager") -> list["ToolLike"]:
    """Return closures bound to one browser manager, or no global tools."""
    if not manager.settings.enabled:
        return []

    @tool_read
    @tool(name="cloakbrowser_status")
    async def cloakbrowser_status() -> dict[str, object]:
        """Report optional browser readiness and bounded session counters."""
        return await manager.status()

    @tool_network_read
    @tool(name="cloakbrowser_open")
    async def cloakbrowser_open(url: str, operation_id: str = "") -> dict[str, object]:
        """Open an operator-allow-listed HTTP(S) URL in the session browser.

        Args:
            url: The HTTP(S) URL to open; its host must be on the allow-list.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.open(url, operation_id or None)

    @tool_network_read
    @tool(name="cloakbrowser_snapshot")
    async def cloakbrowser_snapshot(page_id: str, operation_id: str = "") -> dict[str, object]:
        """Return bounded page text, links, and accessible controls.

        Args:
            page_id: The ``page_id`` returned by cloakbrowser_open.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.snapshot(page_id, operation_id or None)

    @tool_network_write
    @tool(name="cloakbrowser_click")
    async def cloakbrowser_click(
        page_id: str, selector: str, operation_id: str = ""
    ) -> dict[str, object]:
        """Click one bounded selector in an open page.

        Args:
            page_id: The ``page_id`` returned by cloakbrowser_open.
            selector: CSS selector of the element to click.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.click(page_id, selector, operation_id or None)

    @tool_network_write
    @tool(name="cloakbrowser_fill")
    async def cloakbrowser_fill(
        page_id: str,
        selector: str,
        value: str,
        operation_id: str = "",
    ) -> dict[str, object]:
        """Fill a non-sensitive visible form field without echoing its value.

        Args:
            page_id: The ``page_id`` returned by cloakbrowser_open.
            selector: CSS selector of the field to fill.
            value: The text to enter; it is not echoed back.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.fill(page_id, selector, value, operation_id or None)

    @tool_network_write
    @tool(name="cloakbrowser_press")
    async def cloakbrowser_press(
        page_id: str,
        key: str,
        selector: str = "body",
        operation_id: str = "",
    ) -> dict[str, object]:
        """Press one allow-listed keyboard key in an open page.

        Args:
            page_id: The ``page_id`` returned by cloakbrowser_open.
            key: The key to press, e.g. ``Enter`` or ``Tab``.
            selector: CSS selector of the element to focus first (default ``body``).
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.press(page_id, key, selector, operation_id or None)

    @tool_network_read
    @tool(name="cloakbrowser_wait_for")
    async def cloakbrowser_wait_for(
        page_id: str,
        condition: str,
        value: str,
        operation_id: str = "",
    ) -> dict[str, object]:
        """Wait for a selector, text, approved URL, or load state.

        Args:
            page_id: The ``page_id`` returned by cloakbrowser_open.
            condition: One of ``selector``, ``text``, ``url`` or ``load_state``.
            value: The selector, text, URL or load state to wait for.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.wait_for(page_id, condition, value, operation_id or None)

    @tool_network_read
    @tool(name="cloakbrowser_screenshot")
    async def cloakbrowser_screenshot(
        page_id: str,
        image_type: str = "png",
        full_page: bool = False,
        operation_id: str = "",
    ) -> dict[str, object]:
        """Save a bounded screenshot below the workspace artifact directory.

        Args:
            page_id: The ``page_id`` returned by cloakbrowser_open.
            image_type: ``png`` or ``jpeg``.
            full_page: Capture the whole scrollable page, not only the viewport.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.screenshot(page_id, image_type, full_page, operation_id or None)

    @tool_write
    @tool(name="cloakbrowser_close")
    async def cloakbrowser_close(
        page_id: str = "",
        all_pages: bool = False,
        operation_id: str = "",
    ) -> dict[str, object]:
        """Close one page or all pages in this session browser context.

        Args:
            page_id: The page to close; ignored when ``all_pages`` is true.
            all_pages: Close every page in this browser context.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.close(page_id, all_pages, operation_id or None)

    return [
        cloakbrowser_status,
        cloakbrowser_open,
        cloakbrowser_snapshot,
        cloakbrowser_click,
        cloakbrowser_fill,
        cloakbrowser_press,
        cloakbrowser_wait_for,
        cloakbrowser_screenshot,
        cloakbrowser_close,
    ]
