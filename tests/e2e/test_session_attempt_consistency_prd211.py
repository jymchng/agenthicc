"""CLI journey for resuming a failed session without stale error display."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus

pytestmark = pytest.mark.e2e


def test_jobs_status_and_agents_report_resumed_attempt_not_old_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    store = BackgroundStore(home / ".agenthicc" / "background")
    artifact = home / ".agenthicc" / "sessions" / "resume-session"
    artifact.mkdir(parents=True)
    session = BackgroundSession.create(
        "resume-session",
        title="Resumed goal",
        cwd=str(tmp_path),
        workflow_name="goal_flow",
        intent="private goal text",
        artifact_dir=str(artifact),
        run_id="run_attempt_consistency",
    )
    store.create(session)
    first = store.claim(session.session_id, pid=11, lease_token="attempt-one")
    store.transition(
        first.session_id,
        SessionStatus.FAILED,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=1,
        expected_lease_token="attempt-one",
        error="Previous attempt failed with stale provider error",
        failure_category="provider",
        latest_activity="Workflow failed",
        lease_token="",
    )
    retrying = store.transition(first.session_id, SessionStatus.RETRYING)
    current = store.claim(retrying.session_id, pid=22, lease_token="attempt-two")
    store.heartbeat(
        current.session_id,
        lease_token="attempt-two",
        attempt=2,
        activity="Current attempt is implementing",
    )

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    status = subprocess.run(
        [sys.executable, "-m", "agenthicc", "jobs", "status", current.session_id, "--json"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["status"] == "running"
    assert payload["attempt"] == 2
    assert payload["error"] is None
    assert payload["latest_activity"] == "Current attempt is implementing"
    assert (
        payload["attempt_history"][-1]["error"]
        == "Previous attempt failed with stale provider error"
    )
    assert "private goal text" not in status.stdout

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
    row = next(
        item for item in json.loads(listing.stdout) if item["session_id"] == current.session_id
    )
    assert row["status"] == "running"
    assert row["latest_activity"] == "Current attempt is implementing"
    assert row["attempt_history"][-1]["attempt"] == 1
