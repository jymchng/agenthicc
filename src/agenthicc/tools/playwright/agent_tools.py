"""Agent-facing tools for a session-scoped Playwright manager."""

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

    from agenthicc.tools.cloakbrowser.session import BrowserSessionManager

PLAYWRIGHT_AGENT_TOOLS: tuple[str, ...] = (
    "playwright_status",
    "playwright_open",
    "playwright_snapshot",
    "playwright_click",
    "playwright_fill",
    "playwright_press",
    "playwright_wait_for",
    "playwright_screenshot",
    "playwright_close",
)

__all__ = ["PLAYWRIGHT_AGENT_TOOLS", "make_playwright_tools"]


def make_playwright_tools(manager: "BrowserSessionManager") -> list["ToolLike"]:
    """Return closures bound to one Playwright manager."""
    if not manager.settings.enabled:
        return []

    @tool_read
    @tool(name="playwright_status")
    async def playwright_status() -> dict[str, object]:
        """Report Playwright readiness and bounded session counters."""
        return await manager.status()

    @tool_network_read
    @tool(name="playwright_open")
    async def playwright_open(url: str, operation_id: str = "") -> dict[str, object]:
        """Open an operator-allow-listed HTTP(S) URL in the Playwright browser.

        Args:
            url: The HTTP(S) URL to open; its host must be on the allow-list.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.open(url, operation_id or None)

    @tool_network_read
    @tool(name="playwright_snapshot")
    async def playwright_snapshot(page_id: str, operation_id: str = "") -> dict[str, object]:
        """Return bounded page text, links, and accessible controls.

        Args:
            page_id: The ``page_id`` returned by playwright_open.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.snapshot(page_id, operation_id or None)

    @tool_network_write
    @tool(name="playwright_click")
    async def playwright_click(
        page_id: str, selector: str, operation_id: str = ""
    ) -> dict[str, object]:
        """Click one bounded selector in an open page.

        Args:
            page_id: The ``page_id`` returned by playwright_open.
            selector: CSS selector of the element to click.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.click(page_id, selector, operation_id or None)

    @tool_network_write
    @tool(name="playwright_fill")
    async def playwright_fill(
        page_id: str,
        selector: str,
        value: str,
        operation_id: str = "",
    ) -> dict[str, object]:
        """Fill a non-sensitive visible form field without echoing its value.

        Args:
            page_id: The ``page_id`` returned by playwright_open.
            selector: CSS selector of the field to fill.
            value: The text to enter; it is not echoed back.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.fill(page_id, selector, value, operation_id or None)

    @tool_network_write
    @tool(name="playwright_press")
    async def playwright_press(
        page_id: str,
        key: str,
        selector: str = "body",
        operation_id: str = "",
    ) -> dict[str, object]:
        """Press one allow-listed keyboard key in an open page.

        Args:
            page_id: The ``page_id`` returned by playwright_open.
            key: The key to press, e.g. ``Enter`` or ``Tab``.
            selector: CSS selector of the element to focus first (default ``body``).
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.press(page_id, key, selector, operation_id or None)

    @tool_network_read
    @tool(name="playwright_wait_for")
    async def playwright_wait_for(
        page_id: str,
        condition: str,
        value: str,
        operation_id: str = "",
    ) -> dict[str, object]:
        """Wait for a selector, text, approved URL, or load state.

        Args:
            page_id: The ``page_id`` returned by playwright_open.
            condition: One of ``selector``, ``text``, ``url`` or ``load_state``.
            value: The selector, text, URL or load state to wait for.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.wait_for(page_id, condition, value, operation_id or None)

    @tool_network_read
    @tool(name="playwright_screenshot")
    async def playwright_screenshot(
        page_id: str,
        image_type: str = "png",
        full_page: bool = False,
        operation_id: str = "",
    ) -> dict[str, object]:
        """Save a bounded screenshot below the workspace artifact directory.

        Args:
            page_id: The ``page_id`` returned by playwright_open.
            image_type: ``png`` or ``jpeg``.
            full_page: Capture the whole scrollable page, not only the viewport.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.screenshot(page_id, image_type, full_page, operation_id or None)

    @tool_write
    @tool(name="playwright_close")
    async def playwright_close(
        page_id: str = "",
        all_pages: bool = False,
        operation_id: str = "",
    ) -> dict[str, object]:
        """Close one page or all pages in this Playwright browser context.

        Args:
            page_id: The page to close; ignored when ``all_pages`` is true.
            all_pages: Close every page in this browser context.
            operation_id: Optional idempotency key; a repeated ID returns the first
                call's result instead of acting twice.
        """
        return await manager.close(page_id, all_pages, operation_id or None)

    return [
        playwright_status,
        playwright_open,
        playwright_snapshot,
        playwright_click,
        playwright_fill,
        playwright_press,
        playwright_wait_for,
        playwright_screenshot,
        playwright_close,
    ]
