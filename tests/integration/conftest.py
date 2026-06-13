"""Shared fixtures for integration tests."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from agenthicc.tools.mcp import McpServerConfig, McpToolBridge, McpToolSchema


@pytest.fixture
def fake_mcp_bridge():
    """A McpToolBridge whose underlying client is fully mocked."""
    cfg = McpServerConfig(name="fake", url="echo test", transport="stdio")
    bridge = McpToolBridge(cfg)
    bridge._connected = True

    mock_client = AsyncMock()

    # Build mock ToolSchema objects — try the real lauren_mcp types first,
    # fall back to McpToolSchema (which list_tools already normalises to).
    try:
        from lauren_mcp._types import ToolSchema  # type: ignore[import]

        echo_schema = ToolSchema(
            name="echo",
            description="Echo input",
            inputSchema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        )
        fail_schema = ToolSchema(
            name="fail_tool",
            description="Always fails",
            inputSchema={},
        )
        schemas: list = [echo_schema, fail_schema]
    except ImportError:
        # lauren_mcp not installed — give bridge._client raw objects that
        # bridge.list_tools() knows how to read (it accesses .name /
        # .description / .inputSchema on whatever list_tools() returns).
        echo_schema = MagicMock()  # type: ignore[assignment]
        echo_schema.name = "echo"
        echo_schema.description = "Echo input"
        echo_schema.inputSchema = {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }
        fail_schema = MagicMock()  # type: ignore[assignment]
        fail_schema.name = "fail_tool"
        fail_schema.description = "Always fails"
        fail_schema.inputSchema = {}
        schemas = [echo_schema, fail_schema]

    mock_client.list_tools = AsyncMock(return_value=schemas)

    async def _call_tool(name, arguments=None):  # noqa: ANN001
        result = MagicMock()
        if name == "fail_tool":
            result.isError = True
            block = MagicMock()
            block.text = "intentional failure"
            result.content = [block]
        else:
            result.isError = False
            block = MagicMock()
            block.text = (arguments or {}).get("message", "ok")
            result.content = [block]
        return result

    mock_client.call_tool = _call_tool
    bridge._client = mock_client
    return bridge
