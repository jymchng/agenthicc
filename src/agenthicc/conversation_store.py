"""Persistent per-project conversation store backed by SQLite.

One database per project working directory, located at:
    ~/.agenthicc/conversation-stores/<cwd-hash>.db

Stores:
- ``turns``            — user + assistant messages for transcript replay
- ``memory_snapshots`` — ShortTermMemory snapshots for LLM context restore
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = ["ConversationStore"]


def _default_db_path() -> Path:
    cwd_hash = hashlib.sha256(os.getcwd().encode()).hexdigest()[:16]
    db_dir = Path.home() / ".agenthicc" / "conversation-stores"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / f"{cwd_hash}.db"


class ConversationStore:
    """Append-only SQLite store for conversation turns and memory snapshots."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or _default_db_path()
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS turns (
                session_id  TEXT    NOT NULL,
                turn_index  INTEGER NOT NULL,
                role        TEXT    NOT NULL,
                content     TEXT    NOT NULL,
                timestamp   REAL    NOT NULL,
                model_short TEXT    NOT NULL DEFAULT '',
                PRIMARY KEY (session_id, turn_index, role)
            );
            CREATE TABLE IF NOT EXISTS memory_snapshots (
                session_id    TEXT NOT NULL PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                updated_at    REAL NOT NULL
            );
        """)
        self._conn.commit()

    # ── writes ────────────────────────────────────────────────────────────

    def save_turn(
        self,
        session_id: str,
        turn_index: int,
        role: str,
        content: str,
        timestamp: float,
        model_short: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?,?)",
            (session_id, turn_index, role, content, timestamp, model_short),
        )
        self._conn.commit()

    def save_memory_snapshot(self, session_id: str, snapshot: Any) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO memory_snapshots VALUES (?,?,?)",
            (session_id, json.dumps(snapshot, default=str), time.time()),
        )
        self._conn.commit()

    # ── reads ─────────────────────────────────────────────────────────────

    def next_turn_index(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(turn_index)+1, 0) FROM turns WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return row[0] if row else 0

    def load_turns(self, session_id: str) -> list[dict[str, Any]]:
        """Return turns ordered by index: [{user: ..., assistant: ..., timestamp: ..., model_short: ...}, ...]."""
        rows = self._conn.execute(
            "SELECT turn_index, role, content, timestamp, model_short "
            "FROM turns WHERE session_id=? ORDER BY turn_index",
            (session_id,),
        ).fetchall()
        by_idx: dict[int, dict[str, Any]] = {}
        for turn_index, role, content, timestamp, model_short in rows:
            if turn_index not in by_idx:
                by_idx[turn_index] = {"timestamp": timestamp, "model_short": model_short}
            by_idx[turn_index][role] = content
        return [by_idx[i] for i in sorted(by_idx)]

    def load_memory_snapshot(self, session_id: str) -> Any:
        """Return the most recent ShortTermMemory snapshot dict, or None."""
        row = self._conn.execute(
            "SELECT snapshot_json FROM memory_snapshots WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        self._conn.close()
