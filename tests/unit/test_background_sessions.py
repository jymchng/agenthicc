"""Unit coverage for PRD-141 background lifecycle, storage, and manager UX."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    InvalidSessionTransition,
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
    assert not legal_transition(SessionStatus.COMPLETED, SessionStatus.RUNNING)
    assert not legal_transition(SessionStatus.DELETED, SessionStatus.RUNNING)


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
    )
    restored = BackgroundSession.from_mapping(session.to_dict())
    assert restored == session
    assert restored.status is SessionStatus.RUNNING
    assert restored.labels == ("important", "project")
    assert restored.detached_goal is True
    assert restored.worker_pid == 42
    assert restored.worker_exit_reason == "workflow_complete"
    assert restored.mode_name == "Yolo"
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
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.claim("session-1", pid=9876, lease_token="worker")
    supervisor = BackgroundSupervisor(store)
    monkeypatch.setattr(supervisor, "_alive", lambda pid: False)
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


def test_manager_approval_action_updates_waiting_session(tmp_path: Path) -> None:
    from rich.console import Console

    from agenthicc.tui.workspace.background_manager import BackgroundManager

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING)
    store.transition(
        "session-1",
        SessionStatus.WAITING_APPROVAL,
        approval_request="write_file",
    )
    manager = BackgroundManager(Console(), store=store, supervisor=BackgroundSupervisor(store))
    manager.handle_key("CHAR", "y")
    assert store.get("session-1").approval_decision is True


def test_manager_paginates_sessions_and_enter_resumes_selected_record(tmp_path: Path) -> None:
    from rich.console import Console

    from agenthicc.tui.workspace.background_manager import BackgroundManager, ManagerResult

    store = BackgroundStore(tmp_path / "background")
    for index in range(5):
        session_id = f"session-{index}"
        store.create(_session(tmp_path, session_id).evolve(title=session_id))

    console = Console(record=True)
    manager = BackgroundManager(
        console,
        store=store,
        supervisor=SimpleNamespace(recover_stale=lambda: []),
        page_size=2,
    )
    manager.refresh(force=True)

    console.print(manager.render())
    first_page = console.export_text()
    assert "page 1/3" in first_page
    assert "session-0" in first_page
    assert "session-2" not in first_page

    manager.handle_key("PAGE_DOWN")
    console.print(manager.render())
    second_page = console.export_text()
    assert "page 2/3" in second_page
    assert "session-2" in second_page

    manager.handle_key("END")
    assert manager.handle_key("ENTER") is None
    assert manager._detail_session_id == "session-4"
    result = manager.handle_key("ENTER")
    assert result == ManagerResult("attach", "session-4")


def test_supervisor_submit_writes_private_request_and_enforces_limit(
    tmp_path: Path, monkeypatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, max_workers=1, artifact_root=tmp_path / "sessions")
    fake_process = SimpleNamespace(pid=1234)
    monkeypatch.setattr(
        "agenthicc.background.supervisor.subprocess.Popen", lambda *a, **kw: fake_process
    )
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


def test_manager_keyboard_delete_restore_and_help(tmp_path: Path, capsys) -> None:
    from rich.console import Console

    from agenthicc.tui.workspace.background_manager import BackgroundManager

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING)
    store.transition("session-1", SessionStatus.COMPLETED)
    manager = BackgroundManager(Console(), store=store, supervisor=BackgroundSupervisor(store))
    manager.handle_key("CHAR", "?")
    assert manager.help_visible is True
    manager.handle_key("CHAR", "?")
    manager.handle_key("CHAR", "\x18")
    deadline = time.monotonic() + 2.0
    while manager._deletion_thread is not None and time.monotonic() < deadline:
        manager._poll_async_delete()
        time.sleep(0.01)
    manager._poll_async_delete()
    assert store.get("session-1", include_deleted=True).status is SessionStatus.DELETED
    manager.handle_key("CHAR", "t")
    assert manager.include_deleted is True
    manager.handle_key("CHAR", "u")
    assert store.get("session-1").status is SessionStatus.COMPLETED
    console = Console(record=True)
    console.print(manager.render())
    assert "Background Sessions" in console.export_text()


def test_manager_keyboard_ctrl_x_reader_key_deletes_without_confirmation(tmp_path: Path) -> None:
    from rich.console import Console

    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.background_manager import BackgroundManager

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path))
    store.transition("session-1", SessionStatus.STARTING)
    store.transition("session-1", SessionStatus.RUNNING)
    store.transition("session-1", SessionStatus.COMPLETED)
    manager = BackgroundManager(Console(), store=store, supervisor=BackgroundSupervisor(store))

    manager.handle_key(Key.CTRL_X)
    deadline = time.monotonic() + 2.0
    while manager._deletion_thread is not None and time.monotonic() < deadline:
        manager._poll_async_delete()
        time.sleep(0.01)
    manager._poll_async_delete()
    assert store.get("session-1", include_deleted=True).status is SessionStatus.DELETED
    assert manager.selected_session is None


def test_manager_filters_pause_and_renders_bounded_redacted_activity(tmp_path: Path) -> None:
    from rich.console import Console

    from agenthicc.tui.workspace.background_manager import BackgroundManager

    session = _session(tmp_path).evolve(phase_history=("plan", "execute"))
    (Path(session.artifact_dir) / "conversation.jsonl").write_text(
        '{"kind":"assistant_message","payload":{"text":"Bearer sk-ant-1234567890123456"}}\n',
        encoding="utf-8",
    )
    store = BackgroundStore(tmp_path / "background")
    store.create(session)
    console = Console(record=True)
    manager = BackgroundManager(console, store=store)
    manager.set_filters(workflow="demo")
    assert manager.selected_session is not None
    manager.handle_key("CHAR", " ")
    assert manager.paused is True
    manager.handle_key("CHAR", "[")
    assert manager.activity_offset == 6
    manager.handle_key("CHAR", "]")
    assert manager.activity_offset == 0
    console.print(manager.render())
    output = console.export_text()
    assert "Bearer" not in output and "sk-ant-" not in output
    assert "assistant_message" in output
    assert "plan → execute" in output


def test_foreground_background_aliases_are_registered_for_trigger_picker() -> None:
    from agenthicc.background.integration import install_tui_handoff
    from agenthicc.commands.builtins import BUILTIN_COMMANDS

    install_tui_handoff()
    command = next(item for item in BUILTIN_COMMANDS if item.name == "/background")
    assert "/bg" in command.aliases
