"""Integration coverage for the shared PRD-172 MCP manager boundary."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agenthicc.tools.mcp import McpServerConfig, McpToolSchema
from agenthicc.tools.mcp_manager import McpServerState, McpSessionManager

pytestmark = pytest.mark.integration


@dataclass
class InProcessMcpServer:
    config: McpServerConfig

    def __post_init__(self) -> None:
        self.server_name = self.config.name
        self.is_connected = False
        self.tools = [McpToolSchema("echo", "Echo", {"type": "object"})]
        self.callback = None
        self.calls: list[tuple[str, dict[str, object], str]] = []

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def list_tools(self) -> list[McpToolSchema]:
        return list(self.tools)

    async def get_instructions(self) -> str:
        return "Call echo for deterministic integration tests."

    def capabilities(self) -> dict[str, object]:
        return {"tools": {"listChanged": True}}

    def set_change_callback(self, callback: object) -> None:
        self.callback = callback

    async def call_tool(
        self, name: str, arguments: dict[str, object], *, tool_call_id: str = ""
    ) -> dict[str, object]:
        self.calls.append((name, dict(arguments), tool_call_id))
        return {"echo": arguments}


@pytest.mark.asyncio
async def test_shared_manager_exposes_one_catalog_and_executes_mcp_tool() -> None:
    servers: dict[str, InProcessMcpServer] = {}

    def factory(config: McpServerConfig, _events: object) -> InProcessMcpServer:
        server = InProcessMcpServer(config)
        servers[config.name] = server
        return server

    manager = McpSessionManager(
        [McpServerConfig(name="demo", url="in-process")],
        bridge_factory=factory,
    )
    await manager.start_all()
    tool = manager.get_tool("mcp:demo:echo")
    assert tool is not None
    assert manager.get_tool(tool.provider_name) is tool

    result = await tool.execute({"value": "hello"}, {"tool_call_id": "call-1"})
    assert result == {"echo": {"value": "hello"}}
    assert servers["demo"].calls == [("echo", {"value": "hello"}, "call-1")]
    assert manager.status("demo")["status"] == McpServerState.READY.value
    assert manager.instructions()[0]["server"] == "demo"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_tools_changed_callback_refreshes_the_shared_surface() -> None:
    servers: dict[str, InProcessMcpServer] = {}

    def factory(config: McpServerConfig, _events: object) -> InProcessMcpServer:
        servers[config.name] = InProcessMcpServer(config)
        return servers[config.name]

    manager = McpSessionManager(
        [McpServerConfig(name="demo", url="in-process")],
        bridge_factory=factory,
        refresh_debounce_s=0,
    )
    await manager.start_all()
    old_revision = manager.catalog_revision
    servers["demo"].tools = [McpToolSchema("new", "New", {"type": "object"})]
    callback = servers["demo"].callback
    assert callback is not None
    await callback()
    assert manager.catalog_revision > old_revision
    assert manager.get_tool("mcp:demo:echo") is None
    assert manager.get_tool("mcp:demo:new") is not None
    await manager.shutdown()

