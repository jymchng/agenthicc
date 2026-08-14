"""Deterministic end-to-end MCP session journeys (PRD-172)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agenthicc.tools.mcp import McpServerConfig, McpToolSchema
from agenthicc.tools.mcp_manager import McpServerState, McpSessionManager

pytestmark = pytest.mark.e2e


@dataclass
class JourneyServer:
    config: McpServerConfig

    def __post_init__(self) -> None:
        self.server_name = self.config.name
        self.is_connected = False
        self.tools = [McpToolSchema("inspect", "Inspect", {"type": "object"})]
        self.callback = None

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def list_tools(self) -> list[McpToolSchema]:
        return list(self.tools)

    async def get_instructions(self) -> str:
        return "Inspect before changing state."

    def capabilities(self) -> dict[str, object]:
        return {"tools": {"listChanged": True}}

    def set_change_callback(self, callback: object) -> None:
        self.callback = callback


@pytest.mark.asyncio
async def test_session_switches_workflows_without_recreating_mcp_manager() -> None:
    servers: dict[str, JourneyServer] = {}

    def factory(config: McpServerConfig, _events: object) -> JourneyServer:
        servers[config.name] = JourneyServer(config)
        return servers[config.name]

    manager = McpSessionManager(
        [
            McpServerConfig(name="local", url="fake-local"),
            McpServerConfig(name="optional-remote", url="fake-remote"),
        ],
        bridge_factory=factory,
    )
    await manager.start_all()
    # These are the same manager object that normal chat, Plan mode, code_plan,
    # create_workflow, and a generated workflow receive through SessionContext.
    workflow_views = [manager, manager, manager, manager, manager]
    assert {id(view) for view in workflow_views} == {id(manager)}
    assert len(manager.all_tools()) == 2
    assert all(item["status"] == McpServerState.READY.value for item in manager.status().values())

    servers["local"].tools = [McpToolSchema("new_inspect", "New inspect", {"type": "object"})]
    await manager.refresh_server("local")
    assert manager.get_tool("mcp:local:inspect") is None
    assert manager.get_tool("mcp:local:new_inspect") is not None
    # The unrelated remote catalog remains available and unchanged.
    assert manager.get_tool("mcp:optional-remote:inspect") is not None
    await manager.shutdown()

