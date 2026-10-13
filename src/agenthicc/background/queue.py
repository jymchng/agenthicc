"""Pure queue policy helpers shared by admission, dispatch, and projections."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
import os
from typing import TYPE_CHECKING

from .model import QueueDispatchState, QueuePriority, SessionStatus

if TYPE_CHECKING:
    from .model import BackgroundSession


_PRIORITY_BASE = {
    QueuePriority.LOW: 0,
    QueuePriority.NORMAL: 1,
    QueuePriority.URGENT: 2,
}


def current_owner_id() -> str:
    """Return a stable, local user key without requiring a platform service."""

    try:
        return f"uid:{os.getuid()}"
    except AttributeError:  # pragma: no cover - Windows
        import getpass  # noqa: PLC0415

        return f"user:{getpass.getuser()}"


def is_pending_queue_record(session: BackgroundSession) -> bool:
    """Whether a session is accepted but has not reserved a worker slot."""

    return (
        session.status is SessionStatus.QUEUED
        and session.dispatch_state is QueueDispatchState.PENDING
    )


def occupies_worker_slot(session: BackgroundSession) -> bool:
    """Count a started/reserved worker, never an unclaimed pending queue item."""

    if session.status is SessionStatus.QUEUED:
        # Legacy queued records may already have a process in flight. Treat
        # records without the new pending marker conservatively as occupying
        # capacity; this prevents a migration from over-admitting workers.
        return not is_pending_queue_record(session)
    return session.status in {
        SessionStatus.STARTING,
        SessionStatus.RUNNING,
        SessionStatus.WAITING_APPROVAL,
        SessionStatus.WAITING_INPUT,
        SessionStatus.RETRYING,
        SessionStatus.CANCELLING,
    }


def effective_priority_rank(
    session: BackgroundSession,
    *,
    now: float,
    aging_interval_s: float,
) -> int:
    """Return an unbounded aged rank, so old low-priority work eventually wins."""

    age = max(0.0, now - session.created_at)
    return _PRIORITY_BASE[session.priority] + int(age // aging_interval_s)


def fair_queue_order(
    sessions: Iterable[BackgroundSession],
    *,
    now: float,
    aging_interval_s: float,
    last_project: str = "",
) -> tuple[BackgroundSession, ...]:
    """Order pending records with project round-robin and per-project aging.

    Project fairness is the outer scheduling discipline: each eligible
    project contributes at most one item per round. Within a project, the
    highest aged priority wins, then FIFO creation time and session ID break
    ties. Position numbers are advisory because capacity can change after this
    projection is calculated.
    """

    groups: dict[str, list[BackgroundSession]] = defaultdict(list)
    for session in sessions:
        if is_pending_queue_record(session):
            groups[session.cwd].append(session)
    if not groups:
        return ()

    for records in groups.values():
        records.sort(
            key=lambda item: (
                -effective_priority_rank(item, now=now, aging_interval_s=aging_interval_s),
                item.created_at,
                item.session_id,
            )
        )

    projects = sorted(groups)
    if last_project in projects:
        pivot = projects.index(last_project) + 1
        projects = projects[pivot:] + projects[:pivot]

    ordered: list[BackgroundSession] = []
    round_index = 0
    while True:
        added = False
        for project in projects:
            records = groups[project]
            if round_index < len(records):
                ordered.append(records[round_index])
                added = True
        if not added:
            break
        round_index += 1
    return tuple(ordered)


def queue_position_map(
    sessions: Iterable[BackgroundSession],
    *,
    now: float,
    aging_interval_s: float,
    last_project: str = "",
) -> dict[str, int]:
    """Return one-based advisory positions for the durable pending queue."""

    return {
        session.session_id: index
        for index, session in enumerate(
            fair_queue_order(
                sessions,
                now=now,
                aging_interval_s=aging_interval_s,
                last_project=last_project,
            ),
            start=1,
        )
    }


def parse_priority(value: QueuePriority | str) -> QueuePriority:
    """Normalize user-facing priorities and reject unknown values."""

    if isinstance(value, QueuePriority):
        return value
    try:
        return QueuePriority(value.strip().lower())
    except (AttributeError, ValueError) as exc:
        expected = ", ".join(item.value for item in QueuePriority)
        raise ValueError(f"priority must be one of: {expected}") from exc
