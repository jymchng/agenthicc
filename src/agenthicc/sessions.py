"""Session index persistence — CRUD helpers for .agenthicc/sessions.json."""

from __future__ import annotations

import json
import os
import time
from typing import cast
from pathlib import Path

from agenthicc.types import JsonObject

_SESSIONS_DIR = Path(".agenthicc/sessions")
_SESSION_INDEX = Path(".agenthicc/sessions.json")
# The TUI and background worker use this user-wide index.  Keep the historical
# project-local index for compatibility, but make the list/show commands see
# the same sessions that the current runtime registers.
_CANONICAL_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"
_CANONICAL_SESSION_INDEX = _CANONICAL_SESSIONS_DIR / "index.json"


def _load_session_index() -> dict[str, JsonObject]:
    if _SESSION_INDEX.exists():
        try:
            loaded = json.loads(_SESSION_INDEX.read_text())
            if isinstance(loaded, dict):
                return cast(dict[str, JsonObject], loaded)
        except Exception:
            return {}
    return {}


def _load_canonical_session_index() -> dict[str, JsonObject]:
    """Load the current user-wide TUI session index for compatibility commands."""

    if not _CANONICAL_SESSION_INDEX.exists():
        return {}
    try:
        loaded = json.loads(_CANONICAL_SESSION_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}

    index: dict[str, JsonObject] = {}
    for session_id, value in loaded.items():
        if not isinstance(session_id, str) or not isinstance(value, dict):
            continue
        record = cast(JsonObject, dict(value))
        if "last_used" not in record:
            record["last_used"] = record.get("last_active", 0.0)
        record.setdefault(
            "log_path",
            str(_CANONICAL_SESSIONS_DIR / session_id / "conversation.jsonl"),
        )
        index[session_id] = record
    return index


def _load_all_session_indexes() -> dict[str, JsonObject]:
    """Merge legacy project-local and current user-wide session records."""

    merged = _load_session_index()
    # Prefer the canonical record when an ID exists in both indexes; it points
    # to the durable user-wide transcript and has the current activity fields.
    merged.update(_load_canonical_session_index())
    return merged


def _ordered_session_records() -> list[tuple[str, JsonObject]]:
    """Return merged sessions in the order used by list surfaces."""

    index = _load_all_session_indexes()
    return sorted(
        index.items(), key=lambda item: _timestamp(item[1].get("last_used")), reverse=True
    )


def _save_session_index(index: dict[str, JsonObject]) -> None:
    _SESSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
    _SESSION_INDEX.write_text(json.dumps(index, indent=2))


def _register_session(session_id: str) -> None:
    index = _load_session_index()
    index[session_id] = {
        "cwd": os.getcwd(),
        "created_at": time.time(),
        "last_used": time.time(),
        "log_path": str(_SESSIONS_DIR / f"{session_id}.jsonl"),
    }
    _save_session_index(index)


def _touch_session(session_id: str) -> None:
    index = _load_session_index()
    if session_id in index:
        index[session_id]["last_used"] = time.time()
        _save_session_index(index)


def _find_latest_session_for_cwd() -> str | None:
    index = _load_session_index()
    cwd = os.getcwd()
    candidates = [
        (_timestamp(data.get("last_used")), sid)
        for sid, data in index.items()
        if data.get("cwd") == cwd
    ]
    return max(candidates)[1] if candidates else None


def _get_session_log_path(session_id: str) -> Path | None:
    index = _load_session_index()
    index.update(_load_canonical_session_index())
    entry = index.get(session_id)
    if entry:
        log_path = entry.get("log_path")
        if isinstance(log_path, str):
            return Path(log_path)
    return None


def _do_sessions(*, page: int = 1, page_size: int = 50) -> None:
    """Print one page of saved sessions, ordered by most recent use."""

    if page < 1:
        raise ValueError("page must be at least 1")
    if page_size < 1:
        raise ValueError("page_size must be at least 1")
    index = _load_all_session_indexes()
    if not index:
        print("No saved sessions.")
        return
    cwd = os.getcwd()
    ordered = _ordered_session_records()
    page_count = max(1, (len(ordered) + page_size - 1) // page_size)
    if page > page_count:
        print(f"Page {page} is out of range; there are {page_count} page(s).")
        return
    start = (page - 1) * page_size
    print(f"Sessions (page {page}/{page_count}; {len(ordered)} total)")
    for sid, data in ordered[start : start + page_size]:
        marker = " *" if data.get("cwd") == cwd else ""
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(_timestamp(data.get("last_used"))))
        print(f"  {sid[:12]}  {last}  {data.get('cwd', '')} {marker}")


def _timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0
