"""Unit tests for exec tools (PRD-16)."""
from __future__ import annotations
import pytest
from unittest.mock import patch
from agenthicc.tools.exec import (ExecToolKit, RunBashTool, RunCommandTool,
    RunPythonTool, RunPythonExprTool, RunTestsTool, _truncate)

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_proc():
    with patch("agenthicc.tools.exec._run_proc") as m:
        yield m


def _result(stdout="", stderr="", rc=0, timed_out=False):
    return {"stdout": stdout, "stderr": stderr, "returncode": rc, "duration_ms": 1.0, "timed_out": timed_out}


class TestTruncate:
    def test_short_unchanged(self): assert _truncate("hi") == "hi"
    def test_long_truncated(self):
        r = _truncate("x" * 200_000, max_bytes=1024)
        assert r.endswith("[... truncated]")
    def test_exact_max_not_truncated(self): assert "[... truncated]" not in _truncate("a" * 100, max_bytes=200)


class TestRunBashTool:
    async def test_success(self, mock_proc):
        mock_proc.return_value = _result(stdout="hello\n")
        r = await RunBashTool().execute({"command": "echo hello"}, {})
        assert r["stdout"] == "hello\n"

    async def test_cwd_from_context(self, mock_proc):
        mock_proc.return_value = _result()
        await RunBashTool().execute({"command": "ls"}, {"workspace_root": "/proj"})
        assert mock_proc.call_args[1]["cwd"] == "/proj"

    async def test_timeout_flag(self, mock_proc):
        mock_proc.return_value = _result(timed_out=True)
        r = await RunBashTool().execute({"command": "sleep 100", "timeout": 1}, {})
        assert r["timed_out"] is True

    async def test_shell_true(self, mock_proc):
        mock_proc.return_value = _result()
        await RunBashTool().execute({"command": "ls"}, {})
        assert mock_proc.call_args[1]["shell"] is True


class TestRunCommandTool:
    async def test_argv_forwarded(self, mock_proc):
        mock_proc.return_value = _result(stdout="out")
        r = await RunCommandTool().execute({"argv": ["echo", "hi"]}, {})
        assert mock_proc.call_args[0][0] == ["echo", "hi"]
        assert mock_proc.call_args[1]["shell"] is False


class TestRunPythonTool:
    async def test_creates_temp_file(self, mock_proc):
        mock_proc.return_value = _result(stdout="test\n")
        r = await RunPythonTool().execute({"code": "print('test')"}, {})
        # executable is sys.executable, second arg is temp .py file
        call_cmd = mock_proc.call_args[0][0]
        assert call_cmd[1].endswith(".py")


class TestRunPythonExprTool:
    async def test_result_set(self, mock_proc):
        mock_proc.return_value = _result(stdout="3\n")
        r = await RunPythonExprTool().execute({"expression": "1+2"}, {})
        assert r["result"] == "3"


class TestRunTestsTool:
    async def test_parses_passed_from_stdout(self, mock_proc):
        mock_proc.return_value = _result(stdout="5 passed, 1 failed in 2.0s\n", rc=1)
        with patch("builtins.open", side_effect=FileNotFoundError):
            r = await RunTestsTool().execute({"framework": "pytest"}, {})
        assert r["passed"] == 5 and r["failed"] == 1


class TestExecToolKit:
    def test_returns_5_tools(self):
        tools = ExecToolKit().tools()
        assert len(tools) == 5
        names = {t.name for t in tools}
        assert "run_bash" in names and "run_tests" in names
