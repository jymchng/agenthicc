"""Due-driven maintenance: only due work, and no work when nothing is due (AGX-04)."""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import BackgroundSession, BackgroundStore, BackgroundSupervisor
from agenthicc.background.input_inbox import BackgroundInputInbox
from agenthicc.background.maintenance import (
    MaintenanceOutcome,
    MaintenanceScheduler,
    MaintenanceTrigger,
)
from agenthicc.background.model import QueueDispatchState, SessionStatus
from agenthicc.background.settings import AgentsHubSettings, BackgroundSettings
from agenthicc.background.supervisor import _WorkerProcessState
from agenthicc.background.watch import FileWatcher
from agenthicc.tui.workspace.agents_hub import AgentsHub
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, session_id: str, **changes: object) -> BackgroundSession:
    return BackgroundSession.create(
        session_id,
        title=session_id,
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="maintenance",
        artifact_dir=str(tmp_path / "artifacts" / session_id),
        now=1.0,
    ).evolve(**changes)


def _running(tmp_path: Path, session_id: str, *, pid: int, last_active: float) -> BackgroundSession:
    return _session(
        tmp_path,
        session_id,
        status=SessionStatus.RUNNING,
        worker_pid=pid,
        attempt=1,
        lease_token=f"lease-{session_id}",
        last_active=last_active,
    )


class _Probes:
    """Record worker probes and answer them from a table."""

    def __init__(self, supervisor: BackgroundSupervisor, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.states: dict[str, _WorkerProcessState] = {}

        def probe(session: BackgroundSession) -> _WorkerProcessState:
            self.calls.append(session.session_id)
            return self.states.get(session.session_id, _WorkerProcessState.LIVE)

        monkeypatch.setattr(supervisor, "_worker_process_state", probe)


def _log_size(store: BackgroundStore) -> int:
    return store.events_path.stat().st_size


def test_with_nothing_due_a_pass_probes_nothing_and_appends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(50):
        store.create(_session(tmp_path, f"done-{index}", status=SessionStatus.COMPLETED))
    now = time.time()
    running = [f"run-{index}" for index in range(3)]
    for offset, session_id in enumerate(running):
        store.create(_running(tmp_path, session_id, pid=4_000 + offset, last_active=now + offset))
    supervisor = BackgroundSupervisor(store)
    probes = _Probes(supervisor, monkeypatch)
    scheduler = MaintenanceScheduler(supervisor, stale_after_s=30.0)

    first = scheduler.run(MaintenanceTrigger(full=True))
    assert sorted(probes.calls) == running
    assert first.changed == ()
    assert first.workers == {"run-0": 4_000, "run-1": 4_001, "run-2": 4_002}

    probes.calls.clear()
    size = _log_size(store)
    watched = frozenset(first.workers)
    for trigger in (
        MaintenanceTrigger(exit_watched=watched),
        MaintenanceTrigger(exit_watched=watched, registry_changed=True),
    ):
        outcome = scheduler.run(trigger)
        assert probes.calls == []
        assert outcome.changed == ()
        assert outcome.probes == 0
        assert _log_size(store) == size
        # The next work is the earliest heartbeat deadline.
        assert outcome.next_due_at == pytest.approx(now + 30.0)


def test_an_expired_heartbeat_is_probed_once_and_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_running(tmp_path, "quiet", pid=4_100, last_active=time.time() - 60.0))
    supervisor = BackgroundSupervisor(store)
    probes = _Probes(supervisor, monkeypatch)
    scheduler = MaintenanceScheduler(supervisor, stale_after_s=30.0)
    watched = frozenset({"quiet"})

    flagged = scheduler.run(MaintenanceTrigger(exit_watched=watched))
    assert probes.calls == ["quiet"]
    assert [item.heartbeat_stale for item in flagged.changed] == [True]
    # Flagging is not a heartbeat.
    assert flagged.changed[0].last_active == store.get("quiet").last_active < time.time() - 59

    probes.calls.clear()
    again = scheduler.run(MaintenanceTrigger(exit_watched=watched, registry_changed=True))
    assert probes.calls == []
    assert again.changed == ()
    # A flagged worker waits for its heartbeat or its exit, not a timer.
    assert again.next_due_at is None


def test_a_resumed_heartbeat_clears_the_stale_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(
        _running(tmp_path, "back", pid=4_200, last_active=time.time()).evolve(heartbeat_stale=True)
    )
    supervisor = BackgroundSupervisor(store)
    _Probes(supervisor, monkeypatch)

    outcome = MaintenanceScheduler(supervisor).run(MaintenanceTrigger())

    assert [item.heartbeat_stale for item in outcome.changed] == [False]


def test_an_exited_worker_is_orphaned_at_once_and_no_longer_watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_running(tmp_path, "crashed", pid=4_300, last_active=time.time()))
    supervisor = BackgroundSupervisor(store)
    probes = _Probes(supervisor, monkeypatch)
    scheduler = MaintenanceScheduler(supervisor)
    first = scheduler.run(MaintenanceTrigger())
    assert first.workers == {"crashed": 4_300}

    probes.calls.clear()
    probes.states["crashed"] = _WorkerProcessState.DEAD
    exited = scheduler.run(
        MaintenanceTrigger(exited=frozenset({"crashed"}), exit_watched=frozenset({"crashed"}))
    )

    assert probes.calls == ["crashed"]
    assert [item.status for item in exited.changed] == [SessionStatus.ORPHANED]
    assert exited.workers == {}


def test_a_new_worker_attempt_is_probed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_running(tmp_path, "retried", pid=4_400, last_active=time.time()))
    supervisor = BackgroundSupervisor(store)
    probes = _Probes(supervisor, monkeypatch)
    scheduler = MaintenanceScheduler(supervisor)
    scheduler.run(MaintenanceTrigger())
    watched = frozenset({"retried"})
    scheduler.run(MaintenanceTrigger(exit_watched=watched))
    assert probes.calls == ["retried"]

    store.update("retried", worker_pid=4_401, attempt=2)
    outcome = scheduler.run(MaintenanceTrigger(exit_watched=watched))

    assert probes.calls == ["retried", "retried"]
    assert outcome.workers == {"retried": 4_401}


def test_a_worker_without_exit_notification_is_polled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_running(tmp_path, "polled", pid=4_500, last_active=time.time()))
    supervisor = BackgroundSupervisor(store)
    probes = _Probes(supervisor, monkeypatch)
    scheduler = MaintenanceScheduler(supervisor, poll_interval_s=5.0)

    first = scheduler.run(MaintenanceTrigger())
    scheduler.run(MaintenanceTrigger())
    assert probes.calls == ["polled"]
    # Polling is the caller's to schedule; the deadline is the heartbeat's.
    assert first.next_due_at == pytest.approx(store.get("polled").last_active + 30.0)

    scheduler.poll_interval_s = 0.0
    scheduler.run(MaintenanceTrigger())
    assert probes.calls == ["polled", "polled"]
    # A watched worker is never polled.
    scheduler.run(MaintenanceTrigger(exit_watched=frozenset({"polled"})))
    assert probes.calls == ["polled", "polled"]


def test_the_windows_probe_never_sends_a_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running(tmp_path, "windows", pid=4_600, last_active=time.time())
    supervisor = BackgroundSupervisor(BackgroundStore(tmp_path / "background"))

    def no_signal(_pid: int, _signal: int) -> None:
        raise AssertionError("os.kill(pid, 0) is CTRL_C_EVENT on Windows")

    with monkeypatch.context() as patch:
        patch.setattr(os, "name", "nt")
        patch.setattr(os, "kill", no_signal)
        state = supervisor._worker_process_state(session)
    assert state is _WorkerProcessState.UNKNOWN


# ── deferred input ───────────────────────────────────────────────────────────


def _completed_with_input(
    store: BackgroundStore, tmp_path: Path, session_id: str, text: str | None
) -> BackgroundSession:
    session = store.create(
        _session(
            tmp_path,
            session_id,
            status=SessionStatus.COMPLETED,
            attempt=1,
            lease_token=f"lease-{session_id}",
        )
    )
    if text is not None:
        BackgroundInputInbox(store).enqueue_deferred(
            session_id,
            text,
            expected_attempt=session.attempt,
            expected_lease_token=session.lease_token,
            message_id=f"message-{session_id}",
        )
    return session


class _Launches:
    def __init__(self, supervisor: BackgroundSupervisor, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sessions: list[str] = []

        def launch(_request: object, record: BackgroundSession) -> BackgroundSession:
            self.sessions.append(record.session_id)
            return record

        monkeypatch.setattr(supervisor, "_launch", launch)


class _ReceiptReads:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sessions: list[str] = []
        original = BackgroundInputInbox.receipts

        def receipts(inbox: BackgroundInputInbox, session_id: str):  # type: ignore[no-untyped-def]
            self.sessions.append(session_id)
            return original(inbox, session_id)

        monkeypatch.setattr(BackgroundInputInbox, "receipts", receipts)


def test_deferred_input_is_found_from_the_inbox_without_reading_every_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(20):
        _completed_with_input(store, tmp_path, f"quiet-{index}", None)
    _completed_with_input(store, tmp_path, "waiting", "please continue")
    supervisor = BackgroundSupervisor(store)
    launches = _Launches(supervisor, monkeypatch)
    reads = _ReceiptReads(monkeypatch)

    outcome = MaintenanceScheduler(supervisor).run(MaintenanceTrigger())

    assert launches.sessions == ["waiting"]
    assert [item.status for item in outcome.changed] == [SessionStatus.STARTING]
    assert set(reads.sessions) == {"waiting"}


def test_an_inbox_with_nothing_pending_is_not_read_again_until_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = _completed_with_input(store, tmp_path, "answered", "first")
    inbox = BackgroundInputInbox(store)
    # An earlier attempt consumed the message.
    inbox._append(
        inbox._paths("answered")[0],
        {"action": "completed", "message_id": "message-answered", "state": "completed"},
    )
    supervisor = BackgroundSupervisor(store)
    launches = _Launches(supervisor, monkeypatch)
    scheduler = MaintenanceScheduler(supervisor)
    reads = _ReceiptReads(monkeypatch)

    scheduler.run(MaintenanceTrigger())
    assert reads.sessions == ["answered"]
    scheduler.run(MaintenanceTrigger(registry_changed=True))
    assert reads.sessions == ["answered"]

    inbox.enqueue_deferred(
        "answered",
        "second",
        expected_attempt=session.attempt,
        expected_lease_token=session.lease_token,
        message_id="message-second",
    )
    scheduler.run(MaintenanceTrigger(inbox_changed=frozenset({"answered.jsonl"})))
    assert launches.sessions == ["answered"]


def test_a_failed_input_start_backs_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = BackgroundStore(tmp_path / "background")
    _completed_with_input(store, tmp_path, "unlucky", "try me")
    supervisor = BackgroundSupervisor(store)
    attempts = 0

    def write_request(_request: object) -> Path:
        nonlocal attempts
        attempts += 1
        raise OSError("disk full")

    monkeypatch.setattr(supervisor, "_write_request", write_request)
    scheduler = MaintenanceScheduler(supervisor, poll_interval_s=5.0)

    first = scheduler.run(MaintenanceTrigger())
    assert attempts == 1
    assert first.next_due_at == pytest.approx(time.time() + 5.0, abs=1.0)
    scheduler.run(MaintenanceTrigger(registry_changed=True))
    assert attempts == 1

    scheduler._input_retry_at["unlucky"] = time.time() - 1.0
    second = scheduler.run(MaintenanceTrigger())
    assert attempts == 2
    # The delay doubles with each failure.
    assert second.next_due_at == pytest.approx(time.time() + 10.0, abs=1.0)


def test_input_blocked_on_capacity_starts_once_a_slot_frees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_running(tmp_path, "busy", pid=4_700, last_active=time.time()))
    _completed_with_input(store, tmp_path, "waiting", "after busy")
    supervisor = BackgroundSupervisor(store, max_workers=1)
    _Probes(supervisor, monkeypatch)
    launches = _Launches(supervisor, monkeypatch)
    scheduler = MaintenanceScheduler(supervisor)

    scheduler.run(MaintenanceTrigger())
    assert launches.sessions == []

    store.update("busy", status=SessionStatus.COMPLETED)
    scheduler.run(MaintenanceTrigger(registry_changed=True))
    assert launches.sessions == ["waiting"]


# ── queue dispatch ───────────────────────────────────────────────────────────


class _Dispatches:
    def __init__(self, supervisor: BackgroundSupervisor, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0

        def dispatch_pending(*, now: float | None = None) -> list[BackgroundSession]:
            self.count += 1
            return []

        monkeypatch.setattr(supervisor, "dispatch_pending", dispatch_pending)


def test_dispatch_runs_for_queued_work_when_the_registry_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(store, queue_enabled=True)
    dispatches = _Dispatches(supervisor, monkeypatch)
    scheduler = MaintenanceScheduler(supervisor)

    scheduler.run(MaintenanceTrigger(registry_changed=True))
    assert dispatches.count == 0  # nothing queued

    store.create(_session(tmp_path, "queued", dispatch_state=QueueDispatchState.PENDING))
    scheduler.run(MaintenanceTrigger())
    assert dispatches.count == 0  # queued, but nothing changed
    scheduler.run(MaintenanceTrigger(registry_changed=True))
    assert dispatches.count == 1
    scheduler.run(MaintenanceTrigger(full=True))
    assert dispatches.count == 2


def test_an_expired_dispatch_lease_is_due(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = BackgroundStore(tmp_path / "background")
    claimed_at = time.time()
    store.create(
        _session(
            tmp_path,
            "claimed",
            status=SessionStatus.STARTING,
            dispatch_state=QueueDispatchState.CLAIMED,
            dispatch_claimed_at=claimed_at,
            last_active=claimed_at,
        )
    )
    supervisor = BackgroundSupervisor(store, queue_enabled=True, dispatch_lease_s=30.0)
    dispatches = _Dispatches(supervisor, monkeypatch)
    scheduler = MaintenanceScheduler(supervisor)

    waiting = scheduler.run(MaintenanceTrigger())
    assert dispatches.count == 0
    assert waiting.next_due_at == pytest.approx(claimed_at + 30.0)

    store.update("claimed", dispatch_claimed_at=claimed_at - 60.0)
    scheduler.run(MaintenanceTrigger())
    assert dispatches.count == 1


# ── configuration ────────────────────────────────────────────────────────────


def test_the_supervisor_applies_every_configured_limit(tmp_path: Path) -> None:
    settings = BackgroundSettings(
        max_workers=3,
        max_workers_per_project=2,
        cancel_grace_s=7.0,
        stale_after_s=12.0,
        wall_timeout_s=90.0,
        queue_enabled=True,
        dispatch_lease_s=45.0,
    )
    supervisor = BackgroundSupervisor.from_settings(BackgroundStore(tmp_path / "bg"), settings)

    assert (supervisor.max_workers, supervisor.max_workers_per_project) == (3, 2)
    assert supervisor.cancel_grace_s == 7.0
    assert supervisor.wall_timeout_s == 90.0
    assert supervisor.queue_enabled and supervisor.dispatch_lease_s == 45.0
    assert supervisor.stale_after_s == 12.0
    assert MaintenanceScheduler(supervisor).stale_after_s == 12.0


def test_recover_stale_is_one_full_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_running(tmp_path, "fresh", pid=4_800, last_active=time.time()))
    supervisor = BackgroundSupervisor(store)
    probes = _Probes(supervisor, monkeypatch)
    probes.states["fresh"] = _WorkerProcessState.DEAD

    changed = supervisor.recover_stale()

    assert probes.calls == ["fresh"]
    assert [item.status for item in changed] == [SessionStatus.ORPHANED]


# ── the screen's side ────────────────────────────────────────────────────────


def _screen(tmp_path: Path) -> AgentsHub:
    store = BackgroundStore(tmp_path / "background")
    return AgentsHub(
        Console(file=io.StringIO()),
        store=store,
        supervisor=BackgroundSupervisor(store),
        settings=AgentsHubSettings(maintenance_interval_s=5.0),
        prefs=HubPrefs(path=tmp_path / "hub.json"),
    )


def test_the_screen_polls_a_worker_it_cannot_watch(tmp_path: Path) -> None:
    manager = _screen(tmp_path)
    manager._watcher = FileWatcher(backend="stat")
    outcome = MaintenanceOutcome(changed=(), probes=1, next_due_at=None, workers={"w": 4_900})

    manager._apply_maintenance_outcome(outcome)

    assert manager._exit_watched == {}
    assert manager._maintenance_next_due == pytest.approx(time.monotonic() + 5.0, abs=0.5)


async def test_the_screen_watches_worker_exits_instead_of_polling(tmp_path: Path) -> None:
    manager = _screen(tmp_path)
    watcher = FileWatcher()
    manager._watcher = watcher
    try:
        outcome = MaintenanceOutcome(
            changed=(), probes=1, next_due_at=None, workers={"w": os.getpid()}
        )
        manager._apply_maintenance_outcome(outcome)
        assert manager._exit_watched == {"w": os.getpid()}
        assert manager._maintenance_next_due is None

        # A worker that is no longer reported stops being watched.
        manager._apply_maintenance_outcome(
            MaintenanceOutcome(changed=(), probes=0, next_due_at=None, workers={})
        )
        assert manager._exit_watched == {}
    finally:
        watcher.close()
