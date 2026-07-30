"""Integration coverage for browser tools crossing session/workflow boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.config import AgenthiccConfig, CloakBrowserSettings
from agenthicc.tools.cloakbrowser import (
    BrowserSessionManager,
    BrowserPolicy,
    make_cloakbrowser_tools,
)
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner

pytestmark = pytest.mark.integration


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
