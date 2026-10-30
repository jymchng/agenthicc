"""Session persistence: index and event log (PRD-67 §3-4)."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, cast

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState

from agenthicc.tui.conversation_store import ConversationEvent
from agenthicc.runners.process_lease import InterProcessLock, atomic_replace

_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"
_SESSION_INDEX = _SESSIONS_DIR / "index.json"

# Visual resume is intentionally bounded.  A session may contain thousands of
# tool events, but the user normally needs only the most recent exchanges to
# orient themselves.  The durable event log remains complete; this limit only
# controls the presentation/input-history projection rebuilt at startup.
DEFAULT_RESUME_TRANSCRIPT_TURNS = 20
_TAIL_READ_BYTES = 64 * 1024
_MAX_RECENT_EVENTS = 4096
DEFAULT_TRANSCRIPT_PREVIEW_EVENTS = 128
DEFAULT_TRANSCRIPT_PREVIEW_BYTES = 128 * 1024
DEFAULT_TRANSCRIPT_EVENT_BYTES = 16 * 1024
MAX_TRANSCRIPT_PREVIEW_EVENTS = 1024
MAX_TRANSCRIPT_PREVIEW_BYTES = 1024 * 1024
MAX_TRANSCRIPT_EVENT_BYTES = 128 * 1024
_TRANSCRIPT_SCAN_CHUNK_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class TranscriptCursor:
    """Stable byte cursor for reading events older than a page.

    ``skip_oversized_line`` indicates that the cursor currently falls inside a
    JSONL record too large to locate within one page's scan budget. Subsequent
    page requests continue the bounded reverse scan until its preceding line
    boundary is found; appended records do not change these offsets.
    """

    offset: int
    skip_oversized_line: bool = False


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One bounded event projection and its source byte range."""

    event: ConversationEvent
    start_offset: int
    end_offset: int
    oversized: bool = False
    partial_line: bool = False


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    """A bounded chronological event page plus an older-page cursor."""

    entries: tuple[TranscriptEntry, ...]
    older_cursor: TranscriptCursor | None
    scanned_bytes: int = 0
    retained_bytes: int = 0
    notice: str = ""

    @property
    def events(self) -> list[ConversationEvent]:
        """Return the page events in chronological presentation order."""

        return [entry.event for entry in self.entries]


def _int_value(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _float_value(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _str_value(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


# ── Index CRUD ────────────────────────────────────────────────────────────────


def create_session_id() -> str:
    return str(uuid.uuid4())


def _load_index() -> dict[str, dict[str, object]]:
    if _SESSION_INDEX.exists():
        try:
            loaded = json.loads(_SESSION_INDEX.read_text())
            if isinstance(loaded, dict):
                return cast(dict[str, dict[str, object]], loaded)
        except Exception:  # noqa: BLE001
            return {}
    return {}


@contextmanager
def _index_guard() -> Iterator[None]:
    """Serialize canonical index read-modify-write operations."""

    with InterProcessLock(_SESSION_INDEX.with_name("index.lock")):
        yield


def _save_index_unlocked(data: dict[str, dict[str, object]]) -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_replace(
        _SESSION_INDEX,
        (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        prefix=".index-",
    )


def _save_index(data: dict[str, dict[str, object]]) -> None:
    """Persist the index under its short cross-process critical section."""

    with _index_guard():
        _save_index_unlocked(data)


def _mutate_index(
    mutator: Callable[[dict[str, dict[str, object]]], None],
) -> dict[str, dict[str, object]]:
    """Apply one index mutation while holding the canonical lock."""

    with _index_guard():
        index = _load_index()
        mutator(index)
        _save_index_unlocked(index)
        return index


def _write_metadata(session_id: str, metadata: dict[str, object]) -> None:
    session_dir = _SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(session_dir, 0o700)
    except OSError:
        pass
    atomic_replace(
        session_dir / "metadata.json",
        (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        prefix=".metadata-",
    )


def register_session(session_id: str, cwd: str, model: str) -> None:
    metadata = {
        "cwd": str(Path(cwd).expanduser().resolve()),
        "model": model,
        "mode": "Safe",
        "created_at": time.time(),
        "last_active": time.time(),
    }

    def _register(index: dict[str, dict[str, object]]) -> None:
        index[session_id] = metadata

    _mutate_index(_register)
    _write_metadata(session_id, metadata)


def update_session_mode(session_id: str, mode: str) -> None:
    """Persist the canonical active mode without recording transient internals."""
    updated: dict[str, object] | None = None

    def _update(index: dict[str, dict[str, object]]) -> None:
        nonlocal updated
        metadata = index.get(session_id)
        if metadata is None:
            return
        metadata["mode"] = mode
        metadata["last_active"] = time.time()
        updated = dict(metadata)

    _mutate_index(_update)
    if updated is not None:
        _write_metadata(session_id, updated)


def load_session_mode(session_id: str) -> str | None:
    """Read a persisted mode name, returning ``None`` for old/corrupt metadata."""
    path = _SESSIONS_DIR / session_id / "metadata.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    mode = value.get("mode") if isinstance(value, dict) else None
    return mode if isinstance(mode, str) and mode.strip() else None


def update_session_agent(session_id: str, agent: str) -> None:
    """Persist the session's primary agent so a resume restores it (AGX-09)."""
    updated: dict[str, object] | None = None

    def _update(index: dict[str, dict[str, object]]) -> None:
        nonlocal updated
        metadata = index.get(session_id)
        if metadata is None:
            return
        metadata["agent"] = agent
        updated = dict(metadata)

    _mutate_index(_update)
    if updated is not None:
        _write_metadata(session_id, updated)


def load_session_agent(session_id: str) -> str | None:
    """The session's persisted primary agent, or ``None`` for old/corrupt metadata."""
    path = _SESSIONS_DIR / session_id / "metadata.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    agent = value.get("agent") if isinstance(value, dict) else None
    return agent if isinstance(agent, str) and agent.strip() else None


def touch_session(session_id: str) -> None:
    def _touch(index: dict[str, dict[str, object]]) -> None:
        if session_id in index:
            index[session_id]["last_active"] = time.time()

    _mutate_index(_touch)


def find_latest_session_for_cwd(cwd: str | None = None) -> str | None:
    canonical_cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
    with _index_guard():
        index = _load_index()
    candidates = [(sid, meta) for sid, meta in index.items() if meta.get("cwd") == canonical_cwd]
    if not candidates:
        return None
    latest = max(
        candidates,
        key=lambda x: (_float_value(x[1].get("last_active")), x[0]),
    )
    return latest[0]


def get_session_log_path(session_id: str) -> Path:
    return _SESSIONS_DIR / session_id / "conversation.jsonl"


def _checked_session_log_path(session_id: str) -> Path:
    """Resolve one session transcript and reject path/symlink escapes."""

    sessions_root = _SESSIONS_DIR.resolve()
    path = get_session_log_path(session_id)
    session_dir = path.parent.resolve()
    if session_dir.parent != sessions_root:
        raise ValueError(
            "session identifier does not resolve directly under the sessions directory"
        )
    return session_dir / path.name


def load_recent_transcript_page(
    session_id: str,
    *,
    last_turns: int = DEFAULT_RESUME_TRANSCRIPT_TURNS,
    max_events: int = DEFAULT_TRANSCRIPT_PREVIEW_EVENTS,
    max_bytes: int = DEFAULT_TRANSCRIPT_PREVIEW_BYTES,
    max_event_bytes: int = DEFAULT_TRANSCRIPT_EVENT_BYTES,
) -> TranscriptPage:
    """Load a strict-bounded newest transcript page.

    A non-positive ``last_turns`` removes only the turn-count limit. Event,
    retained-byte, per-event, and reverse-scan limits always remain active.
    An oversized JSONL record is represented by a synthetic presentation-only
    marker; its exact persisted event can be fetched by selecting it in the
    history overlay.
    """

    path = _checked_session_log_path(session_id)
    if not path.exists():
        return TranscriptPage((), None)
    return _load_transcript_page(
        path,
        upper_offset=path.stat().st_size,
        last_turns=last_turns,
        max_events=max_events,
        max_bytes=max_bytes,
        max_event_bytes=max_event_bytes,
        rendered=False,
    )


def load_older_transcript_page(
    session_id: str,
    cursor: TranscriptCursor,
    *,
    max_events: int = 128,
    max_bytes: int = DEFAULT_TRANSCRIPT_PREVIEW_BYTES,
    max_event_bytes: int = DEFAULT_TRANSCRIPT_EVENT_BYTES,
) -> TranscriptPage:
    """Load one older page before a stable byte cursor.

    The end offset is captured by the previous page, so records appended while
    the history view is open do not shift or repeat this page.
    """

    try:
        path = _checked_session_log_path(session_id)
        if not path.exists():
            return TranscriptPage((), None)
        upper = max(0, min(cursor.offset, path.stat().st_size))
    except (OSError, RuntimeError, ValueError):
        return TranscriptPage((), None, notice="Unable to read transcript page.")
    return _load_transcript_page(
        path,
        upper_offset=upper,
        last_turns=0,
        max_events=max_events,
        max_bytes=max_bytes,
        max_event_bytes=max_event_bytes,
        rendered=False,
        skip_oversized_line=cursor.skip_oversized_line,
    )


def load_transcript_entry(entry: TranscriptEntry) -> ConversationEvent | None:
    """Load one selected event's complete payload for explicit inspection.

    Ordinary preview/page reads never retain a line over its per-event limit.
    Explicit detail reads are still capped at the hard event limit and never
    load the surrounding transcript or an unbounded oversized record.
    """

    if not entry.oversized and not entry.partial_line:
        return entry.event
    try:
        path = _checked_session_log_path_from_entry(entry)
        with path.open("rb") as handle:
            start = entry.start_offset
            if entry.partial_line:
                full_start, _scanned, complete = _find_line_start_before(
                    handle, entry.start_offset, MAX_TRANSCRIPT_PREVIEW_BYTES
                )
                if not complete:
                    return _inspection_limit_event(entry, "record start exceeds 1 MiB scan limit")
                start = full_start
            span = entry.end_offset - start
            if span < 0:
                return None
            if span > MAX_TRANSCRIPT_EVENT_BYTES:
                return _inspection_limit_event(
                    entry,
                    f"record is {span} bytes; inspection limit is {MAX_TRANSCRIPT_EVENT_BYTES} bytes",
                )
            handle.seek(start)
            raw = handle.read(span)
        return _decode_event(raw, rendered=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _inspection_limit_event(entry: TranscriptEntry, reason: str) -> ConversationEvent:
    """Describe an event that exceeds the explicit detail-view hard limit."""

    return ConversationEvent(
        event_id=entry.event.event_id,
        kind="system",
        payload={
            "text": f"This transcript event was not expanded: {reason}.",
            "source_event_id": entry.event.event_id,
        },
        timestamp=entry.event.timestamp,
        rendered=False,
    )


def _checked_session_log_path_from_entry(entry: TranscriptEntry) -> Path:
    """Resolve the current session path for an entry's private source marker."""

    session_id = entry.event.payload.get("_agenthicc_session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("transcript entry has no source session")
    return _checked_session_log_path(session_id)


def _load_transcript_page(
    path: Path,
    *,
    upper_offset: int,
    last_turns: int,
    max_events: int,
    max_bytes: int,
    max_event_bytes: int,
    rendered: bool,
    skip_oversized_line: bool = False,
) -> TranscriptPage:
    """Read one bounded suffix of the JSONL log and project its newest events.

    Reading a bounded suffix once is important: a line-at-a-time reverse scan
    that re-reads a fixed-size chunk for every event can make a nominally small
    page perform unbounded I/O for logs containing many short events.
    """

    max_events = min(MAX_TRANSCRIPT_PREVIEW_EVENTS, max(1, int(max_events)))
    max_bytes = min(MAX_TRANSCRIPT_PREVIEW_BYTES, max(1024, int(max_bytes)))
    max_event_bytes = min(MAX_TRANSCRIPT_EVENT_BYTES, max(256, int(max_event_bytes)))
    # Reserve one byte for checking whether the bounded window starts on an
    # actual JSONL record boundary.
    max_scan_bytes = max(1, max_bytes + max_event_bytes - 1)
    upper = max(0, min(upper_offset, path.stat().st_size))
    lower = max(0, upper - max_scan_bytes)
    selected: list[TranscriptEntry] = []
    retained_bytes = 0
    scanned_bytes = 0
    notice = ""

    try:
        with path.open("rb") as handle:
            starts_at_line_boundary = lower == 0
            if lower > 0:
                handle.seek(lower - 1)
                starts_at_line_boundary = handle.read(1) == b"\n"
            handle.seek(lower)
            raw_window = handle.read(upper - lower)
        scanned_bytes = len(raw_window) + (1 if lower > 0 else 0)

        # For an oversized record split across pages, discard its tail until
        # its newline. If it spans the whole byte budget, advance the cursor
        # by one bounded window and continue on the next explicit page action.
        if skip_oversized_line:
            last_newline = raw_window.rfind(b"\n")
            if last_newline < 0:
                cursor = TranscriptCursor(lower, True) if lower > 0 else None
                return TranscriptPage(
                    (),
                    cursor,
                    scanned_bytes,
                    0,
                    "Scanning past an oversized transcript event; press older again.",
                )
            raw_window = raw_window[: last_newline + 1]
            upper = lower + len(raw_window)

        # An append can be interrupted between writing a JSON record and its
        # newline. A valid JSON object is still safe to display; malformed tail
        # fragments are skipped like any other corrupt record.
        if lower > 0 and not starts_at_line_boundary:
            first_newline = raw_window.find(b"\n")
            if first_newline < 0:
                if skip_oversized_line:
                    cursor = TranscriptCursor(lower, True)
                    return TranscriptPage(
                        (),
                        cursor,
                        scanned_bytes,
                        0,
                        "Scanning past an oversized transcript event; press older again.",
                    )
                marker = _oversized_preview_event(
                    path, lower, upper, partial=True, rendered=rendered
                )
                return TranscriptPage(
                    (TranscriptEntry(marker, lower, upper, oversized=True, partial_line=True),),
                    TranscriptCursor(lower, True),
                    scanned_bytes,
                    min(max_bytes, _marker_size(marker)),
                    "An exceptionally large event is shown as a marker; inspect it with Enter.",
                )
            if first_newline == len(raw_window) - 1:
                # The bounded window contains only the suffix of one record,
                # followed by its newline. It is the newest event, not an
                # older prefix to discard.
                marker = _oversized_preview_event(
                    path, lower, upper, partial=True, rendered=rendered
                )
                return TranscriptPage(
                    (TranscriptEntry(marker, lower, upper, oversized=True, partial_line=True),),
                    TranscriptCursor(lower, True),
                    scanned_bytes,
                    min(max_bytes, _marker_size(marker)),
                    "An exceptionally large event is shown as a marker; inspect it with Enter.",
                )
            raw_window = raw_window[first_newline + 1 :]
            lower += first_newline + 1

        turn_count = 0
        line_end = len(raw_window)
        if line_end and raw_window.endswith(b"\n"):
            line_end -= 1
        records_scanned = 0
        max_records_scanned = max_events * 4
        oldest_scanned_offset: int | None = None
        while (
            line_end >= 0 and len(selected) < max_events and records_scanned < max_records_scanned
        ):
            previous_newline = raw_window.rfind(b"\n", 0, line_end)
            line_start = previous_newline + 1
            body = raw_window[line_start:line_end]
            end = line_end + 1 if line_end < len(raw_window) else line_end
            start = lower + line_start
            end += lower
            line_end = previous_newline
            if not body:
                continue
            records_scanned += 1
            oldest_scanned_offset = start
            if body.endswith(b"\r"):
                body = body[:-1]
            record_bytes = len(body)
            event: ConversationEvent | None
            oversized = record_bytes > max_event_bytes
            if oversized:
                event = _oversized_preview_event(path, start, end, partial=False, rendered=rendered)
                entry = TranscriptEntry(event, start, end, oversized=True)
                cost = _marker_size(event)
                notice = "An oversized event is shown as a marker; inspect it with Enter."
            else:
                event = _decode_event(body, rendered=rendered)
                if event is None:
                    notice = notice or "Malformed transcript records were skipped."
                    continue
                entry = TranscriptEntry(event, start, end)
                cost = record_bytes

            if retained_bytes + cost > max_bytes:
                if not selected and not oversized:
                    marker = _oversized_preview_event(
                        path, start, end, partial=False, rendered=rendered
                    )
                    marker_cost = _marker_size(marker)
                    if marker_cost <= max_bytes:
                        selected.append(TranscriptEntry(marker, start, end, oversized=True))
                        retained_bytes += marker_cost
                        notice = "An event exceeded the preview byte budget; inspect it with Enter."
                break

            selected.append(entry)
            retained_bytes += cost
            if event.kind == "turn_start":
                turn_count += 1
                if last_turns > 0 and turn_count >= last_turns:
                    break

        selected.reverse()
        if selected:
            earliest_offset = selected[0].start_offset
            older_cursor = TranscriptCursor(earliest_offset) if earliest_offset > 0 else None
        elif oldest_scanned_offset is not None:
            older_cursor = TranscriptCursor(oldest_scanned_offset)
        elif lower > 0:
            older_cursor = TranscriptCursor(lower, skip_oversized_line)
        else:
            older_cursor = None
        if records_scanned >= max_records_scanned and older_cursor is not None:
            notice = notice or "Page event scan reached its safety limit; press older to continue."
        if scanned_bytes >= max_scan_bytes and older_cursor is not None:
            notice = notice or "Page scan reached its safety limit; press older to continue."
        return TranscriptPage(
            tuple(selected),
            scanned_bytes=scanned_bytes,
            retained_bytes=retained_bytes,
            notice=notice,
            older_cursor=older_cursor,
        )
    except OSError:
        return TranscriptPage(
            (), None, scanned_bytes, retained_bytes, "Unable to read transcript page."
        )


def _marker_size(event: ConversationEvent) -> int:
    """Return the serialized-size estimate of a bounded synthetic marker."""

    return len(
        json.dumps(
            {
                "event_id": event.event_id,
                "kind": event.kind,
                "payload": event.payload,
                "timestamp": event.timestamp,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _find_line_start_before(
    handle: BinaryIO, upper_offset: int, scan_budget: int
) -> tuple[int, int, bool]:
    """Find the current oversized record's start using bounded reverse I/O."""

    search_end = max(0, upper_offset)
    scanned = 0
    remaining = scan_budget
    while search_end > 0 and remaining > 0:
        amount = min(_TRANSCRIPT_SCAN_CHUNK_BYTES, remaining, search_end)
        start = search_end - amount
        handle.seek(start)
        block = handle.read(amount)
        scanned += len(block)
        newline = block.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1, scanned, True
        search_end = start
        remaining = scan_budget - scanned
    if search_end == 0:
        return 0, scanned, True
    return search_end, scanned, False


def _oversized_preview_event(
    path: Path,
    start: int,
    end: int,
    *,
    partial: bool,
    rendered: bool,
) -> ConversationEvent:
    """Create a bounded presentation marker for a non-retained JSONL record."""

    span = max(0, end - start)
    reason = "exceptionally large" if partial else f"{span} bytes"
    session_id = path.parent.name
    return ConversationEvent(
        event_id=f"preview-omitted:{session_id}:{start}:{end}",
        kind="system",
        payload={
            "text": f"Transcript event ({reason}) omitted from preview; use /history and Enter to inspect.",
            "_agenthicc_session_id": session_id,
        },
        timestamp=0.0,
        rendered=rendered,
    )


def load_pending_question_waits(session_id: str) -> dict[str, dict[str, object]]:
    """Return durable, non-sensitive question waits without replaying answers.

    A process can disappear after publishing ``question_wait_started`` but
    before publishing a terminal event.  The next session construction uses
    this projection to rehydrate the remaining deadline or reconcile it as
    expired.  The projection contains only bounded lifecycle metadata; the
    original question payload remains in the normal agent/workflow journal.
    """
    pending: dict[str, dict[str, object]] = {}
    terminal_kinds = {
        "question_answered",
        "question_timed_out",
        "question_cancelled",
        "question_wait_failed",
    }
    for event in SessionEventLog.load(session_id, kinds=None):
        if event.kind == "question_wait_started":
            request_id = event.payload.get("request_id")
            fingerprint = event.payload.get("question_fingerprint")
            if (
                isinstance(request_id, str)
                and request_id
                and isinstance(fingerprint, str)
                and fingerprint
            ):
                pending[fingerprint] = dict(event.payload)
        elif event.kind in terminal_kinds:
            fingerprint = event.payload.get("question_fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                pending.pop(fingerprint, None)
    return pending


# ── Event log ─────────────────────────────────────────────────────────────────


class SessionEventLog:
    """Appends ConversationEvents to a JSONL file."""

    def __init__(self, session_id: str) -> None:
        self._path = get_session_log_path(session_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8")

    def append(self, ev: ConversationEvent) -> None:
        try:
            record = {
                "event_id": ev.event_id,
                "kind": ev.kind,
                "payload": ev.payload,
                "timestamp": ev.timestamp,
            }
            self._file.write(json.dumps(record) + "\n")
            self._file.flush()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def load(
        session_id: str,
        *,
        rendered: bool = True,
        last_turns: int | None = None,
        kinds: Collection[str] | None = None,
    ) -> list[ConversationEvent]:
        """Load conversation events, optionally marking them for display.

        Normal restore uses ``rendered=True`` because it only reconstructs
        metrics/state. The interactive resume path requests ``rendered=False``
        and hands those events directly to the scroll appender; they are not
        re-added to persistence subscribers.

        ``last_turns`` bounds visual/history loading to the newest complete
        turn groups.  It is a tail read of the JSONL file, so an old session
        does not need to be read and decoded in full before the TUI can start.
        A non-positive value keeps the backwards-compatible full-load
        behavior. ``kinds`` is useful for narrow projections such as legacy
        token accounting and avoids retaining unrelated event payloads.
        """
        path = get_session_log_path(session_id)
        if not path.exists():
            return []
        if last_turns is not None and last_turns > 0:
            events = _load_recent_turns(path, last_turns, rendered=rendered, kinds=kinds)
        else:
            events = list(_iter_events(path, rendered=rendered, kinds=kinds))
        return events


def load_user_message_history(
    session_id: str,
    *,
    last_turns: int | None = None,
) -> list[str]:
    """Return persisted user submissions for input-history navigation.

    The reactive conversation store is rebuilt for a resumed TUI, while the
    input navigator is a fresh object.  Keep its history derived from the
    durable transcript and include only accepted user-message events, not
    assistant output or internal lifecycle events.
    """
    return user_message_history_from_events(SessionEventLog.load(session_id, last_turns=last_turns))


def user_message_history_from_events(events: Collection[ConversationEvent]) -> list[str]:
    """Project accepted user submissions from an already-loaded event page."""

    history: list[str] = []
    for event in events:
        if event.kind != "user_message":
            continue
        text = event.payload.get("text")
        if isinstance(text, str) and text.strip():
            history.append(text.strip())
    return history


def _iter_events(
    path: Path,
    *,
    rendered: bool,
    kinds: Collection[str] | None = None,
) -> Iterator[ConversationEvent]:
    """Yield valid events without retaining the whole JSONL file in memory."""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            event = _decode_event(line, rendered=rendered)
            if event is not None and (kinds is None or event.kind in kinds):
                yield event


def _decode_event(raw: str | bytes, *, rendered: bool) -> ConversationEvent | None:
    """Decode one event line, treating malformed records as skippable history."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        event_id = data.get("event_id")
        kind = data.get("kind")
        payload = data.get("payload")
        timestamp = data.get("timestamp")
        if (
            not isinstance(event_id, str)
            or not isinstance(kind, str)
            or not isinstance(payload, dict)
            or not isinstance(timestamp, (int, float))
        ):
            return None
        return ConversationEvent(
            event_id=event_id,
            kind=kind,
            payload=cast(dict[str, object], payload),
            timestamp=float(timestamp),
            rendered=rendered,
        )
    except Exception:  # noqa: BLE001
        return None


def _load_recent_turns(
    path: Path,
    last_turns: int,
    *,
    rendered: bool,
    kinds: Collection[str] | None = None,
) -> list[ConversationEvent]:
    """Read the newest turn groups by scanning a JSONL file backwards.

    The returned events remain in their original order.  Scanning from the
    tail stops as soon as the requested number of ``turn_start`` markers has
    been found, with a hard event cap protecting against a malformed or
    exceptionally large single turn.  Logs created before turn markers were
    introduced naturally fall back to their newest bounded event suffix.
    """
    selected: list[ConversationEvent] = []
    turn_count = 0
    position = path.stat().st_size
    pending = b""

    with path.open("rb") as handle:
        while position > 0 and len(selected) < _MAX_RECENT_EVENTS:
            start = max(0, position - _TAIL_READ_BYTES)
            handle.seek(start)
            block = handle.read(position - start)
            lines = (block + pending).split(b"\n")
            pending = lines[0]

            for raw in reversed(lines[1:]):
                if not raw.strip():
                    continue
                event = _decode_event(raw, rendered=rendered)
                if event is None:
                    continue
                if event.kind == "turn_start":
                    turn_count += 1
                if kinds is None or event.kind in kinds:
                    selected.append(event)
                if turn_count >= last_turns:
                    return list(reversed(selected))
                if len(selected) >= _MAX_RECENT_EVENTS:
                    break
            position = start

        if pending.strip() and len(selected) < _MAX_RECENT_EVENTS:
            event = _decode_event(pending, rendered=rendered)
            if event is not None:
                selected.append(event)

    return list(reversed(selected))


# ── Session restoration ───────────────────────────────────────────────────────


async def restore_session(session_id: str, app_state: AppState) -> None:
    """Restore a previous session's metrics into ConversationStore."""
    from agenthicc.tui.conversation_store import ConversationTurn
    from agenthicc.memory.journal import ConversationJournal, journal_path_for
    from agenthicc.runners.usage_ledger import summarize_usage_records

    events = SessionEventLog.load(session_id)

    conv = app_state.conversation

    # Canonical usage records and old rendered token events are deliberately
    # mutually exclusive here: when canonical records exist they win, so a
    # migrated/dual-written session cannot double count the same call.
    usage_records: list[object] = []
    usage_path = journal_path_for(session_id)
    if usage_path.exists():
        journal = ConversationJournal(usage_path)
        try:
            usage_records = [
                {"record": record, "kind": "usage_record"}
                for record in journal.fold_usage_records()
            ]
        finally:
            journal.close()

    if usage_records:
        summary = summarize_usage_records(
            [entry["record"] for entry in usage_records if isinstance(entry, dict)]
        )
        conv.tokens_in.set(_int_value(summary.get("input")))
        conv.tokens_out.set(_int_value(summary.get("output")))
        conv.cost_usd.set(_float_value(summary.get("cost_usd")))
        conv.usage_status.set(_str_value(summary.get("status"), "unavailable"))
        conv.cost_status.set(_str_value(summary.get("cost_status"), "unavailable"))
        conv.usage_calls.set(_int_value(summary.get("calls")))
    else:
        # Restore cumulative metrics from the legacy conversation log.
        total_in, total_out, total_cost = 0, 0, 0.0
        token_events = [event for event in events if event.kind == "tokens"]
        for event in token_events:
            total_in += _int_value(event.payload.get("input_tokens"))
            total_out += _int_value(event.payload.get("output_tokens"))
            total_cost += _float_value(event.payload.get("cost_usd"))
        if token_events:
            conv.tokens_in.set(total_in)
            conv.tokens_out.set(total_out)
            conv.cost_usd.set(total_cost)
            conv.usage_status.set("complete")
            conv.cost_status.set("estimated")
            conv.usage_calls.set(len(token_events))
        else:
            conv.usage_status.set("unavailable")
            conv.cost_status.set("unavailable")
            conv.usage_calls.set(0)

    # Reconstruct turn list (for turn_count Computed)
    current: ConversationTurn | None = None
    turns: list[ConversationTurn] = []
    for ev in events:
        if ev.kind == "turn_start":
            current = ConversationTurn(
                turn_id=_str_value(ev.payload.get("turn_id"), ev.event_id),
                agent_name=_str_value(ev.payload.get("agent_name"), "assistant"),
                timestamp=ev.timestamp,
            )
            turns.append(current)
        elif current is not None:
            current.events.append(ev)
    conv.turns.set(turns)

    # Show resume notification
    conv.notification.set(f"Resumed session {session_id[:8]}… ({len(turns)} previous turns)")
