"""Tests for the read-only agent-facing MCP setup guidance tool."""

from __future__ import annotations

import pytest

from agenthicc.agent_tools import MCP_AGENT_TOOLS, mcp_connection_guide
from agenthicc.plugins.registry import build_registry
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_project_remote_guide_is_copy_ready_without_secret_values() -> None:
    result = await mcp_connection_guide(
        transport="streamable",
        scope="project",
        server_name="remote-tools",
    )

    assert result["ok"] is True
    assert result["config_path"] == ".agenthicc/agenthicc.toml"
    assert result["transport"] == "streamable_http"
    assert "--project" in str(result["cli_command"])
    assert 'Authorization = "MCP_TOKEN"' in str(result["toml"])
    assert "replace-me" not in str(result)
    assert "actual-token-value" not in str(result)
    assert any(str(item).startswith("/mcp reload") for item in result["session_commands"])  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_global_stdio_guide_points_at_global_config_and_local_launcher() -> None:
    result = await mcp_connection_guide(
        transport="stdio",
        scope="global",
        server_name="local-tools",
    )

    assert result["ok"] is True
    assert result["config_path"] == "~/.agenthicc/agenthicc.toml"
    assert result["transport"] == "stdio"
    assert "--global" in str(result["cli_command"])
    assert "python -m my_mcp_server" in str(result["toml"])


@pytest.mark.asyncio
async def test_guide_rejects_invalid_inputs() -> None:
    assert (await mcp_connection_guide(transport="nope"))["ok"] is False
    assert (await mcp_connection_guide(scope="workspace"))["ok"] is False
    assert (await mcp_connection_guide(server_name="bad name"))["ok"] is False


def test_guidance_tool_is_in_registry_and_is_read_only() -> None:
    registry = build_registry(project_plugin_tools=[])
    assert "mcp_connection_guide" in registry.names
    assert MCP_AGENT_TOOLS == [mcp_connection_guide]
    capabilities = get_tool_capabilities(mcp_connection_guide)
    assert ToolCapability.READ in capabilities
    assert ToolCapability.WRITE not in capabilities
    assert ToolCapability.EXECUTE not in capabilities
