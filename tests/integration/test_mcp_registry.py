"""Integration tests for McpToolRegistry and AgenthiccMcpTool executor pipeline.

Covers PRD-31: executor integration, permission patterns, error envelopes,
multi-bridge discovery, and clean shutdown.

All tests use in-process mocks — no subprocess, no npx, no real MCP server.
"""
from __future__ import annotations

import fnmatch
import pytest
from unittest.mock import AsyncMock, MagicMock

from agenthicc.tools.mcp import (
    AgenthiccMcpTool,
    McpServerConfig,
    McpToolBridge,
    McpToolSchema,
    McpToolRegistry,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper factory fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_bridge_factory():
    """Return a factory that creates pre-connected bridges with the given tool names."""

    def _factory(server_name: str, tool_names: list[str]) -> McpToolBridge:
        cfg = McpServerConfig(name=server_name, url="fake://", transport="stdio")
        bridge = McpToolBridge(cfg)
        bridge._connected = True

        schemas = [
            McpToolSchema(name=n, description=f"Tool {n}", input_schema={})
            for n in tool_names
        ]

        mock_client = AsyncMock()
        mock_client.list_tools = AsyncMock(return_value=schemas)

        async def _call_tool(name, arguments=None):  # noqa: ANN001
            result = MagicMock()
            result.isError = False
            block = MagicMock()
            block.text = "ok"
            result.content = [block]
            return result

        mock_client.call_tool = _call_tool
        bridge._client = mock_client
        return bridge

    return _factory


# ---------------------------------------------------------------------------
# Test 1 — discover_all registers tools with compound names
# ---------------------------------------------------------------------------


async def test_registry_discover_registers_tools(fake_bridge_factory):
    """discover_all() returns AgenthiccMcpTool objects with mcp:<server>:<tool> names."""
    reg = McpToolRegistry()
    bridge = fake_bridge_factory("srv", ["tool_a", "tool_b"])
    reg._bridges["srv"] = bridge

    tools = await reg.discover_all()

    assert len(tools) == 2
    names = {t.name for t in tools}
    assert "mcp:srv:tool_a" in names
    assert "mcp:srv:tool_b" in names


# ---------------------------------------------------------------------------
# Test 2 — discover_all emits ToolRegistered events
# ---------------------------------------------------------------------------


async def test_registry_emits_tool_registered_events(fake_bridge_factory):
    """Each discovered tool triggers one ToolRegistered event on the processor."""
    mock_proc = MagicMock()
    mock_proc.emit = AsyncMock()

    reg = McpToolRegistry(event_processor=mock_proc)
    bridge = fake_bridge_factory("s", ["ping"])
    reg._bridges["s"] = bridge

    await reg.discover_all()

    mock_proc.emit.assert_called_once()
    event = mock_proc.emit.call_args[0][0]
    assert event.event_type == "ToolRegistered"
    assert event.payload["name"] == "mcp:s:ping"


# ---------------------------------------------------------------------------
# Test 3 — connect_server on demand (auto_connect=False bridge)
# ---------------------------------------------------------------------------


async def test_registry_connect_server_on_demand(fake_bridge_factory):
    """connect_server() explicitly connects an auto_connect=False bridge."""
    reg = McpToolRegistry()
    bridge = fake_bridge_factory("on_demand", ["lazy_tool"])
    # Override auto_connect flag so discover_all would skip it
    bridge._cfg = McpServerConfig(
        name="on_demand", url="fake://", transport="stdio", auto_connect=False
    )
    bridge._connected = True  # mock: already "connected"
    reg._bridges["on_demand"] = bridge

    tools = await reg.connect_server("on_demand")

    assert len(tools) == 1
    assert tools[0].name == "mcp:on_demand:lazy_tool"


# ---------------------------------------------------------------------------
# Test 4 — get_tool lookup after discover
# ---------------------------------------------------------------------------


async def test_registry_get_tool_lookup(fake_bridge_factory):
    """get_tool() returns the AgenthiccMcpTool registered during discover_all."""
    reg = McpToolRegistry()
    bridge = fake_bridge_factory("x", ["ping"])
    reg._bridges["x"] = bridge

    await reg.discover_all()

    tool = reg.get_tool("mcp:x:ping")
    assert tool is not None
    assert isinstance(tool, AgenthiccMcpTool)
    assert tool.name == "mcp:x:ping"


# ---------------------------------------------------------------------------
# Test 5 — AgenthiccMcpTool flows through AgenthiccToolExecutor (success path)
# ---------------------------------------------------------------------------


async def test_mcp_tool_through_executor(fake_mcp_bridge):
    """AgenthiccMcpTool passes through the executor pipeline and returns ok=True."""
    from agenthicc.tools.executor import AgenthiccToolExecutor

    schema = McpToolSchema(name="echo", description="Echo", input_schema={})
    tool = AgenthiccMcpTool(fake_mcp_bridge, schema)

    executor = AgenthiccToolExecutor(event_processor=None)
    env = await executor.execute(tool, {"message": "hello"}, {})

    assert env.ok is True
    assert env.value == "hello"
    assert env.duration_ms >= 0


# ---------------------------------------------------------------------------
# Test 6 — MCP tool error becomes a failed envelope (not a crash)
# ---------------------------------------------------------------------------


async def test_mcp_tool_error_becomes_envelope(fake_mcp_bridge):
    """A tool that returns isError=True raises McpToolCallError; executor wraps it."""
    from agenthicc.tools.executor import AgenthiccToolExecutor

    schema = McpToolSchema(name="fail_tool", description="Fails", input_schema={})
    tool = AgenthiccMcpTool(fake_mcp_bridge, schema)

    executor = AgenthiccToolExecutor(event_processor=None)
    env = await executor.execute(tool, {}, {})

    assert env.ok is False
    assert env.error is not None
    assert "failure" in env.error.lower()


# ---------------------------------------------------------------------------
# Test 7 — fnmatch-based permission deny blocks mcp:* tools
# ---------------------------------------------------------------------------


async def test_mcp_tool_permission_denied():
    """PermissionChecker returning False for mcp:* produces a permission_denied envelope."""
    from agenthicc.tools.executor import AgenthiccToolExecutor

    def deny_all_mcp(name: str, args: dict, ctx: dict) -> bool | None:
        return False if fnmatch.fnmatch(name, "mcp:*") else None

    # Build a minimal bridge mock — no real async calls happen here
    bridge = MagicMock()
    bridge.server_name = "s"
    schema = McpToolSchema(name="t", description="", input_schema={})
    tool = AgenthiccMcpTool(bridge, schema)

    executor = AgenthiccToolExecutor(
        event_processor=None,
        permission_checker=deny_all_mcp,
    )
    env = await executor.execute(tool, {}, {})

    assert env.ok is False
    assert env.error is not None
    assert "permission_denied" in env.error


# ---------------------------------------------------------------------------
# Test 8 — all_tools() returns flat list from multiple bridges
# ---------------------------------------------------------------------------


async def test_all_tools_returns_flat_list(fake_bridge_factory):
    """all_tools() returns the combined set of tools from all registered bridges."""
    reg = McpToolRegistry()
    reg._bridges["alpha"] = fake_bridge_factory("alpha", ["a1", "a2"])
    reg._bridges["beta"] = fake_bridge_factory("beta", ["b1"])

    await reg.discover_all()

    all_tools = reg.all_tools()
    assert len(all_tools) == 3
    names = {t.name for t in all_tools}
    assert names == {"mcp:alpha:a1", "mcp:alpha:a2", "mcp:beta:b1"}


# ---------------------------------------------------------------------------
# Test 9 — shutdown() disconnects all bridges
# ---------------------------------------------------------------------------


async def test_shutdown_disconnects_all(fake_bridge_factory):
    """shutdown() calls disconnect() on every registered bridge exactly once."""
    reg = McpToolRegistry()
    bridge_a = fake_bridge_factory("a", ["tool1"])
    bridge_b = fake_bridge_factory("b", ["tool2"])
    reg._bridges["a"] = bridge_a
    reg._bridges["b"] = bridge_b

    # Patch disconnect on both bridges to track calls
    bridge_a.disconnect = AsyncMock()
    bridge_b.disconnect = AsyncMock()

    await reg.shutdown()

    bridge_a.disconnect.assert_called_once()
    bridge_b.disconnect.assert_called_once()
