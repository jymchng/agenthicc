"""Durable event storage for the client-neutral session service."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from agenthicc.runners.process_lease import (
    InterProcessLock,
    InterProcessLockError,
    directory_fsync,
)

from .models import SessionEvent

__all__ = ["SessionEventStore"]

log = logging.getLogger(__name__)

_INDEX_VERSION = 1
_INDEX_NAME = "index.json"
_INDEX_LOCK_NAME = "index.lock"
_MAX_INDEX_BYTES = 8 * 1024 * 1024
_MAX_METADATA_LINE_BYTES = 1 * 1024 * 1024


def _safe_session_id(session_id: str) -> str:
    cleaned = session_id.strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or Path(cleaned).name != cleaned
        or Path(cleaned).is_absolute()
        or "\\" in cleaned
    ):
        raise ValueError("session_id must be a single relative identifier")
    return cleaned


class SessionEventStore:
    """Append-only JSONL store with fsync and replay support.

    The store contains service events only. Existing kernel and conversation
    artifacts remain owned by their current persistence layers; this store is
    the durable cursor/projection ledger for multi-client coordination.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or (Path.home() / ".agenthicc" / "session-service"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # The index is an acceleration structure only.  Event JSONL files
        # remain authoritative and are read on selected-session access.
        self._index_needs_repair = False
        self._index = self._load_index()
        self._index_dirty = self._index_needs_repair
        self._metadata_bytes_scanned = 0

    @property
    def index_dirty(self) -> bool:
        """Whether the disposable metadata index needs a repair attempt."""
        with self._lock:
            return self._index_dirty

    @property
    def index_path(self) -> Path:
        """Return the atomic metadata-index path for this store."""
        return self.root / _INDEX_NAME

    @property
    def metadata_bytes_scanned(self) -> int:
        """Return bytes read by the most recent bounded metadata scan."""
        with self._lock:
            return self._metadata_bytes_scanned

    def session_metadata(self) -> dict[str, dict[str, object]]:
        """Return bounded session metadata without replaying event history.

        Legacy stores have no index.  For those stores we inspect only the
        first valid JSONL record and file stat metadata, never the complete
        event stream.  The service materializes the complete runtime only when
        a caller selects that session.
        """
        with self._lock:
            self._metadata_bytes_scanned = 0
            known = dict(self._index)
            changed = False
            # The index is disposable acceleration data. Remove records whose
            # authoritative log disappeared during external cleanup.
            for session_id in tuple(known):
                if not self.path_for(session_id).is_file():
                    known.pop(session_id, None)
                    changed = True
            for path in sorted(self.root.glob("*.jsonl")):
                session_id = path.stem
                try:
                    _safe_session_id(session_id)
                except ValueError:
                    # Ignore an unrelated or maliciously named file rather
                    # than allowing it to poison every session listing.
                    continue
                indexed = known.get(session_id)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if (
                    indexed is None
                    or indexed.get("file_size") != stat.st_size
                    or indexed.get("file_mtime_ns") != stat.st_mtime_ns
                ):
                    known[session_id] = self._legacy_metadata(path)
                    changed = True
            if changed or self._index_dirty:
                try:
                    # Another process may have appended a session since this
                    # store instance was constructed.  Re-read under the
                    # cross-process lock before publishing a repair so a
                    # local stale projection cannot erase newer records.
                    with self._index_guard():
                        latest = self._load_index()
                        current_ids = {path.stem for path in self.root.glob("*.jsonl")}
                        for session_id in tuple(latest):
                            if session_id not in current_ids:
                                latest.pop(session_id, None)
                        latest.update(known)
                        self._index = latest
                        self._write_index(latest)
                    self._index_dirty = False
                except OSError:
                    self._index = known
                    self._index_dirty = True
                    log.warning("could not repair session metadata index", exc_info=True)
            return {key: dict(value) for key, value in known.items()}

    def update_session_metadata(self, session_id: str, metadata: Mapping[str, object]) -> None:
        """Atomically update one redacted index record.

        Failure to update the index must not make a successfully appended
        durable event fail.  Callers therefore treat ``OSError`` as a
        recoverable diagnostic and rebuild from the authoritative JSONL log on
        the next selected-session access.
        """
        # Keep the index's fingerprint in sync with the authoritative log.
        # Without this small projection, every subsequent listing would have
        # to reopen the first JSONL record to rediscover that nothing changed.
        # A missing file is still represented by the caller's metadata and is
        # repaired on the next metadata scan.
        safe_session_id = _safe_session_id(session_id)
        indexed_metadata = dict(metadata)
        try:
            stat = (self.root / f"{safe_session_id}.jsonl").stat()
        except OSError:
            pass
        else:
            indexed_metadata["file_size"] = stat.st_size
            indexed_metadata["file_mtime_ns"] = stat.st_mtime_ns
        record = self._safe_metadata(safe_session_id, indexed_metadata)
        with self._lock:
            try:
                with self._index_guard():
                    # Index records are projections and can be updated by
                    # more than one attached client. Merge the one current
                    # record into the latest on-disk projection while holding
                    # the OS lock, rather than replacing it with stale memory.
                    latest = self._load_index()
                    latest[safe_session_id] = record
                    self._index = latest
                    self._write_index(latest)
                self._index_dirty = False
            except OSError:
                self._index[safe_session_id] = record
                self._index_dirty = True
                log.warning("could not update session metadata index", exc_info=True)

    def remove_session_metadata(self, session_id: str) -> None:
        """Remove an index record without deleting the authoritative event log."""
        with self._lock:
            try:
                with self._index_guard():
                    latest = self._load_index()
                    latest.pop(session_id, None)
                    self._index = latest
                    self._write_index(latest)
                self._index_dirty = False
            except OSError:
                self._index.pop(session_id, None)
                self._index_dirty = True
                log.warning("could not remove session metadata index", exc_info=True)

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{_safe_session_id(session_id)}.jsonl"

    def exists(self, session_id: str) -> bool:
        return self.path_for(session_id).exists()

    def append(self, event: SessionEvent) -> None:
        path = self.path_for(event.session_id)
        record = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(record + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append_many(self, events: Iterable[SessionEvent]) -> None:
        materialized = list(events)
        if not materialized:
            return
        path = self.path_for(materialized[0].session_id)
        if any(event.session_id != materialized[0].session_id for event in materialized):
            raise ValueError("all events in one append_many call must share a session")
        with self._lock, path.open("a", encoding="utf-8") as handle:
            for event in materialized:
                handle.write(
                    json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())

    def iter_events(self, session_id: str, *, after_sequence: int = 0) -> Iterator[SessionEvent]:
        path = self.path_for(session_id)
        if not path.exists():
            return
        with self._lock, path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = SessionEvent.from_mapping(json.loads(line))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if event.sequence > after_sequence:
                    yield event

    def all_events(self, session_id: str) -> list[SessionEvent]:
        return list(self.iter_events(session_id))

    def delete(self, session_id: str) -> None:
        path = self.path_for(session_id)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self._index.pop(session_id, None)
            try:
                with self._index_guard():
                    latest = self._load_index()
                    latest.pop(session_id, None)
                    self._index = latest
                    self._write_index(latest)
                self._index_dirty = False
            except OSError:
                self._index_dirty = True
                log.warning("could not update session metadata after deletion", exc_info=True)

    def session_ids(self) -> list[str]:
        return sorted(self.session_metadata())

    def _load_index(self) -> dict[str, dict[str, object]]:
        path = self.index_path
        try:
            if not path.is_file():
                return {}
            if path.stat().st_size > _MAX_INDEX_BYTES:
                self._index_needs_repair = True
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._index_needs_repair = True
            log.warning("ignoring invalid session metadata index %s", path)
            return {}
        if not isinstance(data, dict) or data.get("version") != _INDEX_VERSION:
            self._index_needs_repair = True
            return {}
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            self._index_needs_repair = True
            return {}
        result: dict[str, dict[str, object]] = {}
        skipped = False
        for session_id, metadata in sessions.items():
            if isinstance(session_id, str) and isinstance(metadata, Mapping):
                try:
                    result[session_id] = self._safe_metadata(session_id, metadata)
                except ValueError:
                    skipped = True
                    # A damaged or hand-edited record must not make unrelated
                    # sessions unavailable.  The event file is still
                    # discoverable through the legacy metadata fallback.
                    continue
            elif not isinstance(session_id, str) or not isinstance(metadata, Mapping):
                skipped = True
        if skipped:
            self._index_needs_repair = True
        return result

    def _write_index(self, records: Mapping[str, Mapping[str, object]]) -> None:
        payload = {
            "version": _INDEX_VERSION,
            "updated_at": time.time(),
            "sessions": {key: dict(value) for key, value in sorted(records.items())},
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_INDEX_BYTES:
            raise OSError("session metadata index exceeds size limit")
        temporary = self.root / (f".{_INDEX_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(self.index_path)
        directory_fsync(self.root)

    @contextmanager
    def _index_guard(self) -> Iterator[None]:
        """Serialize index merges across threads and attached processes."""
        try:
            with InterProcessLock(self.root / _INDEX_LOCK_NAME):
                yield
        except InterProcessLockError as exc:
            raise OSError(f"cannot lock session metadata index: {exc}") from exc

    @staticmethod
    def _safe_metadata(session_id: str, metadata: Mapping[str, object]) -> dict[str, object]:
        """Keep only small, non-sensitive index fields."""
        if (
            not session_id
            or Path(session_id).name != session_id
            or Path(session_id).is_absolute()
            or "\\" in session_id
        ):
            raise ValueError("invalid session id in metadata index")

        def number(key: str, default: float = 0.0) -> float:
            value = metadata.get(key, default)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return default
            converted = float(value)
            return converted if math.isfinite(converted) else default

        def integer(key: str, default: int = 0) -> int:
            value = metadata.get(key, default)
            return value if isinstance(value, int) and not isinstance(value, bool) else default

        capabilities = metadata.get("capabilities", ())
        safe_capabilities = (
            [item for item in capabilities if isinstance(item, str)]
            if isinstance(capabilities, (list, tuple))
            else []
        )
        project_root = metadata.get("project_root", "")
        state = metadata.get("state", "created")
        return {
            "session_id": session_id,
            "project_root": project_root if isinstance(project_root, str) else "",
            "created_at": number("created_at"),
            "updated_at": number("updated_at"),
            "state": state if isinstance(state, str) else "created",
            "last_event_sequence": integer("last_event_sequence"),
            "capabilities": safe_capabilities,
            "deleted": metadata.get("deleted", False) is True,
            "file_size": integer("file_size"),
            "file_mtime_ns": integer("file_mtime_ns"),
        }

    def _legacy_metadata(self, path: Path) -> dict[str, object]:
        """Build a cheap metadata record for a pre-index JSONL file."""
        try:
            stat = path.stat()
        except OSError:
            return self._safe_metadata(path.stem, {})
        first = self._first_event(path)
        payload = first.payload if first is not None else {}
        capabilities = payload.get("capabilities", [])
        return self._safe_metadata(
            path.stem,
            {
                "project_root": payload.get("project_root", ""),
                "created_at": first.occurred_at if first is not None else stat.st_ctime,
                "updated_at": stat.st_mtime,
                # The current state is unknown without replay.  Materializing
                # the selected session replaces this conservative value.
                "state": "idle",
                "last_event_sequence": first.sequence if first is not None else 0,
                "capabilities": capabilities,
                "file_size": stat.st_size,
                "file_mtime_ns": stat.st_mtime_ns,
            },
        )

    def _first_event(self, path: Path) -> SessionEvent | None:
        """Read the first bounded valid event and account for probe bytes."""
        scanned = 0
        try:
            with path.open("rb") as handle:
                while True:
                    line = handle.readline(_MAX_METADATA_LINE_BYTES + 1)
                    scanned += len(line)
                    if not line:
                        break
                    if len(line) > _MAX_METADATA_LINE_BYTES:
                        # Do not retain a malformed megabyte-scale record in
                        # memory while looking for the first valid event.
                        while not line.endswith(b"\n"):
                            line = handle.readline(_MAX_METADATA_LINE_BYTES + 1)
                            scanned += len(line)
                            if not line:
                                break
                        continue
                    if not line.strip():
                        continue
                    try:
                        return SessionEvent.from_mapping(json.loads(line))
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                        continue
        except OSError:
            pass
        finally:
            self._metadata_bytes_scanned += scanned
        return None

    def compact(self, session_id: str, *, before_sequence: int) -> int:
        """Drop old events and return the earliest retained sequence.

        Compaction is explicit because clients must receive a replay-gap when
        they request a cursor older than the retained history.
        """

        events = [
            event for event in self.all_events(session_id) if event.sequence >= before_sequence
        ]
        if not events:
            return 0
        path = self.path_for(session_id)
        temp = path.with_suffix(".jsonl.tmp")
        with self._lock, temp.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp.replace(path)
        return events[0].sequence
