"""Extensive PRD-149 tests for owned background terminal processes."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agenthicc.background.terminals import (
    TerminalManager,
    TerminalRecord,
    TerminalState,
    TerminalStore,
    reset_current_terminal_manager,
    set_current_terminal_manager,
)
from agenthicc.tools.exec import RunBashTool, RunCommandTool, WaitTerminalTool
from agenthicc.tools.exec.agent_tools import run_bash

pytestmark = pytest.mark.unit


def _manager(tmp_path: Path, **kwargs: object) -> TerminalManager:
    return TerminalManager(
        session_id="session-test",
        cwd=tmp_path,
        store_root=tmp_path / "registry",
        **kwargs,
    )


async def _wait(manager: TerminalManager, result: dict[str, object]) -> dict[str, object]:
    terminal_id = result.get("terminal_id")
    assert isinstance(terminal_id, str)
    return await manager.wait(terminal_id)


class TestTerminalLifecycle:
    async def test_background_command_returns_handle_and_persists_output(self, tmp_path: Path):
        manager = _manager(tmp_path)
        started = await RunCommandTool().execute(
            {
                "argv": [sys.executable, "-c", "print('hello'); print('second')"],
                "background": True,
                "label": "unit output",
            },
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        assert started["ok"] is True
        assert started["state"] == "running"
        result = await _wait(manager, started)
        assert result["ok"] is True
        assert result["state"] == "exited"
        assert result["returncode"] == 0
        assert result["stdout"] == "hello\nsecond\n"
        record = manager.list_records()[0]
        assert record.label == "unit output"
        assert record.finished_at is not None
        assert TerminalStore(tmp_path / "registry").get(record.terminal_id) is not None
        await manager.close()

    async def test_nonzero_exit_is_failed_and_retains_stderr(self, tmp_path: Path):
        manager = _manager(tmp_path)
        started = await RunCommandTool().execute(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; print('bad', file=sys.stderr); sys.exit(3)",
                ],
                "background": True,
            },
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        result = await _wait(manager, started)
        assert result["ok"] is False
        assert result["state"] == "failed"
        assert result["returncode"] == 3
        assert result["stderr"] == "bad\n"
        await manager.close()

    async def test_shell_background_mode_preserves_foreground_contract(self, tmp_path: Path):
        manager = _manager(tmp_path)
        result = await RunBashTool().execute(
            {"command": "printf foreground", "background": False},
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        assert result["stdout"] == "foreground"
        assert "terminal_id" not in result
        await manager.close()

    async def test_wait_timeout_is_non_destructive(self, tmp_path: Path):
        manager = _manager(tmp_path, cancel_grace_s=0.1)
        started = await RunCommandTool().execute(
            {"argv": [sys.executable, "-c", "import time; time.sleep(30)"], "background": True},
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        result = await _wait_with_timeout(manager, started, 0.05)
        assert result["state"] == "waiting"
        assert result["waiting"] is True
        assert manager.running_count() == 1
        await manager.stop(str(started["terminal_id"]), reason="test cleanup")
        assert manager.running_count() == 0
        await manager.close()

    async def test_explicit_stop_is_idempotent_and_uses_process_group(self, tmp_path: Path):
        manager = _manager(tmp_path, cancel_grace_s=0.1)
        started = await RunBashTool().execute(
            {"command": "sleep 30", "background": True, "label": "long task"},
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        terminal_id = str(started["terminal_id"])
        assert manager.request_stop(terminal_id) is True
        await asyncio.sleep(0.2)
        assert manager.get(terminal_id) is not None
        assert manager.get(terminal_id).state == TerminalState.STOPPED
        assert await manager.stop(terminal_id) is False
        await manager.close()

    async def test_concurrent_waits_keep_independent_output_and_current_wait(self, tmp_path: Path):
        manager = _manager(tmp_path)
        first = await RunCommandTool().execute(
            {
                "argv": [sys.executable, "-c", "import time; time.sleep(.1); print('first')"],
                "background": True,
            },
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        second = await RunCommandTool().execute(
            {
                "argv": [sys.executable, "-c", "import time; time.sleep(.15); print('second')"],
                "background": True,
            },
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        first_result, second_result = await asyncio.gather(
            _wait(manager, first), _wait(manager, second)
        )
        assert first_result["stdout"] == "first\n"
        assert second_result["stdout"] == "second\n"
        assert manager.current_wait_id() is None
        await manager.close()

    async def test_output_is_bounded_and_redacted(self, tmp_path: Path):
        manager = _manager(tmp_path, max_output_bytes=1024)
        command = "print('token=super-secret'); print('x' * 10000)"
        started = await RunCommandTool().execute(
            {"argv": [sys.executable, "-c", command], "background": True},
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        result = await _wait(manager, started)
        stdout = str(result["stdout"])
        assert len(stdout.encode()) <= 1_100
        assert "super-secret" not in stdout
        assert result["truncated"] is True
        await manager.close()


async def _wait_with_timeout(
    manager: TerminalManager, started: dict[str, object], timeout: float
) -> dict[str, object]:
    terminal_id = started.get("terminal_id")
    assert isinstance(terminal_id, str)
    return await manager.wait(terminal_id, timeout=timeout)


class TestTerminalLimitsAndRecovery:
    async def test_session_limit_rejects_second_running_terminal(self, tmp_path: Path):
        manager = _manager(tmp_path, max_terminals=1)
        first = await RunCommandTool().execute(
            {"argv": [sys.executable, "-c", "import time; time.sleep(1)"], "background": True},
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        second = await RunCommandTool().execute(
            {"argv": [sys.executable, "-c", "print('no')"], "background": True},
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        assert first["ok"] is True
        assert second["ok"] is False
        assert "limit" in str(second["error"])
        await _wait(manager, first)
        await manager.close()

    async def test_disabled_manager_returns_structured_rejection(self, tmp_path: Path):
        manager = _manager(tmp_path, enabled=False)
        result = await RunBashTool().execute(
            {"command": "echo no", "background": True},
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        assert result["ok"] is False
        assert result["state"] == "rejected"
        await manager.close()

    async def test_restart_marks_active_disk_record_orphaned_without_killing_pid(
        self, tmp_path: Path
    ):
        store = TerminalStore(tmp_path / "registry")
        store.upsert(
            TerminalRecord(
                terminal_id="term-old",
                session_id="session-test",
                project_root=str(tmp_path),
                cwd=str(tmp_path),
                kind="exec",
                command="sleep 100",
                label="old",
                state=TerminalState.RUNNING,
                created_at=1.0,
                pid=999999,
                pgid=999999,
            )
        )
        manager = _manager(tmp_path)
        record = manager.get("term-old")
        assert record is not None
        assert record.state == TerminalState.ORPHANED
        assert "restarted" in record.stop_reason
        await manager.close()

    async def test_other_session_record_is_not_claimed_as_orphan(self, tmp_path: Path):
        store = TerminalStore(tmp_path / "registry")
        store.upsert(
            TerminalRecord(
                terminal_id="term-other",
                session_id="other-session",
                project_root=str(tmp_path),
                cwd=str(tmp_path),
                kind="exec",
                command="sleep 100",
                label="other",
                state=TerminalState.RUNNING,
                created_at=1.0,
                pid=999999,
                pgid=999999,
            )
        )
        manager = _manager(tmp_path)
        record = manager.get("term-other")
        assert record is not None
        assert record.state == TerminalState.RUNNING
        await manager.close()

    async def test_persistence_is_jsonl_and_public_record_is_redacted(self, tmp_path: Path):
        manager = _manager(tmp_path)
        started = await RunBashTool().execute(
            {
                "command": "echo --token super-secret",
                "background": True,
                "label": "deploy password=secret",
            },
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        await _wait(manager, started)
        lines = (tmp_path / "registry" / "events.jsonl").read_text().splitlines()
        assert lines
        assert all("super-secret" not in line for line in lines)
        assert all("password=secret" not in line for line in lines)
        json.loads(lines[-1])
        await manager.close()


class TestTerminalContextAndWait:
    async def test_agent_wrapper_uses_current_manager_context(self, tmp_path: Path):
        manager = _manager(tmp_path)
        token = set_current_terminal_manager(manager)
        try:
            started = await run_bash(
                "printf wrapped",
                background=True,
                label="wrapper",
            )
        finally:
            reset_current_terminal_manager(token)
        result = await _wait(manager, started)
        assert result["stdout"] == "wrapped"
        await manager.close()

    async def test_wait_tool_rejects_unknown_or_missing_manager(self, tmp_path: Path):
        assert (await WaitTerminalTool().execute({"terminal_id": "term-nope"}, {}))["ok"] is False
        manager = _manager(tmp_path)
        result = await WaitTerminalTool().execute(
            {"terminal_id": "term-nope"}, {"terminal_manager": manager}
        )
        assert result["error"] == "unknown terminal"
        await manager.close()

    async def test_declared_workflow_policy_defaults_terminal_tool_to_background(
        self, tmp_path: Path
    ):
        manager = _manager(tmp_path)
        result = await RunCommandTool().execute(
            {"argv": [sys.executable, "-c", "print('policy')"]},
            {
                "workspace_root": str(tmp_path),
                "terminal_manager": manager,
                "terminal_wait_policy": "background",
            },
        )
        assert result["background"] is True
        assert result["state"] == "running"
        completed = await _wait(manager, result)
        assert completed["stdout"] == "policy\n"
        await manager.close()
