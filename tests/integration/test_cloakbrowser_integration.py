"""Integration coverage for browser tools crossing session/workflow boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.config import AgenthiccConfig, CloakBrowserSettings
from agenthicc.tools.cloakbrowser import (
    BrowserSessionManager,
    BrowserPolicy,
    make_cloakbrowser_tools,
)
from agenthicc.tools.cloakbrowser.client import BrowserHealth, PageState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner

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


def test_workflow_config_exposes_session_browser_tools_once(tmp_path: Path) -> None:
    settings = CloakBrowserSettings(enabled=True, allowed_domains=["example.com"])
    manager = BrowserSessionManager(
        settings,
        "conversation-1",
        tmp_path,
        policy=BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
    )
    browser_tools = make_cloakbrowser_tools(manager)
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
    assert names.count("cloakbrowser_open") == 1


def test_cloakbrowser_tools_reopen_after_session_cleanup(tmp_path: Path) -> None:
    async def check() -> None:
        manager = BrowserSessionManager(
            CloakBrowserSettings(enabled=True, allowed_domains=["example.com"]),
            "conversation-1",
            tmp_path,
            client=_LifecycleClient(),
            policy=BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
        )
        tools = {tool.__name__: tool for tool in make_cloakbrowser_tools(manager)}

        assert (await tools["cloakbrowser_open"]("https://example.com/"))["ok"] is True
        await manager.close_session()
        assert (await tools["cloakbrowser_open"]("https://example.com/"))["ok"] is True

    asyncio.run(check())


async def _addresses() -> list[str]:
    return ["93.184.216.34"]


def test_create_workflow_authoring_surface_documents_browser_tools() -> None:
    names = {tool.__name__ for tool in make_inspection_tools()}
    assert "describe_cloakbrowser_tools" in names


def test_create_workflow_runner_excludes_browser_tools_from_base_tools() -> None:
    manager = BrowserSessionManager(
        CloakBrowserSettings(enabled=True, allowed_domains=["example.com"]),
        "conversation-1",
        Path.cwd(),
        policy=BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
    )
    browser_tools = make_cloakbrowser_tools(manager)
    app_state = SimpleNamespace(
        active_mode=lambda: SimpleNamespace(blocked_capabilities=frozenset())
    )
    config = SimpleNamespace(
        app_state=app_state,
        all_plugin_tools=lambda: browser_tools,
        mcp_registry=None,
        memory_router=None,
        semantic_index=None,
    )
    runner = object.__new__(CreateWorkflowRunner)
    runner._cfg = config
    names = {tool.__name__ for tool in runner._base_tools()}
    assert not any(name.startswith("cloakbrowser_") for name in names)
