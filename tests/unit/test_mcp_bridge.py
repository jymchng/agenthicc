"""Unit tests for the MCP bridge and registry (PRD-28)."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agenthicc.tools.mcp import (
    McpToolSchema,
    McpToolBridge,
    AgenthiccMcpTool,
    McpToolRegistry,
    McpToolCallError,
)
from agenthicc.tools.mcp_server_config import McpServerConfig, McpConfigurationError

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
    cfg = McpServerConfig.from_dict(
        {
            "name": "srv",
            "url": "cmd",
            "transport": "ws",
            "token": "tok",
            "auto_connect": False,
            "reconnect_attempts": 5,
            "reconnect_delay_seconds": 2.5,
            "metadata": {"k": "v"},
        }
    )
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


def test_config_validation_requires_secret_headers_to_use_references():
    with pytest.raises(McpConfigurationError, match="sensitive MCP headers"):
        McpServerConfig(
            name="remote",
            url="https://example.test/mcp",
            transport="streamable_http",
            headers={"Authorization": "Bearer raw-secret"},
        ).validate()


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


def test_provider_name_is_valid_for_llm_tool_schema():
    tool = _make_tool()
    assert tool.provider_name == "mcp_myserver_my_tool"
    assert all(char.isalnum() or char in "-_" for char in tool.provider_name)


def test_provider_name_sanitizes_realistic_mcp_identity():
    bridge = MagicMock()
    bridge.server_name = "database-dev"
    tool = AgenthiccMcpTool(
        bridge,
        McpToolSchema(name="create_row", description="Create a row", input_schema={}),
    )
    assert tool.name == "mcp:database-dev:create_row"
    assert tool.provider_name == "mcp_database-dev_create_row"


def test_tool_description_passthrough():
    tool = _make_tool()
    assert tool.description == "Does stuff"


def test_tool_parameters_are_provider_safe():
    tool = _make_tool()
    assert tool.parameters == {"type": "object", "properties": {}}


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
async def test_connect_preserves_bounded_stderr_after_startup_failure():
    """Failed stdio startup retains diagnostics after the client is discarded."""

    class FailedClient:
        stderr_tail = "server: missing API key"

        async def connect(self) -> None:
            raise RuntimeError("server exited")

        async def close(self) -> None:
            return None

    cfg = McpServerConfig(name="s", url="u", transport="stdio", reconnect_attempts=0)
    bridge = McpToolBridge(cfg)
    with (
        patch("agenthicc.tools.mcp._LAUREN_MCP_AVAILABLE", True),
        patch.object(bridge, "_build_client", AsyncMock(return_value=FailedClient())),
    ):
        with pytest.raises(McpToolCallError, match="Failed to connect"):
            await bridge.connect()

    assert bridge.stderr_tail == "server: missing API key"


@pytest.mark.asyncio
async def test_call_tool_raises_when_not_connected_and_reconnect_fails():
    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    bridge.connect = AsyncMock(side_effect=McpToolCallError("Failed to connect"))  # type: ignore[method-assign]
    with pytest.raises(McpToolCallError, match="not connected"):
        await bridge.call_tool("t", {})


@pytest.mark.asyncio
async def test_call_tool_reports_a_remote_error():
    cfg = McpServerConfig(name="s", url="u")
    bridge = McpToolBridge(cfg)
    bridge._connected = True
    err_result = MagicMock()
    err_result.isError = True
    err_result.content = []
    bridge._client = AsyncMock()
    bridge._client.call_tool = AsyncMock(return_value=err_result)
    result = await bridge.call_tool("t", {})
    assert (
        result["ok"] is False
        and result["error_code"] == "internal"
        and result["reason"] == "remote_error"
    )


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
    ok_result = {"isError": False, "content": [{"type": "text", "text": "output"}]}
    bridge._client = AsyncMock()
    bridge._client.call_tool = AsyncMock(return_value=ok_result)
    result = await bridge.call_tool("t", {"arg": "val"}, tool_call_id="id1")
    assert result == {"ok": True, "content": "output"}


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
async def test_list_tools_accepts_mapping_shaped_schema():
    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    bridge._connected = True
    bridge._client = AsyncMock()
    bridge._client.list_tools = AsyncMock(
        return_value=[
            {
                "name": "mapping_tool",
                "description": "Mapping tool",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ]
    )

    schemas = await bridge.list_tools()

    assert schemas == [
        McpToolSchema(
            name="mapping_tool",
            description="Mapping tool",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]


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


# ── tool_result (TOOLR-06) ───────────────────────────────────────────────────


def _content(result: object) -> object:
    from agenthicc.tools.mcp_content import tool_result

    envelope = tool_result(result, server="s")
    assert envelope["ok"] is True
    return envelope["content"]


def test_extract_single_text_block():
    block = MagicMock(spec=["text"])
    block.text = "hello"
    result = MagicMock(spec=["content"])
    result.content = [block]
    assert _content(result) == "hello"


def test_extract_structured_content_from_json_result():
    result = {
        "content": [{"type": "text", "text": '["README.md"]'}],
        "structuredContent": {"result": ["README.md"]},
        "isError": False,
    }
    assert _content(result) == ["README.md"]


def test_structured_content_without_a_result_key_is_used_whole():
    result = {"content": [], "structuredContent": {"rows": 3, "ok": True}}
    assert _content(result) == {"rows": 3, "ok": True}


def test_extract_json_encoded_text_payload():
    result = {"content": [{"type": "text", "text": '{"created": true}'}]}
    assert _content(result) == {"created": True}


def test_extract_multiple_text_blocks_are_joined():
    result = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    assert _content(result) == "a\nb"


def test_extract_empty_content_returns_none():
    assert _content({"content": []}) is None


def test_an_image_block_becomes_an_artifact_not_base64(tmp_path, monkeypatch):
    import base64

    from agenthicc.tools import mcp_content

    monkeypatch.setattr(mcp_content, "_artifact_root", tmp_path)
    png = b"\x89PNG\r\n" + b"\0" * 200_000
    encoded = base64.b64encode(png).decode()
    result = {
        "content": [
            {"type": "text", "text": "a chart"},
            {"type": "image", "data": encoded, "mimeType": "image/png"},
        ]
    }

    value = _content(result)

    assert value["text"] == "a chart"
    (artifact,) = value["artifacts"]
    assert artifact["mime_type"] == "image/png" and artifact["bytes"] == len(png)
    assert open(artifact["path"], "rb").read() == png
    assert encoded[:100] not in str(value)


def test_resources_and_links_are_described():
    result = {
        "content": [
            {"type": "resource", "resource": {"uri": "file:///a", "text": "inline text"}},
            {"type": "resource_link", "uri": "file:///b", "name": "b", "mimeType": "text/plain"},
        ]
    }
    value = _content(result)
    assert value["text"] == "inline text"
    assert value["resource_links"] == [{"uri": "file:///b", "name": "b", "mime_type": "text/plain"}]


def test_a_huge_text_result_is_bounded():
    from agenthicc.tools.mcp_content import MAX_RESULT_CHARS, tool_result

    envelope = tool_result({"content": [{"type": "text", "text": "x" * 1_000_000}]}, server="s")
    assert envelope["truncated"] is True and envelope["original_chars"] == 1_000_000
    assert len(envelope["content"]) == MAX_RESULT_CHARS


def test_an_is_error_result_is_an_envelope():
    from agenthicc.tools.mcp_content import tool_result

    envelope = tool_result(
        {"isError": True, "content": [{"type": "text", "text": "quota exceeded"}]}, server="s"
    )
    assert (
        envelope["ok"] is False
        and envelope["error_code"] == "internal"
        and envelope["reason"] == "remote_error"
    )
    assert "quota exceeded" in envelope["error"]


@pytest.mark.parametrize(
    ("schema", "wrapped"),
    [
        (
            {
                "type": "object",
                "properties": {"item": {"$ref": "#/$defs/Item"}},
                "$defs": {"Item": {"type": "object", "properties": {"id": {"type": "string"}}}},
            },
            False,
        ),
        (
            {
                "oneOf": [
                    {"type": "object", "properties": {"a": {"type": "string"}}},
                    {"type": "object", "properties": {"b": {"type": "integer"}}},
                ]
            },
            False,
        ),
        ({"type": "string"}, True),
        ({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}, False),
    ],
    ids=["ref", "one-of", "string-root", "meta"],
)
def test_schemas_are_made_provider_safe(schema, wrapped):
    from agenthicc.tools.mcp_content import provider_safe_schema

    safe = provider_safe_schema(schema)

    assert safe is not None and safe.wrapped is wrapped
    rendered = str(safe.schema)
    assert safe.schema["type"] == "object" and isinstance(safe.schema["properties"], dict)
    assert "$ref" not in rendered and "$defs" not in rendered and "$schema" not in rendered
    assert "oneOf" not in rendered


def test_an_unrepairable_schema_is_rejected():
    from agenthicc.tools.mcp_content import provider_safe_schema

    assert provider_safe_schema({"type": "object", "properties": ["not", "a", "map"]}) is None


async def test_a_wrapped_schema_unwraps_arguments_on_call():
    bridge = MagicMock()
    bridge.server_name = "s"
    bridge.call_tool = AsyncMock(return_value={"ok": True, "content": "done"})
    tool = AgenthiccMcpTool(bridge, McpToolSchema("echo", "", {"type": "string"}))

    assert tool.parameters["required"] == ["value"]
    await tool.execute({"value": "hello"}, {})

    bridge.call_tool.assert_awaited_once_with("echo", "hello", tool_call_id="")


async def test_a_dead_server_reconnects_once_and_a_failing_one_opens_the_circuit():
    import agenthicc.tools.mcp as mcp_module

    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    connects: list[int] = []

    async def connect() -> None:
        connects.append(1)
        bridge._connected = True
        bridge._client = AsyncMock()
        bridge._client.call_tool = AsyncMock(
            return_value={"content": [{"type": "text", "text": "ok"}]}
        )

    bridge.connect = connect  # type: ignore[method-assign]
    result = await bridge.call_tool("t", {})
    assert result == {"ok": True, "content": "ok"} and connects == [1]

    async def connect_to_a_broken_server() -> None:
        connects.append(1)
        bridge._connected = True
        bridge._client = AsyncMock()
        bridge._client.call_tool = AsyncMock(side_effect=ConnectionResetError("gone"))

    bridge.connect = connect_to_a_broken_server  # type: ignore[method-assign]
    bridge._client.call_tool = AsyncMock(side_effect=ConnectionResetError("gone"))
    for _ in range(mcp_module.CIRCUIT_FAILURES):
        with pytest.raises(McpToolCallError):
            await bridge.call_tool("t", {})
    fast = await bridge.call_tool("t", {})
    assert fast["ok"] is False and fast["error_code"] == "unavailable"
    # Each failure after the first reconnected before failing again.
    assert len(connects) == mcp_module.CIRCUIT_FAILURES


# ── _build_client transport paths ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_client_ws_transport():
    """_build_client uses McpServer.ws for ws/websocket transport."""
    cfg = McpServerConfig(
        name="s",
        url="wss://example.com/mcp",
        transport="ws",
        token="tok",
        startup_timeout_s=4.0,
    )
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    mock_server_cls.ws.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        await bridge._build_client()

    mock_server_cls.ws.assert_called_once()
    args, kwargs = mock_server_cls.ws.call_args
    assert args[0] == "wss://example.com/mcp"
    assert kwargs["startup_timeout"] == 4.0


@pytest.mark.asyncio
async def test_build_client_websocket_transport_alias():
    """_build_client accepts 'websocket' as alias for 'ws'."""
    cfg = McpServerConfig(name="s", url="ws://host/mcp", transport="websocket")
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    mock_server_cls.ws.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        await bridge._build_client()

    mock_server_cls.ws.assert_called_once()


@pytest.mark.asyncio
async def test_build_client_streamable_transport():
    """_build_client uses McpServer.streamable_http for streamable/http transport."""
    cfg = McpServerConfig(name="s", url="https://example.com/mcp", transport="streamable")
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    mock_server_cls.streamable_http.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        await bridge._build_client()

    mock_server_cls.streamable_http.assert_called_once()


@pytest.mark.asyncio
async def test_build_client_http_transport_alias():
    """_build_client accepts 'http' as alias for streamable transport."""
    cfg = McpServerConfig(name="s", url="https://host/mcp", transport="http")
    bridge = McpToolBridge(cfg)

    mock_server_cls = MagicMock()
    mock_server_cls.streamable_http.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        await bridge._build_client()

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
    cfg = McpServerConfig(
        name="s",
        command=("npx", "run", "server"),
        cwd=".",
        env={"MCP_MODE": "test"},
        transport="stdio",
        startup_timeout_s=4.0,
    )
    bridge = McpToolBridge(cfg, workspace_root=Path.cwd())

    mock_server_cls = MagicMock()
    mock_server_cls.stdio.return_value = MagicMock()

    with patch("agenthicc.tools.mcp._McpServer", mock_server_cls):
        await bridge._build_client()

    mock_server_cls.stdio.assert_called_once()
    args, kwargs = mock_server_cls.stdio.call_args
    assert args[0] == ["npx", "run", "server"]
    assert kwargs["cwd"] == str(Path.cwd().resolve())
    assert kwargs["env"] == {"MCP_MODE": "test"}
    assert kwargs["startup_timeout"] == 4.0


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


def test_change_callback_uses_lauren_mcp_notification_surface():
    """Protocol list-change notifications are wired to catalog refreshes."""
    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    client = MagicMock()
    bridge._client = client
    callback = MagicMock()

    bridge.set_change_callback(callback)

    client.on_list_changed.assert_called_once_with(callback)


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
