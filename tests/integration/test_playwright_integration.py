"""Integration coverage for Playwright backend selection and workflow wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agenthicc.config import AgenthiccConfig, PlaywrightSettings
from agenthicc.tools.cloakbrowser import BrowserPolicy, BrowserSessionManager
from agenthicc.tools.cloakbrowser.client import BrowserHealth, PageState
from agenthicc.tools.playwright import create_playwright_session, make_playwright_tools
from agenthicc.workflows.config import WorkflowConfig

pytestmark = pytest.mark.integration


class _LifecycleClient:
    async def health(self) -> BrowserHealth:
        return BrowserHealth("ready", "local", "")

    async def open_page(self, session_id: str, url: str) -> PageState:
        return PageState("page-1", url, "Fixture")

    async def close_page(self, session_id: str, page_id: str) -> None:
        return None

    async def close_session(self, session_id: str) -> None:
        return None


def test_selected_playwright_backend_exposes_one_session_tool_set(tmp_path: Path) -> None:
    manager = create_playwright_session(
        PlaywrightSettings(enabled=True, allowed_domains=["example.com"]),
        "conversation-1",
        tmp_path,
    )
    # Replace the optional live transport at the integration boundary; the
    # workflow wiring and shared policy remain real.
    manager.client = _ReadyClient()
    manager.policy = BrowserPolicy(("example.com",), resolver=lambda _host: _addresses())
    browser_tools = make_playwright_tools(manager)
    config = WorkflowConfig(
        conv_store=object(),
        app_state=object(),
        processor=object(),
        agent_runner=object(),
        approval_svc=None,
        cfg=AgenthiccConfig(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=object(),
        agents_registry=object(),
        browser_tools=browser_tools,
    )

    names = [tool.__name__ for tool in config.all_plugin_tools()]

    assert names == [
        "playwright_status",
        "playwright_open",
        "playwright_snapshot",
        "playwright_click",
        "playwright_fill",
        "playwright_press",
        "playwright_wait_for",
        "playwright_screenshot",
        "playwright_close",
    ]


def test_playwright_tools_reopen_after_session_cleanup(tmp_path: Path) -> None:
    async def check() -> None:
        manager = BrowserSessionManager(
            PlaywrightSettings(enabled=True, allowed_domains=["example.com"]),
            "conversation-1",
            tmp_path,
            client=_LifecycleClient(),
            policy=BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
            backend_name="Playwright",
        )
        tools = {tool.__name__: tool for tool in make_playwright_tools(manager)}

        assert (await tools["playwright_open"]("https://example.com/"))["ok"] is True
        await manager.close_session()
        assert (await tools["playwright_open"]("https://example.com/"))["ok"] is True

    asyncio.run(check())


class _ReadyClient:
    async def health(self):
        from agenthicc.tools.cloakbrowser.client import BrowserHealth

        return BrowserHealth("ready", "local", "")


async def _addresses() -> list[str]:
    return ["93.184.216.34"]
