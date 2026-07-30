"""Session persistence: index and event log (PRD-67 §3-4)."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState

from agenthicc.tui.conversation_store import ConversationEvent

_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"
_SESSION_INDEX = _SESSIONS_DIR / "index.json"


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
    def load(session_id: str) -> list[ConversationEvent]:
        path = get_session_log_path(session_id)
        if not path.exists():
            return []
        events: list[ConversationEvent] = []
        for line in path.read_text().splitlines():
            try:
                data = json.loads(line)
                events.append(
                    ConversationEvent(
                        event_id=data["event_id"],
                        kind=data["kind"],
                        payload=data["payload"],
                        timestamp=data["timestamp"],
                        rendered=True,  # already displayed; skip on restore
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        return events


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
