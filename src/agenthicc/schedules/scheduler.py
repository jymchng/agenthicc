"""Single-owner scheduler and replay-safe background-workflow dispatcher."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.runners.process_lease import InterProcessLock, InterProcessLockBusy
from agenthicc.schedules.manager import occurrence_session_id
from agenthicc.schedules.model import (
    CoalescePolicy,
    Occurrence,
    OccurrenceState,
    Schedule,
)
from agenthicc.schedules.recurrence import next_run_after
from agenthicc.schedules.store import ScheduleConflict, ScheduleNotFound, ScheduleStore

if TYPE_CHECKING:
    from agenthicc.background.model import BackgroundSession
    from agenthicc.background.store import BackgroundStore
    from agenthicc.background.supervisor import BackgroundSupervisor


_SCHEDULE_NAMESPACE = uuid.UUID("0f783d2e-bdde-45fb-b782-7a5e5cd7a602")
logger = logging.getLogger(__name__)
_PENDING_STATES = (
    OccurrenceState.AWAITING_SERVICE,
    OccurrenceState.QUEUED,
    OccurrenceState.WAITING_CAPACITY,
    OccurrenceState.PREPARING,
)


class ScheduleServiceAlreadyRunning(RuntimeError):
    """Another process owns the user's schedule-service lock."""


class ScheduleServiceLease:
    """Non-blocking singleton ownership for the always-on scheduler service."""

    def __init__(self, store: ScheduleStore) -> None:
        self.store = store
        self._lock: InterProcessLock | None = None

    def acquire(self) -> None:
        if self._lock is not None:
            return
        lock = InterProcessLock(self.store.service_lock_path, blocking=False)
        try:
            lock.__enter__()
        except InterProcessLockBusy as exc:
            raise ScheduleServiceAlreadyRunning(
                "the Agenthicc schedule service already owns this state directory"
            ) from exc
        self._lock = lock

    def release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.__exit__(None, None, None)

    def __enter__(self) -> ScheduleServiceLease:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    @classmethod
    def is_running(cls, store: ScheduleStore) -> bool | None:
        """Return False/True, or None if the platform cannot verify ownership."""

        probe = InterProcessLock(store.service_lock_path, blocking=False)
        try:
            probe.__enter__()
        except InterProcessLockBusy:
            return True
        except OSError:
            return None
        else:
            probe.__exit__(None, None, None)
            return False


class ScheduleScheduler:
    """Claims due ticks and submits each one through the background supervisor.

    The schedule database stores a deterministic session ID and receipt before
    dispatch. If the process dies after background acceptance but before the
    local receipt is updated, the same ID is adopted on restart rather than
    launching a duplicate model run.
    """

    def __init__(
        self,
        store: ScheduleStore,
        dispatch: Callable[[Schedule, Occurrence], Mapping[str, object]],
        *,
        background_lookup: Callable[[str], BackgroundSession | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.dispatch = dispatch
        self.background_lookup = background_lookup
        self.clock = clock

    def tick(self) -> None:
        """Perform one bounded reconciliation/claim/dispatch pass."""

        now = self.clock()
        self._reconcile_background(now)
        self._claim_due(now)
        self._dispatch_pending(now)

    def _claim_due(self, now: float) -> None:
        for schedule in self.store.due_schedules(now=now):
            previous = schedule.next_run_at
            if previous is None:
                continue
            next_run = next_run_after(
                schedule.recurrence_kind,
                schedule.recurrence_spec,
                schedule.timezone,
                previous=previous,
                now=now,
            )
            inflight = self.store.inflight_for(schedule.schedule_id)
            coalesce_target = ""
            if schedule.coalesce_policy is CoalescePolicy.SKIP and inflight:
                state = OccurrenceState.SKIPPED
            elif schedule.coalesce_policy is CoalescePolicy.QUEUE_ONE and inflight:
                queued = next(
                    (item for item in inflight if item.state is OccurrenceState.QUEUED), None
                )
                if queued is not None:
                    state = OccurrenceState.QUEUED
                    coalesce_target = queued.occurrence_id
                else:
                    state = OccurrenceState.QUEUED
            elif (
                schedule.coalesce_policy is CoalescePolicy.ALLOW_BOUNDED
                and len(inflight) >= schedule.max_in_flight
            ):
                state = OccurrenceState.SKIPPED
            else:
                state = OccurrenceState.AWAITING_SERVICE
            stable_key = f"scheduled:{schedule.schedule_id}:{schedule.revision}:{previous:.6f}"
            occurrence_id = uuid.uuid5(_SCHEDULE_NAMESPACE, stable_key).hex
            session_id = (
                "" if state is OccurrenceState.SKIPPED else occurrence_session_id(occurrence_id)
            )
            try:
                occurrence = self.store.claim_due_occurrence(
                    schedule=schedule,
                    expected_due_at=previous,
                    next_run_at=next_run,
                    occurrence_id=occurrence_id,
                    occurrence_key=stable_key,
                    session_id=session_id,
                    state=state,
                    now=now,
                    coalesce_target_id=coalesce_target,
                )
                if occurrence.state is OccurrenceState.SKIPPED:
                    self.store.update_occurrence(
                        occurrence.occurrence_id,
                        now=now,
                        outcome={"reason": "coalesced_or_concurrency_policy"},
                    )
            except (ScheduleConflict, ScheduleNotFound):
                # A pause/revision or another owner won the compare-and-swap.
                continue

    def _reconcile_background(self, now: float) -> None:
        if self.background_lookup is None:
            return
        for occurrence in self.store.list_occurrences(limit=5000):
            if not occurrence.session_id or occurrence.state not in {
                OccurrenceState.RUNNING,
                OccurrenceState.WAITING_CAPACITY,
            }:
                continue
            try:
                session = self.background_lookup(occurrence.session_id)
            except Exception:  # noqa: BLE001
                continue
            if session is None:
                continue
            status = session.status.value.casefold()
            if status in {"completed", "complete", "succeeded"}:
                state = OccurrenceState.COMPLETED
            elif status in {"failed", "error"}:
                state = OccurrenceState.FAILED
            elif status in {"cancelled", "canceled", "deleted"}:
                state = OccurrenceState.CANCELLED
            elif status in {"queued", "starting", "running", "waiting_approval", "waiting_input"}:
                state = (
                    OccurrenceState.WAITING_CAPACITY
                    if status in {"queued", "starting"}
                    else OccurrenceState.RUNNING
                )
            else:
                continue
            if state is occurrence.state:
                continue
            outcome: dict[str, object] = {
                "background_status": status,
                "worker_exit_reason": session.worker_exit_reason[:256],
                "worker_exit_code": session.worker_exit_code,
                "completed_at": session.completed_at,
            }
            error = (session.error or "")[:1000]
            self.store.update_occurrence(
                occurrence.occurrence_id,
                now=now,
                state=state,
                outcome=outcome,
                error=error,
            )

    def _dispatch_pending(self, now: float) -> None:
        # PREPARING is deliberately durable so a later service owner can
        # recover a crash mid-dispatch. Serialize dispatch itself across all
        # scheduler instances, then release the OS lock automatically if the
        # process exits. A singleton service lock alone does not protect
        # callers that invoke tick() directly or during overlapping shutdown.
        try:
            with InterProcessLock(self.store.dispatch_lock_path, blocking=False):
                self._dispatch_pending_exclusive(now)
        except InterProcessLockBusy:
            return

    def _dispatch_pending_exclusive(self, now: float) -> None:
        for occurrence in self.store.list_occurrences(states=_PENDING_STATES, limit=1000):
            try:
                schedule = self.store.get_revision(
                    occurrence.schedule_id, occurrence.schedule_revision
                )
            except ScheduleNotFound:
                self.store.update_occurrence(
                    occurrence.occurrence_id,
                    now=now,
                    state=OccurrenceState.BLOCKED,
                    error="schedule revision is missing; dispatch stopped safely",
                )
                continue
            inflight = self.store.inflight_for(schedule.schedule_id)
            others = [item for item in inflight if item.occurrence_id != occurrence.occurrence_id]
            if occurrence.state is OccurrenceState.QUEUED and others:
                continue
            if (
                schedule.coalesce_policy is CoalescePolicy.ALLOW_BOUNDED
                and len(others) >= schedule.max_in_flight
            ):
                continue
            if schedule.coalesce_policy is CoalescePolicy.SKIP and others:
                self.store.update_occurrence(
                    occurrence.occurrence_id,
                    now=now,
                    state=OccurrenceState.SKIPPED,
                    outcome={"reason": "another_occurrence_is_in_flight"},
                    error="",
                )
                continue
            if (
                schedule.coalesce_policy is CoalescePolicy.QUEUE_ONE
                and occurrence.state is not OccurrenceState.QUEUED
                and others
            ):
                continue
            try:
                claimed = self.store.update_occurrence(
                    occurrence.occurrence_id,
                    now=now,
                    expected_states=_PENDING_STATES,
                    state=OccurrenceState.PREPARING,
                    error="",
                )
            except ScheduleConflict:
                continue
            try:
                receipt = dict(self.dispatch(schedule, claimed))
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"[:1000]
                state = (
                    OccurrenceState.WAITING_CAPACITY
                    if "worker limit reached" in str(exc).casefold()
                    or "queue is full" in str(exc).casefold()
                    else OccurrenceState.BLOCKED
                )
                self.store.update_occurrence(
                    claimed.occurrence_id,
                    now=self.clock(),
                    state=state,
                    receipt={"session_id": claimed.session_id, "dispatch_attempted": True},
                    error=message,
                )
                continue
            background_status = str(receipt.get("status", "running")).casefold()
            state = (
                OccurrenceState.WAITING_CAPACITY
                if background_status in {"queued", "starting"}
                else OccurrenceState.RUNNING
            )
            self.store.update_occurrence(
                claimed.occurrence_id,
                now=self.clock(),
                state=state,
                receipt={
                    "accepted": True,
                    "accepted_at": self.clock(),
                    "session_id": claimed.session_id,
                    "background_status": background_status,
                    "schedule_revision": schedule.revision,
                },
                error="",
            )

    async def run(self, *, stop: asyncio.Event, poll_interval_s: float = 1.0) -> None:
        """Hold the scheduler singleton and tick until orderly shutdown."""

        if not 0.1 <= poll_interval_s <= 300:
            raise ValueError("poll_interval_s must be between 0.1 and 300 seconds")
        with ScheduleServiceLease(self.store):
            while not stop.is_set():
                try:
                    await asyncio.to_thread(self.tick)
                except Exception:  # noqa: BLE001
                    logger.exception("scheduled-workflow scheduler pass failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval_s)
                except TimeoutError:
                    pass


def build_background_dispatcher(
    *,
    store: ScheduleStore,
    supervisor: BackgroundSupervisor,
    background_store: BackgroundStore,
) -> Callable[[Schedule, Occurrence], Mapping[str, object]]:
    """Build a bounded dispatcher over the existing background worker path."""

    def dispatch(schedule: Schedule, occurrence: Occurrence) -> Mapping[str, object]:
        from agenthicc.worktrees.manager import WorktreeManager
        from agenthicc.worktrees.model import WorktreeRecord

        repository = Path(schedule.repository_root).resolve()
        git = WorktreeManager(repository)
        base_commit = occurrence.base_commit
        working_directory = repository
        worktree_record: WorktreeRecord | None = None
        if schedule.permission_ceiling.casefold() != "plan":
            # Any non-Plan runtime may acquire write/execute capabilities, so
            # it must be isolated. No schedule worker can merge these changes.
            if not base_commit:
                git.ensure_clean()
                base_commit = git.head()
                occurrence = store.update_occurrence(
                    occurrence.occurrence_id,
                    now=time.time(),
                    base_commit=base_commit,
                    state=OccurrenceState.PREPARING,
                )
            worktree_identity = uuid.uuid5(
                _SCHEDULE_NAMESPACE, f"{schedule.schedule_id}:{occurrence.occurrence_id}"
            ).hex[:16]
            branch = (
                f"agenthicc/schedule-{schedule.schedule_id[:12]}/{occurrence.occurrence_id[:20]}"
            )
            if occurrence.worktree is not None:
                worktree_record = WorktreeRecord.from_mapping(occurrence.worktree)
            else:
                worktree_record = git.create(
                    parent_session_id=f"schedule-{schedule.schedule_id[:12]}",
                    task_id=occurrence.occurrence_id,
                    base_commit=base_commit,
                    worktree_id=worktree_identity,
                    branch_name=branch,
                )
                occurrence = store.update_occurrence(
                    occurrence.occurrence_id,
                    now=time.time(),
                    worktree=worktree_record.to_dict(),
                    base_commit=base_commit,
                    state=OccurrenceState.PREPARING,
                )
            working_directory = Path(worktree_record.path)
        elif not base_commit:
            base_commit = git.head()
            occurrence = store.update_occurrence(
                occurrence.occurrence_id,
                now=time.time(),
                base_commit=base_commit,
                state=OccurrenceState.PREPARING,
            )

        expected_session = occurrence_session_id(occurrence.occurrence_id)
        if occurrence.session_id and occurrence.session_id != expected_session:
            raise RuntimeError("schedule occurrence session identity failed validation")
        existing = None
        try:
            existing = background_store.get(expected_session, include_deleted=True)
        except Exception:  # noqa: BLE001
            existing = None
        if existing is not None:
            if (
                existing.cwd != str(working_directory.resolve())
                or existing.workflow_name != schedule.workflow_name
                or existing.intent != schedule.prompt
                or existing.requested_mode_name != schedule.permission_ceiling
                or existing.requested_profile_name != schedule.profile_name
            ):
                raise RuntimeError(
                    "deterministic background session ID is occupied by another request"
                )
            result_session = existing
        else:
            overrides = (f"execution.profile={schedule.profile_name}",)
            result_session = supervisor.submit(
                intent=schedule.prompt,
                workflow_name=schedule.workflow_name,
                title=f"Scheduled {schedule.workflow_name}"[:80],
                cwd=str(working_directory),
                session_id=expected_session,
                config_path=schedule.config_path or None,
                set_overrides=overrides,
                dangerously_skip_permissions=False,
                parent_session_id=f"schedule:{schedule.schedule_id}",
                task_id=occurrence.occurrence_id,
                worktree_id=worktree_record.worktree_id if worktree_record else "",
                branch=worktree_record.branch if worktree_record else "",
                base_commit=base_commit,
                mode_name=schedule.permission_ceiling,
                idempotency_key=f"schedule:{schedule.schedule_id}:{occurrence.occurrence_id}",
                requested_profile_name=schedule.profile_name,
            )
        status = result_session.status.value
        return {
            "session_id": expected_session,
            "status": str(status),
            "workflow_name": schedule.workflow_name,
            "profile_name": schedule.profile_name,
            "permission_ceiling": schedule.permission_ceiling,
            "working_directory": str(working_directory),
            "base_commit": base_commit,
            "worktree_id": worktree_record.worktree_id if worktree_record else "",
        }

    return dispatch


def background_lookup(store: BackgroundStore) -> Callable[[str], BackgroundSession | None]:
    """Adapt a canonical background store without leaking its concrete type."""

    def lookup(session_id: str) -> BackgroundSession | None:
        from agenthicc.background.store import SessionNotFound

        try:
            return store.get(session_id, include_deleted=True)
        except SessionNotFound:
            return None

    return lookup
