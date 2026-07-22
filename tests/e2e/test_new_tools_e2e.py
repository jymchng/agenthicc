"""E2E tests: agents use fs, exec, and git tools via AgentRunnerBase + MockTransport (PRD-14/15/16).

NOTE: no from __future__ import annotations — @tool() needs real annotations.
"""

import pytest
from lauren_ai._agents import agent, use_tools
from lauren_ai._signals import SignalBus
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
from lauren_ai.testing import _build_runner_for_agent
from agenthicc.tools.fs import ReadFileTool, WriteFileTool
from agenthicc.tools.exec import RunBashTool

pytestmark = pytest.mark.e2e


def _c(content="ok", n=1):
    return Completion(
        id=f"c{n}",
        model="mock",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=5, output_tokens=5),
    )


# ── Filesystem E2E ────────────────────────────────────────────────────────


async def test_agent_write_read_file(tmp_path):
    workspace = str(tmp_path)

    @tool()
    async def write_f(path: str, content: str) -> dict:
        """Write a file. Args: path: File path. content: File content."""
        return await WriteFileTool().execute(
            {"path": path, "content": content}, {"workspace_root": workspace}
        )

    @tool()
    async def read_f(path: str) -> dict:
        """Read a file. Args: path: File path."""
        return await ReadFileTool().execute({"path": path}, {"workspace_root": workspace})

    @agent(model="mock")
    @use_tools(write_f, read_f)
    class FileAgent: ...

    mock = MockTransport()
    mock.queue_tool_use("write_f", {"path": "output.txt", "content": "hello world"})
    mock.queue_tool_use("read_f", {"path": "output.txt"})
    mock.queue_response(_c("File written and read.", n=3))

    inst = FileAgent()
    runner = _build_runner_for_agent(inst, mock, signals=SignalBus())
    response = await runner.run(inst, "Write hello world to output.txt then read it back")

    assert response.stop_reason == "end_turn"
    assert len(response.tool_calls_made) == 2
    assert (tmp_path / "output.txt").read_text() == "hello world"


# ── Exec E2E ──────────────────────────────────────────────────────────────


async def test_agent_runs_bash(tmp_path):
    @tool()
    async def bash(command: str) -> dict:
        """Run bash. Args: command: Shell command."""
        return await RunBashTool().execute({"command": command}, {"workspace_root": str(tmp_path)})

    @agent(model="mock")
    @use_tools(bash)
    class DevAgent: ...

    mock = MockTransport()
    mock.queue_tool_use("bash", {"command": "echo 'tests passed'"})
    mock.queue_response(_c("Command executed.", n=2))

    inst = DevAgent()
    runner = _build_runner_for_agent(inst, mock, signals=SignalBus())
    response = await runner.run(inst, "Run the echo command")

    assert response.turns == 2
    assert response.tool_calls_made[0].name == "bash"
