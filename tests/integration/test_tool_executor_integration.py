"""Integration coverage for real built-in tools through lauren-ai dispatch."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lauren_ai import tool

from agenthicc.tools.exec import RunCommandTool
from agenthicc.tools.executor import AgenthiccToolExecutor
from agenthicc.tools.fs import ReadFileTool
from agenthicc.tools.git import GitStatusTool
from agenthicc.tools.mcp import AgenthiccMcpTool, McpToolSchema
from agenthicc.tools.outlook import OutlookListFoldersTool
from agenthicc.tools.sandbox import ToolSandbox

pytestmark = pytest.mark.integration


@tool()
async def plugin_echo(value: str) -> dict[str, str]:
    """Return a plugin-provided value."""
    return {"value": value}


@pytest.mark.asyncio
async def test_builtin_and_plugin_tools_share_lauren_execution_contract(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    outlook = SimpleNamespace(list_folders=AsyncMock(return_value=[{"name": "Inbox"}]))
    bridge = SimpleNamespace(
        server_name="demo",
        call_tool=AsyncMock(return_value={"content": "pong"}),
    )
    mcp_tool = AgenthiccMcpTool(
        bridge,
        McpToolSchema(
            name="ping",
            description="Ping the MCP server.",
            input_schema={"type": "object", "properties": {}},
        ),
    )
    executor = AgenthiccToolExecutor(sandbox=ToolSandbox(root=tmp_path))
    executor.register(ReadFileTool(), source="builtin")
    executor.register(GitStatusTool(), source="git")
    executor.register(RunCommandTool(), source="exec")
    executor.register(OutlookListFoldersTool(outlook), source="outlook")
    executor.register(mcp_tool, source="mcp")
    executor.register(plugin_echo, source="plugin")

    read = await executor.execute("read_file", {"path": "hello.txt"}, "read-1")
    git = await executor.execute("git_status", {}, "git-1")
    command = await executor.execute("run_command", {"argv": ["echo", "ok"]}, "exec-1")
    folders = await executor.execute("outlook_list_folders", {}, "outlook-1")
    mcp = await executor.execute("mcp:demo:ping", {}, "mcp-1")
    plugin = await executor.execute("plugin_echo", {"value": "ok"}, "plugin-1")

    assert read.ok is True and read.value["content"] == "hello"
    assert git.ok is True and isinstance(git.value, dict)
    assert command.ok is True and command.value["stdout"].strip() == "ok"
    assert folders.ok is True and folders.value == {"folders": [{"name": "Inbox"}], "count": 1}
    assert mcp.ok is True and mcp.value == {"content": "pong"}
    assert plugin.ok is True and plugin.value == {"value": "ok"}
    bridge.call_tool.assert_awaited_once_with("ping", {}, tool_call_id="mcp-1")

    sources = {record["source"] for record in executor.catalog()}
    assert sources == {"builtin", "git", "exec", "outlook", "mcp", "plugin"}
