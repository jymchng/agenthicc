"""Append-only background-session lifecycle storage (PRD-141)."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Iterator, List, Mapping

from .model import (
    ACTIVE_STATUSES,
    ATTEMPT_HISTORY_LIMIT,
    BackgroundAttempt,
    BackgroundSession,
    ModeApplicationStatus,
    SessionStatus,
    legal_transition,
)


class SessionNotFound(KeyError):
    """Raised when a requested background session is not in the registry."""


class InvalidSessionTransition(ValueError):
    """Raised when a lifecycle operation would violate the state contract."""


def default_background_root() -> Path:
    return Path.home() / ".agenthicc" / "background"


def default_artifact_dir(session_id: str) -> Path:
    return Path.home() / ".agenthicc" / "sessions" / session_id


def _json_object(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class BackgroundPage:
    """A bounded interactive projection of the background registry.

    ``events.jsonl`` remains authoritative.  The page contains only the
    records needed by a viewport while ``total`` and ``generation`` let a UI
    reconcile selection and detect stale asynchronous results.
    """

    sessions: tuple[BackgroundSession, ...]
    total: int
    page: int
    page_size: int
    generation: tuple[int, int, int, int]
    stale: bool = False


class BackgroundStore:
    """Durable registry whose authoritative history is ``events.jsonl``.

    The JSONL history is intentionally simple and rebuildable. Each event is
    appended with ``O_APPEND`` and fsync'd. The in-memory projection is loaded
    from an atomic snapshot when possible, then folds only complete appended
    events; a cold or incompatible projection is rebuilt from the log. A
    deleted session receives a tombstone so a stale worker or rebuilt index
    cannot resurrect it.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        projection_batch_size: int = 256,
    ) -> None:
        if projection_batch_size < 1:
            raise ValueError("projection_batch_size must be at least 1")
        self.root = Path(root or default_background_root()).expanduser()
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "registry.lock"
        self.trash_root = self.root / "trash"
        self.projection_path = self.root / "projection-v1.json"
        self.projection_batch_size = projection_batch_size
        # Manager refresh and stale-worker maintenance share one store across
        # executor threads.  Projection folding mutates several related
        # indexes, so protect the complete read/update transaction (not just
        # snapshot writes) with a re-entrant lock.
        self._projection_lock = threading.RLock()
        # The event stream remains authoritative.  These process-local
        # fields are only a rebuildable projection cache: a file fingerprint
        # invalidates it after another process appends an event, and every
        # local append invalidates it explicitly.  Keeping this cache here
        # means all existing store consumers benefit without introducing a
        # second session registry.
        self._projection_fingerprint: tuple[int, int, int] | None = None
        self._projection: dict[str, BackgroundSession] | None = None
        self._next_sequence = 0
        self._projection_offset = 0
        self._projection_tail = b""
        self._events_since_snapshot = 0
        self._ordered_keys: list[tuple[bool, float, str]] = []
        self._ordered_ids: list[str] = []
        self._session_ids: list[str] = []

    @property
    def projection_schema_version(self) -> int:
        """Return the on-disk derived projection schema version."""

        return 1

    def _events_fingerprint(self) -> tuple[int, int, int]:
        """Return a cheap external-change fingerprint for ``events.jsonl``."""

        try:
            stat = self.events_path.stat()
        except OSError:
            return (0, 0, 0)
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def invalidate_projection(self) -> None:
        """Invalidate the in-process projection after an external mutation."""

        with self._projection_lock:
            self._projection = None
            self._projection_fingerprint = None
            self._projection_offset = 0
            self._projection_tail = b""
            self._ordered_keys.clear()
            self._ordered_ids.clear()
            self._session_ids.clear()

    @staticmethod
    def _order_key(session: BackgroundSession) -> tuple[bool, float, str]:
        return (not session.pinned, -session.last_active, session.session_id)

    def _remove_ordered(self, session: BackgroundSession) -> None:
        key = self._order_key(session)
        index = bisect_left(self._ordered_keys, key)
        if index < len(self._ordered_keys) and self._ordered_keys[index] == key:
            self._ordered_keys.pop(index)
            self._ordered_ids.pop(index)

    def _add_ordered(self, session: BackgroundSession) -> None:
        key = self._order_key(session)
        index = bisect_left(self._ordered_keys, key)
        self._ordered_keys.insert(index, key)
        self._ordered_ids.insert(index, session.session_id)

    def _add_session_id(self, session_id: str) -> None:
        index = bisect_left(self._session_ids, session_id)
        if index == len(self._session_ids) or self._session_ids[index] != session_id:
            self._session_ids.insert(index, session_id)

    def _rebuild_order_index(self) -> None:
        records = self._projection or {}
        ordered = sorted(
            (self._order_key(item), item.session_id)
            for item in records.values()
            if item.status != SessionStatus.DELETED
        )
        self._ordered_keys = [item[0] for item in ordered]
        self._ordered_ids = [item[1] for item in ordered]
        self._session_ids = sorted(records)

    @property
    def _snapshot_event_threshold(self) -> int:
        """Adapt snapshot cadence to projection size to bound write amplification."""

        record_count = len(self._projection or {})
        return max(self.projection_batch_size, min(100_000, record_count * 8))

    def _lifecycle_page_after(
        self, after_session_id: str, limit: int
    ) -> tuple[tuple[BackgroundSession, ...], str]:
        """Return a bounded stable-ID slice for stale-worker maintenance."""

        if limit < 1:
            raise ValueError("maintenance page limit must be at least 1")
        with self._projection_lock:
            projection = self._fold()
            start = bisect_right(self._session_ids, after_session_id) if after_session_id else 0
            if start >= len(self._session_ids):
                start = 0
            ids = self._session_ids[start : start + limit]
            next_cursor = ids[-1] if ids and start + len(ids) < len(self._session_ids) else ""
            return tuple(projection[session_id] for session_id in ids), next_cursor

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            try:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            try:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()

    def _iter_events(self) -> Iterator[dict[str, object]]:
        """Yield authoritative lifecycle events without retaining the log."""

        if not self.events_path.exists():
            return
        try:
            with self.events_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(raw, dict):
                        yield {str(key): item for key, item in raw.items()}
        except OSError:
            return

    def _read_events(self) -> list[dict[str, object]]:
        """Return a complete event list for compatibility and focused tests."""

        return list(self._iter_events())

    def _complete_event_offset(self) -> tuple[int, bytes]:
        """Find the last complete JSONL boundary without copying the full log."""

        try:
            with self.events_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size == 0:
                    return 0, b""
                handle.seek(size - 1)
                if handle.read(1) == b"\n":
                    return size, b""
                cursor = size
                while cursor > 0:
                    chunk_start = max(0, cursor - 64 * 1024)
                    handle.seek(chunk_start)
                    chunk = handle.read(cursor - chunk_start)
                    newline = chunk.rfind(b"\n")
                    if newline >= 0:
                        offset = chunk_start + newline + 1
                        return offset, b"\x00" if offset < size else b""
                    cursor = chunk_start
        except OSError:
            return 0, b""
        return 0, b"\x00" if size else b""

    def _read_event_tail(self, offset: int) -> tuple[list[dict[str, object]], int, bytes]:
        """Read complete JSONL records after *offset*.

        A writer can be observed between ``write`` and the terminating
        newline.  The incomplete bytes are retained and retried on the next
        projection pass rather than being treated as corruption.
        """

        try:
            with self.events_path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return [], offset, b""
        if not data:
            return [], offset, b""
        combined = data
        lines = combined.splitlines(keepends=True)
        tail = b""
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            tail = lines.pop()
        consumed = len(combined) - len(tail)
        events: list[dict[str, object]] = []
        for line in lines:
            try:
                raw = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                events.append({str(key): item for key, item in raw.items()})
        return events, offset + consumed, tail

    def _apply_event(
        self,
        records: dict[str, BackgroundSession],
        event: Mapping[str, object],
        *,
        update_order: bool = False,
    ) -> int:
        """Apply one validated event and return its sequence number."""

        sequence = event.get("seq")
        max_sequence = (
            sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else 0
        )
        payload_value = event.get("payload")
        # JSON decoding already gives us a plain dict. Avoid copying the
        # payload and invoking typing.Mapping's runtime ABC checks for every
        # record in a large cold replay; retain the generic mapping fallback
        # for direct callers and tests.
        payload = payload_value if isinstance(payload_value, dict) else _json_object(payload_value)
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return max_sequence
        event_type = event.get("event_type")
        previous = records.get(session_id)
        if event_type == "created":
            try:
                updated = BackgroundSession.from_mapping(payload)
            except (TypeError, ValueError):
                return max_sequence
            if update_order and previous is not None and previous.status != SessionStatus.DELETED:
                self._remove_ordered(previous)
            records[session_id] = updated
            if update_order:
                self._add_session_id(session_id)
                if updated.status != SessionStatus.DELETED:
                    self._add_ordered(updated)
            return max_sequence
        current = records.get(session_id)
        if current is None:
            return max_sequence
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
                updated = (
                    replace(current, last_active=float(value))
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    else current
                )
            else:
                try:
                    updated = current.evolve(**changes)
                except (TypeError, ValueError):
                    return max_sequence
            if update_order and current.status != SessionStatus.DELETED:
                self._remove_ordered(current)
            records[session_id] = updated
            if update_order and updated.status != SessionStatus.DELETED:
                self._add_ordered(updated)
        elif event_type == "deleted":
            event_timestamp = event.get("timestamp")
            timestamp = (
                float(event_timestamp)
                if isinstance(event_timestamp, (int, float))
                and not isinstance(event_timestamp, bool)
                else time.time()
            )
            updated = current.evolve(
                status=SessionStatus.DELETED,
                trash_dir=str(payload.get("trash_dir", current.trash_dir)),
                artifact_dir="",
                last_active=timestamp,
                delete_operation_id=str(payload.get("operation_id", current.delete_operation_id)),
                delete_phase="completed",
                delete_error="",
            )
            if update_order and current.status != SessionStatus.DELETED:
                self._remove_ordered(current)
            records[session_id] = updated
        return max_sequence

    def _load_snapshot(self, fingerprint: tuple[int, int, int]) -> bool:
        """Load a compatible snapshot, returning whether it was accepted."""

        try:
            raw = json.loads(self.projection_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(raw, dict) or raw.get("schema_version") != self.projection_schema_version:
            return False
        snapshot_inode = raw.get("event_inode")
        snapshot_size = raw.get("event_size")
        snapshot_mtime_ns = raw.get("event_mtime_ns")
        offset = raw.get("event_offset")
        sequence = raw.get("sequence")
        records_raw = raw.get("records")
        if any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in (snapshot_inode, snapshot_size, snapshot_mtime_ns, offset, sequence)
        ):
            return False
        assert isinstance(snapshot_inode, int)
        assert isinstance(snapshot_size, int)
        assert isinstance(snapshot_mtime_ns, int)
        assert isinstance(offset, int)
        assert isinstance(sequence, int)
        if not isinstance(records_raw, list) or snapshot_inode != fingerprint[0]:
            return False
        if snapshot_size < 0 or offset < 0 or offset > fingerprint[1] or snapshot_size != offset:
            return False
        if snapshot_size == fingerprint[1] and snapshot_mtime_ns != fingerprint[2]:
            return False
        records: dict[str, BackgroundSession] = {}
        try:
            for item in records_raw:
                if not isinstance(item, dict):
                    return False
                session = BackgroundSession.from_mapping(item)
                records[session.session_id] = session
        except (TypeError, ValueError):
            return False
        self._projection = records
        self._rebuild_order_index()
        self._projection_offset = offset
        self._projection_tail = b""
        self._next_sequence = sequence
        self._projection_fingerprint = (fingerprint[0], offset, fingerprint[2])
        self._events_since_snapshot = 0
        return True

    def _write_snapshot(self) -> None:
        """Atomically checkpoint the derived projection for future readers."""

        if self._projection is None:
            return
        fingerprint = self._events_fingerprint()
        payload = {
            "schema_version": self.projection_schema_version,
            "event_inode": fingerprint[0],
            "event_size": self._projection_offset,
            "event_offset": self._projection_offset,
            "event_mtime_ns": fingerprint[2],
            "sequence": self._next_sequence,
            "records": [item.to_dict() for item in self._projection.values()],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / (f".projection-v1.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, separators=(",", ":")))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.projection_path)
            try:
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
            self._events_since_snapshot = 0
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _append(self, event_type: str, payload: Mapping[str, object]) -> None:
        with self._projection_lock:
            self._append_locked(event_type, payload)

    def _append_locked(self, event_type: str, payload: Mapping[str, object]) -> None:
        # Synchronise the cache before allocating a sequence number.  The
        # registry lock serialises writers in this process and across local
        # processes; the append itself remains O_APPEND + fsync'd.
        self._fold()
        if self._projection_tail:
            # A process may have died halfway through a JSONL append. Preserve
            # every complete event and discard only the incomplete final line
            # before allocating the next sequence number.
            fd = os.open(self.events_path, os.O_WRONLY)
            try:
                os.ftruncate(fd, self._projection_offset)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._projection_tail = b""
            self._projection_fingerprint = self._events_fingerprint()
        seq = self._next_sequence + 1
        record = {
            "seq": seq,
            "event_type": event_type,
            "timestamp": time.time(),
            "payload": dict(payload),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.events_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        record = {
            "seq": seq,
            "event_type": event_type,
            "timestamp": record["timestamp"],
            "payload": dict(payload),
        }
        if self._projection is None:
            self.invalidate_projection()
            return
        self._apply_event(self._projection, record, update_order=True)
        self._next_sequence = seq
        fingerprint = self._events_fingerprint()
        self._projection_fingerprint = fingerprint
        self._projection_offset = fingerprint[1]
        self._projection_tail = b""
        self._events_since_snapshot += 1
        if (
            self._events_since_snapshot >= self._snapshot_event_threshold
            or not self.projection_path.exists()
        ):
            self._write_snapshot()

    def _fold(self) -> dict[str, BackgroundSession]:
        with self._projection_lock:
            return self._fold_locked()

    def _fold_locked(self, *, force_full_rebuild: bool = False) -> dict[str, BackgroundSession]:
        fingerprint = self._events_fingerprint()
        if self._projection is not None and self._projection_fingerprint == fingerprint:
            return self._projection

        # A same-inode append can be folded from the tail.  Small legacy
        # registries without a checkpoint retain the old full-read behavior;
        # once a checkpoint exists, the hot path is strictly incremental.
        if self._projection is None:
            # A sequence mismatch means the snapshot cannot be used as a
            # replay base. Bypass it on the recovery pass; otherwise the same
            # snapshot is reloaded recursively and the mismatch repeats until
            # Python raises RecursionError.
            if not force_full_rebuild and self._load_snapshot(fingerprint):
                if fingerprint[1] == self._projection_offset:
                    self._projection_fingerprint = fingerprint
                    return self._projection or {}
                if self._projection is not None and fingerprint[1] > self._projection_offset:
                    events, offset, tail = self._read_event_tail(self._projection_offset)
                    max_sequence = self._next_sequence
                    rebuild_order = len(events) > 128
                    for event in events:
                        sequence = event.get("seq")
                        if isinstance(sequence, int) and not isinstance(sequence, bool):
                            if sequence <= max_sequence or (
                                max_sequence > 0 and sequence != max_sequence + 1
                            ):
                                self.invalidate_projection()
                                return self._fold_locked(force_full_rebuild=True)
                        max_sequence = max(
                            max_sequence,
                            self._apply_event(
                                self._projection, event, update_order=not rebuild_order
                            ),
                        )
                    if rebuild_order:
                        self._rebuild_order_index()
                    self._next_sequence = max_sequence
                    self._projection_offset = offset
                    self._projection_tail = tail
                    self._projection_fingerprint = fingerprint
                    self._events_since_snapshot = len(events)
                    if self._events_since_snapshot >= self._snapshot_event_threshold:
                        self._write_snapshot()
                    return self._projection
            records: dict[str, BackgroundSession] = {}
            deferred_activity: dict[str, float] = {}
            max_sequence = 0
            has_events = False
            for event in self._iter_events():
                has_events = True
                payload_value = event.get("payload")
                payload = payload_value if isinstance(payload_value, dict) else {}
                session_id = payload.get("session_id")
                event_type = event.get("event_type")
                changes_value = payload.get("changes")
                changes = changes_value if isinstance(changes_value, dict) else {}
                activity = changes.get("last_active")
                sequence = event.get("seq")

                # Heartbeats often account for nearly every event in a long
                # session history. During a cold replay, defer constructing a
                # new frozen session for each heartbeat and retain only its
                # newest timestamp. Flush before any other event for that ID
                # so event ordering and update semantics remain unchanged.
                if (
                    event_type == "updated"
                    and isinstance(session_id, str)
                    and session_id in records
                    and len(changes) == 1
                    and "last_active" in changes
                    and isinstance(activity, (int, float))
                    and not isinstance(activity, bool)
                ):
                    deferred_activity[session_id] = float(activity)
                    if isinstance(sequence, int) and not isinstance(sequence, bool):
                        max_sequence = max(max_sequence, sequence)
                    continue

                if isinstance(session_id, str):
                    pending_activity = deferred_activity.pop(session_id, None)
                    if pending_activity is not None and event_type != "created":
                        current = records.get(session_id)
                        if current is not None:
                            records[session_id] = replace(current, last_active=pending_activity)
                max_sequence = max(max_sequence, self._apply_event(records, event))
            for session_id, last_active in deferred_activity.items():
                current = records.get(session_id)
                if current is not None:
                    records[session_id] = replace(current, last_active=last_active)
            self._projection = records
            self._next_sequence = max_sequence
            self._projection_offset, self._projection_tail = self._complete_event_offset()
            self._projection_fingerprint = fingerprint
            self._events_since_snapshot = 0
            self._rebuild_order_index()
            if has_events:
                self._write_snapshot()
            return records

        cached = self._projection_fingerprint
        if (
            cached is not None
            and fingerprint[0] == cached[0]
            and fingerprint[1] >= self._projection_offset
        ):
            events, offset, tail = self._read_event_tail(self._projection_offset)
            if events:
                rebuild_order = len(events) > 128
                for event in events:
                    sequence = event.get("seq")
                    if isinstance(sequence, int) and not isinstance(sequence, bool):
                        if sequence <= self._next_sequence or (
                            self._next_sequence > 0 and sequence != self._next_sequence + 1
                        ):
                            self.invalidate_projection()
                            return self._fold_locked(force_full_rebuild=True)
                    self._next_sequence = max(
                        self._next_sequence,
                        self._apply_event(self._projection, event, update_order=not rebuild_order),
                    )
                if rebuild_order:
                    self._rebuild_order_index()
            self._projection_offset = offset
            self._projection_tail = tail
            self._projection_fingerprint = fingerprint
            if events:
                self._events_since_snapshot += len(events)
            if self._events_since_snapshot >= self._snapshot_event_threshold:
                self._write_snapshot()
            return self._projection

        # Rotation, truncation, or an incompatible external rewrite requires
        # an authoritative rebuild.
        self.invalidate_projection()
        return self._fold_locked(force_full_rebuild=True)

    def get(self, session_id: str, *, include_deleted: bool = False) -> BackgroundSession:
        with self._projection_lock:
            record = self._fold().get(session_id)
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
        return list(
            self._matching(
                include_archived=include_archived,
                include_deleted=include_deleted,
                cwd=cwd,
                workflow_name=workflow_name,
                query=query,
                status=status,
            )
        )

    def _matching(
        self,
        *,
        include_archived: bool,
        include_deleted: bool,
        cwd: str | None,
        workflow_name: str | None,
        query: str,
        status: SessionStatus | None,
    ) -> tuple[BackgroundSession, ...]:
        with self._projection_lock:
            projection = self._fold()
            query_lower = query.strip().lower()
            records: list[BackgroundSession] = []
            candidates: Iterable[BackgroundSession]
            if include_deleted:
                candidates = sorted(projection.values(), key=self._order_key)
            elif self._ordered_ids:
                candidates = (projection[session_id] for session_id in self._ordered_ids)
            else:
                candidates = projection.values()
            for session in candidates:
                if self._matches_session(
                    session,
                    include_archived=include_archived,
                    include_deleted=include_deleted,
                    cwd=cwd,
                    workflow_name=workflow_name,
                    query_lower=query_lower,
                    status=status,
                ):
                    records.append(session)
            return tuple(records)

    @staticmethod
    def _matches_session(
        session: BackgroundSession,
        *,
        include_archived: bool,
        include_deleted: bool,
        cwd: str | None,
        workflow_name: str | None,
        query_lower: str,
        status: SessionStatus | None,
    ) -> bool:
        if session.status == SessionStatus.DELETED and not include_deleted:
            return False
        if not include_archived and session.status == SessionStatus.ARCHIVED:
            return False
        if cwd is not None and session.cwd != cwd:
            return False
        if workflow_name is not None and session.workflow_name != workflow_name:
            return False
        if status is not None and session.status != status:
            return False
        if query_lower:
            haystack = " ".join(
                (session.session_id, session.title, session.cwd, session.workflow_name)
            ).lower()
            return query_lower in haystack
        return True

    def query_page(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        include_archived: bool = True,
        include_deleted: bool = False,
        cwd: str | None = None,
        workflow_name: str | None = None,
        query: str = "",
        status: SessionStatus | None = None,
    ) -> BackgroundPage:
        """Return one deterministic page without rebuilding the query."""

        with self._projection_lock:
            return self._query_page_locked(
                page=page,
                page_size=page_size,
                include_archived=include_archived,
                include_deleted=include_deleted,
                cwd=cwd,
                workflow_name=workflow_name,
                query=query,
                status=status,
            )

    def _query_page_locked(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        include_archived: bool = True,
        include_deleted: bool = False,
        cwd: str | None = None,
        workflow_name: str | None = None,
        query: str = "",
        status: SessionStatus | None = None,
    ) -> BackgroundPage:
        """Build a page while holding the projection lock."""

        if page < 1:
            raise ValueError("page must be at least 1")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        no_filters = cwd is None and workflow_name is None and not query.strip() and status is None
        start = (page - 1) * page_size
        if no_filters and not include_deleted and include_archived:
            projection = self._fold()
            selected_ids = self._ordered_ids[start : start + page_size]
            sessions = tuple(projection[session_id] for session_id in selected_ids)
            total = len(self._ordered_ids)
        else:
            projection = self._fold()
            candidates = (
                iter(sorted(projection.values(), key=self._order_key))
                if include_deleted
                else (projection[session_id] for session_id in self._ordered_ids)
            )
            query_lower = query.strip().lower()
            matches: list[BackgroundSession] = []
            total = 0
            for session in candidates:
                if not self._matches_session(
                    session,
                    include_archived=include_archived,
                    include_deleted=include_deleted,
                    cwd=cwd,
                    workflow_name=workflow_name,
                    query_lower=query_lower,
                    status=status,
                ):
                    continue
                if start <= total < start + page_size:
                    matches.append(session)
                total += 1
            sessions = tuple(matches)
        applied = self._projection_fingerprint or self._events_fingerprint()
        durable = self._events_fingerprint()
        return BackgroundPage(
            sessions=sessions,
            total=total,
            page=page,
            page_size=page_size,
            generation=(applied[0], applied[1], applied[2], self._next_sequence),
            stale=applied != durable,
        )

    def change_token(self) -> tuple[int, int, int, int]:
        """Return the current projection generation/change token."""

        with self._projection_lock:
            self._fold()
            fingerprint = self._projection_fingerprint or self._events_fingerprint()
            return (fingerprint[0], fingerprint[1], fingerprint[2], self._next_sequence)

    def create(self, session: BackgroundSession) -> BackgroundSession:
        with self._lock():
            existing = self._fold().get(session.session_id)
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
        # ``object`` keeps ``**changes`` compatible with older dynamic callers
        # while the deletion methods below always pass a string or ``None``.
        expected_delete_operation_id: object | None = None,
        **changes: object,
    ) -> BackgroundSession:
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
            serialized_changes = updated.to_dict()
            serialized_changes.pop("session_id", None)
            self._append(
                "updated",
                {"session_id": session_id, "changes": serialized_changes},
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
            **changes,
        )

    def claim(self, session_id: str, *, pid: int, lease_token: str) -> BackgroundSession:
        current = self.get(session_id)
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
        )

    def heartbeat(
        self,
        session_id: str,
        *,
        lease_token: str,
        attempt: int | None = None,
        phase: str = "",
        activity: str = "",
    ) -> BackgroundSession:
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
                {
                    "session_id": session_id,
                    "changes": {
                        key: value
                        for key, value in restored.to_dict().items()
                        if key != "session_id"
                    },
                },
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
