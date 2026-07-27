"""End-to-end command/build/service journeys for PRD-151."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agenthicc.background.terminals import TerminalManager
from agenthicc.tools.exec import RunBashTool, RunCommandTool, WaitTerminalTool

pytestmark = pytest.mark.e2e


async def test_finite_build_fixture_reports_success_only_after_zero_exit(tmp_path: Path) -> None:
    (tmp_path / "build.py").write_text(
        "from pathlib import Path; Path('dist').mkdir(); print('build complete')\n",
        encoding="utf-8",
    )
    result = await RunCommandTool().execute(
        {"argv": [sys.executable, "build.py"], "cwd": str(tmp_path), "timeout": 5},
        {"workspace_root": str(tmp_path)},
    )
    assert result["ok"] is True
    assert result["state"] == "exited"
    assert result["returncode"] == 0
    assert (tmp_path / "dist").is_dir()


async def test_service_fixture_returns_handle_ready_and_stops_exactly(tmp_path: Path) -> None:
    manager = TerminalManager(
        session_id="e2e-prd151",
        cwd=tmp_path,
        store_root=tmp_path / "terminals",
        cancel_grace_s=0.1,
    )
    try:
        started = await RunBashTool().execute(
            {
                "command": f"{sys.executable} -c \"import time; print('DEV_READY', flush=True); time.sleep(30)\"",
                "cwd": str(tmp_path),
                "background": True,
                "lifecycle": "service",
                "label": "fixture dev server",
                "readiness": {"marker": "DEV_READY", "timeout": 3},
            },
            {"workspace_root": str(tmp_path), "terminal_manager": manager},
        )
        terminal_id = str(started["terminal_id"])
        assert started["state"] == "running"
        assert started["ready"] is True

        stopped = await manager.stop(terminal_id, reason="e2e stop")
        assert stopped is True
        completed = await WaitTerminalTool().execute(
            {"terminal_id": terminal_id}, {"terminal_manager": manager}
        )
        assert completed["state"] == "stopped"
        assert completed["ok"] is False
        assert manager.running_count() == 0
    finally:
        await manager.close()
