"""Unit tests for the MCP bridge and registry (PRD-28)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agenthicc.tools.mcp import (
    McpServerConfig,
    McpToolSchema,
    McpToolBridge,
    AgenthiccMcpTool,
    McpToolRegistry,
    McpToolCallError,
    _extract_tool_content,
)

pytestmark = pytest.mark.unit


# ── McpServerConfig ──────────────────────────────────────────────────────────


def test_resolved_token_expands_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret123")
    cfg = McpServerConfig(name="s", url="u", token="${MY_TOKEN}")
    assert cfg.resolved_token() == "secret123"


def test_resolved_token_missing_env_keeps_literal(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    cfg = McpServerConfig(name="s", url="u", token="${MISSING_VAR}")
    assert cfg.resolved_token() == "${MISSING_VAR}"


def test_resolved_token_empty_when_no_token():
    cfg = McpServerConfig(name="s", url="u")
    assert cfg.resolved_token() == ""


def test_resolved_url_expands_env(monkeypatch):
    monkeypatch.setenv("MCP_HOST", "ws://example.com")
    cfg = McpServerConfig(name="s", url="${MCP_HOST}/path")
    assert cfg.resolved_url() == "ws://example.com/path"


def test_from_dict_ignores_unknown_keys():
    cfg = McpServerConfig.from_dict({"name": "x", "url": "y", "unknown": "z"})
    assert cfg.name == "x"
    assert cfg.url == "y"
    assert not hasattr(cfg, "unknown")


def test_from_dict_maps_all_known_fields():
    cfg = McpServerConfig.from_dict({
        "name": "srv",
        "url": "cmd",
        "transport": "ws",
        "token": "tok",
        "auto_connect": False,
        "reconnect_attempts": 5,
        "reconnect_delay_seconds": 2.5,
        "metadata": {"k": "v"},
    })
    assert cfg.name == "srv"
    assert cfg.transport == "ws"
    assert cfg.auto_connect is False
    assert cfg.reconnect_attempts == 5
    assert cfg.reconnect_delay_seconds == 2.5
    assert cfg.metadata == {"k": "v"}


def test_from_dict_defaults_apply():
    cfg = McpServerConfig.from_dict({"name": "x", "url": "y"})
    assert cfg.transport == "stdio"
    assert cfg.auto_connect is True
    assert cfg.reconnect_attempts == 3


# ── AgenthiccMcpTool ─────────────────────────────────────────────────────────


def _make_tool():
    bridge = MagicMock()
    bridge.server_name = "myserver"
    schema = McpToolSchema(
        name="my_tool", description="Does stuff", input_schema={"type": "object"}
    )
    return AgenthiccMcpTool(bridge, schema)


def test_tool_name_is_compound():
    tool = _make_tool()
    assert tool.name == "mcp:myserver:my_tool"


def test_tool_description_passthrough():
    tool = _make_tool()
    assert tool.description == "Does stuff"


def test_tool_parameters_passthrough():
    tool = _make_tool()
    assert tool.parameters == {"type": "object"}


@pytest.mark.asyncio
async def test_tool_execute_passes_tool_call_id():
    bridge = MagicMock()
    bridge.server_name = "s"
    bridge.call_tool = AsyncMock(return_value="result")
    schema = McpToolSchema(name="t", description="", input_schema={})
    tool = AgenthiccMcpTool(bridge, schema)
    result = await tool.execute({"x": 1}, {"tool_call_id": "abc123"})
    bridge.call_tool.assert_called_once_with("t", {"x": 1}, tool_call_id="abc123")
    assert result == "result"


@pytest.mark.asyncio
async def test_tool_execute_uses_empty_tool_call_id_when_missing():
    bridge = MagicMock()
    bridge.server_name = "s"
    bridge.call_tool = AsyncMock(return_value="ok")
    schema = McpToolSchema(name="t", description="", input_schema={})
    tool = AgenthiccMcpTool(bridge, schema)
    await tool.execute({}, {})
    bridge.call_tool.assert_called_once_with("t", {}, tool_call_id="")


# ── McpToolBridge ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_sets_connected_flag():
    cfg = McpServerConfig(name="s", url="echo hello", transport="stdio")
    bridge = McpToolBridge(cfg)
    mock_client = AsyncMock()
    with (
        patch("agenthicc.tools.mcp._LAUREN_MCP_AVAILABLE", True),
        patch.object(bridge, "_build_client", AsyncMock(return_value=mock_client)),
    ):
        await bridge.connect()
    assert bridge.is_connected


@pytest.mark.asyncio
async def test_connect_idempotent():
    cfg = McpServerConfig(name="s", url="u", transport="stdio")
    bridge = McpToolBridge(cfg)
    bridge._connected = True
    # Should not raise; _build_client is never called because already connected
    await bridge.connect()
    assert bridge.is_connected


@pytest.mark.asyncio
async def test_connect_raises_when_lauren_mcp_unavailable():
    cfg = McpServerConfig(name="s", url="u", transport="stdio", reconnect_attempts=0)
    bridge = McpToolBridge(cfg)
    with patch("agenthicc.tools.mcp._LAUREN_MCP_AVAILABLE", False):
        with pytest.raises(ImportError, match="lauren_mcp"):
            await bridge.connect()


@pytest.mark.asyncio
async def test_connect_retries_and_raises_after_exhaustion():
    cfg = McpServerConfig(
        name="s", url="u", transport="stdio", reconnect_attempts=1, reconnect_delay_seconds=0.0
    )
    bridge = McpToolBridge(cfg)
    mock_client = AsyncMock()
    mock_client.connect.side_effect = RuntimeError("refused")
    with (
        patch("agenthicc.tools.mcp._LAUREN_MCP_AVAILABLE", True),
        patch.object(bridge, "_build_client", AsyncMock(return_value=mock_client)),
    ):
        with pytest.raises(McpToolCallError, match="Failed to connect"):
            await bridge.connect()
    # reconnect_attempts=1 means 2 total attempts
    assert mock_client.connect.call_count == 2


@pytest.mark.asyncio
async def test_call_tool_raises_when_not_connected():
    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    with pytest.raises(McpToolCallError, match="not connected"):
        await bridge.call_tool("t", {})


@pytest.mark.asyncio
async def test_call_tool_raises_on_mcp_error():
    cfg = McpServerConfig(name="s", url="u")
    bridge = McpToolBridge(cfg)
    bridge._connected = True
    err_result = MagicMock()
    err_result.isError = True
    err_result.content = []
    bridge._client = AsyncMock()
    bridge._client.call_tool = AsyncMock(return_value=err_result)
    with pytest.raises(McpToolCallError):
        await bridge.call_tool("t", {})


@pytest.mark.asyncio
async def test_call_tool_raises_on_client_exception():
    cfg = McpServerConfig(name="s", url="u")
    bridge = McpToolBridge(cfg)
    bridge._connected = True
    bridge._client = AsyncMock()
    bridge._client.call_tool = AsyncMock(side_effect=RuntimeError("network error"))
    with pytest.raises(McpToolCallError, match="network error"):
        await bridge.call_tool("t", {})


@pytest.mark.asyncio
async def test_call_tool_returns_content_on_success():
    cfg = McpServerConfig(name="s", url="u")
    bridge = McpToolBridge(cfg)
    bridge._connected = True
    ok_result = MagicMock()
    ok_result.isError = False
    block = MagicMock()
    block.text = "output"
    ok_result.content = [block]
    bridge._client = AsyncMock()
    bridge._client.call_tool = AsyncMock(return_value=ok_result)
    result = await bridge.call_tool("t", {"arg": "val"}, tool_call_id="id1")
    assert result == "output"


@pytest.mark.asyncio
async def test_list_tools_raises_when_not_connected():
    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    with pytest.raises(McpToolCallError, match="not connected"):
        await bridge.list_tools()


@pytest.mark.asyncio
async def test_list_tools_returns_schemas():
    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    bridge._connected = True
    raw_tool = MagicMock()
    raw_tool.name = "ping"
    raw_tool.description = "Ping tool"
    raw_tool.inputSchema = {"type": "object"}
    bridge._client = AsyncMock()
    bridge._client.list_tools = AsyncMock(return_value=[raw_tool])
    schemas = await bridge.list_tools()
    assert len(schemas) == 1
    assert schemas[0].name == "ping"
    assert schemas[0].description == "Ping tool"
    assert schemas[0].input_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_disconnect_clears_state():
    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    bridge._connected = True
    bridge._client = AsyncMock()
    bridge._client.close = AsyncMock()
    await bridge.disconnect()
    assert not bridge.is_connected
    assert bridge._client is None


# ── McpToolRegistry ───────────────────────────────────────────────────────────


def test_register_server_duplicate_raises():
    reg = McpToolRegistry()
    cfg = McpServerConfig(name="x", url="u")
    reg.register_server(cfg)
    with pytest.raises(ValueError, match="already registered"):
        reg.register_server(cfg)


def test_register_server_creates_bridge():
    reg = McpToolRegistry()
    cfg = McpServerConfig(name="myserver", url="u")
    reg.register_server(cfg)
    assert "myserver" in reg._bridges
    assert isinstance(reg._bridges["myserver"], McpToolBridge)


@pytest.mark.asyncio
async def test_discover_all_skips_non_auto_connect():
    reg = McpToolRegistry()
    cfg = McpServerConfig(name="x", url="u", auto_connect=False)
    reg.register_server(cfg)
    discovered = await reg.discover_all()
    assert discovered == []


@pytest.mark.asyncio
async def test_discover_all_connects_and_returns_tools():
    reg = McpToolRegistry()
    cfg = McpServerConfig(name="srv", url="echo", transport="stdio", auto_connect=True)
    reg.register_server(cfg)

    schema = McpToolSchema(name="ping", description="Ping", input_schema={})
    bridge = reg._bridges["srv"]

    with (
        patch.object(bridge, "connect", AsyncMock()),
        patch.object(bridge, "list_tools", AsyncMock(return_value=[schema])),
    ):
        tools = await reg.discover_all()

    assert len(tools) == 1
    assert tools[0].name == "mcp:srv:ping"


@pytest.mark.asyncio
async def test_discover_all_registers_tool_in_registry():
    reg = McpToolRegistry()
    cfg = McpServerConfig(name="srv", url="echo", transport="stdio", auto_connect=True)
    reg.register_server(cfg)

    schema = McpToolSchema(name="echo_tool", description="", input_schema={})
    bridge = reg._bridges["srv"]

    with (
        patch.object(bridge, "connect", AsyncMock()),
        patch.object(bridge, "list_tools", AsyncMock(return_value=[schema])),
    ):
        await reg.discover_all()

    assert reg.get_tool("mcp:srv:echo_tool") is not None


@pytest.mark.asyncio
async def test_discover_all_emits_tool_registered_event():
    mock_processor = MagicMock()
    mock_processor.emit = AsyncMock()
    reg = McpToolRegistry(event_processor=mock_processor)
    cfg = McpServerConfig(name="srv", url="u", auto_connect=True)
    reg.register_server(cfg)

    schema = McpToolSchema(name="tool1", description="", input_schema={})
    bridge = reg._bridges["srv"]

    with (
        patch.object(bridge, "connect", AsyncMock()),
        patch.object(bridge, "list_tools", AsyncMock(return_value=[schema])),
    ):
        await reg.discover_all()

    mock_processor.emit.assert_called_once()
    emitted_event = mock_processor.emit.call_args[0][0]
    assert emitted_event.event_type == "ToolRegistered"
    assert emitted_event.payload["name"] == "mcp:srv:tool1"


@pytest.mark.asyncio
async def test_discover_all_logs_error_and_continues_on_failure():
    reg = McpToolRegistry()
    cfg_fail = McpServerConfig(name="bad", url="u", auto_connect=True)
    cfg_ok = McpServerConfig(name="good", url="u", auto_connect=True)
    reg.register_server(cfg_fail)
    reg.register_server(cfg_ok)

    schema = McpToolSchema(name="ping", description="", input_schema={})
    bad_bridge = reg._bridges["bad"]
    good_bridge = reg._bridges["good"]

    with (
        patch.object(bad_bridge, "connect", AsyncMock(side_effect=McpToolCallError("boom"))),
        patch.object(good_bridge, "connect", AsyncMock()),
        patch.object(good_bridge, "list_tools", AsyncMock(return_value=[schema])),
    ):
        tools = await reg.discover_all()

    # "bad" server failed, but "good" server succeeded
    assert len(tools) == 1
    assert tools[0].name == "mcp:good:ping"


@pytest.mark.asyncio
async def test_connect_server_unknown_raises():
    reg = McpToolRegistry()
    with pytest.raises(KeyError, match="nonexistent"):
        await reg.connect_server("nonexistent")


@pytest.mark.asyncio
async def test_connect_server_returns_tools():
    reg = McpToolRegistry()
    cfg = McpServerConfig(name="s", url="u")
    reg.register_server(cfg)
    schema = McpToolSchema(name="t", description="", input_schema={})
    bridge = reg._bridges["s"]

    with (
        patch.object(bridge, "connect", AsyncMock()),
        patch.object(bridge, "list_tools", AsyncMock(return_value=[schema])),
    ):
        tools = await reg.connect_server("s")

    assert len(tools) == 1
    assert tools[0].name == "mcp:s:t"


def test_get_tool_returns_none_when_missing():
    reg = McpToolRegistry()
    assert reg.get_tool("mcp:x:y") is None


def test_all_tools_initially_empty():
    reg = McpToolRegistry()
    assert reg.all_tools() == []


@pytest.mark.asyncio
async def test_shutdown_disconnects_all_bridges():
    reg = McpToolRegistry()
    cfg1 = McpServerConfig(name="a", url="u")
    cfg2 = McpServerConfig(name="b", url="u")
    reg.register_server(cfg1)
    reg.register_server(cfg2)

    bridge_a = reg._bridges["a"]
    bridge_b = reg._bridges["b"]
    bridge_a._client = AsyncMock()
    bridge_a._client.close = AsyncMock()
    bridge_b._client = AsyncMock()
    bridge_b._client.close = AsyncMock()
    bridge_a._connected = True
    bridge_b._connected = True

    await reg.shutdown()

    assert not bridge_a.is_connected
    assert not bridge_b.is_connected


# ── _extract_tool_content ─────────────────────────────────────────────────────


def test_extract_single_text_block():
    block = MagicMock()
    block.text = "hello"
    result = MagicMock()
    result.content = [block]
    assert _extract_tool_content(result) == "hello"


def test_extract_multiple_blocks():
    b1, b2 = MagicMock(), MagicMock()
    b1.text = "a"
    b2.text = "b"
    result = MagicMock()
    result.content = [b1, b2]
    out = _extract_tool_content(result)
    assert out == ["a", "b"]


def test_extract_empty_content_returns_none():
    result = MagicMock()
    result.content = []
    assert _extract_tool_content(result) is None


def test_extract_no_content_attr_returns_none():
    result = MagicMock(spec=[])  # no attributes
    assert _extract_tool_content(result) is None


def test_extract_data_block_when_no_text():
    block = MagicMock(spec=["data"])
    block.data = b"bytes"
    result = MagicMock()
    result.content = [block]
    assert _extract_tool_content(result) == b"bytes"


# ── _build_client transport paths ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_client_ws_transport():
    """_build_client uses McpServer.ws for ws/websocket transport."""
    cfg = McpServerConfig(name="s", url="wss://example.com/mcp", transport="ws", token="tok")
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    mock_server_cls.ws.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        client = await bridge._build_client()

    mock_server_cls.ws.assert_called_once()
    args, kwargs = mock_server_cls.ws.call_args
    assert args[0] == "wss://example.com/mcp"


@pytest.mark.asyncio
async def test_build_client_websocket_transport_alias():
    """_build_client accepts 'websocket' as alias for 'ws'."""
    cfg = McpServerConfig(name="s", url="ws://host/mcp", transport="websocket")
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    mock_server_cls.ws.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        client = await bridge._build_client()

    mock_server_cls.ws.assert_called_once()


@pytest.mark.asyncio
async def test_build_client_streamable_transport():
    """_build_client uses McpServer.streamable_http for streamable/http transport."""
    cfg = McpServerConfig(name="s", url="https://example.com/mcp", transport="streamable")
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    mock_server_cls.streamable_http.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        client = await bridge._build_client()

    mock_server_cls.streamable_http.assert_called_once()


@pytest.mark.asyncio
async def test_build_client_http_transport_alias():
    """_build_client accepts 'http' as alias for streamable transport."""
    cfg = McpServerConfig(name="s", url="https://host/mcp", transport="http")
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    mock_server_cls.streamable_http.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        client = await bridge._build_client()

    mock_server_cls.streamable_http.assert_called_once()


@pytest.mark.asyncio
async def test_build_client_unknown_transport_raises():
    """_build_client raises ValueError for an unsupported transport."""
    cfg = McpServerConfig(name="s", url="u", transport="grpc")
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        with pytest.raises(ValueError, match="Unknown MCP transport"):
            await bridge._build_client()


@pytest.mark.asyncio
async def test_build_client_stdio_transport():
    """_build_client uses McpServer.stdio for stdio transport."""
    cfg = McpServerConfig(name="s", url="npx run server", transport="stdio")
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    mock_server_cls.stdio.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        client = await bridge._build_client()

    mock_server_cls.stdio.assert_called_once()
    args, kwargs = mock_server_cls.stdio.call_args
    # shlex.split("npx run server") → ["npx", "run", "server"]
    assert args[0] == ["npx", "run", "server"]


# ── disconnect suppresses close() errors ──────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_suppresses_close_error():
    """disconnect() should not raise even if client.close() throws."""
    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    bridge._connected = True
    bridge._client = AsyncMock()
    bridge._client.close = AsyncMock(side_effect=RuntimeError("close failed"))

    # Should complete without raising
    await bridge.disconnect()
    assert not bridge.is_connected
    assert bridge._client is None


# ── McpCallError transparent re-raise path ───────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_wraps_mcp_call_error_transparently():
    """When _LAUREN_MCP_AVAILABLE is True and a McpCallError is raised, it wraps it."""
    cfg = McpServerConfig(name="s", url="u")
    bridge = McpToolBridge(cfg)
    bridge._connected = True

    # Create a fake McpCallError class and simulate _LAUREN_MCP_AVAILABLE path
    class FakeMcpCallError(Exception):
        pass

    bridge._client = AsyncMock()
    bridge._client.call_tool = AsyncMock(side_effect=FakeMcpCallError("protocol error"))

    with (
        patch("agenthicc.tools.mcp._LAUREN_MCP_AVAILABLE", True),
        patch("agenthicc.tools.mcp._McpCallError", FakeMcpCallError),
    ):
        with pytest.raises(McpToolCallError, match="protocol error"):
            await bridge.call_tool("t", {})
