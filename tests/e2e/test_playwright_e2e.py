"""Deterministic E2E journey through the Playwright-facing tool surface."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agenthicc.config import PlaywrightSettings
from agenthicc.tools.cloakbrowser import BrowserPolicy, BrowserSessionManager
from agenthicc.tools.playwright import make_playwright_tools
from tests.unit.test_cloakbrowser_tools import FakeBrowserClient

pytestmark = pytest.mark.e2e


@pytest.mark.playwright
@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="install the optional playwright extra for the upstream API contract check",
)
def test_optional_playwright_exports_are_available() -> None:
    from playwright.async_api import async_playwright

    assert callable(async_playwright)


@pytest.mark.asyncio
async def test_playwright_agent_journey_open_observe_interact_capture_close(
    tmp_path: Path,
) -> None:
    async def resolve(_host: str) -> list[str]:
        return ["93.184.216.34"]

    manager = BrowserSessionManager(
        PlaywrightSettings(enabled=True, allowed_domains=["example.com"]),
        "conversation-e2e",
        tmp_path,
        client=FakeBrowserClient(),
        policy=BrowserPolicy(("example.com",), resolver=resolve),
        backend_name="Playwright",
    )
    tools = {tool.__name__: tool for tool in make_playwright_tools(manager)}

    opened = await tools["playwright_open"]("https://example.com/")
    assert opened["ok"] is True
    page_id = str(opened["page"]["page_id"])
    assert (await tools["playwright_snapshot"](page_id))["ok"] is True
    assert (await tools["playwright_click"](page_id, "button.submit"))["ok"] is True
    screenshot = await tools["playwright_screenshot"](page_id)
    assert screenshot["ok"] is True
    assert str(screenshot["artifact"]["path"]).startswith(".agenthicc/browser-artifacts/")
    assert (await tools["playwright_close"](page_id))["ok"] is True
    await manager.close_session()
