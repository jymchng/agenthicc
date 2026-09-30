"""Persisted session/run reconciliation for PRD-211."""

from __future__ import annotations

from pathlib import Path

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.background.model import BackgroundAttempt
from agenthicc.runs.manager import GoalRunManager
from agenthicc.runs.model import GoalRun, GoalRunStatus, RunAgentRecord
from agenthicc.runs.store import RunStore


class _Supervisor:
    def __init__(self, root: Path) -> None:
        self.store = BackgroundStore(root / "background")


def _session(
    root: Path,
    session_id: str,
    *,
    status: SessionStatus,
    run_id: str,
    attempt: int,
    error: str | None = None,
    role: str = "worker",
) -> BackgroundSession:
    artifact = root / "artifacts" / session_id
    artifact.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        session_id,
        title=session_id,
        cwd=str(root),
        workflow_name="goal_flow",
        intent=session_id,
        artifact_dir=str(artifact),
        run_id=run_id,
    ).evolve(
        status=status,
        attempt=attempt,
        attempt_started_at=10.0 + attempt,
        latest_activity=f"{session_id} attempt {attempt} activity",
        error=error,
        failure_category="old_failure" if error else "",
        role=role,
        attempt_history=(
            BackgroundAttempt(
                attempt=attempt - 1,
                status=SessionStatus.FAILED,
                error="previous provider failure",
                latest_activity="previous attempt failed",
            ),
        )
        if attempt > 1
        else (),
    )


def test_run_reconciliation_uses_current_main_and_worker_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    supervisor = _Supervisor(tmp_path)
    runs = RunStore(tmp_path / "runs")
    manager = GoalRunManager(store=runs, supervisor=supervisor, require_git=False)
    run = runs.create(
        GoalRun.create("Keep session state current", repository=str(tmp_path), run_id="run_211")
    )
    main = _session(
        tmp_path,
        "main-session",
        status=SessionStatus.RUNNING,
        run_id=run.run_id,
        attempt=2,
        error="stale error from attempt one",
        role="main",
    )
    failed_worker = _session(
        tmp_path,
        "worker-failed",
        status=SessionStatus.FAILED,
        run_id=run.run_id,
        attempt=1,
        error="worker A failed now",
    )
    active_worker = _session(
        tmp_path,
        "worker-active",
        status=SessionStatus.RUNNING,
        run_id=run.run_id,
        attempt=2,
        error="stale error from worker B attempt one",
    )
    for session in (main, failed_worker, active_worker):
        supervisor.store.create(session)
    runs.update(
        run.run_id,
        status=GoalRunStatus.FAILED,
        failure_reason="stale error from attempt one",
        attention_reasons=("stale error from attempt one",),
        main_session_id=main.session_id,
        main_agent_id=main.session_id,
        exit_code=1,
    )
    runs.link_agent(
        run.run_id,
        RunAgentRecord(
            agent_id=main.session_id,
            run_id=run.run_id,
            role="main",
            session_id=main.session_id,
            status="failed",
            attention_reason="stale error from attempt one",
            attempt=1,
        ),
    )

    projected = manager.projection(run.run_id)

    assert projected.status is GoalRunStatus.RUNNING
    assert projected.failure_reason == ""
    assert projected.attention_reasons == ()
    assert projected.exit_code is None
    agents = {item.session_id: item for item in projected.agents}
    assert agents[main.session_id].status == "running"
    assert agents[main.session_id].attempt == 2
    assert agents[main.session_id].attention_reason == ""
    assert agents[failed_worker.session_id].attention_reason == "worker A failed now"
    assert agents[active_worker.session_id].attention_reason == ""
    assert agents[active_worker.session_id].attempt == 2


def test_reopened_store_retains_attempt_history_and_current_snapshot(tmp_path: Path) -> None:
    supervisor = _Supervisor(tmp_path)
    store = supervisor.store
    session = _session(
        tmp_path,
        "persisted-session",
        status=SessionStatus.FAILED,
        run_id="run_211",
        attempt=1,
        error="attempt one failure",
    )
    store.create(session)

    reopened = BackgroundStore(store.root)
    restored = reopened.get(session.session_id)
    assert restored.error == "attempt one failure"
    assert restored.attempt_history == session.attempt_history

    retrying = reopened.transition(session.session_id, SessionStatus.RETRYING)
    claimed = reopened.claim(retrying.session_id, pid=707, lease_token="lease-two")
    after_restart = BackgroundStore(store.root).get(claimed.session_id)
    assert after_restart.attempt == 2
    assert after_restart.status is SessionStatus.RUNNING
    assert after_restart.error is None
    assert after_restart.attempt_history[-1].error == "attempt one failure"
