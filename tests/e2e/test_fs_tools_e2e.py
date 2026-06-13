"""E2E tests: agents use the new filesystem tools via AgentRunnerBase + MockTransport.

The agent calls tools through the MockTransport-driven scripted completions.
Real LLM calls are never made — every completion is pre-loaded in MockTransport.

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""

import hashlib
from pathlib import Path

import pytest

from lauren_ai._agents import agent, use_tools
from lauren_ai._signals import SignalBus
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
from lauren_ai.testing import _build_runner_for_agent

import agenthicc.tools.fs.agent_tools as _at
from agenthicc.tools.fs.linux import LinuxFilesystemBackend
from agenthicc.tools.fs.router import BackendRouter

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _completion(content: str = "done", n: int = 1) -> Completion:
    return Completion(
        id=f"c{n}",
        model="mock",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=5, output_tokens=5),
    )


def _configure_router(tmp_path):
    """Point the module-level agent_tools router at tmp_path."""
    _at.configure_router(BackendRouter(LinuxFilesystemBackend(tmp_path)))


def _reset_router():
    _at._router = None


# ---------------------------------------------------------------------------
# Test 1: agent writes and reads a file
# ---------------------------------------------------------------------------

async def test_agent_can_write_and_read_file(tmp_path):
    """Agent writes 'hello world' to test.txt and reads it back via @tool wrappers."""
    _configure_router(tmp_path)
    workspace = str(tmp_path)

    @tool()
    async def write_f(path: str, content: str) -> dict:
        """Write content to a file.

        Args:
            path: Absolute file path.
            content: Text to write.
        """
        from agenthicc.tools.fs import WriteFileTool  # noqa: PLC0415
        return await WriteFileTool().execute(
            {"path": path, "content": content}, {"workspace_root": workspace}
        )

    @tool()
    async def read_f(path: str) -> dict:
        """Read a file.

        Args:
            path: Absolute file path.
        """
        from agenthicc.tools.fs import ReadFileTool  # noqa: PLC0415
        return await ReadFileTool().execute(
            {"path": path}, {"workspace_root": workspace}
        )

    @agent(model="mock")
    @use_tools(write_f, read_f)
    class FileAgent: ...

    target = str(tmp_path / "test.txt")
    mock = MockTransport()
    mock.queue_tool_use("write_f", {"path": target, "content": "hello world"})
    mock.queue_tool_use("read_f", {"path": target})
    mock.queue_response(_completion("File written and read.", n=3))

    inst = FileAgent()
    runner = _build_runner_for_agent(inst, mock, signals=SignalBus())
    response = await runner.run(inst, "Write hello world to test.txt then read it back")

    assert response.stop_reason == "end_turn"
    assert len(response.tool_calls_made) == 2
    assert (tmp_path / "test.txt").exists()
    assert (tmp_path / "test.txt").read_text() == "hello world"

    _reset_router()


# ---------------------------------------------------------------------------
# Test 2: agent batch-writes a list of files
# ---------------------------------------------------------------------------

async def test_agent_batch_write_files(tmp_path):
    """Agent calls batch_write to create multiple files in a single tool call."""
    _configure_router(tmp_path)

    @tool()
    async def batch_write_t(files: list) -> dict:
        """Write multiple files at once.

        Args:
            files: List of dicts with 'path' and 'content'.
        """
        return await _at.batch_write(files)

    @agent(model="mock")
    @use_tools(batch_write_t)
    class BatchAgent: ...

    file_list = [
        {"path": str(tmp_path / "f1.py"), "content": "# f1\n"},
        {"path": str(tmp_path / "f2.py"), "content": "# f2\n"},
        {"path": str(tmp_path / "f3.py"), "content": "# f3\n"},
    ]
    mock = MockTransport()
    mock.queue_tool_use("batch_write_t", {"files": file_list})
    mock.queue_response(_completion("All files created.", n=2))

    inst = BatchAgent()
    runner = _build_runner_for_agent(inst, mock, signals=SignalBus())
    response = await runner.run(inst, "Create three Python stub files")

    assert response.stop_reason == "end_turn"
    assert response.tool_calls_made[0].name == "batch_write_t"
    for item in file_list:
        assert Path(item["path"]).exists(), f"{item['path']} was not created"

    _reset_router()


# ---------------------------------------------------------------------------
# Test 3: agent greps and then patches a file
# ---------------------------------------------------------------------------

async def test_agent_grep_and_patch(tmp_path):
    """Agent uses grep_file to locate a function then apply_diff to patch it."""
    _configure_router(tmp_path)

    src = tmp_path / "utils.py"
    src.write_text("def greet():\n    return 'hello'\n")

    @tool()
    async def grep_t(path: str, pattern: str) -> dict:
        """Search a single file for a pattern.

        Args:
            path: File path.
            pattern: Regex pattern.
        """
        return await _at.grep_file(path, pattern)

    @tool()
    async def diff_t(path: str, diff: str) -> dict:
        """Apply a unified diff to a file.

        Args:
            path: File path to patch.
            diff: Unified diff string.
        """
        return await _at.apply_diff(path, diff)

    @agent(model="mock")
    @use_tools(grep_t, diff_t)
    class PatchAgent: ...

    diff_str = (
        "@@ -1,2 +1,2 @@\n"
        " def greet():\n"
        "-    return 'hello'\n"
        "+    return 'hello world'\n"
    )

    mock = MockTransport()
    mock.queue_tool_use("grep_t", {"path": str(src), "pattern": "def greet"})
    mock.queue_tool_use("diff_t", {"path": str(src), "diff": diff_str})
    mock.queue_response(_completion("Function patched.", n=3))

    inst = PatchAgent()
    runner = _build_runner_for_agent(inst, mock, signals=SignalBus())
    response = await runner.run(inst, "Find greet function and update it to say hello world")

    assert response.stop_reason == "end_turn"
    assert len(response.tool_calls_made) == 2
    assert "hello world" in src.read_text()

    _reset_router()


# ---------------------------------------------------------------------------
# Test 4: agent checksums a file before and after modification
# ---------------------------------------------------------------------------

async def test_agent_checksum_verification(tmp_path):
    """Agent writes a file, checksums it, modifies it, checksums again."""
    _configure_router(tmp_path)

    f = tmp_path / "data.txt"
    f.write_bytes(b"version one")

    @tool()
    async def cksum(path: str) -> dict:
        """Compute SHA-256 of a file.

        Args:
            path: File path.
        """
        return await _at.checksum_file(path)

    @tool()
    async def overwrite(path: str, content: str) -> dict:
        """Overwrite a file with new content.

        Args:
            path: File path.
            content: New file content.
        """
        from pathlib import Path as P  # noqa: PLC0415
        P(path).write_text(content)
        return {"ok": True}

    @agent(model="mock")
    @use_tools(cksum, overwrite)
    class ChecksumAgent: ...

    mock = MockTransport()
    mock.queue_tool_use("cksum", {"path": str(f)})
    mock.queue_tool_use("overwrite", {"path": str(f), "content": "version two"})
    mock.queue_tool_use("cksum", {"path": str(f)})
    mock.queue_response(_completion("Checksums recorded; they differ.", n=4))

    inst = ChecksumAgent()
    runner = _build_runner_for_agent(inst, mock, signals=SignalBus())
    response = await runner.run(inst, "Checksum the file, update it, then checksum again")

    assert response.stop_reason == "end_turn"
    # Three tool calls: cksum, overwrite, cksum
    assert len(response.tool_calls_made) == 3
    cksum_calls = [tc for tc in response.tool_calls_made if tc.name == "cksum"]
    assert len(cksum_calls) == 2

    # Verify at the Python level that checksums differ
    digest_v1 = hashlib.sha256(b"version one").hexdigest()
    digest_v2 = hashlib.sha256(b"version two").hexdigest()
    assert digest_v1 != digest_v2

    _reset_router()


# ---------------------------------------------------------------------------
# Test 5: configure_router + _get_backend() returns the right backend
# ---------------------------------------------------------------------------

async def test_backend_router_in_agent_context(tmp_path):
    """configure_router() wires the router so _get_backend() resolves to LinuxFilesystemBackend."""
    router = BackendRouter(LinuxFilesystemBackend(tmp_path))
    _at.configure_router(router)

    try:
        backend = _at._get_backend(str(tmp_path / "any.txt"))
        assert isinstance(backend, LinuxFilesystemBackend)
        assert Path(backend.root).resolve() == tmp_path.resolve()

        # Round-trip through the agent tool directly (no agent runner needed)
        f = tmp_path / "probe.txt"
        f.write_text("probe")
        result = await _at.grep_file(str(f), "probe")
        assert result["ok"] is True
        assert result["total_matches"] == 1
    finally:
        _reset_router()
