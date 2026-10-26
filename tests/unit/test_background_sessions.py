"""Unit coverage for PRD-141 background lifecycle, storage, and manager UX."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    InvalidSessionTransition,
    ModeApplicationStatus,
    SessionStatus,
    legal_transition,
)
from agenthicc.background.settings import BackgroundSettings, load_background_settings
from agenthicc.background.supervisor import BackgroundRequest

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, sid: str = "session-1") -> BackgroundSession:
    artifact = tmp_path / "sessions" / sid
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "conversation.jsonl").write_text("{}\n", encoding="utf-8")
    return BackgroundSession.create(
        sid,
        title="Build feature",
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="build feature",
        artifact_dir=str(artifact),
        now=100.0,
    )


def test_lifecycle_transition_contract() -> None:
    assert legal_transition(SessionStatus.QUEUED, SessionStatus.STARTING)
    assert legal_transition(SessionStatus.RUNNING, SessionStatus.WAITING_APPROVAL)
    assert legal_transition(SessionStatus.CANCELLING, SessionStatus.CANCELLED)
    assert legal_transition(SessionStatus.COMPLETED, SessionStatus.STARTING)
    assert not legal_transition(SessionStatus.COMPLETED, SessionStatus.RUNNING)
    assert not legal_transition(SessionStatus.DELETED, SessionStatus.RUNNING)


def test_input_reactivation_preserves_prior_attempt_and_resets_live_metadata(
    tmp_path: Path,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    completed = _session(tmp_path).evolve(
        status=SessionStatus.COMPLETED,
        attempt=7,
        completed_at=200.0,
        error="previous attempt error",
        exit_reason="previous attempt exit",
        worker_exit_reason="previous worker exit",
        worker_exit_code=1,
        worker_started_at=101.0,
        worker_finished_at=200.0,
    )
    store.create(completed)

    starting = store.transition(
        completed.session_id,
        SessionStatus.STARTING,
        expected_status=SessionStatus.COMPLETED,
        expected_attempt=completed.attempt,
        resume_marker="input:followup",
        completed_at=None,
        error=None,
        exit_reason="",
        worker_exit_reason="",
        worker_exit_code=None,
        worker_finished_at=None,
    )
    assert starting.status is SessionStatus.STARTING
    assert starting.attempt_history[-1].status is SessionStatus.COMPLETED
    assert starting.attempt_history[-1].error == "previous attempt error"

    claimed = store.claim(completed.session_id, pid=42, lease_token="next-worker")
    assert claimed.status is SessionStatus.RUNNING
    assert claimed.attempt == completed.attempt + 1
    assert claimed.completed_at is None
    assert claimed.error is None
    assert claimed.exit_reason == ""
    assert claimed.worker_exit_reason == ""
    assert claimed.worker_exit_code is None
    assert claimed.worker_finished_at is None
    assert claimed.worker_started_at is not None
    assert claimed.worker_started_at > completed.worker_started_at
    assert claimed.attempt_history[-1].status is SessionStatus.COMPLETED


def test_handoff_reactivates_completed_session_without_replacing_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = BackgroundStore(tmp_path / "background")
    completed = _session(tmp_path, "completed-handoff").evolve(
        status=SessionStatus.COMPLETED,
        attempt=3,
        run_id="existing-goal-run",
        completed_at=200.0,
        error=None,
        latest_activity="Workflow complete",
    )
    store.create(completed)
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    monkeypatch.setattr(
        "agenthicc.background.supervisor.subprocess.Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=4321),
    )

    prepared = supervisor.handoff(
        session_id=completed.session_id,
        intent="historical goal text",
        workflow_name="goal_flow",
        cwd=str(tmp_path),
        run_id="",
        start=False,
        resume_existing_only=True,
    )

    assert prepared.session_id == completed.session_id
    assert prepared.status is SessionStatus.STARTING
    assert prepared.attempt == 3
    assert prepared.resume_marker == "resume:4"
    assert prepared.run_id == "existing-goal-run"
    assert prepared.attempt_history[-1].attempt == 3
    assert prepared.attempt_history[-1].status is SessionStatus.COMPLETED
    request = BackgroundRequest.from_mapping(
        json.loads((store.root / "requests" / f"{completed.session_id}.json").read_text())
    )
    assert request.session_id == completed.session_id
    assert request.run_id == "existing-goal-run"
    assert request.workflow_name == "goal_flow"
    assert request.resume_existing_only is True

    started = supervisor.start(completed.session_id)
    assert started.session_id == completed.session_id
    assert started.status is SessionStatus.STARTING
    assert started.attempt == 3  # claim, not preparation, advances the attempt


def test_background_settings_validate_files_and_cli_overrides(tmp_path: Path) -> None:
    config = tmp_path / "agenthicc.toml"
    config.write_text(
        "[background]\nmax_workers = 4\nmax_workers_per_project = 3\nwall_timeout_s = 12\n",
        encoding="utf-8",
    )
    settings = load_background_settings(
        config_path=str(config),
        overrides=("background.max_workers=1",),
    )
    assert settings.max_workers == 1
    assert settings.max_workers_per_project == 3
    assert settings.wall_timeout_s == 12
    with pytest.raises(ValueError, match="max_workers"):
        BackgroundSettings.from_mapping({"max_workers": 0})


def test_session_round_trip_normalizes_and_redacts_none_values(tmp_path: Path) -> None:
    session = _session(tmp_path).evolve(
        status="running",
        attempt=1,
        labels=["important", "project"],
        pinned=True,
        worker_pid=42,
        detached_goal=True,
        worker_started_at=101.0,
        worker_finished_at=102.0,
        worker_exit_code=0,
        worker_exit_reason="workflow_complete",
        worker_finalization_attempts=1,
        mode_name="Yolo",
        mode_application_status=ModeApplicationStatus.APPLIED,
        mode_application_attempt=1,
    )
    restored = BackgroundSession.from_mapping(session.to_dict())
    assert restored == session
    assert restored.status is SessionStatus.RUNNING
    assert restored.labels == ("important", "project")
    assert restored.detached_goal is True
    assert restored.worker_pid == 42
    assert restored.worker_exit_reason == "workflow_complete"
    assert restored.mode_name == "Yolo"
    assert restored.mode_application_status is ModeApplicationStatus.APPLIED
    legacy = session.to_dict()
    legacy.pop("mode_name")
    assert BackgroundSession.from_mapping(legacy).mode_name == ""
    legacy_request = BackgroundRequest.from_mapping(
        {"session_id": "legacy", "intent": "resume", "cwd": str(tmp_path)}
    )
    assert legacy_request.mode_name is None
    empty_mode_request = BackgroundRequest.from_mapping(
        {"session_id": "empty", "intent": "resume", "cwd": str(tmp_path), "mode_name": ""}
    )
    assert empty_mode_request.mode_name == ""
    with pytest.raises(ValueError, match="mode_name must be a string"):
        BackgroundRequest.from_mapping(
            {"session_id": "malformed", "intent": "resume", "cwd": str(tmp_path), "mode_name": 3}
        )


def test_mode_name_is_not_effective_without_applied_attestation(tmp_path: Path) -> None:
    pending = _session(tmp_path).evolve(mode_name="Yolo")
    assert pending.mode_name == ""
    assert pending.mode_application_status is ModeApplicationStatus.PENDING

    malformed_pending = BackgroundSession.from_mapping(
        {
            **_session(tmp_path, "malformed-mode").to_dict(),
            "mode_name": "Yolo",
            "mode_application_status": "pending",
        }
    )
    assert malformed_pending.mode_name == ""
    assert malformed_pending.mode_application_status is ModeApplicationStatus.PENDING

    legacy_data = _session(tmp_path, "legacy-mode").to_dict()
    for key in (
        "requested_mode_name",
        "mode_application_status",
        "mode_application_attempt",
        "mode_application_error",
    ):
        legacy_data.pop(key)
    legacy_applied = BackgroundSession.from_mapping({**legacy_data, "mode_name": "Yolo"})
    assert legacy_applied.mode_name == "Yolo"
    assert legacy_applied.mode_application_status is ModeApplicationStatus.APPLIED


def test_worker_exit_event_is_bounded_and_replayable(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.claim("session-1", pid=42, lease_token="lease")
    store.transition(
        "session-1",
        SessionStatus.COMPLETED,
        expected_status=SessionStatus.RUNNING,
        expected_lease_token="lease",
        worker_finished_at=102.0,
        worker_exit_code=0,
        worker_exit_reason="workflow_complete",
        worker_finalization_attempts=1,
        lease_token="",
    )
    store.record_worker_exit(
        "session-1",
        worker_pid=42,
        exit_reason="x" * 500,
        exit_code=0,
        cleanup_error="y" * 3_000,
    )
    restored = store.get("session-1")
    assert restored.worker_pid == 42
    events = store._read_events()
    exit_event = next(item for item in events if item.get("event_type") == "worker_exited")
    payload = exit_event["payload"]
    assert isinstance(payload, dict)
    assert len(str(payload["exit_reason"])) == 128
    assert len(str(payload["cleanup_error"])) == 2_000


def test_store_replays_updates_and_ignores_corrupt_lines(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = _session(tmp_path)
    store.create(session)
    store.update(session.session_id, title="Renamed", labels=("tag",), pinned=True)
    store.transition(session.session_id, SessionStatus.STARTING)
    store.transition(session.session_id, SessionStatus.RUNNING, current_phase="execute")
    store.heartbeat(session.session_id, lease_token="", phase="execute", activity="Reading")
    with store.events_path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    loaded = store.get(session.session_id)
    assert loaded.title == "Renamed"
    assert loaded.status is SessionStatus.RUNNING
    assert loaded.current_phase == "execute"
    assert loaded.pinned is True
    assert store.list(query="renamed")[0].session_id == session.session_id


def test_store_enforces_expected_state_and_lease(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    with pytest.raises(InvalidSessionTransition):
        store.transition("session-1", SessionStatus.COMPLETED)
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING, lease_token="lease-1")
    with pytest.raises(InvalidSessionTransition, match="stale"):
        store.heartbeat("session-1", lease_token="lease-2")


def test_mode_application_is_durable_and_attempt_and_lease_scoped(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path).evolve(requested_mode_name="YOLO"))
    first = store.claim("session-1", pid=42, lease_token="lease-one")

    with pytest.raises(InvalidSessionTransition, match="current running worker attempt"):
        store.update(
            first.session_id,
            mode_name="Yolo",
            mode_application_status=ModeApplicationStatus.APPLIED,
            mode_application_attempt=first.attempt,
        )

    applied = store.record_mode_application(
        first.session_id,
        requested_mode_name="YOLO",
        effective_mode_name="Yolo",
        attempt=first.attempt,
        lease_token="lease-one",
    )
    assert applied.requested_mode_name == "YOLO"
    assert applied.mode_name == "Yolo"
    assert applied.mode_application_status is ModeApplicationStatus.APPLIED
    assert applied.mode_application_attempt == first.attempt

    failed = store.transition(
        first.session_id,
        SessionStatus.FAILED,
        expected_status=SessionStatus.RUNNING,
        expected_attempt=first.attempt,
        expected_lease_token="lease-one",
        lease_token="",
    )
    retrying = store.transition(failed.session_id, SessionStatus.RETRYING)
    second = store.claim(retrying.session_id, pid=43, lease_token="lease-two")
    assert second.mode_name == ""
    assert second.mode_application_status is ModeApplicationStatus.PENDING
    assert second.requested_mode_name == "YOLO"

    with pytest.raises(InvalidSessionTransition, match="stale"):
        store.record_mode_application(
            second.session_id,
            requested_mode_name="YOLO",
            effective_mode_name="Yolo",
            attempt=first.attempt,
            lease_token="lease-one",
        )
    assert store.get(second.session_id).mode_application_status is ModeApplicationStatus.PENDING


def test_claim_and_orphan_recovery(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    claimed = store.claim("session-1", pid=99, lease_token="worker")
    assert claimed.status is SessionStatus.RUNNING
    assert claimed.attempt == 1
    orphaned = store.mark_orphaned("session-1")
    assert orphaned.status is SessionStatus.ORPHANED
    resumed = store.transition("session-1", SessionStatus.STARTING)
    assert resumed.status is SessionStatus.STARTING


def test_stale_recovery_marks_dead_worker_immediately(tmp_path: Path, monkeypatch) -> None:
    from agenthicc.background.supervisor import _WorkerProcessState

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.claim("session-1", pid=9876, lease_token="worker")
    supervisor = BackgroundSupervisor(store)
    monkeypatch.setattr(
        supervisor, "_worker_process_state", lambda _session: _WorkerProcessState.DEAD
    )
    changed = supervisor.recover_stale(stale_after_s=10_000)
    assert [item.session_id for item in changed] == ["session-1"]
    assert store.get("session-1").status is SessionStatus.ORPHANED


def test_delete_moves_exact_artifacts_to_trash_and_restores(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING)
    store.transition("session-1", SessionStatus.COMPLETED)
    deleted = store.delete("session-1")
    assert deleted.status is SessionStatus.DELETED
    assert not (tmp_path / "sessions" / "session-1").exists()
    assert store.get("session-1", include_deleted=True).status is SessionStatus.DELETED
    restored = store.restore_deleted("session-1")
    assert restored.status is SessionStatus.COMPLETED
    assert (tmp_path / "sessions" / "session-1" / "conversation.jsonl").exists()


def test_active_delete_requires_force_and_tombstone_survives_replay(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    with pytest.raises(InvalidSessionTransition):
        store.delete("session-1")
    store.delete("session-1", force=True)
    assert store.list() == []
    rebuilt = BackgroundStore(tmp_path / "background")
    assert rebuilt.list(include_deleted=True)[0].status is SessionStatus.DELETED


def test_store_archive_and_restore(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING)
    store.transition("session-1", SessionStatus.COMPLETED)
    archived = store.archive("session-1")
    assert archived.status is SessionStatus.ARCHIVED
    assert store.list(include_archived=False) == []
    assert store.restore_archive("session-1").status is SessionStatus.COMPLETED


async def _hub(store: BackgroundStore, tmp_path: Path, supervisor: object | None = None) -> object:
    from rich.console import Console

    from agenthicc.tui.workspace.agents_hub import AgentsHub
    from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

    hub = AgentsHub(
        Console(record=True, width=120),
        store=store,
        supervisor=supervisor or BackgroundSupervisor(store),  # type: ignore[arg-type]
        prefs=HubPrefs(path=tmp_path / "hub.json"),
    )
    await hub.start()
    await hub.settle()
    return hub


def _frame(hub: object, width: int = 120, height: int = 30) -> str:
    from rich.console import Console

    from agenthicc.tui.workspace.agents_hub import AgentsHub

    assert isinstance(hub, AgentsHub)
    console = Console(record=True, width=width)
    console.print(hub.render(width, height))
    return console.export_text()


async def test_hub_approval_action_updates_waiting_session(tmp_path: Path) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.agents_hub import AgentsHub

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING)
    store.transition(
        "session-1",
        SessionStatus.WAITING_APPROVAL,
        approval_request="write_file",
    )
    hub = await _hub(store, tmp_path)
    assert isinstance(hub, AgentsHub)
    try:
        hub.handle_key(Key.CHAR, "y")
        await hub.settle()
        assert store.get("session-1").approval_decision is True
    finally:
        await hub.stop()


async def test_hub_scrolls_long_lists_and_enter_attaches_the_selected_agent(
    tmp_path: Path,
) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.agents_hub import AgentsHub, HubResult

    store = BackgroundStore(tmp_path / "background")
    for index in range(5):
        session_id = f"session-{index}"
        store.create(_session(tmp_path, session_id).evolve(title=session_id))
    hub = await _hub(store, tmp_path)
    assert isinstance(hub, AgentsHub)
    try:
        order = [row.session_id for row in hub.rows if row.selectable]
        first = _frame(hub, height=6)  # tab bar, header, three rows, footer
        assert order[0] in first and order[-1] not in first
        hub.handle_key(Key.PAGE_DOWN, "")
        selected = hub.selected_session_id
        assert selected is not None and order.index(selected) >= 2  # moved by a screen
        assert selected in _frame(hub, height=6)  # and stays in view
        hub.handle_key(Key.END, "")
        assert hub.handle_key(Key.ENTER, "") is None
        assert hub.detail_session_id == order[-1]
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        assert hub.attach_result == HubResult("attach", order[-1])
    finally:
        await hub.stop()


def test_supervisor_submit_writes_private_request_and_enforces_limit(
    tmp_path: Path, monkeypatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, max_workers=1, artifact_root=tmp_path / "sessions")
    fake_process = SimpleNamespace(pid=1234)

    def inspect_before_launch(*args: object, **kwargs: object) -> SimpleNamespace:
        durable = store.list()[0]
        assert durable.requested_mode_name == "Yolo"
        assert durable.mode_name == ""
        assert durable.mode_application_status is ModeApplicationStatus.PENDING
        return fake_process

    monkeypatch.setattr("agenthicc.background.supervisor.subprocess.Popen", inspect_before_launch)
    first = supervisor.submit(
        intent="first",
        cwd=str(tmp_path),
        run_id="run-first",
        detached_goal=True,
        mode_name="Yolo",
    )
    assert first.status is SessionStatus.QUEUED
    assert first.worker_pid == 1234
    assert first.detached_goal is True
    assert first.requested_mode_name == "Yolo"
    assert first.mode_name == ""
    assert first.mode_application_status is ModeApplicationStatus.PENDING
    request = json.loads(
        (tmp_path / "background" / "requests" / f"{first.session_id}.json").read_text()
    )
    assert request["intent"] == "first"
    assert request["run_id"] == "run-first"
    assert request["detached_goal"] is True
    assert request["mode_name"] == "Yolo"
    assert request["dangerously_skip_permissions"] is False
    assert "first" not in " ".join(supervisor._worker_command(Path("request.json")))
    with pytest.raises(RuntimeError, match="limit"):
        supervisor.submit(intent="second", cwd=str(tmp_path))


@pytest.mark.parametrize(
    ("persisted_mode", "explicit_mode", "expected_request_mode"),
    ((None, None, "Yolo"), ("Plan", None, None), ("Plan", "Yolo", "Yolo")),
)
def test_resume_preserves_unconsumed_mode_but_respects_persisted_or_explicit_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persisted_mode: str | None,
    explicit_mode: str | None,
    expected_request_mode: str | None,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    session = _session(tmp_path, "mode-resume")
    store.create(session)
    store.transition(session.session_id, SessionStatus.STARTING)
    store.transition(session.session_id, SessionStatus.ORPHANED)
    supervisor._write_request(
        BackgroundRequest(
            session_id=session.session_id,
            workflow_name=session.workflow_name,
            intent=session.intent,
            cwd=session.cwd,
            mode_name="Yolo",
        )
    )
    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log.load_session_mode",
        lambda _session_id: persisted_mode,
    )
    captured: list[BackgroundRequest] = []

    def launch(request: BackgroundRequest, resumed: BackgroundSession) -> BackgroundSession:
        captured.append(request)
        supervisor._write_request(request)
        return resumed

    monkeypatch.setattr(supervisor, "_launch", launch)

    supervisor.resume(session.session_id, mode_name=explicit_mode)

    assert captured[0].mode_name == expected_request_mode
    persisted_request = BackgroundRequest.from_mapping(
        json.loads(supervisor._request_path(session.session_id).read_text(encoding="utf-8"))
    )
    assert persisted_request.mode_name == expected_request_mode


def test_resume_does_not_confuse_placeholder_safe_metadata_with_applied_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    session = _session(tmp_path, "pending-yolo").evolve(requested_mode_name="YOLO")
    store.create(session)
    supervisor._write_request(
        BackgroundRequest(
            session_id=session.session_id,
            workflow_name=session.workflow_name,
            intent=session.intent,
            cwd=session.cwd,
            mode_name="YOLO",
        )
    )
    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log.load_session_mode",
        lambda _session_id: "Safe",
    )

    assert supervisor._mode_name_for_resume(session.session_id, None) == "YOLO"
    assert supervisor._mode_name_for_resume(session.session_id, "Plan") == "Plan"


def test_supervisor_enforces_per_project_limit(tmp_path: Path, monkeypatch) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(
        store,
        max_workers=3,
        max_workers_per_project=1,
        artifact_root=tmp_path / "sessions",
    )
    monkeypatch.setattr(
        "agenthicc.background.supervisor.subprocess.Popen",
        lambda *a, **kw: SimpleNamespace(pid=1234),
    )
    supervisor.submit(intent="first", cwd=str(tmp_path))
    with pytest.raises(RuntimeError, match="project"):
        supervisor.submit(intent="second", cwd=str(tmp_path))


def test_supervisor_cancel_archive_delete_and_restore(tmp_path: Path, monkeypatch) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    monkeypatch.setattr(
        "agenthicc.background.supervisor.subprocess.Popen",
        lambda *a, **kw: SimpleNamespace(pid=1234),
    )
    monkeypatch.setattr(supervisor, "_alive", lambda pid: False)
    monkeypatch.setattr(supervisor, "_terminate", lambda pid: None)
    session = supervisor.submit(intent="work", cwd=str(tmp_path))
    cancelled = supervisor.cancel(session.session_id)
    assert cancelled.status is SessionStatus.CANCELLED
    archived = supervisor.archive(session.session_id)
    assert archived.status is SessionStatus.ARCHIVED
    resumed = supervisor.resume(session.session_id)
    assert resumed.status is SessionStatus.STARTING


def test_detached_cancel_preserves_worker_identity_and_exit_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    session = _session(tmp_path).evolve(detached_goal=True, run_id="run-detached")
    store.create(session)
    store.transition(session.session_id, SessionStatus.STARTING)
    store.transition(
        session.session_id,
        SessionStatus.RUNNING,
        worker_pid=4321,
        lease_token="worker-lease",
    )
    monkeypatch.setattr(supervisor, "_terminate", lambda _pid: None)
    monkeypatch.setattr(supervisor, "_alive", lambda _pid: False)

    cancelled = supervisor.cancel(session.session_id)

    assert cancelled.status is SessionStatus.CANCELLED
    assert cancelled.worker_pid == 4321
    assert cancelled.worker_exit_code == 130
    assert cancelled.worker_exit_reason == "cancelled"
    assert cancelled.exit_reason == "cancelled"
    assert cancelled.worker_finished_at is not None
    assert sum(event.get("event_type") == "worker_exited" for event in store._read_events()) == 1


def test_stale_recovery_does_not_trust_a_reused_pid(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    # The current test process is alive, but it is not the worker represented
    # by the persisted session.  A liveness-only check would incorrectly keep
    # this session running after a PID reuse.
    store.claim("session-1", pid=os.getpid(), lease_token="worker")
    supervisor = BackgroundSupervisor(store)

    changed = supervisor.recover_stale(stale_after_s=10_000)

    assert [item.session_id for item in changed] == ["session-1"]
    assert store.get("session-1").status is SessionStatus.ORPHANED


def test_supervisor_foreground_attach_stops_worker_and_releases_lease(
    tmp_path: Path, monkeypatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, artifact_root=tmp_path / "sessions")
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition(
        "session-1",
        SessionStatus.RUNNING,
        worker_pid=4321,
        lease_token="worker-lease",
    )
    terminated: list[int] = []
    monkeypatch.setattr(supervisor, "_terminate", terminated.append)
    monkeypatch.setattr(supervisor, "_alive", lambda pid: False)

    released = supervisor.attach_foreground("session-1")

    assert released.status is SessionStatus.CANCELLED
    assert released.worker_pid is None
    assert released.lease_token == ""
    assert released.cancellation_reason == "foreground handoff requested"
    assert "ready for foreground" in released.latest_activity
    assert terminated == [4321]


def test_supervisor_foreground_attach_refuses_live_worker_after_grace(
    tmp_path: Path, monkeypatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, cancel_grace_s=0.1)
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING, worker_pid=4321)
    monkeypatch.setattr(supervisor, "_terminate", lambda pid: None)
    monkeypatch.setattr(supervisor, "_alive", lambda pid: True)

    with pytest.raises(RuntimeError, match="did not stop"):
        supervisor.attach_foreground("session-1")
    assert store.get("session-1").status is SessionStatus.CANCELLING


async def test_hub_keyboard_delete_restore_and_help(tmp_path: Path) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.agents_hub import AgentsHub

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING)
    store.transition("session-1", SessionStatus.COMPLETED)
    hub = await _hub(store, tmp_path)
    assert isinstance(hub, AgentsHub)
    try:
        hub.handle_key(Key.CHAR, "?")
        assert hub.help_visible is True
        hub.handle_key(Key.CHAR, "?")
        assert hub.help_visible is False
        hub.handle_key(Key.CTRL_X, "")
        await hub.settle()
        assert store.get("session-1", include_deleted=True).status is SessionStatus.DELETED
        assert all(row.session_id != "session-1" for row in hub.rows)
        store.restore_deleted("session-1")
        assert store.get("session-1").status is SessionStatus.COMPLETED
        hub._request_load("test", force=True)
        await hub.settle()
        frame = _frame(hub)
        assert "Agents" in frame and "Background Sessions" not in frame
        assert "session-1" in [row.session_id for row in hub.rows]
    finally:
        await hub.stop()


async def test_hub_ctrl_x_deletes_without_confirmation(tmp_path: Path) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.agents_hub import AgentsHub

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING)
    store.transition("session-1", SessionStatus.COMPLETED)
    hub = await _hub(store, tmp_path)
    assert isinstance(hub, AgentsHub)
    try:
        hub.handle_key(Key.CTRL_X, "")
        await hub.settle()
        assert store.get("session-1", include_deleted=True).status is SessionStatus.DELETED
        assert hub.selected_row() is None
    finally:
        await hub.stop()


async def test_hub_filters_pauses_and_shows_a_redacted_transcript(tmp_path: Path) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.agents_hub import AgentsHub

    session = _session(tmp_path).evolve(
        status=SessionStatus.RUNNING, phase_history=("plan", "execute"), current_phase="execute"
    )
    (Path(session.artifact_dir) / "conversation.jsonl").write_text(
        '{"kind":"assistant_message","payload":{"text":"Bearer sk-ant-1234567890123456"}}\n',
        encoding="utf-8",
    )
    store = BackgroundStore(tmp_path / "background")
    store.create(session)
    hub = await _hub(store, tmp_path)
    assert isinstance(hub, AgentsHub)
    try:
        hub.set_search("agent:demo")
        assert hub.selected_session_id == "session-1"
        hub.set_search("agent:other")
        assert hub.selected_row() is None
        hub.set_search("")
        hub.handle_key(Key.CHAR, "z")
        assert hub.paused is True
        hub.handle_key(Key.CHAR, "z")
        assert hub.paused is False
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        hub.handle_key(Key.CHAR, "[")
        hub.handle_key(Key.CHAR, "]")
        output = _frame(hub, height=40)
        assert "Bearer" not in output and "sk-ant-" not in output
        assert "assistant" in output
        assert "plan ✓ → execute ●" in output
    finally:
        await hub.stop()


def test_foreground_background_aliases_are_registered_for_trigger_picker() -> None:
    from agenthicc.commands.builtins import BUILTIN_COMMANDS

    command = next(item for item in BUILTIN_COMMANDS if item.name == "/background")
    assert "/bg" in command.aliases
