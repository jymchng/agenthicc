"""Three-tier memory layers for agenthicc (PRD-05).

Tiers:

- **Session**  — :class:`SessionMemoryLayer`, an in-process LRU cache with
  per-entry TTL.  Lost on process exit.
- **Project**  — :class:`ProjectMemoryLayer`, a SQLite-backed key-value store
  (namespaced) plus a content-addressed artifact table.  Persisted under the
  project directory.
- **Global**   — :class:`GlobalMemoryLayer`, the same SQLite pattern at a
  separate, user-wide path (``~/.agenthicc/global.db`` by default).

Concurrency invariants (PRD-05 §2.5):

1. Reads never block — no lock acquisition on the read path.
2. Writes are serialised per tier through a single ``asyncio.Lock``.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ArtifactRecord",
    "GlobalMemoryLayer",
    "MemoryTier",
    "ProjectMemoryLayer",
    "SessionEntry",
    "SessionMemoryLayer",
]


class MemoryTier(str, enum.Enum):
    """The three memory tiers.  ``global`` is a keyword, hence ``GLOBAL_``."""

    SESSION = "session"
    PROJECT = "project"
    GLOBAL_ = "global"


# ---------------------------------------------------------------------------
# Tier 1 — Session layer (in-process LRU with TTL)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionEntry:
    """A single entry in the session-layer LRU cache."""

    key: str
    value: Any
    namespace: str = "default"
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None  # monotonic deadline; None = no expiry

    def is_expired(self) -> bool:
        return self.expires_at is not None and time.monotonic() >= self.expires_at


class SessionMemoryLayer:
    """In-process LRU cache implementing the session memory tier.

    Backed by a :class:`collections.OrderedDict` bounded at ``max_entries``.
    TTL expiry uses ``time.monotonic`` and is enforced *lazily*: an expired
    entry is evicted on the ``get()`` that observes it (or by an explicit
    :meth:`prune_expired` call from a compaction task).

    **Why lock-free reads are safe**: writes are serialised through
    ``self._write_lock`` (an ``asyncio.Lock``), while reads touch the dict
    without any lock.  In CPython the GIL guarantees that the individual
    ``dict``/``OrderedDict`` operations used here (``get``, ``__setitem__``,
    ``pop``, ``move_to_end``, ``popitem``) each execute atomically — a reader
    can never observe a half-applied mutation.  A read may race a concurrent
    write only at the granularity of whole operations, i.e. it sees either
    the old or the new entry, both of which are consistent values.  We never
    iterate the dict on the read path, so resize-during-iteration is not a
    concern either.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._cache: OrderedDict[tuple[str, str], SessionEntry] = OrderedDict()
        self._write_lock = asyncio.Lock()

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @staticmethod
    def _ns_key(key: str, namespace: str) -> tuple[str, str]:
        return (namespace, key)

    # -- read path (lock-free) ------------------------------------------------

    def get(self, key: str, *, namespace: str = "default") -> tuple[bool, Any]:
        """Return ``(found, value)``.  Lazily evicts an expired entry."""
        ns_key = self._ns_key(key, namespace)
        entry = self._cache.get(ns_key)
        if entry is None:
            return (False, None)
        if entry.is_expired():
            # Lazy eviction: atomic pop under the GIL; idempotent if a
            # concurrent reader already removed it.
            self._cache.pop(ns_key, None)
            return (False, None)
        try:
            self._cache.move_to_end(ns_key)  # mark as most-recently-used
        except KeyError:  # pragma: no cover - raced with eviction
            pass
        return (True, entry.value)

    # -- write path (serialised) ----------------------------------------------

    async def set(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "default",
        ttl: float | None = None,
    ) -> None:
        """Insert or overwrite an entry; evicts LRU entries above capacity."""
        expires_at = (time.monotonic() + ttl) if ttl is not None else None
        entry = SessionEntry(
            key=key, value=value, namespace=namespace, expires_at=expires_at
        )
        ns_key = self._ns_key(key, namespace)
        async with self._write_lock:
            if ns_key in self._cache:
                self._cache.move_to_end(ns_key)
            self._cache[ns_key] = entry
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)

    async def delete(self, key: str, *, namespace: str = "default") -> None:
        async with self._write_lock:
            self._cache.pop(self._ns_key(key, namespace), None)

    async def prune_expired(self) -> int:
        """Remove all expired entries.  Returns the number removed."""
        async with self._write_lock:
            expired = [k for k, v in self._cache.items() if v.is_expired()]
            for k in expired:
                del self._cache[k]
            return len(expired)

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: str) -> bool:
        return self.get(key)[0]


# ---------------------------------------------------------------------------
# Tiers 2 & 3 — SQLite-backed key-value + artifact store
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ArtifactRecord:
    """A content-addressed artifact stored in the project/global layer."""

    artifact_id: str  # sha256 hex digest of the raw content
    content: bytes
    content_type: str = "text/plain"
    published_by: str | None = None
    created_at: float = 0.0

    @property
    def size_bytes(self) -> int:
        return len(self.content)


_KV_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
"""

_ARTIFACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id  TEXT PRIMARY KEY,
    content      BLOB NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    published_by TEXT,
    created_at   REAL NOT NULL
);
"""


class ProjectMemoryLayer:
    """SQLite-backed project-scoped memory layer.

    Key-value records carry a ``namespace`` column for multi-agent isolation
    within a project; artifacts are content-addressed by the sha256 of their
    raw bytes.  All SQLite work runs on a worker thread via
    ``asyncio.to_thread`` with one short-lived connection per call, so the
    event loop never blocks on disk I/O.  Writes are additionally serialised
    through a per-layer ``asyncio.Lock``; SQLite WAL mode lets readers
    proceed concurrently with the single writer.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()
        self._init_schema()

    @property
    def db_path(self) -> str:
        return self._db_path

    # -- connection helpers (called on worker threads) -------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_KV_SCHEMA + _ARTIFACT_SCHEMA)

    # -- key-value API ----------------------------------------------------------

    async def get(self, key: str, *, namespace: str = "default") -> tuple[bool, Any]:
        """Return ``(found, value)`` with the value JSON-decoded."""

        def _read() -> tuple[bool, Any]:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM kv WHERE namespace = ? AND key = ?",
                    (namespace, key),
                ).fetchone()
            if row is None:
                return (False, None)
            return (True, json.loads(row["value"]))

        return await asyncio.to_thread(_read)

    async def set(self, key: str, value: Any, *, namespace: str = "default") -> None:
        """Insert or overwrite ``key`` in ``namespace`` (value JSON-encoded)."""
        serialised = json.dumps(value, default=str)
        now = time.time()

        def _write() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO kv (namespace, key, value, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (namespace, key)
                    DO UPDATE SET value = excluded.value
                    """,
                    (namespace, key, serialised, now),
                )

        async with self._write_lock:
            await asyncio.to_thread(_write)

    async def delete(self, key: str, *, namespace: str = "default") -> None:
        def _delete() -> None:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM kv WHERE namespace = ? AND key = ?",
                    (namespace, key),
                )

        async with self._write_lock:
            await asyncio.to_thread(_delete)

    # -- artifact API -------------------------------------------------------------

    async def put_artifact(
        self,
        content: bytes | str,
        *,
        content_type: str = "text/plain",
        published_by: str | None = None,
    ) -> ArtifactRecord:
        """Store an artifact, content-addressed by sha256.  Idempotent."""
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        artifact_id = hashlib.sha256(raw).hexdigest()
        now = time.time()

        def _write() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO artifacts
                        (artifact_id, content, content_type, published_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (artifact_id) DO NOTHING
                    """,
                    (artifact_id, raw, content_type, published_by, now),
                )

        async with self._write_lock:
            await asyncio.to_thread(_write)
        return ArtifactRecord(
            artifact_id=artifact_id,
            content=raw,
            content_type=content_type,
            published_by=published_by,
            created_at=now,
        )

    async def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        def _read() -> ArtifactRecord | None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
            if row is None:
                return None
            return ArtifactRecord(
                artifact_id=row["artifact_id"],
                content=bytes(row["content"]),
                content_type=row["content_type"],
                published_by=row["published_by"],
                created_at=row["created_at"],
            )

        return await asyncio.to_thread(_read)

    async def vacuum(self) -> None:
        async with self._write_lock:
            await asyncio.to_thread(
                lambda: self._connect().execute("VACUUM").connection.close()
            )


class GlobalMemoryLayer(ProjectMemoryLayer):
    """User-wide memory layer: the same SQLite pattern at a separate path.

    Defaults to ``~/.agenthicc/global.db``; pass ``db_path`` explicitly (e.g.
    a tmp directory) in tests.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = Path.home() / ".agenthicc" / "global.db"
        super().__init__(db_path)
