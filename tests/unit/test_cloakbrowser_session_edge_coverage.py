"""Coverage for browser-session lifecycle and structured error handling."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from agenthicc.config import CloakBrowserSettings
from agenthicc.tools.cloakbrowser import (
    BrowserErrorKind,
    BrowserSessionManager,
    BrowserToolError,
    PageState,
    ScreenshotData,
    is_browser_tool,
    is_cloakbrowser_tool,
)
from agenthicc.tools.cloakbrowser.client import BrowserHealth

from .test_cloakbrowser_tools import FakeBrowserClient, _manager, _policy

pytestmark = pytest.mark.unit


class _ErrorClient(FakeBrowserClient):
    def __init__(self, status: str = "ready") -> None:
        super().__init__()
        self.status = status

    async def health(self) -> BrowserHealth:
        return BrowserHealth(self.status, "local", "unavailable")


class _FailingClient(FakeBrowserClient):
    async def open_page(self, session_id: str, url: str) -> PageState:
        raise RuntimeError("secret Playwright navigation details")


def test_browser_tool_classification_and_invalid_policy_status(tmp_path: Path) -> None:
    def cloakbrowser_open() -> None:
        pass

    def playwright_open() -> None:
        pass

    assert is_cloakbrowser_tool(cloakbrowser_open) is True
    assert is_browser_tool(cloakbrowser_open) is True
    assert is_browser_tool(playwright_open) is True
    assert is_browser_tool(lambda: None) is False

    manager = BrowserSessionManager(
        CloakBrowserSettings(enabled=True, allowed_domains=["*"]),
        "conversation-1",
        tmp_path,
        client=FakeBrowserClient(),
    )
    assert manager.enabled is False
    status = asyncio.run(manager.status())
    assert status["status"] == BrowserErrorKind.POLICY_DENIED.value
    opened = asyncio.run(manager.open("https://example.com"))
    assert opened["error_kind"] == BrowserErrorKind.POLICY_DENIED.value


async def test_browser_session_safe_errors_status_and_page_lifecycle(tmp_path: Path) -> None:
    manager, client = _manager(tmp_path)
    assert (await manager.status())["ok"] is True
    opened = await manager.open("https://example.com/")
    assert opened["ok"] is True
    page_id = str(opened["page"]["page_id"])

    pressed = await manager.press(page_id, "Enter")
    waited = await manager.wait_for(page_id, "load_state", "networkidle")
    assert pressed["ok"] is True and waited["ok"] is True
    screenshot = await manager.screenshot(page_id)
    assert screenshot["ok"] is True
    bad_format = await manager.screenshot(page_id, image_type="gif")
    assert bad_format["error_kind"] == BrowserErrorKind.INVALID_INPUT.value
    closed = await manager.close(page_id)
    assert closed["closed_page"] == page_id
    missing = await manager.close(page_id)
    assert missing["error_kind"] == BrowserErrorKind.NOT_FOUND.value

    opened_again = await manager.open("https://example.com/")
    assert opened_again["ok"] is True
    await manager.open("https://example.com/other")
    all_closed = await manager.close(all_pages=True)
    assert all_closed["closed_pages"] == "all"
    assert any(name == "close" for name, _args in client.calls)

    invalid = await manager._safe(
        lambda: _raise(BrowserToolError(BrowserErrorKind.INVALID_INPUT, "bad"))
    )
    assert invalid["error_kind"] == BrowserErrorKind.INVALID_INPUT.value
    timeout = await manager._safe(lambda: _raise(TimeoutError()))
    assert timeout["error_kind"] == BrowserErrorKind.TIMEOUT.value
    network = await manager._safe(lambda: _raise(OSError()))
    assert network["error_kind"] == BrowserErrorKind.NETWORK.value
    generic = await manager._safe(lambda: _raise(RuntimeError("secret details")))
    assert generic["error_kind"] == BrowserErrorKind.EXECUTION.value

    await manager.close_session()
    await manager.close_session()
    assert (await manager.status())["status"] == BrowserErrorKind.CLOSED.value
    assert client.calls[-1][0] == "close_session"


async def test_unexpected_browser_error_is_structured_without_traceback_leak(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = BrowserSessionManager(
        CloakBrowserSettings(enabled=True, allowed_domains=["example.com"]),
        "conversation-1",
        tmp_path,
        client=_FailingClient(),
        policy=_policy(),
        backend_name="Playwright",
    )

    with caplog.at_level(logging.DEBUG, logger="agenthicc.tools.cloakbrowser.session"):
        result = await manager.open("https://example.com/")

    assert result == {
        "ok": False,
        "error_kind": BrowserErrorKind.EXECUTION.value,
        "error": "Browser operation failed.",
        "operation_id": result["operation_id"],
    }
    assert "RuntimeError" in caplog.text
    assert "secret Playwright navigation details" not in caplog.text
    assert "Traceback" not in caplog.text


async def _raise(exc: BaseException) -> dict[str, object]:
    raise exc


async def test_browser_session_health_mapping_restore_and_mutating_call(tmp_path: Path) -> None:
    for status in ("binary_missing", "unhealthy", "unknown-status"):
        client = _ErrorClient(status)
        manager = BrowserSessionManager(
            CloakBrowserSettings(enabled=True, allowed_domains=["example.com"]),
            "conversation-1",
            tmp_path,
            client=client,
            policy=_policy(),
        )
        with pytest.raises(BrowserToolError) as error:
            await manager._ensure_ready()
        assert error.value.kind is BrowserErrorKind.BROWSER_UNAVAILABLE

    manager, _client = _manager(tmp_path)
    opened = await manager.open("https://example.com/")
    page_id = str(opened["page"]["page_id"])
    called: list[bool] = []

    async def mutate() -> PageState:
        called.append(True)
        return PageState(page_id, "https://example.com/changed", "Changed")

    result = await manager._mutating_page_call(page_id, mutate)
    assert result["ok"] is True and called
    checkpoint = manager.checkpoint_payload()
    checkpoint["completed_operation_ids"] = ["ok-id", "bad id", 7]
    manager.restore_checkpoint(checkpoint)
    stale = await manager.open("https://example.com/", operation_id="ok-id")
    assert stale["error_kind"] == BrowserErrorKind.STALE_PAGE.value
    with pytest.raises(ValueError, match="different session"):
        manager.restore_checkpoint({"conversation_id": "other"})
    with pytest.raises(ValueError, match="not compatible"):
        manager.restore_checkpoint({"session_id": "not-ours"})


async def test_browser_session_limits_screenshot_and_cancelled_cleanup(tmp_path: Path) -> None:
    class LargeScreenshotClient(FakeBrowserClient):
        async def screenshot(
            self, session_id: str, page_id: str, image_type: str, full_page: bool
        ) -> ScreenshotData:
            return ScreenshotData(b"x" * 1025, "image/png")

    settings = CloakBrowserSettings(
        enabled=True,
        allowed_domains=["example.com"],
        max_pages=1,
        max_screenshot_bytes=1024,
    )
    manager = BrowserSessionManager(
        settings, "conversation-1", tmp_path, client=LargeScreenshotClient(), policy=_policy()
    )
    first = await manager.open("https://example.com/")
    assert first["ok"] is True
    second = await manager.open("https://example.com/other")
    assert second["error_kind"] == BrowserErrorKind.LIMIT_EXCEEDED.value
    too_large = await manager.screenshot("page-1")
    assert too_large["error_kind"] == BrowserErrorKind.OUTPUT_LIMIT.value

    client = FakeBrowserClient()
    manager = BrowserSessionManager(
        CloakBrowserSettings(enabled=True, allowed_domains=["example.com"]),
        "conversation-1",
        tmp_path,
        client=client,
        policy=_policy(),
    )

    async def cancelled() -> dict[str, object]:
        raise asyncio.CancelledError

    cancelled_result = await manager._safe(cancelled)
    assert cancelled_result["error_kind"] == BrowserErrorKind.CANCELLED.value
    assert client.calls[-1][0] == "close_session"
    assert manager._pages == {}
