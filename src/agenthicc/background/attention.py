"""Cross-process presence for foreground TUI attention routing."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agents_service import AgentsService
    from .inbox import BackgroundInboxItem
    from .model import BackgroundSession

from agenthicc.runners.process_lease import (
    InterProcessLock,
    InterProcessLockBusy,
    InterProcessLockError,
)

__all__ = ["TUIAttentionLease", "tui_is_active"]


class TUIAttentionLease:
    """Hold one advisory lease while a TUI owns its terminal."""

    def __init__(self, background_root: Path | str) -> None:
        self._directory = Path(background_root).expanduser() / "attention-clients"
        self.path = self._directory / f"tui-{os.getpid()}-{uuid.uuid4().hex}.lock"
        self._lock = InterProcessLock(self.path, blocking=False)
        self._held = False

    def acquire(self) -> None:
        """Publish this live TUI without blocking another session."""
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._directory, 0o700)
        except OSError:
            pass
        self._lock.__enter__()
        self._held = True
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        """Release the live marker and remove its private lock file."""
        if not self._held:
            return
        self._held = False
        self._lock.__exit__(None, None, None)
        try:
            self.path.unlink()
        except OSError:
            pass


def tui_is_active(background_root: Path | str) -> bool:
    """Whether any process currently holds a TUI attention lease."""
    directory = Path(background_root).expanduser() / "attention-clients"
    try:
        candidates = tuple(directory.glob("tui-*.lock"))
    except OSError:
        return True
    for path in candidates:
        lock = InterProcessLock(path, blocking=False)
        try:
            lock.__enter__()
        except InterProcessLockBusy:
            return True
        except InterProcessLockError:
            # An unreadable marker must not turn an active TUI into terminal
            # output from an independent notification dispatcher.
            return True
        else:
            lock.__exit__(None, None, None)
            try:
                path.unlink()
            except OSError:
                pass
    return False


@dataclass(frozen=True)
class _BackgroundAttentionSnapshot:
    sessions: tuple[BackgroundSession, ...]
    needs_you_count: int
    running_count: int
    pending_item_ids: frozenset[str]
    pending_session_ids: frozenset[str]
    oldest_item: BackgroundInboxItem | None


async def _load_attention_snapshot(service: AgentsService) -> _BackgroundAttentionSnapshot:
    """Read a bounded lifecycle and exact inbox projection off the event loop."""
    import time

    from .inbox import BackgroundInboxItem
    from .index import SessionFilter, SessionOrder
    from .model import ACTIVE_STATUSES, SessionStatus

    active = await service.select_async(
        SessionFilter(statuses=ACTIVE_STATUSES, include_archived=False),
        order=SessionOrder.RECENT,
    )
    recent_terminal = await service.select_async(
        SessionFilter(
            statuses=frozenset({SessionStatus.COMPLETED, SessionStatus.FAILED}),
            include_archived=False,
            active_since=time.time() - 30 * 60,
        ),
        order=SessionOrder.RECENT,
        limit=250,
    )
    value = await service.run_blocking(service.inbox.pending_summary)
    if (
        not isinstance(value, tuple)
        or len(value) != 4
        or not isinstance(value[0], int)
        or isinstance(value[0], bool)
        or value[0] < 0
        or not isinstance(value[1], frozenset)
        or not all(isinstance(session_id, str) for session_id in value[1])
        or not isinstance(value[2], frozenset)
        or not all(isinstance(item_id, str) for item_id in value[2])
        or (value[3] is not None and not isinstance(value[3], BackgroundInboxItem))
    ):
        raise TypeError("background inbox returned an invalid attention summary")
    pending_count, pending_session_ids, pending_item_ids, oldest_item = value
    sessions_by_id: dict[str, BackgroundSession] = {
        session.session_id: session for session in (*active, *recent_terminal)
    }
    sessions = tuple(sessions_by_id.values())
    waiting_statuses = {SessionStatus.WAITING_APPROVAL, SessionStatus.WAITING_INPUT}
    waiting_uncovered = {
        session.session_id
        for session in active
        if session.status in waiting_statuses and session.session_id not in pending_session_ids
    }
    lost_count = sum(session.status is SessionStatus.ORPHANED for session in active)
    running_count = sum(
        session.status not in waiting_statuses | {SessionStatus.ORPHANED} for session in active
    )
    return _BackgroundAttentionSnapshot(
        sessions=sessions,
        needs_you_count=pending_count + len(waiting_uncovered) + lost_count,
        running_count=running_count,
        pending_item_ids=pending_item_ids,
        pending_session_ids=pending_session_ids,
        oldest_item=oldest_item,
    )
