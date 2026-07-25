"""Subprocess-backed PRD-149 end-to-end scenarios."""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from agenthicc.background.terminals import TerminalManager, TerminalState
from agenthicc.tools.exec import RunBashTool, RunCommandTool, WaitTerminalTool

pytestmark = pytest.mark.e2e


@pytest.fixture
def manager(tmp_path: Path) -> TerminalManager:
    return TerminalManager(
        session_id="e2e-session",
        cwd=tmp_path,
        store_root=tmp_path / "terminals",
        max_terminals=4,
        max_terminals_per_project=4,
        cancel_grace_s=0.2,
    )


async def test_real_background_terminal_start_wait_and_restart(manager: TerminalManager) -> None:
    started = await RunCommandTool().execute(
        {
            "argv": [
                sys.executable,
                "-c",
                "print('e2e stdout'); print('e2e stderr', file=__import__('sys').stderr)",
            ],
            "background": True,
            "label": "real subprocess",
        },
        {"workspace_root": manager.cwd, "terminal_manager": manager},
    )
    assert started["ok"] is True
    terminal_id = str(started["terminal_id"])
    result = await WaitTerminalTool().execute(
        {"terminal_id": terminal_id}, {"terminal_manager": manager}
    )
    assert result["state"] == "exited"
    assert result["stdout"] == "e2e stdout\n"
    assert result["stderr"] == "e2e stderr\n"

    # A fresh manager can observe the completed durable record without
    # relaunching it or changing its final state.
    restarted = TerminalManager(
        session_id="e2e-session",
        cwd=manager.cwd,
        store_root=Path(manager.store.root),
    )
    record = restarted.get(terminal_id)
    assert record is not None
    assert record.state == TerminalState.EXITED
    await restarted.close()
    await manager.close()


async def test_real_process_group_stop_is_scoped_and_idempotent(manager: TerminalManager) -> None:
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "print(p.pid, flush=True); time.sleep(30)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"
    started = await RunBashTool().execute(
        {"command": command, "background": True, "label": "process tree"},
        {"workspace_root": manager.cwd, "terminal_manager": manager},
    )
    terminal_id = str(started["terminal_id"])
    await asyncio.sleep(0.15)
    record = manager.get(terminal_id)
    assert record is not None and record.pid is not None and record.pgid is not None
    assert record.pgid == record.pid
    assert manager.request_stop(terminal_id) is True
    assert manager.request_stop(terminal_id) is True
    await asyncio.sleep(0.5)
    record = manager.get(terminal_id)
    assert record is not None
    assert record.state == TerminalState.STOPPED
    assert manager.running_count() == 0
    await manager.close()
