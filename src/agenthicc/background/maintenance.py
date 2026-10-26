"""Due-driven background maintenance (AGX-04).

Maintenance used to walk the next 1,024 sessions every few seconds, probing
each one's worker and reading each finished session's input inbox whether or
not anything had changed. A :class:`MaintenanceScheduler` does only the work
that is due and reports when the next piece of work falls due:

- **Workers.** A worker is probed when it is first seen (so the caller can
  watch it for exit), when its process exits, when its heartbeat deadline
  passes, and when a stale heartbeat resumes. A worker the caller cannot
  watch natively is also probed by any pass ``poll_interval_s`` after its
  last probe; such a caller schedules those passes itself.
- **Deferred input.** Only sessions whose input inbox holds an accepted,
  deferred message are candidates, and an inbox file is re-read only when it
  changes. A failed start is retried with exponential backoff.
- **Queue dispatch.** Runs when pending requests exist and the registry
  changed, and when a claimed reservation's dispatch lease expires.

With nothing due, a pass probes no process and appends nothing.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

from .input_inbox import BackgroundInputInbox
from .model import ACTIVE_STATUSES, BackgroundSession, QueueDispatchState, SessionStatus
from .queue import is_pending_queue_record
from .store import SessionNotFound
from .supervisor import BackgroundSupervisor, _WorkerProcessState, reconciles_worker

__all__ = ["MaintenanceOutcome", "MaintenanceScheduler", "MaintenanceTrigger"]

_INBOX_SUFFIX = ".jsonl"
_INPUT_RETRY_CAP_S = 300.0
_WATCHABLE = frozenset({_WorkerProcessState.LIVE, _WorkerProcessState.UNKNOWN})


@dataclass(frozen=True)
class MaintenanceTrigger:
    """Why a maintenance pass runs.

    A ``full`` pass probes every active worker, rescans the input inbox and
    dispatches the queue: the agents screen's first pass, an operator refresh
    and :meth:`BackgroundSupervisor.recover_stale`. Any other pass does what
    these fields and the scheduler's own deadlines make due.
    """

    full: bool = False
    #: The registry log changed since the previous pass.
    registry_changed: bool = False
    #: Names of input-inbox entries that changed; ``None`` when unknown.
    inbox_changed: frozenset[str] | None = frozenset()
    #: Sessions whose watched worker process exited.
    exited: frozenset[str] = frozenset()
    #: Sessions whose worker exit the caller watches natively.
    exit_watched: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MaintenanceOutcome:
    """What one pass changed, and when the next pass falls due."""

    changed: tuple[BackgroundSession, ...]
    #: Worker processes inspected in this pass.
    probes: int
    #: Wall-clock time of the next deadline, or ``None`` when nothing is pending.
    #: Polling is excluded: a caller that cannot watch one of ``workers`` for
    #: exit runs another pass within ``poll_interval_s``.
    next_due_at: float | None
    #: Session ID → PID of each probed, still-active worker the caller may watch.
    workers: dict[str, int] = field(default_factory=dict)


class MaintenanceScheduler:
    """Keep what earlier passes learned so each pass does only what is due."""

    def __init__(
        self,
        supervisor: BackgroundSupervisor,
        *,
        stale_after_s: float | None = None,
        poll_interval_s: float = 5.0,
        inbox_rescan_s: float | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.store = supervisor.store
        self.stale_after_s = supervisor.stale_after_s if stale_after_s is None else stale_after_s
        self.poll_interval_s = poll_interval_s
        #: Rescan the whole input inbox this often; set it when the caller's
        #: watcher cannot report which inbox files changed.
        self.inbox_rescan_s = inbox_rescan_s
        self._inbox = BackgroundInputInbox(self.store)
        # Session ID → (PID, attempt, state) of the last probe of its worker.
        self._probed: dict[str, tuple[int, int, _WorkerProcessState]] = {}
        self._probed_at: dict[str, float] = {}
        # Session ID → (inbox fingerprint, holds accepted deferred input).
        self._inbox_files: dict[str, tuple[tuple[int, int, int], bool]] = {}
        self._inbox_scanned_at: float | None = None
        # A deferred-input candidate whose record and inbox are unchanged since
        # it was found to have nothing to start is not re-read.
        self._input_settled: dict[str, tuple[BackgroundSession, tuple[int, int, int]]] = {}
        self._input_retry_at: dict[str, float] = {}
        self._input_failures: dict[str, int] = {}

    def run(self, trigger: MaintenanceTrigger) -> MaintenanceOutcome:
        now = time.time()
        active = tuple(self.store.active_sessions())
        deadlines: list[float] = []
        changed: dict[str, BackgroundSession] = {}
        probes = self._reconcile_workers(active, trigger, now, deadlines, changed)
        if changed:
            active = tuple(self.store.active_sessions())
        self._recover_input(active, trigger, now, deadlines, changed)
        self._dispatch(active, trigger, now, deadlines, changed)
        workers = {
            session_id: pid
            for session_id, (pid, _attempt, state) in self._probed.items()
            if state in _WATCHABLE
            and (session_id not in changed or changed[session_id].status in ACTIVE_STATUSES)
        }
        return MaintenanceOutcome(
            changed=tuple(changed.values()),
            probes=probes,
            next_due_at=min(deadlines) if deadlines else None,
            workers=workers,
        )

    # ── workers ─────────────────────────────────────────────────────────────

    def _reconcile_workers(
        self,
        active: tuple[BackgroundSession, ...],
        trigger: MaintenanceTrigger,
        now: float,
        deadlines: list[float],
        changed: dict[str, BackgroundSession],
    ) -> int:
        tracked = [session for session in active if reconciles_worker(session)]
        tracked_ids = {session.session_id for session in tracked}
        for session_id in tuple(self._probed):
            if session_id not in tracked_ids:
                del self._probed[session_id]
                self._probed_at.pop(session_id, None)
        probes = 0
        states: dict[str, _WorkerProcessState] = {}
        due: list[BackgroundSession] = []
        for session in tracked:
            session_id = session.session_id
            deadline = session.last_active + self.stale_after_s if session.last_active else None
            expired = deadline is not None and now > deadline
            pid = session.worker_pid
            probed = self._probed.get(session_id)
            unseen = pid is not None and (
                probed is None or probed[0] != pid or probed[1] != session.attempt
            )
            polled = (
                pid is not None
                and session_id not in trigger.exit_watched
                and now - self._probed_at.get(session_id, -math.inf) >= self.poll_interval_s
            )
            if (
                trigger.full
                or session_id in trigger.exited
                or unseen
                or polled
                or (expired and not session.heartbeat_stale)
                or (session.heartbeat_stale and not expired)
            ):
                if pid is not None:
                    state = self.supervisor._worker_process_state(session)
                    probes += 1
                    states[session_id] = state
                    self._probed[session_id] = (pid, session.attempt, state)
                    self._probed_at[session_id] = now
                due.append(session)
            if deadline is not None and not expired and not session.heartbeat_stale:
                deadlines.append(deadline)
        for updated in self.supervisor._recover_stale_records(
            due, self.stale_after_s, process_states=states
        ):
            changed[updated.session_id] = updated
        return probes

    # ── deferred input ──────────────────────────────────────────────────────

    def _recover_input(
        self,
        active: tuple[BackgroundSession, ...],
        trigger: MaintenanceTrigger,
        now: float,
        deadlines: list[float],
        changed: dict[str, BackgroundSession],
    ) -> None:
        self._refresh_inbox(trigger, now)
        if self.inbox_rescan_s is not None and self._inbox_scanned_at is not None:
            deadlines.append(self._inbox_scanned_at + self.inbox_rescan_s)
        active_ids = {session.session_id for session in active}
        for session_id, (fingerprint, holds) in tuple(self._inbox_files.items()):
            if not holds or session_id in active_ids:
                # A live attempt consumes its own inbox.
                continue
            retry_at = self._input_retry_at.get(session_id)
            if retry_at is not None and now < retry_at:
                deadlines.append(retry_at)
                continue
            try:
                current = self.store.get(session_id, include_deleted=True)
            except SessionNotFound:
                continue
            if self._input_settled.get(session_id) == (current, fingerprint):
                continue
            message_id = self.supervisor._pending_input_message(current, self._inbox)
            if message_id is None:
                self._input_settled[session_id] = (current, fingerprint)
                continue
            if not self.supervisor.has_input_capacity(current.cwd, active):
                continue  # retried when a registry change frees a slot
            started = self.supervisor._try_start_deferred_input(current, message_id)
            if started is not None:
                changed[session_id] = started
            if started is not None and started.status in ACTIVE_STATUSES:
                self._input_failures.pop(session_id, None)
                self._input_retry_at.pop(session_id, None)
                continue
            failures = self._input_failures.get(session_id, 0) + 1
            self._input_failures[session_id] = failures
            delay = min(_INPUT_RETRY_CAP_S, self.poll_interval_s * 2 ** (failures - 1))
            self._input_retry_at[session_id] = now + delay
            deadlines.append(now + delay)

    def _refresh_inbox(self, trigger: MaintenanceTrigger, now: float) -> None:
        rescan = (
            trigger.full
            or self._inbox_scanned_at is None
            or trigger.inbox_changed is None
            or (
                self.inbox_rescan_s is not None
                and now - self._inbox_scanned_at >= self.inbox_rescan_s
            )
        )
        if rescan:
            present = self._inbox_session_ids()
            for session_id in tuple(self._inbox_files):
                if session_id not in present:
                    del self._inbox_files[session_id]
            self._inbox_scanned_at = now
            check: set[str] = present
        else:
            assert trigger.inbox_changed is not None
            check = {
                name[: -len(_INBOX_SUFFIX)]
                for name in trigger.inbox_changed
                if name.endswith(_INBOX_SUFFIX)
            }
        for session_id in check:
            self._refresh_inbox_file(session_id)

    def _inbox_session_ids(self) -> set[str]:
        try:
            with os.scandir(self._inbox.root) as entries:
                return {
                    entry.name[: -len(_INBOX_SUFFIX)]
                    for entry in entries
                    if entry.name.endswith(_INBOX_SUFFIX) and entry.is_file()
                }
        except FileNotFoundError:
            return set()

    def _refresh_inbox_file(self, session_id: str) -> None:
        try:
            fingerprint = self._inbox.fingerprint(session_id)
        except ValueError:
            return  # not a session's inbox
        if fingerprint == (0, 0, 0):
            self._inbox_files.pop(session_id, None)
            return
        cached = self._inbox_files.get(session_id)
        if cached is not None and cached[0] == fingerprint:
            return
        try:
            receipts = self._inbox.receipts(session_id)
        except (OSError, ValueError):
            return
        holds = any(item.state == "accepted" and item.deferred for item in receipts)
        self._inbox_files[session_id] = (fingerprint, holds)

    # ── queue dispatch ──────────────────────────────────────────────────────

    def _dispatch(
        self,
        active: tuple[BackgroundSession, ...],
        trigger: MaintenanceTrigger,
        now: float,
        deadlines: list[float],
        changed: dict[str, BackgroundSession],
    ) -> None:
        lease_ends = [
            session.dispatch_claimed_at + self.supervisor.dispatch_lease_s
            for session in active
            if session.status is SessionStatus.STARTING
            and session.dispatch_state is QueueDispatchState.CLAIMED
            and session.worker_pid is None
            and session.dispatch_claimed_at is not None
        ]
        deadlines.extend(end for end in lease_ends if end > now)
        queued = any(is_pending_queue_record(session) for session in active)
        if not (
            trigger.full
            or any(end <= now for end in lease_ends)
            or (queued and (trigger.registry_changed or bool(changed)))
        ):
            return
        for updated in self.supervisor.dispatch_pending(now=now):
            changed[updated.session_id] = updated
