"""Append-only background-session lifecycle storage (PRD-141)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, List, Mapping

from agenthicc.runners.process_lease import InterProcessLock
from .index import IndexState, SearchMode, SessionFilter, SessionIndex, SessionOrder
from .model import (
    ACTIVE_STATUSES,
    ATTEMPT_HISTORY_LIMIT,
    BackgroundAttempt,
    BackgroundSession,
    ModeApplicationStatus,
    PriorityChange,
    QueueDispatchState,
    QueuePriority,
    SessionStatus,
    legal_transition,
)

if TYPE_CHECKING:
    from .notifications import BackgroundNotificationCoordinator
    from .settings import BackgroundNotificationSettings


class SessionNotFound(KeyError):
    """Raised when a requested background session is not in the registry."""


class InvalidSessionTransition(ValueError):
    """Raised when a lifecycle operation would violate the state contract."""


class IndexRebuildCancelled(RuntimeError):
    """Raised inside a rebuild that ``cancel_rebuild`` stopped; nothing is kept."""


class BackgroundEventLogError(RuntimeError):
    """Raised when the authoritative background event log cannot be read."""


def _sqlite_is_busy(error: sqlite3.OperationalError) -> bool:
    """Return whether SQLite reports lock contention rather than a schema fault."""

    message = str(error).casefold()
    return "database is locked" in message or "database is busy" in message


def _sqlite_is_schema_error(error: sqlite3.OperationalError) -> bool:
    """Recognize missing-schema errors that justify repairing this derived index."""

    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "no such column",
            "no such table",
            "has no column named",
            "malformed database schema",
        )
    )


def default_background_root() -> Path:
    return Path.home() / ".agenthicc" / "background"


def default_artifact_dir(session_id: str) -> Path:
    return Path.home() / ".agenthicc" / "sessions" / session_id


def _json_object(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _changed_fields(previous: BackgroundSession, updated: BackgroundSession) -> dict[str, object]:
    """Return the serialized fields *updated* changed, always with last_active.

    updated events carry only these (AGX-04); replay applies them with
    evolve, exactly as it applies the full records older logs contain.
    """
    before = previous.to_dict()
    after = updated.to_dict()
    changes = {
        name: value
        for name, value in after.items()
        if name != "session_id" and before.get(name) != value
    }
    if "mode_name" in changes:
        # evolve() reads a mode name that arrives without its application
        # status as a new, still-pending request; replay needs the status the
        # update actually recorded.
        changes["mode_application_status"] = after["mode_application_status"]
    changes["last_active"] = after["last_active"]
    return changes


#: A filter that keeps every session except deleted ones.
_EVERY_SESSION = SessionFilter()


@dataclass(frozen=True)
class BackgroundWindow:
    """A bounded window of a registry listing, starting at row ``offset``.

    ``events.jsonl`` remains authoritative. The window holds only the records
    a viewport needs, while ``total`` and ``generation`` let a UI reconcile its
    selection and detect stale asynchronous results.
    """

    sessions: tuple[BackgroundSession, ...]
    total: int
    offset: int
    generation: tuple[int, int, int, int]
    stale: bool = False


class _EventFolder:
    """Fold lifecycle events into session records, deferring heartbeats.

    Heartbeats — ``updated`` events that only move ``last_active`` — are
    most of a long log. Only each session's newest heartbeat is applied, just
    before that session's next other event or at the end, which yields the same
    records as applying every event in order.
    """

    __slots__ = ("records", "_lookup", "_pending")

    def __init__(self, lookup: Callable[[str], BackgroundSession | None]) -> None:
        self.records: dict[str, BackgroundSession] = {}
        self._lookup = lookup
        self._pending: dict[str, float] = {}

    def apply(self, event: dict[str, object]) -> None:
        payload_value = event.get("payload")
        payload = payload_value if type(payload_value) is dict else {}
        session_id = payload.get("session_id")
        if type(session_id) is not str or not session_id:
            return
        records = self.records
        event_type = event.get("event_type")
        if session_id not in records and event_type != "created":
            loaded = self._lookup(session_id)
            if loaded is not None:
                records[session_id] = loaded
        changes_value = payload.get("changes")
        changes = changes_value if type(changes_value) is dict else {}
        activity = changes.get("last_active")
        if (
            event_type == "updated"
            and session_id in records
            and len(changes) == 1
            and (type(activity) is int or type(activity) is float)
        ):
            self._pending[session_id] = float(activity)
            return
        pending_activity = self._pending.pop(session_id, None)
        if pending_activity is not None and event_type != "created":
            current = records.get(session_id)
            if current is not None:
                records[session_id] = replace(current, last_active=pending_activity)
        updated = BackgroundStore._apply_event(records.get(session_id), event)
        if updated is not None:
            records[session_id] = updated

    def finish(self) -> dict[str, BackgroundSession]:
        records = self.records
        for session_id, last_active in self._pending.items():
            current = records.get(session_id)
            if current is not None:
                records[session_id] = replace(current, last_active=last_active)
        self._pending.clear()
        return records


class BackgroundStore:
    """Durable registry whose authoritative history is ``events.jsonl``.

    The JSONL history is intentionally simple and rebuildable. Each event is
    appended with ``O_APPEND`` and fsync'd. Reads go to a derived SQLite index,
    ``index.sqlite3`` (AGX-03), with one row per session: a reader first applies
    any complete events appended since the position the index records, and a
    missing, corrupt, rotated, truncated or rewritten log rebuilds it. A deleted
    session receives a tombstone so a stale worker or rebuilt index cannot
    resurrect it.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        search_mode: SearchMode | None = None,
    ) -> None:
        self.root = Path(root or default_background_root()).expanduser()
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "registry.lock"
        self.trash_root = self.root / "trash"
        self.index_path = self.root / "index.sqlite3"
        # The JSON projection older registries kept; imported once to seed the
        # index, then deleted (AGX-03 migration).
        self._legacy_snapshot_path = self.root / "projection-v1.json"
        self._index = SessionIndex(self.index_path, search_mode=search_mode)
        # Manager refresh and stale-worker maintenance share one store across
        # executor threads; they share its index connection under this lock.
        self._index_lock = threading.RLock()
        # The log fingerprint this process last saw fully applied. A read whose
        # log still matches it touches neither the log nor the index meta.
        self._applied: tuple[int, int, int] | None = None
        self._applied_sequence = 0
        # Whether an incomplete final record follows the applied offset: a
        # writer between ``write`` and its newline, or one that died there.
        self._partial_tail = False
        self._rebuild_progress: tuple[int, int] | None = None
        self._rebuild_cancelled = threading.Event()
        self._notification_coordinator: BackgroundNotificationCoordinator | None = None

    @property
    def search_mode(self) -> SearchMode:
        """Whether registry search uses the FTS5 trigram index or a ``LIKE`` scan."""

        with self._index_lock:
            return self._index.search_mode

    def rebuild_progress(self) -> tuple[int, int] | None:
        """Return ``(bytes_read, log_bytes)`` while the index is rebuilding."""

        return self._rebuild_progress

    def cancel_rebuild(self) -> None:
        """Stop an index rebuild in progress (its transaction rolls back).

        Closing the agents screen calls this, so quitting during a long first
        rebuild does not wait for it; the next open starts the rebuild again.
        """

        if self._rebuild_progress is not None:
            self._rebuild_cancelled.set()

    def _events_fingerprint(self) -> tuple[int, int, int]:
        """Return a cheap external-change fingerprint for ``events.jsonl``."""

        try:
            stat = self.events_path.stat()
        except FileNotFoundError:
            return (0, 0, 0)
        except OSError as exc:
            raise BackgroundEventLogError(
                "The authoritative background event log could not be accessed"
            ) from exc
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def invalidate_index(self) -> None:
        """Forget what this process applied; the next read re-checks the log."""

        with self._index_lock:
            self._applied = None

    def _lock(self) -> InterProcessLock:
        """Exclusive cross-process lock; raises ``InterProcessLockError`` if unavailable."""
        return InterProcessLock(self.lock_path)

    def _iter_events(self) -> Iterator[dict[str, object]]:
        """Yield authoritative lifecycle events without retaining the log."""

        for event, _offset in self._iter_events_with_offsets():
            yield event

    def _iter_events_with_offsets(self) -> Iterator[tuple[dict[str, object], int]]:
        """Yield complete events with the byte offset just past each one."""

        try:
            self.events_path.stat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BackgroundEventLogError(
                "The authoritative background event log could not be accessed"
            ) from exc
        decode = json.JSONDecoder().decode
        try:
            with self.events_path.open("rb") as handle:
                offset = 0
                for line in handle:
                    offset += len(line)
                    if not line.endswith(b"\n"):
                        return  # an incomplete final record is not yet an event
                    try:
                        raw = decode(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    # JSON object keys are always strings.
                    if isinstance(raw, dict):
                        yield raw, offset
        except OSError as exc:
            raise BackgroundEventLogError(
                "The authoritative background event log could not be read"
            ) from exc

    def iter_events(self) -> Iterator[dict[str, object]]:
        """Iterate authoritative events for durable sidecar reconciliation."""

        return self._iter_events()

    def configure_notifications(
        self,
        settings: BackgroundNotificationSettings,
        *,
        dispatcher_launcher: Callable[[Path], object] | None = None,
    ) -> BackgroundNotificationCoordinator | None:
        """Attach the optional notification outbox and recover journal gaps.

        The settings argument is intentionally typed at runtime in the
        notifications module to avoid coupling lifecycle persistence to
        desktop or HTTP delivery implementations.
        """

        from .settings import BackgroundNotificationSettings

        if not isinstance(settings, BackgroundNotificationSettings):
            raise TypeError("settings must be BackgroundNotificationSettings")
        if not settings.enabled and not (self.root / "notifications" / "outbox.sqlite3").exists():
            self._notification_coordinator = None
            return None
        existing = self._notification_coordinator
        if existing is not None and existing.settings == settings:
            existing.reconcile(self)
            return existing
        from .notifications import BackgroundNotificationCoordinator

        coordinator = BackgroundNotificationCoordinator(
            self.root,
            settings,
            dispatcher_launcher=dispatcher_launcher,
        )
        self._notification_coordinator = coordinator
        coordinator.reconcile(self)
        return coordinator

    def _read_events(self) -> list[dict[str, object]]:
        """Return a complete event list for compatibility and focused tests."""

        return list(self._iter_events())

    def _read_event_tail(self, offset: int) -> tuple[list[dict[str, object]], int, bytes]:
        """Read complete JSONL records after *offset*.

        A writer can be observed between ``write`` and the terminating
        newline.  The incomplete bytes are retained and retried on the next
        catch-up rather than being treated as corruption.
        """

        try:
            with self.events_path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError as exc:
            raise BackgroundEventLogError(
                "The authoritative background event log could not be read"
            ) from exc
        if not data:
            return [], offset, b""
        lines = data.splitlines(keepends=True)
        tail = b""
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            tail = lines.pop()
        consumed = len(data) - len(tail)
        events: list[dict[str, object]] = []
        for line in lines:
            try:
                raw = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                events.append({str(key): item for key, item in raw.items()})
        return events, offset + consumed, tail

    @staticmethod
    def _apply_event(
        current: BackgroundSession | None, event: Mapping[str, object]
    ) -> BackgroundSession | None:
        """Return the session after *event*, or ``None`` when it changes nothing."""

        payload_value = event.get("payload")
        # JSON decoding already gives us a plain dict. Avoid copying the
        # payload and invoking typing.Mapping's runtime ABC checks for every
        # record in a large cold replay; retain the generic mapping fallback
        # for direct callers and tests.
        payload = payload_value if isinstance(payload_value, dict) else _json_object(payload_value)
        event_type = event.get("event_type")
        if event_type == "created":
            try:
                return BackgroundSession.from_mapping(payload)
            except (TypeError, ValueError):
                return None
        if current is None:
            return None
        if event_type == "updated":
            changes_value = payload.get("changes")
            changes = (
                changes_value if isinstance(changes_value, dict) else _json_object(changes_value)
            )
            if len(changes) == 1 and "last_active" in changes:
                # Activity heartbeats are by far the most frequent lifecycle
                # event. ``evolve`` normalizes every field on the 50+ field
                # dataclass for this one changed value. The current record is
                # already typed/validated, so preserve its semantics with a
                # focused replacement; malformed values remain ignored just
                # as ``evolve`` would do.
                value = changes["last_active"]
                return (
                    replace(current, last_active=float(value))
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    else None
                )
            try:
                return current.evolve(**changes)
            except (TypeError, ValueError):
                return None
        if event_type == "deleted":
            event_timestamp = event.get("timestamp")
            timestamp = (
                float(event_timestamp)
                if isinstance(event_timestamp, (int, float))
                and not isinstance(event_timestamp, bool)
                else time.time()
            )
            return current.evolve(
                status=SessionStatus.DELETED,
                trash_dir=str(payload.get("trash_dir", current.trash_dir)),
                artifact_dir="",
                last_active=timestamp,
                delete_operation_id=str(payload.get("operation_id", current.delete_operation_id)),
                delete_phase="completed",
                delete_error="",
            )
        if event_type == "priority_changed":
            previous_priority = payload.get("previous")
            current_priority = payload.get("current")
            if not isinstance(previous_priority, str) or not isinstance(current_priority, str):
                return None
            change = PriorityChange.from_mapping(
                {
                    "previous": previous_priority,
                    "current": current_priority,
                    "changed_at": event.get("timestamp"),
                    "changed_by": payload.get("changed_by"),
                }
            )
            return current.evolve(
                priority=change.current,
                priority_history=(*current.priority_history, change)[-32:],
            )
        return None

    @staticmethod
    def _event_session_id(event: Mapping[str, object]) -> str:
        payload = event.get("payload")
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        return session_id if isinstance(session_id, str) else ""

    @staticmethod
    def _event_sequence(event: Mapping[str, object]) -> int | None:
        sequence = event.get("seq")
        return sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else None

    # ── index synchronisation ───────────────────────────────────────────────

    def _sync(self) -> None:
        """Bring the index up to the complete records in ``events.jsonl``."""

        with self._index_lock:
            fingerprint = self._events_fingerprint()
            if self._applied == fingerprint and self._index.connection_matches_path():
                return
            schema_reset = False
            try:
                state = self._index.state()
            except sqlite3.OperationalError as exc:
                if _sqlite_is_schema_error(exc):
                    self._index.reset_schema()
                    state = None
                    schema_reset = True
                elif _sqlite_is_busy(exc):
                    raise
                else:
                    raise
            except sqlite3.DatabaseError:
                self._index.discard()
                state = None
            if fingerprint == (0, 0, 0) and state is not None and state.inode != 0:
                # An empty fingerprint means the file is absent, not that the
                # previously indexed event history became an empty registry.
                # Never replace useful projection data with an empty replay.
                raise BackgroundEventLogError("The authoritative background event log is missing")
            if state is not None and fingerprint == (state.inode, state.offset, state.mtime_ns):
                self._remember(fingerprint, state.sequence, partial=False)
                return
            try:
                self._sync_transaction(fingerprint)
            except sqlite3.OperationalError as exc:
                if _sqlite_is_schema_error(exc) and not schema_reset:
                    self._index.reset_schema()
                    self._applied = None
                    self._sync_transaction(fingerprint)
                else:
                    raise
            except sqlite3.DatabaseError:
                # A corrupt file can fail anywhere; it is derived, so rebuild.
                self._index.discard()
                self._sync_transaction(fingerprint)

    def _sync_transaction(self, fingerprint: tuple[int, int, int]) -> None:
        connection = self._index.begin()
        try:
            # Re-read inside the write transaction: another process may have
            # applied the same tail while this one waited for the lock.
            state = self._index.state()
            if state is not None and fingerprint == (state.inode, state.offset, state.mtime_ns):
                self._index.commit(connection)
                self._remember(fingerprint, state.sequence, partial=False)
                return
            if state is not None and self._tail_applicable(state, fingerprint):
                applied = self._apply_tail(connection, state, fingerprint)
                if applied:
                    self._index.commit(connection)
                    return
            seeded = (
                None if state is not None else self._seed_from_snapshot(connection, fingerprint)
            )
            if seeded is None or not self._apply_tail(connection, seeded, fingerprint):
                self._rebuild(connection, fingerprint)
            self._index.commit(connection)
        except BaseException:
            self._index.rollback(connection)
            self._rebuild_progress = None
            raise
        # The index now covers everything an old projection could describe.
        try:
            self._legacy_snapshot_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _seed_from_snapshot(
        self, connection: sqlite3.Connection, fingerprint: tuple[int, int, int]
    ) -> IndexState | None:
        """Import the pre-index JSON projection if it still describes this log.

        Registries written before the SQLite index kept ``projection-v1.json``
        — every session plus the log position it covered. Loading it replaces
        a replay of the whole log, which full-record update events make slow,
        with a load of the records and a replay of the tail.
        """
        try:
            raw = json.loads(self._legacy_snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            return None
        inode = raw.get("event_inode")
        size = raw.get("event_size")
        mtime_ns = raw.get("event_mtime_ns")
        offset = raw.get("event_offset")
        sequence = raw.get("sequence")
        records = raw.get("records")
        if not (
            type(inode) is int
            and type(size) is int
            and type(mtime_ns) is int
            and type(offset) is int
            and type(sequence) is int
            and isinstance(records, list)
        ):
            return None
        if inode != fingerprint[0] or not 0 <= offset <= fingerprint[1] or size != offset:
            return None
        if size == fingerprint[1] and mtime_ns != fingerprint[2]:
            return None  # same length, rewritten
        sessions: list[BackgroundSession] = []
        try:
            for item in records:
                if not isinstance(item, dict):
                    return None
                sessions.append(BackgroundSession.from_mapping(item))
        except (TypeError, ValueError):
            return None
        state = IndexState(inode=inode, offset=offset, mtime_ns=mtime_ns, sequence=sequence)
        self._index.replace_all(connection, sessions, state)
        return state

    @staticmethod
    def _tail_applicable(state: IndexState, fingerprint: tuple[int, int, int]) -> bool:
        inode, size, mtime_ns = fingerprint
        if state.inode != inode or state.offset > size:
            return False  # rotated or truncated
        # The same size with another mtime is a rewrite, not an append.
        return state.offset < size or state.mtime_ns == mtime_ns

    def _apply_tail(
        self,
        connection: sqlite3.Connection,
        state: IndexState,
        fingerprint: tuple[int, int, int],
    ) -> bool:
        """Apply events after the recorded offset; ``False`` asks for a rebuild."""

        events, offset, tail = self._read_event_tail(state.offset)
        sequence = state.sequence
        folder = _EventFolder(self._index.get)
        for event in events:
            event_sequence = self._event_sequence(event)
            if event_sequence is not None:
                if event_sequence <= sequence or (sequence > 0 and event_sequence != sequence + 1):
                    return False
                sequence = event_sequence
            folder.apply(event)
        changed = folder.finish()
        applied = IndexState(
            inode=fingerprint[0],
            offset=offset,
            mtime_ns=fingerprint[2],
            sequence=sequence,
        )
        self._index.upsert(connection, changed.values(), applied)
        self._remember(fingerprint, sequence, partial=bool(tail))
        return True

    def _rebuild(self, connection: sqlite3.Connection, fingerprint: tuple[int, int, int]) -> None:
        """Replace the index with a fold of the whole log, reporting progress."""

        total = fingerprint[1]
        self._rebuild_cancelled.clear()
        self._rebuild_progress = (0, total)
        # A rebuild starts empty: every session comes from its ``created`` event.
        folder = _EventFolder(lambda _session_id: None)
        max_sequence = 0
        offset = 0
        for count, (event, offset) in enumerate(self._iter_events_with_offsets(), start=1):
            if count % 4096 == 0:
                self._rebuild_progress = (offset, total)
                if self._rebuild_cancelled.is_set():
                    raise IndexRebuildCancelled("session index rebuild cancelled")
            event_sequence = event.get("seq")
            if type(event_sequence) is int and event_sequence > max_sequence:
                max_sequence = event_sequence
            folder.apply(event)
        records = folder.finish()
        state = IndexState(
            inode=fingerprint[0],
            offset=offset,
            mtime_ns=fingerprint[2],
            sequence=max_sequence,
        )
        self._index.replace_all(connection, records.values(), state)
        self._rebuild_progress = None
        self._remember(fingerprint, max_sequence, partial=offset < fingerprint[1])

    def _remember(self, fingerprint: tuple[int, int, int], sequence: int, *, partial: bool) -> None:
        self._applied = fingerprint
        self._applied_sequence = sequence
        self._partial_tail = partial

    def recent_from_log_tail(
        self, *, max_bytes: int = 4 * 1024 * 1024, limit: int = 50
    ) -> tuple[BackgroundSession, ...]:
        """Return recently created sessions from the end of the log, unindexed.

        While a missing or invalid index is rebuilt, the agents screen shows
        these as a provisional page: sessions whose ``created`` event lies in
        the last *max_bytes*, with the events that follow it applied. A log
        written before AGX-04 also yields sessions from full-record
        ``updated`` events; a delta ``updated`` event cannot reconstruct a
        session created before the window.
        """

        try:
            with self.events_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                start = max(0, size - max_bytes)
                # One byte earlier shows whether the window starts mid-line.
                handle.seek(max(0, start - 1))
                data = handle.read()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise BackgroundEventLogError(
                "The authoritative background event log could not be read"
            ) from exc
        if start > 0:
            newline = data.find(b"\n")
            data = data[newline + 1 :] if newline >= 0 else b""
        records: dict[str, BackgroundSession] = {}
        for line in data.splitlines():
            try:
                raw = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            session_id = self._event_session_id(raw)
            if not session_id:
                continue
            if raw.get("event_type") != "created" and session_id not in records:
                # Update events written before AGX-04 carry the whole record.
                full = self._full_record(session_id, raw)
                if full is None:
                    continue
                records[session_id] = full
                continue
            updated = self._apply_event(records.get(session_id), raw)
            if updated is not None:
                records[session_id] = updated
        visible = [item for item in records.values() if item.status != SessionStatus.DELETED]
        visible.sort(key=lambda item: (not item.pinned, -item.last_active, item.session_id))
        return tuple(visible[:limit])

    @staticmethod
    def _full_record(session_id: str, event: Mapping[str, object]) -> BackgroundSession | None:
        """Return the session an ``updated`` event carries in full, if it does."""

        if event.get("event_type") != "updated":
            return None
        payload = event.get("payload")
        changes = payload.get("changes") if isinstance(payload, dict) else None
        if not isinstance(changes, dict) or not {"title", "cwd", "workflow_name", "intent"} <= set(
            changes
        ):
            return None
        try:
            return BackgroundSession.from_mapping({**changes, "session_id": session_id})
        except (TypeError, ValueError):
            return None

    # ── appends ──────────────────────────────────────────────────────────────

    def _append(self, event_type: str, payload: Mapping[str, object]) -> None:
        with self._index_lock:
            self._append_locked(event_type, payload)

    def _append_locked(self, event_type: str, payload: Mapping[str, object]) -> None:
        # Synchronise the index before allocating a sequence number.  The
        # registry lock serialises writers in this process and across local
        # processes; the append itself remains O_APPEND + fsync'd.
        self._sync()
        if self._partial_tail:
            # A process may have died halfway through a JSONL append. Preserve
            # every complete event and discard only the incomplete final line
            # before allocating the next sequence number.
            self._truncate_partial_tail()
        seq = self._applied_sequence + 1
        record: dict[str, object] = {
            "seq": seq,
            "event_type": event_type,
            "timestamp": time.time(),
            "payload": dict(payload),
        }
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        synced = self._applied
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.events_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        # Index the new event before the registry lock is released. The log is
        # the commit point: if another process holds the index (rebuilding
        # it), a later read applies this event instead.
        try:
            if synced is None or not self._index_own_append(record, synced, len(line)):
                self._sync()
        except sqlite3.OperationalError:
            self._applied = None

    def _index_own_append(
        self, event: dict[str, object], synced: tuple[int, int, int], length: int
    ) -> bool:
        """Apply this process's own append without re-reading the log.

        Valid only when the log grew by exactly this record from the position
        the index recorded; otherwise the caller falls back to a catch-up.
        """
        fingerprint = self._events_fingerprint()
        if fingerprint[0] != synced[0] or fingerprint[1] != synced[1] + length:
            return False
        connection = self._index.begin()
        try:
            state = self._index.state()
            if state is None or (state.inode, state.offset, state.mtime_ns) != synced:
                self._index.rollback(connection)
                return False
            session_id = self._event_session_id(event)
            updated = self._apply_event(self._index.get(session_id), event) if session_id else None
            sequence = self._event_sequence(event) or state.sequence
            applied = IndexState(
                inode=fingerprint[0],
                offset=fingerprint[1],
                mtime_ns=fingerprint[2],
                sequence=sequence,
            )
            self._index.upsert(connection, () if updated is None else (updated,), applied)
            self._index.commit(connection)
        except BaseException:
            self._index.rollback(connection)
            raise
        self._remember(fingerprint, sequence, partial=False)
        return True

    def _truncate_partial_tail(self) -> None:
        """Cut an incomplete final record and record the log's new mtime."""

        connection = self._index.begin()
        try:
            state = self._index.state()
            if state is None:
                self._index.rollback(connection)
                self._applied = None
                self._sync()
                return
            fd = os.open(self.events_path, os.O_WRONLY)
            try:
                os.ftruncate(fd, state.offset)
                os.fsync(fd)
            finally:
                os.close(fd)
            fingerprint = self._events_fingerprint()
            truncated = IndexState(
                inode=fingerprint[0],
                offset=state.offset,
                mtime_ns=fingerprint[2],
                sequence=state.sequence,
            )
            # The complete records are unchanged, so only the position moves.
            self._index.upsert(connection, (), truncated)
            self._index.commit(connection)
        except BaseException:
            self._index.rollback(connection)
            raise
        self._remember(fingerprint, truncated.sequence, partial=False)

    def _lookup(self, session_id: str) -> BackgroundSession | None:
        with self._index_lock:
            self._sync()
            return self._index.get(session_id)

    def get(self, session_id: str, *, include_deleted: bool = False) -> BackgroundSession:
        record = self._lookup(session_id)
        if record is None or (record.status == SessionStatus.DELETED and not include_deleted):
            raise SessionNotFound(session_id)
        return record

    def list(
        self,
        *,
        include_archived: bool = True,
        include_deleted: bool = False,
        cwd: str | None = None,
        workflow_name: str | None = None,
        query: str = "",
        status: SessionStatus | None = None,
    ) -> list[BackgroundSession]:
        with self._index_lock:
            self._sync()
            return self._index.sessions(
                SessionFilter(
                    include_archived=include_archived,
                    include_deleted=include_deleted,
                    cwd=cwd,
                    workflow_name=workflow_name,
                    query=query,
                    status=status,
                )
            )

    def active_sessions(self) -> List[BackgroundSession]:
        """Return queued, starting, running, waiting, retrying and cancelling sessions.

        Capacity, dispatch and queue-position decisions consider only these, so
        they read a few rows instead of decoding the whole registry.
        """

        with self._index_lock:
            self._sync()
            return self._index.sessions(SessionFilter(statuses=ACTIVE_STATUSES))

    def with_idempotency_key(self, key: str) -> List[BackgroundSession]:
        """Return sessions (archived included) submitted with *key*."""

        with self._index_lock:
            self._sync()
            return self._index.sessions(SessionFilter(idempotency_key=key))

    def status_counts(
        self,
        *,
        include_archived: bool = True,
        cwd: str | None = None,
        workflow_name: str | None = None,
        query: str = "",
    ) -> dict[SessionStatus, int]:
        """Return the number of matching sessions per status (an index lookup)."""

        with self._index_lock:
            self._sync()
            return self._index.status_counts(
                SessionFilter(
                    include_archived=include_archived,
                    cwd=cwd,
                    workflow_name=workflow_name,
                    query=query,
                )
            )

    def query_window(
        self,
        query_filter: SessionFilter = _EVERY_SESSION,
        *,
        order: SessionOrder = SessionOrder.REGISTRY,
        offset: int = 0,
        limit: int = 25,
    ) -> BackgroundWindow:
        """Return *limit* matching sessions from row *offset*, with the total."""

        if offset < 0:
            raise ValueError("offset must not be negative")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._index_lock:
            self._sync()
            rows, total = self._index.page(query_filter, limit=limit, offset=offset, order=order)
            sessions = tuple(rows)
            applied = self._applied or self._events_fingerprint()
            sequence = self._applied_sequence
        durable = self._events_fingerprint()
        return BackgroundWindow(
            sessions=sessions,
            total=total,
            offset=offset,
            generation=(applied[0], applied[1], applied[2], sequence),
            stale=applied != durable,
        )

    def select(
        self,
        query_filter: SessionFilter,
        *,
        order: SessionOrder = SessionOrder.REGISTRY,
        limit: int | None = None,
    ) -> List[BackgroundSession]:
        """Return every matching session (or the first *limit*) in *order*."""

        with self._index_lock:
            self._sync()
            return self._index.sessions(query_filter, order=order, limit=limit)

    def known_ids(self, session_ids: tuple[str, ...]) -> frozenset[str]:
        """Which of *session_ids* are background sessions (one index lookup)."""

        with self._index_lock:
            self._sync()
            return self._index.known(session_ids)

    def count(self, query_filter: SessionFilter = _EVERY_SESSION) -> int:
        """Return the number of matching sessions (an index lookup)."""

        with self._index_lock:
            self._sync()
            return self._index.count(query_filter)

    def change_token(self) -> tuple[int, int, int, int]:
        """Return the current index generation/change token."""

        with self._index_lock:
            self._sync()
            fingerprint = self._applied or self._events_fingerprint()
            return (fingerprint[0], fingerprint[1], fingerprint[2], self._applied_sequence)

    def close(self) -> None:
        """Close this store's index connection."""

        with self._index_lock:
            self._index.close()
            self._applied = None

    def create(self, session: BackgroundSession) -> BackgroundSession:
        with self._lock():
            existing = self._lookup(session.session_id)
            if existing is not None and existing.status != SessionStatus.DELETED:
                raise ValueError(f"Background session already exists: {session.session_id}")
            self._append("created", session.to_dict())
        return session

    def update(
        self,
        session_id: str,
        *,
        expected_status: SessionStatus | None = None,
        expected_lease_token: str | None = None,
        expected_attempt: int | None = None,
        expected_last_active: float | None = None,
        expected_dispatch_state: QueueDispatchState | None = None,
        # ``object`` keeps ``**changes`` compatible with older dynamic callers
        # while the deletion methods below always pass a string or ``None``.
        expected_delete_operation_id: object | None = None,
        **changes: object,
    ) -> BackgroundSession:
        notification_descriptor: Mapping[str, object] | None = None
        coordinator = self._notification_coordinator
        with self._lock():
            current = self.get(session_id, include_deleted=True)
            if expected_status is not None and current.status != expected_status:
                raise InvalidSessionTransition(
                    f"Expected {expected_status.value}, found {current.status.value}"
                )
            if expected_lease_token is not None and current.lease_token != expected_lease_token:
                raise InvalidSessionTransition("Background worker lease is stale")
            if expected_attempt is not None and current.attempt != expected_attempt:
                raise InvalidSessionTransition("Background worker attempt is stale")
            if expected_last_active is not None and current.last_active != expected_last_active:
                raise InvalidSessionTransition(
                    "Background session activity changed during recovery"
                )
            if (
                expected_dispatch_state is not None
                and current.dispatch_state is not expected_dispatch_state
            ):
                raise InvalidSessionTransition("Background dispatch state changed during recovery")
            if (
                expected_delete_operation_id is not None
                and current.delete_operation_id != expected_delete_operation_id
            ):
                raise InvalidSessionTransition("Delete operation is not the current owner")
            allowed = set(BackgroundSession.__dataclass_fields__) - {"session_id"}
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError(f"Unknown background session fields: {sorted(unknown)}")
            raw_mode_status = changes.get("mode_application_status")
            if (
                raw_mode_status is ModeApplicationStatus.APPLIED
                or raw_mode_status == ModeApplicationStatus.APPLIED.value
            ):
                applied_name = changes.get("mode_name")
                applied_attempt = changes.get("mode_application_attempt")
                if (
                    current.status is not SessionStatus.RUNNING
                    or expected_status is not SessionStatus.RUNNING
                    or not isinstance(expected_attempt, int)
                    or isinstance(expected_attempt, bool)
                    or expected_lease_token is None
                    or not expected_lease_token
                    or not isinstance(applied_name, str)
                    or not applied_name
                    or not isinstance(applied_attempt, int)
                    or isinstance(applied_attempt, bool)
                    or applied_attempt != expected_attempt
                ):
                    raise InvalidSessionTransition(
                        "Mode application must be attested by the current running worker attempt"
                    )
            if "status" in changes and changes["status"] != current.status:
                changes.setdefault("state_changed_at", time.time())
            if "latest_activity" in changes and isinstance(changes["latest_activity"], str):
                changes["latest_activity"] = changes["latest_activity"][:512]
            if "error" in changes and isinstance(changes["error"], str):
                changes["error"] = changes["error"][:2_000]
            changes.setdefault("last_active", time.time())
            updated = current.evolve(**changes)
            event_payload: dict[str, object] = {
                "session_id": session_id,
                "changes": _changed_fields(current, updated),
            }
            if current.status is not updated.status:
                event_payload["previous_status"] = current.status.value
                event_payload["current_status"] = updated.status.value
                if coordinator is not None:
                    try:
                        candidate = coordinator.describe_transition(current, updated)
                        if isinstance(candidate, Mapping):
                            notification_descriptor = dict(candidate)
                            event_payload["notification"] = notification_descriptor
                    except Exception as exc:  # noqa: BLE001
                        # Notification policy may not break a durable session
                        # lifecycle update. The source event still commits.
                        import logging

                        logging.getLogger(__name__).warning(
                            "notification descriptor omitted (%s)", type(exc).__name__
                        )
            self._append("updated", event_payload)
        if notification_descriptor is not None:
            if coordinator is not None:
                try:
                    coordinator.enqueue_and_dispatch(notification_descriptor)
                except Exception as exc:  # noqa: BLE001
                    # The redacted descriptor is already journaled and will
                    # be re-enqueued on the next coordinator startup.
                    import logging

                    logging.getLogger(__name__).warning(
                        "notification enqueue deferred (%s)", type(exc).__name__
                    )
        return updated

    def record_mode_application(
        self,
        session_id: str,
        *,
        requested_mode_name: str | None,
        effective_mode_name: str,
        attempt: int,
        lease_token: str,
    ) -> BackgroundSession:
        """Durably attest the canonical runtime mode for the owning attempt."""

        if not isinstance(effective_mode_name, str) or not effective_mode_name.strip():
            raise ValueError("effective_mode_name must be a non-empty canonical mode")
        effective_mode_name = effective_mode_name.strip()
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
            or not lease_token
        ):
            raise ValueError("mode application requires a current attempt and worker lease")
        return self.update(
            session_id,
            expected_status=SessionStatus.RUNNING,
            expected_attempt=attempt,
            expected_lease_token=lease_token,
            requested_mode_name=requested_mode_name,
            mode_name=effective_mode_name,
            mode_application_status=ModeApplicationStatus.APPLIED,
            mode_application_attempt=attempt,
            mode_application_error="",
        )

    def mark_delete_requested(
        self,
        session_id: str,
        *,
        operation_id: str,
        requested_by: str = "",
    ) -> BackgroundSession:
        """Durably claim one target for an idempotent delete operation."""

        if not operation_id or not operation_id.replace("-", "").isalnum():
            raise ValueError("operation_id must be a non-empty identifier")
        current = self.get(session_id, include_deleted=True)
        if current.status == SessionStatus.DELETED:
            return current
        if current.delete_operation_id and current.delete_operation_id != operation_id:
            if current.delete_phase not in {"failed", ""}:
                raise InvalidSessionTransition(
                    f"Session {session_id} is already being deleted by "
                    f"{current.delete_operation_id}"
                )
        activity = "Delete requested"
        if requested_by:
            activity += f" by {requested_by[:80]}"
        return self.update(
            session_id,
            expected_delete_operation_id=current.delete_operation_id,
            delete_operation_id=operation_id,
            delete_phase="requested",
            delete_error="",
            delete_requested_at=current.delete_requested_at or time.time(),
            delete_attempt=current.delete_attempt + 1,
            latest_activity=activity,
        )

    def mark_delete_progress(
        self,
        session_id: str,
        *,
        operation_id: str,
        phase: str,
        activity: str = "",
    ) -> BackgroundSession:
        """Persist a deletion phase without changing worker lifecycle state."""

        current = self.get(session_id, include_deleted=True)
        if current.status == SessionStatus.DELETED:
            return current
        if current.delete_operation_id != operation_id:
            raise InvalidSessionTransition("Delete operation is not the current owner")
        return self.update(
            session_id,
            expected_delete_operation_id=operation_id,
            delete_phase=phase[:64],
            delete_error="",
            latest_activity=(activity or f"Deleting: {phase}")[:512],
        )

    def mark_delete_failed(
        self,
        session_id: str,
        *,
        operation_id: str,
        error: str,
        retryable: bool = True,
    ) -> BackgroundSession:
        """Persist a recoverable deletion failure for the exact operation."""

        current = self.get(session_id, include_deleted=True)
        if current.status == SessionStatus.DELETED:
            return current
        if current.delete_operation_id != operation_id:
            raise InvalidSessionTransition("Delete operation is not the current owner")
        return self.update(
            session_id,
            expected_delete_operation_id=operation_id,
            delete_phase="failed",
            delete_error=error[:2_000],
            error=error[:2_000],
            failure_category="delete_retryable" if retryable else "delete_failed",
            latest_activity="Deletion failed; retry or inspect trash recovery",
        )

    def transition(
        self,
        session_id: str,
        target: SessionStatus,
        *,
        expected_status: SessionStatus | None = None,
        expected_lease_token: str | None = None,
        expected_attempt: int | None = None,
        expected_last_active: float | None = None,
        expected_dispatch_state: QueueDispatchState | None = None,
        **changes: object,
    ) -> BackgroundSession:
        current = self.get(session_id, include_deleted=True)
        if current.status == target:
            return self.update(
                session_id,
                expected_status=expected_status,
                expected_lease_token=expected_lease_token,
                expected_attempt=expected_attempt,
                expected_last_active=expected_last_active,
                expected_dispatch_state=expected_dispatch_state,
                **changes,
            )
        if current.status == SessionStatus.DELETED:
            raise InvalidSessionTransition("Deleted background sessions cannot change state")
        if not legal_transition(current.status, target):
            raise InvalidSessionTransition(
                f"Cannot transition {current.status.value} → {target.value}"
            )
        changes["status"] = target
        if target in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}:
            changes.setdefault("completed_at", time.time())
        if (
            target
            in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
                SessionStatus.ORPHANED,
            }
            and current.attempt > 0
            and not any(item.attempt == current.attempt for item in current.attempt_history)
        ):
            snapshot = current.evolve(**changes)
            changes["attempt_history"] = (
                *current.attempt_history,
                BackgroundAttempt.from_session(snapshot),
            )[-ATTEMPT_HISTORY_LIMIT:]
        elif (
            target in {SessionStatus.STARTING, SessionStatus.RETRYING}
            and current.status
            in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
                SessionStatus.ORPHANED,
                SessionStatus.ARCHIVED,
            }
            and current.attempt > 0
            and not any(item.attempt == current.attempt for item in current.attempt_history)
        ):
            # Compatibility path for legacy terminal records written before
            # attempt history existed. Preserve the outcome before its status
            # changes; the subsequent successful claim clears live fields.
            changes["attempt_history"] = (
                *current.attempt_history,
                BackgroundAttempt.from_session(current),
            )[-ATTEMPT_HISTORY_LIMIT:]
        return self.update(
            session_id,
            expected_status=expected_status,
            expected_lease_token=expected_lease_token,
            expected_attempt=expected_attempt,
            expected_last_active=expected_last_active,
            expected_dispatch_state=expected_dispatch_state,
            **changes,
        )

    def change_priority(
        self,
        session_id: str,
        priority: QueuePriority,
        *,
        changed_by: str = "local-user",
    ) -> BackgroundSession:
        """Persist an explicit priority change as an auditable event."""

        if not isinstance(priority, QueuePriority):
            raise ValueError("priority must be a QueuePriority")
        with self._lock():
            current = self.get(session_id, include_deleted=True)
            if current.status not in ACTIVE_STATUSES:
                raise InvalidSessionTransition(
                    f"Cannot change priority of {current.status.value} session"
                )
            if current.priority is priority:
                return current
            self._append(
                "priority_changed",
                {
                    "session_id": session_id,
                    "previous": current.priority.value,
                    "current": priority.value,
                    "changed_by": changed_by[:128],
                },
            )
            return self.get(session_id, include_deleted=True)

    def claim(
        self,
        session_id: str,
        *,
        pid: int,
        lease_token: str,
        dispatch_token: str | None = None,
    ) -> BackgroundSession:
        current = self.get(session_id)
        if current.dispatch_state is QueueDispatchState.PENDING and not dispatch_token:
            raise InvalidSessionTransition("Queued session has not been claimed by the scheduler")
        if current.dispatch_state is QueueDispatchState.CLAIMED:
            if not dispatch_token or dispatch_token != current.dispatch_token:
                raise InvalidSessionTransition("Background dispatch lease is stale")
        elif dispatch_token is not None and dispatch_token != current.dispatch_token:
            raise InvalidSessionTransition("Background dispatch lease is stale")
        if current.status == SessionStatus.QUEUED:
            current = self.transition(session_id, SessionStatus.STARTING)
        elif current.status == SessionStatus.RETRYING:
            current = self.transition(session_id, SessionStatus.STARTING)
        elif current.status not in {SessionStatus.STARTING, SessionStatus.ORPHANED}:
            raise InvalidSessionTransition(f"Cannot claim {current.status.value} session")
        now = time.time()
        return self.transition(
            session_id,
            SessionStatus.RUNNING,
            expected_status=current.status,
            expected_attempt=current.attempt,
            worker_pid=pid,
            lease_token=lease_token,
            attempt=current.attempt + 1,
            started_at=current.started_at or time.time(),
            worker_started_at=time.time(),
            worker_finished_at=None,
            worker_exit_code=None,
            worker_exit_reason="",
            worker_finalization_attempts=0,
            worker_cleanup_error="",
            completed_at=None,
            error=None,
            failure_category="",
            cancellation_reason="",
            exit_reason="",
            attempt_started_at=now,
            heartbeat_stale=False,
            mode_name="",
            mode_application_status=ModeApplicationStatus.PENDING,
            mode_application_attempt=current.attempt + 1,
            mode_application_error="",
            latest_activity="Worker started",
            last_active=now,
            dispatch_state=(
                QueueDispatchState.STARTED if dispatch_token is not None else current.dispatch_state
            ),
        )

    def heartbeat(
        self,
        session_id: str,
        *,
        lease_token: str,
        attempt: int | None = None,
        phase: str = "",
        activity: str = "",
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
    ) -> BackgroundSession:
        """Record that the worker is alive, with its activity and usage so far.

        ``tokens_in``, ``tokens_out`` and ``cost_usd`` (AGX-07 §5.5) are the
        session's totals; ``None`` leaves a total unchanged. Only fields whose
        value changed are written (AGX-04).
        """
        current = self.get(session_id)
        return self.update(
            session_id,
            expected_status=current.status,
            expected_lease_token=lease_token,
            expected_attempt=attempt,
            current_phase=phase or current.current_phase,
            latest_activity=activity or current.latest_activity,
            last_active=time.time(),
            heartbeat_stale=False,
            tokens_in=current.tokens_in if tokens_in is None else tokens_in,
            tokens_out=current.tokens_out if tokens_out is None else tokens_out,
            cost_usd=current.cost_usd if cost_usd is None else cost_usd,
        )

    def record_worker_exit(
        self,
        session_id: str,
        *,
        worker_pid: int | None,
        exit_reason: str,
        exit_code: int,
        cleanup_error: str = "",
        expected_attempt: int | None = None,
    ) -> BackgroundSession:
        """Append one bounded worker-exit event after finalization.

        The session record is updated by the finalization transition itself;
        this event is an audit boundary that remains available even when a
        later projection or recovery pass folds only state-changing events.
        """

        with self._lock():
            current = self.get(session_id, include_deleted=True)
            if expected_attempt is not None and current.attempt != expected_attempt:
                raise InvalidSessionTransition("Background worker attempt is stale")
            self._append(
                "worker_exited",
                {
                    "session_id": session_id,
                    "run_id": current.run_id,
                    "attempt": current.attempt,
                    "worker_pid": worker_pid,
                    "exit_reason": exit_reason[:128],
                    "exit_code": exit_code,
                    "cleanup_error": cleanup_error[:2_000],
                    "timestamp": time.time(),
                },
            )
            return current

    def mark_orphaned(
        self,
        session_id: str,
        *,
        expected_attempt: int | None = None,
        expected_lease_token: str | None = None,
        expected_status: SessionStatus | None = None,
        expected_last_active: float | None = None,
        expected_dispatch_state: QueueDispatchState | None = None,
    ) -> BackgroundSession:
        current = self.get(session_id)
        if current.status in ACTIVE_STATUSES and current.status != SessionStatus.CANCELLING:
            return self.transition(
                session_id,
                SessionStatus.ORPHANED,
                expected_attempt=expected_attempt,
                expected_lease_token=expected_lease_token,
                expected_status=expected_status,
                expected_last_active=expected_last_active,
                expected_dispatch_state=expected_dispatch_state,
                latest_activity="Worker disappeared",
            )
        if current.status == SessionStatus.CANCELLING:
            return self.transition(
                session_id,
                SessionStatus.ORPHANED,
                expected_attempt=expected_attempt,
                expected_lease_token=expected_lease_token,
                expected_status=expected_status,
                expected_last_active=expected_last_active,
                expected_dispatch_state=expected_dispatch_state,
                latest_activity="Cancellation cleanup expired",
            )
        return current

    def archive(self, session_id: str) -> BackgroundSession:
        return self.transition(session_id, SessionStatus.ARCHIVED)

    def rename(self, session_id: str, title: str) -> BackgroundSession:
        """Persist a user-facing title without changing execution metadata."""

        cleaned = " ".join(title.split()).strip()
        if not cleaned:
            raise ValueError("Session title must not be empty")
        return self.update(session_id, title=cleaned[:160])

    def set_labels(self, session_id: str, labels: tuple[str, ...]) -> BackgroundSession:
        """Replace normalized, bounded user labels for a session."""

        normalized: list[str] = []
        for label in labels:
            cleaned = " ".join(label.split()).strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned[:48])
        return self.update(session_id, labels=tuple(normalized[:16]))

    def restore_archive(self, session_id: str) -> BackgroundSession:
        current = self.get(session_id)
        if current.status != SessionStatus.ARCHIVED:
            raise InvalidSessionTransition("Only archived sessions can be restored")
        return self.update(session_id, status=SessionStatus.COMPLETED, latest_activity="Restored")

    def _move_artifacts_to_trash(
        self, session: BackgroundSession, *, operation_id: str = ""
    ) -> Path:
        source = Path(session.artifact_dir).expanduser() if session.artifact_dir else None
        suffix = operation_id or uuid.uuid4().hex[:10]
        trash = self.trash_root / f"{session.session_id}-{suffix}"
        trash.mkdir(parents=True, exist_ok=True)
        manifest = {
            "session_id": session.session_id,
            "original_artifact_dir": str(source) if source is not None else "",
        }
        destination = trash / "session"
        if source is not None and source.name == session.session_id and source.exists():
            if destination.exists():
                raise InvalidSessionTransition(
                    "Delete artifact destination already exists for this operation"
                )
            shutil.move(str(source), str(destination))
            kernel = source.parent / f"{session.session_id}.jsonl"
            if kernel.exists() and kernel.is_file():
                shutil.move(str(kernel), str(trash / "kernel.jsonl"))
        manifest_path = trash / "manifest.json"
        if not manifest_path.exists():
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return trash

    def delete(
        self,
        session_id: str,
        *,
        force: bool = False,
        operation_id: str = "",
    ) -> BackgroundSession:
        with self._lock():
            current = self.get(session_id, include_deleted=True)
            if current.status == SessionStatus.DELETED:
                if not operation_id or current.delete_operation_id in {"", operation_id}:
                    return current
                raise InvalidSessionTransition("Session was deleted by another operation")
            if current.status in ACTIVE_STATUSES and not force:
                raise InvalidSessionTransition("Cancel the active session before deleting it")
            if operation_id and current.delete_operation_id not in {"", operation_id}:
                raise InvalidSessionTransition("Delete operation is not the current owner")
            trash = self._move_artifacts_to_trash(current, operation_id=operation_id)
            self._append(
                "deleted",
                {
                    "session_id": session_id,
                    "trash_dir": str(trash),
                    "operation_id": operation_id or current.delete_operation_id,
                },
            )
            return current.evolve(
                status=SessionStatus.DELETED,
                artifact_dir="",
                trash_dir=str(trash),
                delete_operation_id=operation_id or current.delete_operation_id,
                delete_phase="completed",
                delete_error="",
            )

    def restore_deleted(self, session_id: str) -> BackgroundSession:
        with self._lock():
            current = self.get(session_id, include_deleted=True)
            if current.status != SessionStatus.DELETED or not current.trash_dir:
                raise InvalidSessionTransition("Session is not in recoverable trash")
            trash = Path(current.trash_dir)
            manifest = _json_object(
                json.loads((trash / "manifest.json").read_text(encoding="utf-8"))
            )
            original_text = str(manifest.get("original_artifact_dir", ""))
            original = Path(original_text).expanduser() if original_text else Path()
            if original_text:
                original.parent.mkdir(parents=True, exist_ok=True)
                moved = trash / "session"
                if moved.exists():
                    if original.exists():
                        raise InvalidSessionTransition("Original session directory already exists")
                    shutil.move(str(moved), str(original))
                kernel = trash / "kernel.jsonl"
                if kernel.exists():
                    shutil.move(str(kernel), str(original.parent / f"{session_id}.jsonl"))
            restored = current.evolve(
                status=SessionStatus.COMPLETED,
                artifact_dir=str(original),
                trash_dir="",
                delete_operation_id="",
                delete_phase="",
                delete_error="",
                delete_requested_at=None,
                delete_attempt=0,
                latest_activity="Restored from trash",
            )
            self._append(
                "updated",
                {"session_id": session_id, "changes": _changed_fields(current, restored)},
            )
            return restored

    def purge_trash(self, *, older_than_s: float) -> List[str]:
        """Permanently remove only expired, manifest-backed trash entries.

        The retention worker never accepts a project path.  It resolves each
        direct child beneath this store's dedicated trash directory and only
        removes a directory containing the expected manifest and session id.
        """

        if older_than_s < 0:
            raise ValueError("older_than_s must be non-negative")
        if not self.trash_root.exists():
            return []
        cutoff = time.time() - older_than_s
        removed: list[str] = []
        trash_root = self.trash_root.resolve()
        for candidate in self.trash_root.iterdir():
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.parent != trash_root or not resolved.is_dir():
                continue
            manifest_path = resolved / "manifest.json"
            try:
                manifest = _json_object(json.loads(manifest_path.read_text(encoding="utf-8")))
                session_id = manifest.get("session_id")
                modified = resolved.stat().st_mtime
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(session_id, str) or not session_id:
                continue
            if modified <= cutoff:
                shutil.rmtree(resolved)
                removed.append(session_id)
        return removed
