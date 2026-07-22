"""Workspace file cache — durable, freshness-validated read cache (PRD-132 L1).

A per-project SQLite store keyed by **absolute path**, recording each file's
``(sha256, mtime, size, encoding, content)``.  A cached entry is served **only**
when the file's current ``(mtime, size, encoding)`` match what was stored — a
changed file always misses and is re-read.  This is a hard correctness
requirement: the cache must never serve stale code.

The cache is the durable substrate the later reuse layers build on (PRD-131 L2
repo map, L3 RAG) and gives a new session content-identical re-reads of unchanged
files (which also stabilises L0's prompt-cache hits).  It is wired through a
process-level singleton (`configure_file_cache` / `get_file_cache`); when none is
configured the read path behaves exactly as before.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

__all__ = ["WorkspaceFileCache", "configure_file_cache", "get_file_cache"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    abspath    TEXT PRIMARY KEY,
    sha256     TEXT NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    encoding   TEXT NOT NULL,
    content    TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class WorkspaceFileCache:
    """Durable cache of file reads, invalidated by ``(mtime, size, encoding)``."""

    __slots__ = ("_db",)

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def get_fresh(self, abspath: str, encoding: str = "utf-8") -> str | None:
        """Return cached content iff the file on disk is unchanged.

        Stats the file; returns the stored content only when ``mtime``, ``size``
        and ``encoding`` all match the cached entry.  Any change — or a missing
        file, or no entry — returns ``None`` (a miss → caller reads fresh).
        """
        try:
            st = os.stat(abspath)
        except OSError:
            return None
        row = self._db.execute(
            "SELECT mtime_ns, size, encoding, content FROM files WHERE abspath=?",
            (abspath,),
        ).fetchone()
        if row is None:
            return None
        mtime_ns, size, enc, content = row
        if mtime_ns == st.st_mtime_ns and size == st.st_size and enc == encoding:
            return content
        return None

    def store(self, abspath: str, content: str, encoding: str = "utf-8") -> None:
        """Record *content* for *abspath* with its current freshness stamp."""
        try:
            st = os.stat(abspath)
        except OSError:
            return
        sha = hashlib.sha256(content.encode(encoding, errors="replace")).hexdigest()
        self._db.execute(
            "INSERT OR REPLACE INTO files "
            "(abspath, sha256, mtime_ns, size, encoding, content, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (abspath, sha, st.st_mtime_ns, st.st_size, encoding, content, time.time()),
        )
        self._db.commit()

    def __len__(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass


# ── process-level singleton ──────────────────────────────────────────────────

_cache: WorkspaceFileCache | None = None


def configure_file_cache(cache: WorkspaceFileCache | None) -> None:
    """Install (or clear, with ``None``) the process-level file cache."""
    global _cache
    _cache = cache


def get_file_cache() -> WorkspaceFileCache | None:
    """Return the configured file cache, or ``None`` when disabled."""
    return _cache
