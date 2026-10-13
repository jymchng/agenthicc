"""Focused policy and lifecycle tests for PRD-225 background scheduling."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background.model import (
    BackgroundSession,
    QueueDispatchState,
    QueuePriority,
    SessionStatus,
)
from agenthicc.background.input_inbox import BackgroundInputInbox
from agenthicc.background.queue import effective_priority_rank, fair_queue_order
from agenthicc.background.settings import BackgroundSettings
from agenthicc.background.store import BackgroundStore, InvalidSessionTransition
from agenthicc.background.supervisor import (
    BackgroundRequest,
    BackgroundSupervisor,
    QueueFullError,
)


def _queued(
    session_id: str,
    project: Path,
    *,
    priority: QueuePriority = QueuePriority.NORMAL,
    created_at: float = 100.0,
) -> BackgroundSession:
    return BackgroundSession.create(
        session_id,
        title=session_id,
        cwd=str(project),
        workflow_name="goal_flow",
        intent=f"intent {session_id}",
        now=created_at,
        priority=priority,
        dispatch_state=QueueDispatchState.PENDING,
    )


def _fake_popen(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def popen(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(pid=50_000 + len(calls))

    monkeypatch.setattr("agenthicc.background.supervisor.subprocess.Popen", popen)
    return calls


def test_queue_policy_ages_low_priority_and_rotates_projects(tmp_path: Path) -> None:
    old_low = _queued(
        "a-old-low",
        tmp_path / "a",
        priority=QueuePriority.LOW,
        created_at=100.0,
    )
    new_urgent = _queued(
        "a-new-urgent",
        tmp_path / "a",
        priority=QueuePriority.URGENT,
        created_at=590.0,
    )
    other_project = _queued("b-normal", tmp_path / "b", created_at=580.0)

    assert effective_priority_rank(old_low, now=600.0, aging_interval_s=100.0) == 5
    ordered = fair_queue_order(
        [new_urgent, old_low, other_project],
        now=600.0,
        aging_interval_s=100.0,
    )
    assert [item.session_id for item in ordered] == [
        "a-old-low",
        "b-normal",
        "a-new-urgent",
    ]

    rotated = fair_queue_order(
        [new_urgent, old_low, other_project],
        now=600.0,
        aging_interval_s=100.0,
        last_project=str(tmp_path / "a"),
    )
    assert rotated[0].cwd == str(tmp_path / "b")


def test_queue_admission_is_durable_bounded_and_cancellable_without_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches = _fake_popen(monkeypatch)
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(
        store,
        max_workers=1,
        max_workers_per_project=1,
        queue_enabled=True,
        max_queue_length=1,
    )

    first = supervisor.submit(intent="first", cwd=str(tmp_path / "project"))
    queued = supervisor.submit(
        intent="second",
        cwd=str(tmp_path / "project"),
        priority="low",
        idempotency_key="request-2",
    )

    assert first.status is SessionStatus.STARTING
    assert first.dispatch_state is QueueDispatchState.LAUNCHED
    assert queued.status is SessionStatus.QUEUED
    assert queued.dispatch_state is QueueDispatchState.PENDING
    assert queued.priority is QueuePriority.LOW
    assert supervisor.queue_positions() == {queued.session_id: 1}
    assert len(launches) == 1

    duplicate = supervisor.submit(
        intent="second",
        cwd=str(tmp_path / "project"),
        priority="low",
        idempotency_key="request-2",
    )
    assert duplicate.session_id == queued.session_id
    assert len(launches) == 1
    with pytest.raises(ValueError, match="different background request"):
        supervisor.submit(
            intent="different",
            cwd=str(tmp_path / "project"),
            idempotency_key="request-2",
        )
    with pytest.raises(QueueFullError, match="queue is full"):
        supervisor.submit(intent="third", cwd=str(tmp_path / "project"))

    cancelled = supervisor.cancel(queued.session_id)
    assert cancelled.status is SessionStatus.CANCELLED
    assert cancelled.dispatch_state is QueueDispatchState.CANCELLED
    assert len(launches) == 1
    reopened = BackgroundStore(store.root).get(queued.session_id, include_deleted=True)
    assert reopened.status is SessionStatus.CANCELLED


def test_handoff_uses_bounded_queue_when_worker_capacity_is_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches = _fake_popen(monkeypatch)
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(
        store,
        max_workers=1,
        max_workers_per_project=1,
        queue_enabled=True,
        max_queue_length=1,
    )
    first = supervisor.submit(intent="first", cwd=str(tmp_path / "project"))
    accepted = supervisor.submit(intent="queued", cwd=str(tmp_path / "project"))

    with pytest.raises(QueueFullError, match="queue is full"):
        supervisor.handoff(
            session_id="handoff-session",
            intent="foreground work",
            cwd=str(tmp_path / "other-project"),
            start=True,
        )
    handed_off = store.get("handoff-session")
    assert handed_off.status is SessionStatus.FAILED
    assert handed_off.worker_exit_reason == "queue_full"
    assert len(launches) == 1
    assert store.get(first.session_id).status is SessionStatus.STARTING
    assert store.get(accepted.session_id).status is SessionStatus.QUEUED

    cancelled = supervisor.cancel(accepted.session_id)
    resumed = supervisor.resume(handed_off.session_id)
    assert cancelled.status is SessionStatus.CANCELLED
    assert resumed.status is SessionStatus.QUEUED
    assert len(launches) == 1


def test_recovery_input_waits_in_queue_when_capacity_is_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches = _fake_popen(monkeypatch)
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(
        store,
        max_workers=1,
        max_workers_per_project=1,
        queue_enabled=True,
    )
    supervisor.submit(intent="active worker", cwd=str(tmp_path / "active"))
    completed = BackgroundSession.create(
        "completed-session",
        title="Completed session",
        cwd=str(tmp_path / "completed"),
        workflow_name="",
        intent="historical intent",
    ).evolve(status=SessionStatus.COMPLETED, completed_at=200.0)
    store.create(completed)

    receipt = supervisor._defer_and_reactivate_completed(
        BackgroundInputInbox(store),
        completed.session_id,
        "new recovery input",
        expected_attempt=0,
        expected_lease_token="",
        message_id="recovery-input-1",
    )

    queued = store.get(completed.session_id)
    assert receipt.state == "accepted"
    assert queued.status is SessionStatus.QUEUED
    assert queued.dispatch_state is QueueDispatchState.PENDING
    assert len(launches) == 1


def test_freed_capacity_dispatches_persisted_queue_after_store_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches = _fake_popen(monkeypatch)
    store = BackgroundStore(tmp_path / "background")
    supervisor = BackgroundSupervisor(
        store,
        max_workers=1,
        queue_enabled=True,
    )
    first = supervisor.submit(intent="first", cwd=str(tmp_path / "project"))
    second = supervisor.submit(intent="second", cwd=str(tmp_path / "project"))
    assert second.dispatch_state is QueueDispatchState.PENDING
    assert len(launches) == 1

    reopened = BackgroundStore(store.root)
    reopened.transition(
        first.session_id,
        SessionStatus.FAILED,
        expected_status=SessionStatus.STARTING,
        error="simulated worker completion",
    )
    recovered = BackgroundSupervisor(reopened, max_workers=1, queue_enabled=True)
    recovered.dispatch_pending()

    started = reopened.get(second.session_id, include_deleted=True)
    assert started.status is SessionStatus.STARTING
    assert started.dispatch_state is QueueDispatchState.LAUNCHED
    assert len(launches) == 2


def test_expired_dispatch_reservation_recovers_once_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches = _fake_popen(monkeypatch)
    store = BackgroundStore(tmp_path / "background")
    session = _queued("reserved-before-crash", tmp_path / "project", created_at=10.0)
    store.create(session)
    supervisor = BackgroundSupervisor(
        store,
        max_workers=1,
        queue_enabled=True,
        dispatch_lease_s=2.0,
    )
    request = BackgroundRequest(
        session_id=session.session_id,
        workflow_name="goal_flow",
        intent=session.intent,
        cwd=session.cwd,
        dispatch_token="persisted-dispatch-token",
    )
    supervisor._write_request(request)
    store.transition(
        session.session_id,
        SessionStatus.STARTING,
        expected_status=SessionStatus.QUEUED,
        expected_attempt=session.attempt,
        dispatch_state=QueueDispatchState.CLAIMED,
        dispatch_token=request.dispatch_token,
        dispatch_claimed_at=20.0,
    )

    recovered = BackgroundSupervisor(
        BackgroundStore(store.root),
        max_workers=1,
        queue_enabled=True,
        dispatch_lease_s=2.0,
    )
    recovered.dispatch_pending(now=23.0)
    recovered.dispatch_pending(now=24.0)

    launched = BackgroundStore(store.root).get(session.session_id)
    assert launched.status is SessionStatus.STARTING
    assert launched.dispatch_state is QueueDispatchState.LAUNCHED
    assert launched.dispatch_token == request.dispatch_token
    assert len(launches) == 1


def test_queue_cancel_and_claim_fence_prevent_unreserved_worker_execution(
    tmp_path: Path,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    pending = _queued("queued-session", tmp_path / "project")
    store.create(pending)

    with pytest.raises(InvalidSessionTransition, match="not been claimed"):
        store.claim(pending.session_id, pid=123, lease_token="worker-1")

    reserved = store.transition(
        pending.session_id,
        SessionStatus.STARTING,
        expected_status=SessionStatus.QUEUED,
        dispatch_state=QueueDispatchState.CLAIMED,
        dispatch_token="dispatch-token",
        dispatch_claimed_at=200.0,
    )
    with pytest.raises(InvalidSessionTransition, match="stale"):
        store.claim(
            reserved.session_id,
            pid=123,
            lease_token="worker-wrong",
            dispatch_token="wrong-token",
        )

    claimed = store.claim(
        reserved.session_id,
        pid=123,
        lease_token="worker-1",
        dispatch_token="dispatch-token",
    )
    assert claimed.status is SessionStatus.RUNNING
    assert claimed.dispatch_state is QueueDispatchState.STARTED
    with pytest.raises(InvalidSessionTransition, match="Cannot claim running"):
        store.claim(
            reserved.session_id,
            pid=124,
            lease_token="worker-2",
            dispatch_token="dispatch-token",
        )


def test_stale_recovery_does_not_orphan_pending_queue_entries(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_queued("pending", tmp_path / "project", created_at=1.0))
    supervisor = BackgroundSupervisor(store, max_workers=1, queue_enabled=True)

    changed = supervisor._recover_stale_records(store.list(), stale_after_s=0.1)

    assert changed == []
    assert store.get("pending").status is SessionStatus.QUEUED


def test_scheduler_serializes_concurrent_admission_across_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches = _fake_popen(monkeypatch)
    root = tmp_path / "background"
    supervisors = [
        BackgroundSupervisor(
            BackgroundStore(root),
            max_workers=1,
            max_workers_per_project=1,
            queue_enabled=True,
        )
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def submit(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            supervisors[index].submit(
                intent=f"task-{index}",
                cwd=str(tmp_path / f"project-{index}"),
            )
        except BaseException as exc:  # captured for assertion in the main thread
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    sessions = BackgroundStore(root).list()
    assert sum(item.dispatch_state is QueueDispatchState.PENDING for item in sessions) == 1
    assert sum(item.status is SessionStatus.STARTING for item in sessions) == 1
    assert len(launches) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_queue_length", 0),
        ("max_queue_length", 100_001),
        ("max_workers_per_user", 0),
        ("priority_aging_interval_s", 0),
        ("dispatch_lease_s", 0),
    ],
)
def test_queue_configuration_rejects_unbounded_or_invalid_limits(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValueError):
        BackgroundSettings.from_mapping({field: value})


def test_priority_changes_are_durable_audit_events(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = _queued("pending", tmp_path / "project")
    store.create(session)
    changed = store.change_priority(session.session_id, QueuePriority.URGENT)

    assert changed.priority is QueuePriority.URGENT
    assert len(changed.priority_history) == 1
    assert changed.priority_history[0].previous is QueuePriority.NORMAL
    assert changed.priority_history[0].current is QueuePriority.URGENT
    reopened = BackgroundStore(store.root).get(session.session_id)
    assert reopened.priority_history == changed.priority_history
