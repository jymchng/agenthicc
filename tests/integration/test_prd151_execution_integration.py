"""Real subprocess integration scenarios for PRD-151."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from agenthicc.background.terminals import TerminalManager
from agenthicc.tools.exec import (
    RunCommandTool,
    WaitTerminalTool,
    _PROPAGATE_TOOL_CANCELLATION,
    _run_proc,
)

pytestmark = pytest.mark.integration


async def test_cancelled_foreground_command_returns_cancelled_and_keeps_output(
    tmp_path: Path,
) -> None:
    task = asyncio.create_task(
        RunCommandTool().execute(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "print('before-cancel', flush=True); import time; time.sleep(30)",
                ],
                "timeout": 0,
            },
            {"workspace_root": str(tmp_path)},
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    result = await task

    assert result["ok"] is False
    assert result["state"] == "cancelled"
    assert result["cancelled"] is True
    assert "before-cancel" in str(result["stdout"])
    assert result["cleanup_result"] != "not_required"


async def test_agent_turn_cancellation_propagates_after_foreground_cleanup(
    tmp_path: Path,
) -> None:
    token = _PROPAGATE_TOOL_CANCELLATION.set(True)
    try:
        task = asyncio.create_task(
            _run_proc(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=str(tmp_path),
                timeout=0.0,
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        _PROPAGATE_TOOL_CANCELLATION.reset(token)


async def test_background_timeout_is_distinct_from_observer_timeout(tmp_path: Path) -> None:
    manager = TerminalManager(
        session_id="integration-prd151",
        cwd=tmp_path,
        store_root=tmp_path / "terminals",
        cancel_grace_s=0.1,
    )
    try:
        started = await RunCommandTool().execute(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "print('tail-before-timeout', flush=True); import time; time.sleep(30)",
                ],
                "background": True,
                "timeout": 0.5,
            },
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        observed = await WaitTerminalTool().execute(
            {"terminal_id": str(started["terminal_id"]), "timeout": 0.02},
            {"terminal_manager": manager},
        )
        assert observed["state"] == "waiting"
        assert manager.running_count() == 1

        completed = await WaitTerminalTool().execute(
            {"terminal_id": str(started["terminal_id"])},
            {"terminal_manager": manager},
        )
        assert completed["state"] == "timed_out"
        assert completed["ok"] is False
        assert completed["timed_out"] is True
        assert "tail-before-timeout" in str(completed["stdout"])
    finally:
        await manager.close()


async def test_spawn_failure_identifies_cwd_and_never_reports_completed(tmp_path: Path) -> None:
    result = await RunCommandTool().execute(
        {"argv": ["definitely-not-an-agenthicc-executable"], "cwd": str(tmp_path)},
        {"workspace_root": str(tmp_path)},
    )
    assert result["ok"] is False
    assert result["state"] == "spawn_failed"
    assert "FileNotFoundError" in str(result["termination_reason"])
    assert result["spawn_failure"] == "executable_or_shell"


async def test_invalid_cwd_is_a_structured_spawn_failure(tmp_path: Path) -> None:
    result = await RunCommandTool().execute(
        {"argv": [sys.executable, "-c", "print('no')"], "cwd": str(tmp_path / "missing")},
        {"workspace_root": str(tmp_path)},
    )
    assert result["ok"] is False
    assert result["state"] == "spawn_failed"
    assert result["spawn_failure"] == "cwd"
