from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_agenthicc(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["AGENTHICC_DISABLE_BACKGROUND"] = "0"
    return subprocess.run(
        [sys.executable, "-m", "agenthicc", *args],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_runs_json_is_available_without_saved_runs(tmp_path: Path) -> None:
    result = _run_agenthicc(tmp_path, "runs", "--json")

    assert result.returncode == 0
    assert json.loads(result.stdout) == []


def test_goal_rejects_competing_workflow_before_creating_run(tmp_path: Path) -> None:
    result = _run_agenthicc(
        tmp_path,
        "--json",
        "--goal",
        "Implement a feature",
        "--detach",
        "--workflow",
        "not_registered",
    )

    assert result.returncode == 2
    assert "goal_flow" in result.stderr
    run_store = tmp_path / ".agenthicc" / "runs" / "events.jsonl"
    assert not run_store.exists()
