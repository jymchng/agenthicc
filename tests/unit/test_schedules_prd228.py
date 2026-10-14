from __future__ import annotations

from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from agenthicc.schedules import (
    CoalescePolicy,
    OccurrenceState,
    RecurrenceKind,
    ScheduleManager,
    ScheduleScheduler,
    ScheduleServiceAlreadyRunning,
    ScheduleServiceLease,
    ScheduleStore,
    first_run_at,
    format_timestamp,
    next_run_after,
    parse_interval,
    parse_recurrence,
)
from agenthicc.schedules.model import Occurrence, Schedule
from agenthicc.schedules.recurrence import CronExpression


class FakeClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def create_schedule(
    tmp_path: Path,
    clock: FakeClock,
    *,
    policy: CoalescePolicy = CoalescePolicy.SKIP,
    recurrence_kind: RecurrenceKind = RecurrenceKind.INTERVAL,
    recurrence_spec: str = "60s",
    mode: str = "Plan",
) -> tuple[ScheduleStore, ScheduleManager, Schedule]:
    store = ScheduleStore(tmp_path / "schedule-state")
    manager = ScheduleManager(store, clock=clock)
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    schedule = manager.create(
        repository_root=repository,
        workflow_name="test_workflow",
        prompt="  Keep this exact prompt.\n",
        profile_name="unit-profile",
        permission_ceiling=mode,
        recurrence_kind=recurrence_kind,
        recurrence_spec=recurrence_spec,
        timezone="UTC",
        coalesce_policy=policy,
        creator="test-user",
    )
    confirmation = format_timestamp(schedule.next_run_at, schedule.timezone)
    schedule = manager.resume(schedule.schedule_id, confirm_next_run=confirmation)
    return store, manager, schedule


def test_recurrence_validation_and_interval_missed_tick_coalescing() -> None:
    assert parse_interval("3h") == 10_800
    assert parse_recurrence("every:5m") == (RecurrenceKind.INTERVAL, "5m")
    with pytest.raises(ValueError, match="positive integer"):
        parse_interval("0s")
    with pytest.raises(ValueError, match="five fields"):
        first_run_at(RecurrenceKind.CRON, "0 9 * *", "UTC", 0)

    due = 1_000.0
    next_time = next_run_after(RecurrenceKind.INTERVAL, "60s", "UTC", previous=due, now=due + 305)
    assert next_time == due + 360


def test_cron_dst_skips_nonexistent_times_and_does_not_repeat_fold(tmp_path: Path) -> None:
    zone = "Europe/Berlin"
    before_spring = datetime(2026, 3, 28, 3, 0, tzinfo=UTC).timestamp()
    spring_next = first_run_at(RecurrenceKind.CRON, "30 2 * * *", zone, before_spring)
    assert format_timestamp(spring_next, zone).startswith("2026-03-30T02:30")

    before_fall_second_fold = datetime(2026, 10, 25, 0, 45, tzinfo=UTC).timestamp()
    fall_next = first_run_at(RecurrenceKind.CRON, "30 2 * * *", zone, before_fall_second_fold)
    assert format_timestamp(fall_next, zone).startswith("2026-10-26T02:30")


def test_cron_steps_ranges_and_standard_day_field_semantics() -> None:
    stepped = CronExpression.parse("5/15 9-11/2 1 * *")
    assert stepped.minutes == frozenset({5, 20, 35, 50})
    assert stepped.hours == frozenset({9, 11})
    assert stepped.matches_date(datetime(2026, 1, 1).date())

    weekdays_or_monthdays = CronExpression.parse("0 0 13 * 1")
    assert weekdays_or_monthdays.matches_date(datetime(2026, 7, 13).date())  # Monday
    assert weekdays_or_monthdays.matches_date(datetime(2026, 8, 13).date())  # 13th
    assert not weekdays_or_monthdays.matches_date(datetime(2026, 8, 14).date())


@pytest.mark.parametrize(
    "expression",
    ["* * * *", "60 * * * *", "*/0 * * * *", "5-1 * * * *", "* * * * * *", "1,,2 * * * *"],
)
def test_invalid_cron_expressions_are_rejected(expression: str) -> None:
    with pytest.raises(ValueError):
        CronExpression.parse(expression)


def test_one_shot_timezone_and_dst_validation() -> None:
    from zoneinfo import ZoneInfo

    utc_timestamp = first_run_at(RecurrenceKind.ONCE, "2027-02-03T12:00:00Z", "Europe/Berlin", 0)
    berlin_timestamp = first_run_at(RecurrenceKind.ONCE, "2027-02-03T13:00:00+01:00", "UTC", 0)
    assert utc_timestamp == berlin_timestamp
    with pytest.raises(ValueError, match="does not exist"):
        first_run_at(RecurrenceKind.ONCE, "2026-03-29T02:30", "Europe/Berlin", 0)

    ambiguous = first_run_at(RecurrenceKind.ONCE, "2026-10-25T02:30", "Europe/Berlin", 0)
    assert datetime.fromtimestamp(ambiguous, ZoneInfo("Europe/Berlin")).fold == 0
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        first_run_at(RecurrenceKind.INTERVAL, "1h", "Mars/Olympus", 0)


def test_interval_clock_jumps_advance_to_first_future_tick_without_replay() -> None:
    due = 10_000.0
    after_forward_jump = next_run_after(
        RecurrenceKind.INTERVAL, "60s", "UTC", previous=due, now=due + 1_000
    )
    assert after_forward_jump == due + 1_020
    assert after_forward_jump > due + 1_000

    # A backward wall-clock correction cannot claim the already-advanced tick.
    after_backward_jump = next_run_after(
        RecurrenceKind.INTERVAL, "60s", "UTC", previous=due, now=due - 300
    )
    assert after_backward_jump == due + 60


def test_schedule_revisions_exact_prompt_and_deletion_preserve_history(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, manager, schedule = create_schedule(tmp_path, clock)
    assert schedule.prompt == "  Keep this exact prompt.\n"
    assert schedule.revision == 2
    assert [item["action"] for item in store.revisions(schedule.schedule_id)] == [
        "created",
        "resumed",
    ]

    occurrence = manager.run_now(schedule.schedule_id)
    assert occurrence.schedule_revision == schedule.revision
    assert occurrence.session_id
    assert store.get(schedule.schedule_id).next_run_at == schedule.next_run_at

    deleted = manager.delete(schedule.schedule_id)
    assert deleted.deleted
    assert store.get(schedule.schedule_id, include_deleted=True).revision == 3
    assert [item.occurrence_id for item in store.history(schedule.schedule_id)] == [
        occurrence.occurrence_id
    ]
    assert store.history(schedule.schedule_id, limit=500)  # deleted jobs remain inspectable


def test_resume_requires_exact_next_run_preview_and_update_rearms_paused(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, manager, schedule = create_schedule(tmp_path, clock)
    paused = manager.pause(schedule.schedule_id)
    with pytest.raises(ValueError, match="confirm the displayed next run"):
        manager.resume(schedule.schedule_id, confirm_next_run="wrong")
    updated = manager.update(
        schedule.schedule_id,
        changes={"recurrence_spec": "2m"},
    )
    assert updated.recurrence_spec == "2m"
    assert not updated.enabled
    assert updated.revision == paused.revision + 1


def test_update_of_enabled_schedule_requires_fresh_resume_confirmation(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, manager, schedule = create_schedule(tmp_path, clock)

    updated = manager.update(schedule.schedule_id, changes={"prompt": "Revised prompt"})

    assert updated.prompt == "Revised prompt"
    assert not updated.enabled
    assert updated.revision == schedule.revision + 1
    assert store.revisions(schedule.schedule_id)[-1]["action"] == "updated"
    expected = format_timestamp(updated.next_run_at, updated.timezone)
    assert "+00:00" in expected
    with pytest.raises(ValueError, match="confirm the displayed next run"):
        manager.resume(schedule.schedule_id, confirm_next_run="stale-preview")
    resumed = manager.resume(schedule.schedule_id, confirm_next_run=expected)
    assert resumed.enabled


def test_update_rejects_attempts_to_bypass_pause_and_mode_ceiling(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    _, manager, schedule = create_schedule(tmp_path, clock)

    with pytest.raises(ValueError, match="unsupported schedule fields: enabled"):
        manager.update(schedule.schedule_id, changes={"enabled": True})
    with pytest.raises(ValueError, match="permission ceiling"):
        manager.update(schedule.schedule_id, changes={"permission_ceiling": "Unrestricted"})
    with pytest.raises(ValueError, match="unsupported schedule fields"):
        manager.update(schedule.schedule_id, changes={"repository_root": "/tmp/other"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_in_flight", True),
        ("max_in_flight", 1.5),
        ("revision", True),
        ("created_at", float("nan")),
        ("updated_at", float("inf")),
        ("next_run_at", float("nan")),
        ("deleted_at", float("inf")),
        ("permission_ceiling", "Unrestricted"),
    ],
)
def test_persisted_schedule_rejects_invalid_security_and_numeric_values(
    tmp_path: Path, field: str, value: object
) -> None:
    clock = FakeClock(1_700_000_000)
    _, _, schedule = create_schedule(tmp_path, clock)
    payload = schedule.to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        Schedule.from_mapping(payload)


def test_scheduler_dispatches_due_once_and_records_stable_receipt(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, _, schedule = create_schedule(tmp_path, clock)
    dispatches: list[tuple[str, str]] = []

    def dispatch(_selected: Schedule, occurrence: Occurrence) -> dict[str, object]:
        dispatches.append((occurrence.occurrence_id, occurrence.session_id))
        return {"status": "running", "session_id": occurrence.session_id}

    scheduler = ScheduleScheduler(store, dispatch, clock=clock)
    clock.advance(60)
    scheduler.tick()
    scheduler.tick()
    records = store.history(schedule.schedule_id)
    assert len(records) == 1
    assert records[0].state is OccurrenceState.RUNNING
    assert records[0].session_id == dispatches[0][1]
    assert records[0].receipt is not None
    assert records[0].receipt["accepted"] is True
    assert len(dispatches) == 1
    assert store.get(schedule.schedule_id).next_run_at == clock.value + 60


def test_skip_policy_records_missed_concurrent_tick_without_replay(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, _, schedule = create_schedule(tmp_path, clock)
    scheduler = ScheduleScheduler(
        store,
        lambda _schedule, _occurrence: {"status": "running"},
        clock=clock,
    )
    clock.advance(60)
    scheduler.tick()
    clock.advance(60)
    scheduler.tick()
    states = [item.state for item in reversed(store.history(schedule.schedule_id))]
    assert states == [OccurrenceState.RUNNING, OccurrenceState.SKIPPED]


def test_queue_one_coalesces_missed_ticks_and_dispatches_after_prior_run(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, _, schedule = create_schedule(tmp_path, clock, policy=CoalescePolicy.QUEUE_ONE)
    dispatches: list[str] = []
    scheduler = ScheduleScheduler(
        store,
        lambda _schedule, occurrence: (
            dispatches.append(occurrence.occurrence_id) or {"status": "running"}
        ),
        clock=clock,
    )
    clock.advance(60)
    scheduler.tick()
    first = store.history(schedule.schedule_id)[0]
    clock.advance(60)
    scheduler.tick()
    clock.advance(60)
    scheduler.tick()
    history = store.history(schedule.schedule_id)
    queued = next(item for item in history if item.state is OccurrenceState.QUEUED)
    assert queued.coalesced_count == 1
    assert len(dispatches) == 1

    store.update_occurrence(first.occurrence_id, now=clock.value, state=OccurrenceState.COMPLETED)
    scheduler.tick()
    assert len(dispatches) == 2
    assert store.get_occurrence(queued.occurrence_id).state is OccurrenceState.RUNNING


def test_manual_run_now_is_waiting_without_service_and_does_not_shift_cursor(
    tmp_path: Path,
) -> None:
    clock = FakeClock(1_700_000_000)
    store, manager, schedule = create_schedule(tmp_path, clock)
    next_run = schedule.next_run_at
    occurrence = manager.run_now(schedule.schedule_id)
    assert occurrence.state is OccurrenceState.AWAITING_SERVICE
    assert occurrence.receipt is not None
    assert occurrence.receipt["accepted"] is True
    assert store.get(schedule.schedule_id).next_run_at == next_run


@pytest.mark.parametrize(
    ("policy", "max_in_flight", "expected_admissions"),
    [
        (CoalescePolicy.SKIP, 1, 1),
        (CoalescePolicy.QUEUE_ONE, 1, 1),
        (CoalescePolicy.ALLOW_BOUNDED, 2, 2),
    ],
)
def test_concurrent_run_now_admission_is_transactionally_bounded(
    tmp_path: Path,
    policy: CoalescePolicy,
    max_in_flight: int,
    expected_admissions: int,
) -> None:
    clock = FakeClock(1_700_000_000)
    store = ScheduleStore(tmp_path / "schedule-state")
    manager = ScheduleManager(store, clock=clock)
    repository = tmp_path / "repo"
    repository.mkdir()
    schedule = manager.create(
        repository_root=repository,
        workflow_name="test_workflow",
        prompt="Only admit within policy",
        profile_name="unit-profile",
        permission_ceiling="Plan",
        recurrence_kind=RecurrenceKind.INTERVAL,
        recurrence_spec="1h",
        timezone="UTC",
        coalesce_policy=policy,
        max_in_flight=max_in_flight,
    )
    barrier = threading.Barrier(8)

    def submit() -> Occurrence | None:
        barrier.wait(timeout=5)
        try:
            return manager.run_now(schedule.schedule_id)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: submit(), range(8)))

    admitted = [item for item in results if item is not None]
    assert len(admitted) == expected_admissions
    assert len(store.inflight_for(schedule.schedule_id)) == expected_admissions
    assert len({item.session_id for item in admitted}) == expected_admissions


def test_queue_one_manual_run_queues_once_behind_active_occurrence(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, manager, schedule = create_schedule(
        tmp_path,
        clock,
        policy=CoalescePolicy.QUEUE_ONE,
    )
    first = manager.run_now(schedule.schedule_id)
    store.update_occurrence(first.occurrence_id, now=clock.value, state=OccurrenceState.RUNNING)

    queued = manager.run_now(schedule.schedule_id)
    assert queued.state is OccurrenceState.QUEUED
    with pytest.raises(ValueError, match="one pending queued occurrence"):
        manager.run_now(schedule.schedule_id)


def test_scheduler_singleton_lock_is_nonblocking_and_released(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule-state")
    first = ScheduleServiceLease(store)
    second = ScheduleServiceLease(store)
    first.acquire()
    assert ScheduleServiceLease.is_running(store) is True
    with pytest.raises(ScheduleServiceAlreadyRunning):
        second.acquire()
    first.release()
    assert ScheduleServiceLease.is_running(store) is False


def test_two_concurrent_scheduler_ticks_claim_one_occurrence(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, _, schedule = create_schedule(tmp_path, clock)
    calls: list[str] = []
    dispatch_started = threading.Event()
    allow_dispatch_to_finish = threading.Event()

    def dispatch(_selected: Schedule, occurrence: Occurrence) -> dict[str, object]:
        calls.append(occurrence.occurrence_id)
        dispatch_started.set()
        assert allow_dispatch_to_finish.wait(timeout=5)
        return {"status": "running"}

    schedulers = [ScheduleScheduler(store, dispatch, clock=clock) for _ in range(2)]
    clock.advance(60)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(schedulers[0].tick)
        assert dispatch_started.wait(timeout=5)
        second = workers.submit(schedulers[1].tick)
        try:
            second.result(timeout=5)
        finally:
            allow_dispatch_to_finish.set()
        first.result(timeout=5)

    history = store.history(schedule.schedule_id)
    assert len(history) == 1
    assert len(calls) == 1
    assert history[0].session_id


def test_crash_after_claim_restarts_same_occurrence_and_session(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, _, schedule = create_schedule(tmp_path, clock)
    dispatch_calls: list[str] = []

    class SimulatedProcessCrash(BaseException):
        pass

    def dispatch(_selected: Schedule, occurrence: Occurrence) -> dict[str, object]:
        session_id = occurrence.session_id
        dispatch_calls.append(session_id)
        if len(dispatch_calls) == 1:
            raise SimulatedProcessCrash
        return {"status": "running", "session_id": session_id}

    clock.advance(60)
    with pytest.raises(SimulatedProcessCrash):
        ScheduleScheduler(store, dispatch, clock=clock).tick()
    claimed = store.history(schedule.schedule_id)
    assert len(claimed) == 1
    assert claimed[0].state is OccurrenceState.PREPARING

    ScheduleScheduler(store, dispatch, clock=clock).tick()
    recovered = store.get_occurrence(claimed[0].occurrence_id)
    assert recovered.state is OccurrenceState.RUNNING
    assert dispatch_calls == [claimed[0].session_id, claimed[0].session_id]


@pytest.mark.parametrize(
    ("background_status", "expected_state"),
    [
        ("completed", OccurrenceState.COMPLETED),
        ("succeeded", OccurrenceState.COMPLETED),
        ("failed", OccurrenceState.FAILED),
        ("cancelled", OccurrenceState.CANCELLED),
        ("queued", OccurrenceState.WAITING_CAPACITY),
        ("waiting_input", OccurrenceState.RUNNING),
    ],
)
def test_scheduler_reconciles_durable_background_session_status(
    tmp_path: Path,
    background_status: str,
    expected_state: OccurrenceState,
) -> None:
    clock = FakeClock(1_700_000_000)
    store, manager, schedule = create_schedule(tmp_path, clock)
    occurrence = manager.run_now(schedule.schedule_id)
    store.update_occurrence(
        occurrence.occurrence_id,
        now=clock.value,
        state=OccurrenceState.RUNNING,
    )
    session = SimpleNamespace(
        status=SimpleNamespace(value=background_status),
        worker_exit_reason="worker result",
        worker_exit_code=0,
        completed_at=clock.value + 1,
        error="provider detail",
    )
    scheduler = ScheduleScheduler(
        store,
        lambda _schedule, _occurrence: {"status": "running"},
        background_lookup=lambda _session_id: session,  # type: ignore[arg-type]
        clock=clock,
    )

    scheduler._reconcile_background(clock.value)

    reconciled = store.get_occurrence(occurrence.occurrence_id)
    assert reconciled.state is expected_state
    if background_status == "waiting_input":
        # Waiting for user input is still an active worker state. Since the
        # occurrence was already RUNNING, reconciliation is intentionally a
        # no-op and must not overwrite its dispatch receipt with a terminal
        # outcome.
        assert reconciled.outcome is None
        assert reconciled.error == ""
    else:
        assert reconciled.outcome is not None
        assert reconciled.outcome["background_status"] == background_status
        assert reconciled.error == "provider detail"


def test_capacity_rejection_keeps_same_occurrence_waiting_for_service_retry(
    tmp_path: Path,
) -> None:
    clock = FakeClock(1_700_000_000)
    store, manager, schedule = create_schedule(tmp_path, clock)
    occurrence = manager.run_now(schedule.schedule_id)
    dispatch_attempts: list[str] = []

    def dispatch(_schedule: Schedule, selected: Occurrence) -> dict[str, object]:
        dispatch_attempts.append(selected.session_id)
        if len(dispatch_attempts) == 1:
            raise RuntimeError("Background worker limit reached")
        return {"status": "running", "session_id": selected.session_id}

    scheduler = ScheduleScheduler(store, dispatch, clock=clock)
    scheduler.tick()
    waiting = store.get_occurrence(occurrence.occurrence_id)
    assert waiting.state is OccurrenceState.WAITING_CAPACITY

    scheduler.tick()
    running = store.get_occurrence(occurrence.occurrence_id)
    assert running.state is OccurrenceState.RUNNING
    assert dispatch_attempts == [occurrence.session_id, occurrence.session_id]
    assert len(store.history(schedule.schedule_id)) == 1


def test_allow_bounded_policy_never_exceeds_saved_in_flight_limit(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, _, schedule = create_schedule(
        tmp_path,
        clock,
        policy=CoalescePolicy.ALLOW_BOUNDED,
    )
    schedule = store.revise(
        schedule.schedule_id,
        expected_revision=schedule.revision,
        action="test_limit",
        actor="test",
        now=clock.value,
        max_in_flight=2,
    )
    dispatched: list[str] = []

    def dispatch(_selected: Schedule, occurrence: Occurrence) -> dict[str, object]:
        dispatched.append(occurrence.occurrence_id)
        return {"status": "running"}

    scheduler = ScheduleScheduler(store, dispatch, clock=clock)
    for _ in range(3):
        clock.advance(60)
        scheduler.tick()

    states = [item.state for item in reversed(store.history(schedule.schedule_id))]
    assert states == [
        OccurrenceState.RUNNING,
        OccurrenceState.RUNNING,
        OccurrenceState.SKIPPED,
    ]
    assert len(dispatched) == 2


def test_failed_occurrence_retry_is_a_new_auditable_attempt(tmp_path: Path) -> None:
    clock = FakeClock(1_700_000_000)
    store, manager, schedule = create_schedule(tmp_path, clock)
    occurrence = manager.run_now(schedule.schedule_id)
    failed_scheduler = ScheduleScheduler(
        store,
        lambda _selected, _occurrence: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
        clock=clock,
    )
    failed_scheduler.tick()
    failed = store.get_occurrence(occurrence.occurrence_id)
    assert failed.state is OccurrenceState.BLOCKED
    assert "provider unavailable" in failed.error

    retried = manager.retry(schedule.schedule_id, failed.occurrence_id, actor="operator")
    assert retried.occurrence_id != failed.occurrence_id
    assert retried.session_id != failed.session_id
    assert retried.retry_of == failed.occurrence_id
    assert retried.attempt == failed.attempt + 1
    assert store.get_occurrence(failed.occurrence_id).state is OccurrenceState.BLOCKED


def test_schedule_overlay_controls_pause_run_now_and_confirmed_delete(tmp_path: Path) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.overlays.schedules import ScheduleJobsOverlay

    clock = FakeClock(1_700_000_000)
    store = ScheduleStore(tmp_path / "schedule-state")
    manager = ScheduleManager(store, clock=clock)
    repository = tmp_path / "repo"
    repository.mkdir()
    schedule = manager.create(
        repository_root=repository,
        workflow_name="test_workflow",
        prompt="Inspect from the overlay",
        profile_name="unit-profile",
        permission_ceiling="Plan",
        recurrence_kind=RecurrenceKind.INTERVAL,
        recurrence_spec="1h",
        timezone="UTC",
    )
    closed: list[bool] = []
    input_text: list[str] = []
    overlay = ScheduleJobsOverlay(
        store,
        manager,
        str(repository),
        on_close=lambda: closed.append(True),
        set_input_text=input_text.append,
    )

    assert overlay.selected_record is not None
    assert overlay.selected_record.schedule_id == schedule.schedule_id
    overlay.handle_key(Key.CHAR, "p")
    assert store.get(schedule.schedule_id).enabled
    overlay.handle_key(Key.CHAR, "p")
    assert not store.get(schedule.schedule_id).enabled
    overlay.handle_key(Key.ENTER, "")
    runs = store.history(schedule.schedule_id)
    assert len(runs) == 1
    assert runs[0].state is OccurrenceState.AWAITING_SERVICE

    overlay.handle_key(Key.CHAR, "d")
    overlay.render()
    overlay.handle_key(Key.CHAR, "y")
    deleted = store.get(schedule.schedule_id, include_deleted=True)
    assert deleted.deleted
    assert len(store.history(schedule.schedule_id)) == 1

    overlay.handle_key(Key.CHAR, "n")
    assert input_text == ["/schedule create "]
    assert closed == [True]
