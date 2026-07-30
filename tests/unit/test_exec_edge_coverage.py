"""Deterministic edge coverage for command execution boundaries."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.tools.exec import (
    InspectTerminalTool,
    RunBashTool,
    RunCommandTool,
    RunPythonExprTool,
    RunPythonTool,
    RunTestsTool,
    StopTerminalTool,
    WaitTerminalReadinessTool,
    WaitTerminalTool,
    _arg_argv,
    _arg_env,
    _cwd,
    _lifecycle,
    _readiness,
    _redact_execution_text,
    _result_text,
    _run_proc,
    _spawn_failure_kind,
    _timeout,
)
from agenthicc.tools.exec.outcome import CommandKind

pytestmark = pytest.mark.unit


def test_exec_argument_helpers_validate_and_redact() -> None:
    assert _redact_execution_text("--token secret password=hush") == (
        "--token=<redacted> password=<redacted>"
    )
    assert _spawn_failure_kind(FileNotFoundError()) == "executable_or_shell"
    assert _spawn_failure_kind(NotADirectoryError()) == "cwd"
    assert _spawn_failure_kind(PermissionError()) == "permission"
    assert _spawn_failure_kind(ValueError()) == "environment"
    assert _spawn_failure_kind(RuntimeError()) == "spawn"
    assert _arg_env({"env": {"A": "B"}}) == {"A": "B"}
    assert _arg_env({}) is None
    assert _arg_argv({"argv": ["echo", "ok"]}) == ["echo", "ok"]
    assert _result_text({"stdout": "out"}, "stdout") == "out"
    assert _result_text({"stdout": 1}, "stdout") == ""
    for value in ({"env": {"A": 1}}, {"argv": []}, {"argv": ["echo", 1]}):
        with pytest.raises(ValueError):
            (_arg_env if "env" in value else _arg_argv)(value)
    with pytest.raises(ValueError):
        _lifecycle({"lifecycle": "invalid"})
    with pytest.raises(ValueError):
        _readiness({"readiness": "invalid"})
    assert _readiness({"readiness": {"marker": "ready"}}) == {"marker": "ready"}
    assert _cwd({"cwd": "sub"}, {"workspace_root": "/tmp/project"}) == "/tmp/project/sub"
    timeout, invalid = _timeout({"timeout": -1}, 30.0, CommandKind.EXEC)
    assert timeout is None and invalid is not None


async def test_run_proc_reports_spawn_failures_and_redacts_output(tmp_path: Path) -> None:
    invalid_cwd = await _run_proc(["echo", "ignored"], cwd=str(tmp_path / "missing"), timeout=1.0)
    assert invalid_cwd["spawn_failure"] == "cwd"

    missing = await _run_proc(
        [str(tmp_path / "missing-executable")], cwd=str(tmp_path), timeout=1.0
    )
    assert missing["state"] == "spawn_failed"

    failed = await _run_proc(
        [sys.executable, "-c", "import sys; print('token=hidden'); sys.exit(3)"],
        cwd=str(tmp_path),
        timeout=2.0,
        env={"TEST_EXEC_EDGE": "yes"},
    )
    assert failed["state"] == "failed"
    assert "hidden" not in failed["stdout"]
    assert "token=<redacted>" in failed["stdout"]


async def test_run_proc_cancellation_performs_cleanup(tmp_path: Path) -> None:
    task = asyncio.create_task(
        _run_proc(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=str(tmp_path),
            timeout=0.0,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task
    assert result["state"] == "cancelled"
    assert result["cancelled"] is True


class _TerminalManager:
    session_id = "session-1"

    def __init__(self) -> None:
        self.started: dict[str, object] = {}
        self.record = SimpleNamespace(session_id="session-1", result=lambda: {"ok": True})

    async def start(self, **kwargs: object) -> dict[str, object]:
        self.started = kwargs
        return {"ok": True, "background": True, "terminal_id": "term-1"}

    async def wait(self, terminal_id: str, *, timeout: float) -> dict[str, object]:
        return {"ok": True, "terminal_id": terminal_id, "timeout": timeout}

    async def wait_readiness(self, terminal_id: str, readiness: object = None) -> dict[str, object]:
        return {"ok": True, "terminal_id": terminal_id, "readiness": readiness}

    async def stop(self, terminal_id: str, *, force: bool, reason: str) -> bool:
        return force and reason == "tool stop" and terminal_id == "term-1"

    def get(self, terminal_id: str) -> object:
        return self.record if terminal_id == "term-1" else None


async def test_execution_tools_reject_bad_inputs_and_delegate_terminal_lifecycle(
    tmp_path: Path,
) -> None:
    context = {"workspace_root": str(tmp_path)}
    assert (await RunBashTool().execute({"command": "echo ok", "timeout": -1}, context))[
        "state"
    ] == ("rejected")
    bad_env = await RunBashTool().execute({"command": "echo ok", "env": {"X": 1}}, context)
    assert "env" in str(bad_env["error"])
    bad_lifecycle = await RunBashTool().execute({"command": "echo ok", "lifecycle": "bad"}, context)
    assert "lifecycle" in str(bad_lifecycle["error"])
    no_manager = await RunBashTool().execute({"command": "echo ok", "background": True}, context)
    assert no_manager["state"] == "rejected"
    service = await RunBashTool().execute(
        {"command": "echo ok", "lifecycle": "service"}, {**context, "terminal_manager": None}
    )
    assert service["state"] == "rejected"

    manager = _TerminalManager()
    delegated = await RunCommandTool().execute(
        {"argv": ["python", "-V"], "background": True, "label": "build"},
        {**context, "terminal_manager": manager, "tool_call_id": "tool-1"},
    )
    assert delegated["terminal_id"] == "term-1"
    assert manager.started["shell"] is False
    assert manager.started["tool_call_id"] == "tool-1"
    invalid_argv = await RunCommandTool().execute({"argv": []}, context)
    assert "argv" in str(invalid_argv["error"])
    invalid_readiness = await RunCommandTool().execute(
        {"argv": ["echo", "ok"], "readiness": "bad"}, context
    )
    assert "readiness" in str(invalid_readiness["error"])

    waited = await WaitTerminalTool().execute(
        {"terminal_id": "term-1", "timeout": 2}, {"terminal_manager": manager}
    )
    assert waited["timeout"] == 2.0
    assert (await WaitTerminalTool().execute({"terminal_id": "term-1"}, {}))["state"] == "rejected"
    ready = await WaitTerminalReadinessTool().execute(
        {"terminal_id": "term-1", "readiness": {"marker": "ready"}},
        {"terminal_manager": manager},
    )
    assert ready["readiness"] == {"marker": "ready"}
    rejected_ready = await WaitTerminalReadinessTool().execute(
        {"terminal_id": "term-1", "readiness": "bad"}, {"terminal_manager": manager}
    )
    assert rejected_ready["state"] == "rejected"
    assert (
        await InspectTerminalTool().execute(
            {"terminal_id": "term-1"}, {"terminal_manager": manager}
        )
    )["ok"]
    assert (
        await InspectTerminalTool().execute({"terminal_id": "other"}, {"terminal_manager": manager})
    )["state"] == "unknown"
    assert (
        await StopTerminalTool().execute(
            {"terminal_id": "term-1", "force": True}, {"terminal_manager": manager}
        )
    )["stop_requested"] is True


async def test_python_and_test_tools_validate_timeout_and_arguments(tmp_path: Path) -> None:
    context = {"workspace_root": str(tmp_path)}
    assert (await RunPythonTool().execute({"code": "print(1)", "timeout": -1}, context))[
        "state"
    ] == "rejected"
    assert (await RunPythonExprTool().execute({"expression": "1", "timeout": -1}, context))[
        "state"
    ] == "rejected"
    with pytest.raises(ValueError, match="list of strings"):
        await RunTestsTool().execute({"args": [1]}, context)
