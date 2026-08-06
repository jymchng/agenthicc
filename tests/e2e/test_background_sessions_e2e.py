"""Process and CLI end-to-end coverage for PRD-141."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)
from agenthicc.background.supervisor import BackgroundRequest

pytestmark = pytest.mark.e2e


def _wait_for_status(store: BackgroundStore, session_id: str, status: SessionStatus) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            if store.get(session_id).status is status:
                return
        except KeyError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"session {session_id} did not reach {status.value}")


def test_owned_worker_process_persists_completion(tmp_path: Path, monkeypatch) -> None:
    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "process-session"
    artifact.mkdir(parents=True)
    session = BackgroundSession.create(
        "process-session",
        title="Process worker",
        cwd=str(tmp_path),
        workflow_name="",
        intent="test",
        artifact_dir=str(artifact),
    )
    store.create(session)
    script = tmp_path / "worker.py"
    script.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "from agenthicc.background import BackgroundStore, SessionStatus\n"
        "store = BackgroundStore(Path(sys.argv[2]))\n"
        "sid = sys.argv[1]\n"
        "store.claim(sid, pid=os.getpid(), lease_token='e2e')\n"
        "time.sleep(0.15)\n"
        "store.transition(sid, SessionStatus.COMPLETED, expected_lease_token='e2e', lease_token='')\n",
        encoding="utf-8",
    )
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    monkeypatch.setattr(
        supervisor,
        "_worker_command",
        lambda request: [sys.executable, str(script), session.session_id, str(store.root)],
    )
    request = BackgroundRequest(
        session_id=session.session_id,
        workflow_name="",
        intent="test",
        cwd=str(tmp_path),
    )
    supervisor._launch(request, session)
    _wait_for_status(store, session.session_id, SessionStatus.COMPLETED)
    assert BackgroundStore(store.root).get(session.session_id).status is SessionStatus.COMPLETED


def test_foreground_takeover_stops_owned_worker_before_resume(tmp_path: Path, monkeypatch) -> None:
    """The manager handoff releases the worker before a resumed TUI can start."""

    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "foreground-session"
    artifact.mkdir(parents=True)
    session = BackgroundSession.create(
        "foreground-session",
        title="Foreground takeover",
        cwd=str(tmp_path),
        workflow_name="",
        intent="continue directing",
        artifact_dir=str(artifact),
    )
    store.create(session)
    script = tmp_path / "long_worker.py"
    script.write_text(
        "import os, sys, time\n"
        "from agenthicc.background import BackgroundStore\n"
        "store = BackgroundStore(sys.argv[2])\n"
        "store.claim(sys.argv[1], pid=os.getpid(), lease_token='e2e')\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    supervisor = BackgroundSupervisor(
        store,
        cancel_grace_s=1.0,
        artifact_root=tmp_path / "sessions",
    )
    monkeypatch.setattr(
        supervisor,
        "_worker_command",
        lambda request: [sys.executable, str(script), session.session_id, str(store.root)],
    )
    request = BackgroundRequest(
        session_id=session.session_id,
        workflow_name="",
        intent=session.intent,
        cwd=str(tmp_path),
    )
    supervisor._launch(request, session)
    _wait_for_status(store, session.session_id, SessionStatus.RUNNING)
    running = store.get(session.session_id)
    assert running.worker_pid is not None
    worker_pid = running.worker_pid

    released = supervisor.attach_foreground(session.session_id)

    assert released.status is SessionStatus.CANCELLED
    assert released.worker_pid is None
    assert not supervisor._alive(worker_pid)


def test_cli_manager_alias_and_background_acceptance(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    manager = subprocess.run(
        [sys.executable, "-m", "agenthicc", "agents"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert manager.returncode == 0
    assert "Background Sessions" in manager.stdout

    accepted = subprocess.run(
        [sys.executable, "-m", "agenthicc", "run", "--background", "--intent", "no provider"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert accepted.returncode == 0
    assert "Background session" in accepted.stdout
    session_id = accepted.stdout.split("Background session ", 1)[1].split()[0]
    status = subprocess.run(
        [sys.executable, "-m", "agenthicc", "jobs", "status", session_id, "--json"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert status.returncode == 0
    assert json.loads(status.stdout)["session_id"] == session_id
