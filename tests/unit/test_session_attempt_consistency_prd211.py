"""Regression coverage for PRD-211 attempt and liveness projections."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from rich.console import Console

import agenthicc.background.supervisor as supervisor_module
from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)
from agenthicc.background.model import BackgroundAttempt
from agenthicc.tui.workspace.background_manager import BackgroundManager

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, *, session_id: str = "session-211") -> BackgroundSession:
    artifact_dir = tmp_path / "artifacts" / session_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        session_id,
        title="Attempt-consistency regression",
        cwd=str(tmp_path),
        workflow_name="goal_flow",
        intent="exercise the current attempt",
        artifact_dir=str(artifact_dir),
        run_id="run_prd211",
        now=time.time(),
    )


def test_claim_clears_current_error_but_keeps_terminal_attempt_history(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    first = store.claim("session-211", pid=101, lease_token="lease-one")
    failed = store.transition(
        first.session_id,
        SessionStatus.FAILED,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=first.attempt,
        expected_lease_token="lease-one",
        error="Attempt one failed at goal_implemented",
        failure_category="workflow",
        latest_activity="Workflow failed",
        worker_exit_code=1,
        worker_exit_reason="irrecoverable_error",
        lease_token="",
    )

    assert failed.attempt_history == (
        BackgroundAttempt(
            attempt=1,
            status=SessionStatus.FAILED,
            started_at=failed.attempt_started_at,
            completed_at=failed.completed_at,
            error="Attempt one failed at goal_implemented",
            failure_category="workflow",
            exit_reason="irrecoverable_error",
            worker_exit_code=1,
            latest_activity="Workflow failed",
        ),
    )

    retrying = store.transition(failed.session_id, SessionStatus.RETRYING)
    resumed = store.claim(retrying.session_id, pid=202, lease_token="lease-two")
    assert resumed.status is SessionStatus.RUNNING
    assert resumed.attempt == 2
    assert resumed.error is None
    assert resumed.failure_category == ""
    assert resumed.completed_at is None
    assert resumed.worker_exit_code is None
    assert resumed.worker_exit_reason == ""
    assert resumed.heartbeat_stale is False
    assert resumed.attempt_history[0].error == "Attempt one failed at goal_implemented"

    current = store.heartbeat(
        resumed.session_id,
        lease_token="lease-two",
        attempt=2,
        activity="Attempt two is working",
    )
    assert current.latest_activity == "Attempt two is working"
    with pytest.raises(ValueError, match="attempt is stale"):
        store.heartbeat(
            resumed.session_id,
            lease_token="lease-two",
            attempt=1,
            activity="late heartbeat from attempt one",
        )

    reopened = BackgroundStore(store.root)
    restored = reopened.get(resumed.session_id)
    assert restored.attempt == 2
    assert restored.latest_activity == "Attempt two is working"
    assert restored.attempt_history[0].error == "Attempt one failed at goal_implemented"


def test_legacy_terminal_record_is_snapshotted_before_resume(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    legacy = _session(tmp_path).evolve(
        status=SessionStatus.FAILED,
        attempt=3,
        error="Legacy failure without attempt history",
        failure_category="provider",
        completed_at=123.0,
    )
    # Simulate a durable record written before PRD-211 introduced attempt history.
    store.create(BackgroundSession.from_mapping({**legacy.to_dict(), "attempt_history": []}))

    retrying = store.transition(legacy.session_id, SessionStatus.RETRYING)
    assert retrying.attempt_history[-1].attempt == 3
    assert retrying.attempt_history[-1].status is SessionStatus.FAILED
    assert retrying.attempt_history[-1].error == "Legacy failure without attempt history"

    resumed = store.claim(legacy.session_id, pid=303, lease_token="new-lease")
    assert resumed.attempt == 4
    assert resumed.error is None
    assert resumed.attempt_history[-1].error == "Legacy failure without attempt history"


def test_late_old_attempt_finalizer_cannot_overwrite_new_attempt(tmp_path: Path) -> None:
    from agenthicc.background.worker import WorkerRequest, _WorkerOutcome, _finalize_worker

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    first = store.claim("session-211", pid=101, lease_token="lease-one")
    failed = store.transition(
        first.session_id,
        SessionStatus.FAILED,
        expected_attempt=1,
        expected_lease_token="lease-one",
        error="attempt one failed",
        lease_token="",
    )
    retrying = store.transition(failed.session_id, SessionStatus.RETRYING)
    current = store.claim(retrying.session_id, pid=202, lease_token="lease-two")
    store.heartbeat(
        current.session_id,
        lease_token="lease-two",
        attempt=2,
        activity="attempt two remains active",
    )
    request = WorkerRequest(
        session_id=current.session_id,
        workflow_name="goal_flow",
        intent="intent",
        cwd=str(tmp_path),
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )

    _finalize_worker(
        store,
        request,
        lease_token="lease-one",
        expected_attempt=1,
        outcome=_WorkerOutcome(
            status=SessionStatus.FAILED,
            error="late old attempt failure",
            activity="late finalization",
            exit_reason="failed",
        ),
        worker_pid=101,
    )

    after = store.get(current.session_id)
    assert after.status is SessionStatus.RUNNING
    assert after.attempt == 2
    assert after.lease_token == "lease-two"
    assert after.error is None
    assert after.latest_activity == "attempt two remains active"


def test_live_worker_with_late_heartbeat_is_flagged_not_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    running = store.claim("session-211", pid=404, lease_token="lease-live").evolve(
        last_active=time.time() - 60.0
    )
    store.update(running.session_id, last_active=running.last_active)
    supervisor = BackgroundSupervisor(store)
    monkeypatch.setattr(
        supervisor,
        "_worker_process_state",
        lambda _session: supervisor_module._WorkerProcessState.LIVE,
    )

    changed = supervisor.recover_stale(stale_after_s=1.0)
    current = store.get(running.session_id)
    assert len(changed) == 1
    assert current.status is SessionStatus.RUNNING
    assert current.heartbeat_stale is True
    assert current.latest_activity == "Worker started"

    fresh = store.heartbeat(
        current.session_id,
        lease_token="lease-live",
        attempt=current.attempt,
        activity="Heartbeat restored",
    )
    assert fresh.status is SessionStatus.RUNNING
    assert fresh.heartbeat_stale is False
    assert fresh.latest_activity == "Heartbeat restored"


def test_recovery_orphans_only_confirmed_dead_worker_and_is_race_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    running = store.claim("session-211", pid=505, lease_token="lease-dead").evolve(
        last_active=time.time() - 60.0
    )
    store.update(running.session_id, last_active=running.last_active)
    supervisor = BackgroundSupervisor(store)
    monkeypatch.setattr(
        supervisor,
        "_worker_process_state",
        lambda _session: supervisor_module._WorkerProcessState.DEAD,
    )
    orphaned = supervisor.recover_stale(stale_after_s=1.0)
    assert orphaned[0].status is SessionStatus.ORPHANED

    racing_store = BackgroundStore(tmp_path / "racing-background")
    racing_store.create(_session(tmp_path, session_id="racing"))
    racing = racing_store.claim("racing", pid=606, lease_token="lease-race").evolve(
        last_active=time.time() - 60.0
    )
    racing_store.update("racing", last_active=racing.last_active)
    racing_supervisor = BackgroundSupervisor(racing_store)

    def heartbeat_then_report_dead(_session: BackgroundSession):
        racing_store.heartbeat(
            "racing", lease_token="lease-race", attempt=racing.attempt, activity="new heartbeat"
        )
        return supervisor_module._WorkerProcessState.DEAD

    monkeypatch.setattr(racing_supervisor, "_worker_process_state", heartbeat_then_report_dead)
    assert racing_supervisor.recover_stale(stale_after_s=1.0) == []
    current = racing_store.get("racing")
    assert current.status is SessionStatus.RUNNING
    assert current.latest_activity == "new heartbeat"


def test_manager_shows_current_activity_and_labels_previous_attempt_error(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path).evolve(
        status=SessionStatus.RUNNING,
        attempt=2,
        latest_activity="Attempt two is executing",
        attempt_history=(
            BackgroundAttempt(
                attempt=1,
                status=SessionStatus.FAILED,
                error="Attempt one provider error",
                latest_activity="Workflow failed",
            ),
        ),
    )
    journal = Path(session.artifact_dir) / "conversation.jsonl"
    journal.write_text(
        json.dumps({"kind": "text", "payload": {"text": "Attempt two is executing now"}}) + "\n",
        encoding="utf-8",
    )
    store = BackgroundStore(tmp_path / "background")
    store.create(session)
    console = Console(width=120, height=30, record=True)
    manager = BackgroundManager(console, store=store)
    manager.refresh(force=True)
    manager._detail_session_id = session.session_id
    console.print(manager.render())

    output = console.export_text()
    assert "running" in output
    assert "Attempt two is executing" in output
    assert "Previous attempt 1 (failed)" in output
    assert "Attempt one provider error" in output
    assert "Error Attempt one provider error" not in output


def test_active_legacy_error_does_not_override_fresh_activity(tmp_path: Path) -> None:
    session = _session(tmp_path).evolve(
        status=SessionStatus.RUNNING,
        attempt=2,
        error="legacy stale failure",
        latest_activity="Current active attempt",
    )
    journal = Path(session.artifact_dir) / "conversation.jsonl"
    journal.write_text(
        json.dumps({"kind": "text", "payload": {"text": "Current active attempt text"}}) + "\n",
        encoding="utf-8",
    )
    store = BackgroundStore(tmp_path / "background")
    store.create(session)
    console = Console(width=120, height=30, record=True)
    manager = BackgroundManager(console, store=store)
    manager.refresh(force=True)
    console.print(manager.render())
    output = console.export_text()
    assert "Current active attempt" in output
    assert "legacy stale failure" not in output.split("Latest text", maxsplit=1)[0]
    assert "Legacy prior error" in output
