"""CLI end-to-end coverage for exact session attachment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore

pytestmark = pytest.mark.e2e


def test_attach_json_targets_each_session_in_one_workspace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    store = BackgroundStore(home / ".agenthicc" / "background")
    session_ids: list[str] = []
    for session_id in ("session-alpha", "session-beta"):
        artifact = home / ".agenthicc" / "sessions" / session_id
        artifact.mkdir(parents=True)
        session = BackgroundSession.create(
            session_id,
            title=session_id,
            cwd=str(tmp_path),
            workflow_name="demo",
            intent="do not resubmit",
            artifact_dir=str(artifact),
        )
        store.create(session)
        session_ids.append(session_id)

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    listing = subprocess.run(
        [sys.executable, "-m", "agenthicc", "agents", "--json"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr
    assert {item["session_id"] for item in json.loads(listing.stdout)} == set(session_ids)

    for session_id in session_ids:
        result = subprocess.run(
            [sys.executable, "-m", "agenthicc", "attach", session_id, "--json"],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["session_id"] == session_id
        assert payload["status"] == "handoff_ready"


def test_goal_run_id_is_rejected_from_positional_attach(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "agenthicc", "attach", "run_example", "--json"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "agents --run" in result.stdout
