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


def _load_session_index() -> dict[str, JsonObject]:
    if _SESSION_INDEX.exists():
        try:
            loaded = json.loads(_SESSION_INDEX.read_text())
            if isinstance(loaded, dict):
                return cast(dict[str, JsonObject], loaded)
        except Exception:
            return {}
    return {}


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
    entry = index.get(session_id)
    if entry:
        log_path = entry.get("log_path")
        if isinstance(log_path, str):
            return Path(log_path)
    return None


def _do_sessions() -> None:
    index = _load_session_index()
    if not index:
        print("No saved sessions.")
        return
    cwd = os.getcwd()
    for sid, data in sorted(
        index.items(), key=lambda x: _timestamp(x[1].get("last_used")), reverse=True
    ):
        marker = " *" if data.get("cwd") == cwd else ""
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(_timestamp(data.get("last_used"))))
        print(f"  {sid[:12]}  {last}  {data.get('cwd', '')} {marker}")


def _timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0
