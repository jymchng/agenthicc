"""Per-worker subagent transcripts (AGX-08 §5.3).

Each pool writes ``<session dir>/subagents/<pool_id>/pool.json`` (its workers)
and one ``<worker_id>.jsonl`` per worker: the task, the agent's text, each tool
call's start and finish, approval requests and the final result. Everything is
redacted with the session-export redactor before it is written, writes happen
on one background thread in order, and a worker's file is kept under
:data:`MAX_TRANSCRIPT_BYTES` by dropping its oldest events behind a marker.

The transcripts are for people: the parent agent never reads them, so its
provider context — the PRD-124 result boundary — is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.runtime.session_export import _Redactor

__all__ = [
    "MAX_TRANSCRIPT_BYTES",
    "PoolRecord",
    "TranscriptEvent",
    "TranscriptTail",
    "TranscriptWriter",
    "WorkerRecord",
    "last_event",
    "list_pools",
    "read_transcript",
]

#: A worker's transcript is kept at or under this size.
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
#: Text fields (an agent message, a result) are cut to this many characters.
_TEXT_LIMIT = 4_000
#: Short fields (a tool target, a failure) are cut to this many characters.
_SHORT_LIMIT = 240

_KINDS = frozenset(
    {
        "prompt",
        "text",
        "tool_started",
        "tool_finished",
        "approval_requested",
        "approval_resolved",
        "finished",
        "stop_requested",
        "lifecycle",
        "message_acknowledged",
        "attempt_started",
        "truncated",
    }
)


def _string_field(value: Mapping[str, object], name: str) -> str:
    field_value = value.get(name, "")
    return field_value if isinstance(field_value, str) else ""


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """One line of a worker transcript."""

    kind: str
    at: float
    #: The task, an agent message, a tool target, a failure or a result.
    text: str = ""
    tool: str = ""
    #: Whether a tool call, an approval or the worker succeeded.
    ok: bool | None = None
    #: How many older events a ``truncated`` marker stands for.
    dropped: int = 0
    request_id: str = ""
    mode: str = ""
    source: str = ""
    reason: str = ""
    phase: str = ""
    partial: bool = False
    stop_reason: str = ""
    undelivered_messages: int = 0
    attempt: int = 1
    cleanup_error: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind, "at": self.at}
        if self.text:
            payload["text"] = self.text
        if self.tool:
            payload["tool"] = self.tool
        if self.ok is not None:
            payload["ok"] = self.ok
        if self.dropped:
            payload["dropped"] = self.dropped
        for name, value in (
            ("request_id", self.request_id),
            ("mode", self.mode),
            ("source", self.source),
            ("reason", self.reason),
            ("phase", self.phase),
            ("stop_reason", self.stop_reason),
            ("cleanup_error", self.cleanup_error),
        ):
            if value:
                payload[name] = value
        if self.partial:
            payload["partial"] = True
        if self.undelivered_messages:
            payload["undelivered_messages"] = self.undelivered_messages
        if self.attempt != 1:
            payload["attempt"] = self.attempt
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TranscriptEvent | None:
        kind = value.get("kind")
        at = value.get("at")
        if kind not in _KINDS or isinstance(at, bool) or not isinstance(at, (int, float)):
            return None
        text = value.get("text", "")
        tool = value.get("tool", "")
        ok = value.get("ok")
        dropped = value.get("dropped", 0)
        undelivered_messages = value.get("undelivered_messages", 0)
        attempt = value.get("attempt", 1)
        return cls(
            kind=str(kind),
            at=float(at),
            text=text if isinstance(text, str) else "",
            tool=tool if isinstance(tool, str) else "",
            ok=ok if isinstance(ok, bool) else None,
            dropped=dropped if isinstance(dropped, int) and not isinstance(dropped, bool) else 0,
            request_id=_string_field(value, "request_id"),
            mode=_string_field(value, "mode"),
            source=_string_field(value, "source"),
            reason=_string_field(value, "reason"),
            phase=_string_field(value, "phase"),
            partial=value.get("partial") is True,
            stop_reason=_string_field(value, "stop_reason"),
            undelivered_messages=(
                undelivered_messages
                if isinstance(undelivered_messages, int)
                and not isinstance(undelivered_messages, bool)
                and undelivered_messages >= 0
                else 0
            ),
            attempt=(
                attempt
                if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1
                else 1
            ),
            cleanup_error=_string_field(value, "cleanup_error"),
        )


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    """A worker as its pool's manifest lists it."""

    worker_id: str
    label: str
    agent_type: str
    task: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "worker_id": self.worker_id,
            "label": self.label,
            "agent_type": self.agent_type,
            "task": self.task,
        }


@dataclass(frozen=True, slots=True)
class PoolRecord:
    """A pool whose transcripts are on disk, newest first in :func:`list_pools`."""

    pool_id: str
    created_at: float
    finished_at: float | None
    workers: tuple[WorkerRecord, ...]
    directory: Path

    def transcript_path(self, worker_id: str) -> Path:
        return self.directory / f"{worker_id}.jsonl"


def _safe_name(value: str) -> str:
    """*value* if it is a pool or worker ID usable as one path component."""
    if not value or value in {".", ".."} or any(not (ch.isalnum() or ch in "-_.") for ch in value):
        raise ValueError(f"unsafe transcript identifier: {value!r}")
    return value


class TranscriptWriter:
    """Writes one pool's manifest and worker transcripts off the event loop.

    Calls return at once; a single background thread performs the writes in
    the order they were made. :meth:`aclose` waits for them.
    """

    def __init__(
        self,
        root: Path,
        pool_id: str,
        *,
        limit: int = MAX_TRANSCRIPT_BYTES,
    ) -> None:
        self.directory = root / _safe_name(pool_id)
        self.pool_id = pool_id
        self._limit = max(1_024, limit)
        self._sizes: dict[str, int] = {}
        self._manifest: dict[str, object] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subagent-transcript")
        self._redactor: _Redactor | None = None
        self._lock = threading.Lock()
        self._closed = False

    # ── what the pool calls ──────────────────────────────────────────────────

    def start(self, workers: Sequence[WorkerRecord], created_at: float) -> None:
        """Record the pool and its workers."""
        self._submit(self._write_manifest, tuple(workers), created_at)

    def append(self, worker_id: str, event: TranscriptEvent) -> None:
        """Queue *event* for *worker_id*'s transcript."""
        self._submit(self._write_event, worker_id, event)

    def finish(self, finished_at: float) -> None:
        """Record that the pool finished."""
        self._submit(self._mark_finished, finished_at)

    async def aclose(self) -> None:
        """Wait for every queued write, then stop the thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        await asyncio.to_thread(self._executor.shutdown, True)

    # ── the background thread ────────────────────────────────────────────────

    def _submit(self, function: Callable[..., None], *args: object) -> None:
        with self._lock:
            if self._closed:
                return
            self._executor.submit(self._guarded, function, *args)

    @staticmethod
    def _guarded(function: Callable[..., None], *args: object) -> None:
        # A transcript is a convenience: a full disk must not fail the worker.
        try:
            function(*args)
        except (OSError, ValueError):
            return

    def _redact(self, text: str, limit: int) -> str:
        if not text:
            return ""
        if self._redactor is None:
            from agenthicc.tui.runtime.session_export import _Redactor

            self._redactor = _Redactor()
        value = str(self._redactor.value(text, "text"))
        return value if len(value) <= limit else value[: limit - 1] + "…"

    def _write_manifest(self, workers: tuple[WorkerRecord, ...], created_at: float) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._manifest = {
            "pool_id": self.pool_id,
            "created_at": created_at,
            "finished_at": None,
            "workers": [
                WorkerRecord(
                    _safe_name(worker.worker_id),
                    worker.label,
                    worker.agent_type,
                    self._redact(worker.task, _SHORT_LIMIT),
                ).to_dict()
                for worker in workers
            ],
        }
        self._replace(self.directory / "pool.json", json.dumps(self._manifest, sort_keys=True))

    def _mark_finished(self, finished_at: float) -> None:
        if not self._manifest:
            return
        self._manifest["finished_at"] = finished_at
        self._replace(self.directory / "pool.json", json.dumps(self._manifest, sort_keys=True))

    def _write_event(self, worker_id: str, event: TranscriptEvent) -> None:
        limit = _TEXT_LIMIT if event.kind in ("prompt", "text", "finished") else _SHORT_LIMIT
        safe = TranscriptEvent(
            kind=event.kind,
            at=event.at,
            text=self._redact(event.text, limit),
            tool=event.tool[:80],
            ok=event.ok,
            dropped=event.dropped,
            request_id=event.request_id[:80],
            mode=event.mode[:16],
            source=event.source[:80],
            reason=self._redact(event.reason, _SHORT_LIMIT),
            phase=event.phase[:32],
            partial=event.partial,
            stop_reason=event.stop_reason[:_SHORT_LIMIT],
            undelivered_messages=max(0, event.undelivered_messages),
            attempt=max(1, event.attempt),
            cleanup_error=self._redact(event.cleanup_error, _SHORT_LIMIT),
        )
        line = json.dumps(safe.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        name = _safe_name(worker_id)
        path = self.directory / f"{name}.jsonl"
        self.directory.mkdir(parents=True, exist_ok=True)
        size = self._sizes.get(name)
        if size is None:
            size = path.stat().st_size if path.exists() else 0
        encoded = line.encode("utf-8")
        if size + len(encoded) > self._limit:
            size = self._compact(path)
        with path.open("ab") as handle:
            handle.write(encoded)
        self._sizes[name] = size + len(encoded)

    def _compact(self, path: Path) -> int:
        """Keep the newest half of *path* behind a ``truncated`` marker."""
        try:
            lines = path.read_bytes().splitlines(keepends=True)
        except OSError:
            lines = []
        previous = 0
        if lines:
            marker = TranscriptEvent.from_mapping(_loads(lines[0]))
            if marker is not None and marker.kind == "truncated":
                previous = marker.dropped
                lines = lines[1:]
        keep: list[bytes] = []
        kept = 0
        for line in reversed(lines):
            if kept + len(line) > self._limit // 2:
                break
            keep.append(line)
            kept += len(line)
        keep.reverse()
        marker = TranscriptEvent(
            "truncated",
            _event_time(keep[0]) if keep else 0.0,
            dropped=previous + len(lines) - len(keep),
        )
        content = (json.dumps(marker.to_dict(), sort_keys=True) + "\n").encode("utf-8")
        content += b"".join(keep)
        self._replace(path, content.decode("utf-8"))
        return len(content)

    @staticmethod
    def _replace(path: Path, text: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)


def _loads(line: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _event_time(line: bytes) -> float:
    event = TranscriptEvent.from_mapping(_loads(line))
    return event.at if event is not None else 0.0


def read_transcript(path: Path) -> tuple[TranscriptEvent, ...]:
    """The events of one worker's transcript; unreadable lines are skipped."""
    try:
        data = path.read_bytes()
    except OSError:
        return ()
    events = (TranscriptEvent.from_mapping(_loads(line)) for line in data.splitlines())
    return tuple(event for event in events if event is not None)


def last_event(path: Path) -> TranscriptEvent | None:
    """The newest event of a transcript, reading only its end."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 16_384))
            data = handle.read()
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        event = TranscriptEvent.from_mapping(_loads(line))
        if event is not None:
            return event
    return None


class TranscriptTail:
    """Reads a transcript as it grows, a few bytes at a time (to follow it)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._partial = b""
        self._events: list[TranscriptEvent] = []

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return tuple(self._events)

    def refresh(self) -> bool:
        """Read what was written since the last call; whether anything was."""
        try:
            stat = self.path.stat()
        except OSError:
            return False
        identity = (stat.st_dev, stat.st_ino)
        if identity != self._identity or stat.st_size < self._offset:
            # A new or compacted file: read it from the start.
            self._identity = identity
            self._offset = 0
            self._partial = b""
            self._events.clear()
        if stat.st_size == self._offset:
            return False
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                data = handle.read(stat.st_size - self._offset)
        except OSError:
            return False
        self._offset += len(data)
        lines = (self._partial + data).split(b"\n")
        self._partial = lines.pop()
        for line in lines:
            event = TranscriptEvent.from_mapping(_loads(line))
            if event is not None:
                self._events.append(event)
        return True


def _worker_records(value: object) -> tuple[WorkerRecord, ...]:
    if not isinstance(value, list):
        return ()
    records: list[WorkerRecord] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        worker_id, label, agent_type = (
            item.get("worker_id"),
            item.get("label"),
            item.get("agent_type"),
        )
        task = item.get("task", "")
        if isinstance(worker_id, str) and isinstance(label, str) and isinstance(agent_type, str):
            records.append(
                WorkerRecord(worker_id, label, agent_type, task if isinstance(task, str) else "")
            )
    return tuple(records)


def list_pools(root: Path, *, limit: int = 20) -> tuple[PoolRecord, ...]:
    """The pools recorded under *root*, newest first."""
    try:
        directories = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return ()
    pools: list[PoolRecord] = []
    for directory in directories:
        try:
            manifest = json.loads((directory / "pool.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        pool_id, created = manifest.get("pool_id"), manifest.get("created_at")
        finished = manifest.get("finished_at")
        if not isinstance(pool_id, str) or isinstance(created, bool):
            continue
        if not isinstance(created, (int, float)):
            continue
        pools.append(
            PoolRecord(
                pool_id=pool_id,
                created_at=float(created),
                finished_at=(
                    float(finished)
                    if isinstance(finished, (int, float)) and not isinstance(finished, bool)
                    else None
                ),
                workers=_worker_records(manifest.get("workers")),
                directory=directory,
            )
        )
    pools.sort(key=lambda pool: pool.created_at, reverse=True)
    return tuple(pools[:limit])
