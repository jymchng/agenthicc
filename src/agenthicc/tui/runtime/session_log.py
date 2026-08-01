"""Session persistence: index and event log (PRD-67 §3-4)."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState

from agenthicc.tui.conversation_store import ConversationEvent

_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"
_SESSION_INDEX = _SESSIONS_DIR / "index.json"

# Visual resume is intentionally bounded.  A session may contain thousands of
# tool events, but the user normally needs only the most recent exchanges to
# orient themselves.  The durable event log remains complete; this limit only
# controls the presentation/input-history projection rebuilt at startup.
DEFAULT_RESUME_TRANSCRIPT_TURNS = 20
_TAIL_READ_BYTES = 64 * 1024
_MAX_RECENT_EVENTS = 4096


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


def _save_index(data: dict[str, dict[str, object]]) -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _SESSION_INDEX.write_text(json.dumps(data, indent=2))


def register_session(session_id: str, cwd: str, model: str) -> None:
    index = _load_index()
    index[session_id] = {
        "cwd": cwd,
        "model": model,
        "mode": "Safe",
        "created_at": time.time(),
        "last_active": time.time(),
    }
    _save_index(index)
    session_dir = _SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "metadata.json").write_text(json.dumps(index[session_id], indent=2))


def update_session_mode(session_id: str, mode: str) -> None:
    """Persist the canonical active mode without recording transient internals."""
    index = _load_index()
    metadata = index.get(session_id)
    if metadata is None:
        return
    metadata["mode"] = mode
    metadata["last_active"] = time.time()
    _save_index(index)
    session_dir = _SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))


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


def touch_session(session_id: str) -> None:
    index = _load_index()
    if session_id in index:
        index[session_id]["last_active"] = time.time()
        _save_index(index)


def find_latest_session_for_cwd(cwd: str | None = None) -> str | None:
    cwd = cwd or os.getcwd()
    index = _load_index()
    candidates = [(sid, meta) for sid, meta in index.items() if meta.get("cwd") == cwd]
    if not candidates:
        return None
    latest = max(candidates, key=lambda x: _float_value(x[1].get("last_active")))
    return latest[0]


def get_session_log_path(session_id: str) -> Path:
    return _SESSIONS_DIR / session_id / "conversation.jsonl"


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
    history: list[str] = []
    for event in SessionEventLog.load(session_id, last_turns=last_turns):
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
    from agenthicc.tui.conversation_store import ConversationTurn  # noqa: PLC0415
    from agenthicc.memory.journal import ConversationJournal, journal_path_for  # noqa: PLC0415
    from agenthicc.runners.usage_ledger import summarize_usage_records  # noqa: PLC0415

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
