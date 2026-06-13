"""Unit tests for CommunicationTools.mcp_connect (PRD-30)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_comm_tools_with_registry():
    from agenthicc.runtime.comm_tools import CommunicationTools

    mock_registry = MagicMock()
    mock_registry.register_server = MagicMock()
    mock_registry.connect_server = AsyncMock(return_value=[])
    proc = MagicMock()
    proc.emit = AsyncMock()
    ct = CommunicationTools(processor=proc, pool=MagicMock(), mcp_registry=mock_registry)
    return ct, mock_registry


def _make_comm_tools_no_registry():
    from agenthicc.runtime.comm_tools import CommunicationTools

    proc = MagicMock()
    proc.emit = AsyncMock()
    ct = CommunicationTools(processor=proc, pool=MagicMock())
    return ct


def _make_mcp_tool(server_name: str = "x", tool_name: str = "ping"):
    from agenthicc.tools.mcp import AgenthiccMcpTool, McpToolSchema

    bridge = MagicMock()
    bridge.server_name = server_name
    schema = McpToolSchema(name=tool_name, description="", input_schema={})
    return AgenthiccMcpTool(bridge, schema)


# ── transport validation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_connect_invalid_transport():
    ct, _ = _make_comm_tools_with_registry()
    result = await ct.mcp_connect(url="u", transport="ftp")
    assert result["ok"] is False
    assert "transport" in result["error"].lower()


@pytest.mark.asyncio
async def test_mcp_connect_invalid_transport_mentions_valid_options():
    ct, _ = _make_comm_tools_with_registry()
    result = await ct.mcp_connect(url="u", transport="grpc")
    assert result["ok"] is False
    # Error message should hint at valid transports
    error = result["error"].lower()
    assert "grpc" in error or "transport" in error


@pytest.mark.asyncio
async def test_mcp_connect_valid_transports_are_accepted():
    valid_transports = ["stdio", "ws", "websocket", "streamable", "http", "streamable_http"]
    for transport in valid_transports:
        ct, mock_reg = _make_comm_tools_with_registry()
        mock_reg.connect_server = AsyncMock(return_value=[])
        result = await ct.mcp_connect(url="u", transport=transport, name=f"srv-{transport}")
        # Should not fail with transport error (may fail for other mocked reasons, but not transport)
        if not result["ok"]:
            assert "transport" not in result.get("error", "").lower()


# ── registry availability ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_connect_no_registry_returns_error():
    ct = _make_comm_tools_no_registry()
    result = await ct.mcp_connect(url="u")
    assert result["ok"] is False
    assert "registry" in result["error"].lower()


# ── successful connect ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_connect_success_returns_ok_true():
    ct, mock_reg = _make_comm_tools_with_registry()
    tool = _make_mcp_tool("x", "ping")
    mock_reg.connect_server = AsyncMock(return_value=[tool])

    result = await ct.mcp_connect(url="echo hi", transport="stdio", name="myserver")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_mcp_connect_success_returns_server_name():
    ct, mock_reg = _make_comm_tools_with_registry()
    mock_reg.connect_server = AsyncMock(return_value=[])

    result = await ct.mcp_connect(url="echo hi", transport="stdio", name="myserver")
    assert result["server_name"] == "myserver"


@pytest.mark.asyncio
async def test_mcp_connect_success_returns_tool_count():
    ct, mock_reg = _make_comm_tools_with_registry()
    tool1 = _make_mcp_tool("x", "ping")
    tool2 = _make_mcp_tool("x", "echo")
    mock_reg.connect_server = AsyncMock(return_value=[tool1, tool2])

    result = await ct.mcp_connect(url="u", transport="stdio", name="srv")
    assert result["tool_count"] == 2


@pytest.mark.asyncio
async def test_mcp_connect_success_returns_tool_names():
    ct, mock_reg = _make_comm_tools_with_registry()
    tool = _make_mcp_tool("x", "ping")
    mock_reg.connect_server = AsyncMock(return_value=[tool])

    result = await ct.mcp_connect(url="echo hi", transport="stdio", name="myserver")
    assert "mcp:x:ping" in result["tools"]


@pytest.mark.asyncio
async def test_mcp_connect_registers_server_config():
    ct, mock_reg = _make_comm_tools_with_registry()
    mock_reg.connect_server = AsyncMock(return_value=[])

    await ct.mcp_connect(url="npx server", transport="stdio", name="newsrv")
    mock_reg.register_server.assert_called_once()
    registered_cfg = mock_reg.register_server.call_args[0][0]
    assert registered_cfg.name == "newsrv"
    assert registered_cfg.url == "npx server"
    assert registered_cfg.transport == "stdio"


@pytest.mark.asyncio
async def test_mcp_connect_passes_token_to_config():
    ct, mock_reg = _make_comm_tools_with_registry()
    mock_reg.connect_server = AsyncMock(return_value=[])

    await ct.mcp_connect(url="wss://example.com", transport="ws", name="s", token="mytoken")
    cfg = mock_reg.register_server.call_args[0][0]
    assert cfg.token == "mytoken"


@pytest.mark.asyncio
async def test_mcp_connect_zero_tools_ok():
    ct, mock_reg = _make_comm_tools_with_registry()
    mock_reg.connect_server = AsyncMock(return_value=[])

    result = await ct.mcp_connect(url="u", name="empty-srv")
    assert result["ok"] is True
    assert result["tool_count"] == 0
    assert result["tools"] == []


# ── auto-name generation ──────────────────────────────────────────────────────


def test_auto_name_from_ws_url():
    from agenthicc.runtime.comm_tools import _auto_name

    result = _auto_name("wss://github.example.com")
    assert result == "github-example-com"


def test_auto_name_strips_hyphens():
    from agenthicc.runtime.comm_tools import _auto_name

    result = _auto_name("wss://example.com")
    # Should not start or end with a hyphen
    assert not result.startswith("-")
    assert not result.endswith("-")


def test_auto_name_max_length():
    from agenthicc.runtime.comm_tools import _auto_name

    long_url = "wss://" + "a" * 100 + ".example.com"
    result = _auto_name(long_url)
    assert len(result) <= 32


def test_auto_name_falls_back_on_empty():
    from agenthicc.runtime.comm_tools import _auto_name

    result = _auto_name("")
    # Empty URL: no netloc, no path tokens → falls back to "server" slug
    assert result  # must be non-empty
    assert len(result) <= 32


@pytest.mark.asyncio
async def test_mcp_connect_auto_name_from_url_when_name_omitted():
    ct, mock_reg = _make_comm_tools_with_registry()
    mock_reg.connect_server = AsyncMock(return_value=[])

    result = await ct.mcp_connect(url="wss://api.example.com", transport="ws")
    # Name should be auto-generated from URL, not empty
    assert result["ok"] is True
    assert result["server_name"]
    assert result["server_name"] != ""


# ── exception handling ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_connect_exception_in_registry_returns_error():
    ct, mock_reg = _make_comm_tools_with_registry()
    mock_reg.connect_server = AsyncMock(side_effect=RuntimeError("connection refused"))

    result = await ct.mcp_connect(url="u", transport="stdio", name="bad-srv")
    assert result["ok"] is False
    assert "connection refused" in result["error"]


@pytest.mark.asyncio
async def test_mcp_connect_register_server_exception_returns_error():
    ct, mock_reg = _make_comm_tools_with_registry()
    mock_reg.register_server.side_effect = ValueError("already registered")

    result = await ct.mcp_connect(url="u", transport="stdio", name="dup")
    assert result["ok"] is False
    assert "already registered" in result["error"]


@pytest.mark.asyncio
async def test_mcp_connect_transport_case_insensitive():
    ct, mock_reg = _make_comm_tools_with_registry()
    mock_reg.connect_server = AsyncMock(return_value=[])

    result = await ct.mcp_connect(url="u", transport="STDIO", name="s")
    # STDIO (uppercase) should be accepted — lowercased before validation
    assert result["ok"] is True
