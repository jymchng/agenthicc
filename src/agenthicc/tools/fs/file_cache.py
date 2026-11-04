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
import threading
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
    """Durable cache of file reads, invalidated by ``(mtime, size, encoding)``.

    The store opens on first use, on the thread that reads files, so installing
    the cache for a session does no disk I/O before its first frame (AGX-02).
    """

    __slots__ = ("_db", "_db_path", "_lock")

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        # Tools read through worker threads (TOOLR-09): one connection,
        # shared under a lock, is used from whichever thread runs the read.
        self._lock = threading.RLock()

    def _connection(self) -> sqlite3.Connection:
        db = self._db
        if db is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self._db_path), check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            db.commit()
            self._db = db
        return db

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
        with self._lock:
            row = (
                self._connection()
                .execute(
                    "SELECT mtime_ns, size, encoding, content FROM files WHERE abspath=?",
                    (abspath,),
                )
                .fetchone()
            )
        if row is None:
            return None
        mtime_ns, size, enc, content = row
        if mtime_ns == st.st_mtime_ns and size == st.st_size and enc == encoding:
            return content if isinstance(content, str) else None
        return None

    def store_stamped(
        self, abspath: str, content: str, *, mtime_ns: int, size: int, encoding: str = "utf-8"
    ) -> None:
        """Record *content* with the stamp the caller observed *before* reading it."""
        sha = hashlib.sha256(content.encode(encoding, errors="replace")).hexdigest()
        with self._lock:
            db = self._connection()
            db.execute(
                "INSERT OR REPLACE INTO files "
                "(abspath, sha256, mtime_ns, size, encoding, content, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (abspath, sha, mtime_ns, size, encoding, content, time.time()),
            )
            db.commit()

    def store(self, abspath: str, content: str, encoding: str = "utf-8") -> None:
        """Record bytes this process just wrote, stamped with the file's current state.

        Only for content the caller wrote itself (TOOLR-01 writes): nothing
        else can have changed it between the write and this stat.
        """
        try:
            st = os.stat(abspath)
        except OSError:
            return
        self.store_stamped(
            abspath, content, mtime_ns=st.st_mtime_ns, size=st.st_size, encoding=encoding
        )

    def forget(self, abspath: str) -> None:
        """Drop *abspath*'s entry."""
        with self._lock:
            db = self._connection()
            db.execute("DELETE FROM files WHERE abspath=?", (abspath,))
            db.commit()

    def __len__(self) -> int:
        with self._lock:
            return int(self._connection().execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def close(self) -> None:
        db = self._db
        if db is None:
            return
        try:
            db.close()
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
