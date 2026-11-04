"""Unit tests for exec tools (PRD-16)."""

from __future__ import annotations
import pytest
from unittest.mock import patch
from agenthicc.tools.exec import (
    ExecToolKit,
    RunBashTool,
    RunCommandTool,
    RunPythonTool,
    RunPythonExprTool,
    RunTestsTool,
)
from agenthicc.tools.exec.child import HEAD_BYTES, TAIL_BYTES, StreamCapture

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_proc():
    with patch("agenthicc.tools.exec._run_proc") as m:
        yield m


def _result(stdout="", stderr="", rc=0, timed_out=False):
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": rc,
        "duration_ms": 1.0,
        "timed_out": timed_out,
    }


class TestStreamCapture:
    def test_short_output_is_kept_whole(self):
        capture = StreamCapture()
        capture.feed(b"hi")
        assert capture.render() == "hi" and capture.omitted == 0

    def test_long_output_keeps_head_and_tail(self):
        capture = StreamCapture()
        capture.feed(b"H" * HEAD_BYTES)
        capture.feed(b"m" * 100_000)
        capture.feed(b"T" * TAIL_BYTES)
        text = capture.render()
        assert text.startswith("H" * HEAD_BYTES) and text.endswith("T" * TAIL_BYTES)
        assert (
            "[100000 bytes omitted]" in text and capture.total == HEAD_BYTES + 100_000 + TAIL_BYTES
        )


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
        await RunCommandTool().execute({"argv": ["echo", "hi"]}, {})
        assert mock_proc.call_args[0][0] == ["echo", "hi"]
        assert mock_proc.call_args[1]["shell"] is False


class TestRunPythonTool:
    async def test_creates_temp_file(self, mock_proc):
        mock_proc.return_value = _result(stdout="test\n")
        await RunPythonTool().execute({"code": "print('test')"}, {})
        # The project's interpreter runs the snippet from a temporary .py file.
        call_cmd = mock_proc.call_args[0][0]
        assert call_cmd[-1].endswith(".py")


class TestRunPythonExprTool:
    async def test_result_set(self, mock_proc):
        mock_proc.return_value = _result(stdout="3\n")
        r = await RunPythonExprTool().execute({"expression": "1+2"}, {})
        assert r["result"] == "3"


class TestRunTestsTool:
    async def test_counts_come_from_the_junit_report(self, mock_proc):
        def run(cmd, **kwargs):
            report = next(arg for arg in cmd if arg.startswith("--junitxml="))
            with open(report.split("=", 1)[1], "w", encoding="utf-8") as handle:
                handle.write(
                    '<testsuites><testsuite tests="6" failures="1" errors="0" skipped="0">'
                    '<testcase classname="tests.test_a" name="test_bad">'
                    '<failure message="assert 1 == 2"/></testcase></testsuite></testsuites>'
                )
            return _result(stdout="", rc=1)

        mock_proc.side_effect = run
        r = await RunTestsTool().execute({"framework": "pytest"}, {})
        assert r["passed"] == 5 and r["failed"] == 1
        assert r["failures"] == [
            {"test": "tests/test_a.py::test_bad", "kind": "failure", "message": "assert 1 == 2"}
        ]


class TestExecToolKit:
    def test_returns_background_aware_tools(self):
        tools = ExecToolKit().tools()
        assert len(tools) == 9
        names = {t.name for t in tools}
        assert {"run_bash", "run_tests", "wait_terminal"} <= names
