"""Integration coverage for Playwright backend selection and workflow wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.config import AgenthiccConfig, PlaywrightSettings
from agenthicc.tools.cloakbrowser import BrowserPolicy
from agenthicc.tools.playwright import create_playwright_session, make_playwright_tools
from agenthicc.workflows.config import WorkflowConfig

pytestmark = pytest.mark.integration


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


class _ReadyClient:
    async def health(self):
        from agenthicc.tools.cloakbrowser.client import BrowserHealth

        return BrowserHealth("ready", "local", "")


async def _addresses() -> list[str]:
    return ["93.184.216.34"]
