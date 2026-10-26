"""Derived SQLite index of the background-session registry (AGX-03).

``events.jsonl`` remains the authority. This index is a rebuildable projection
of it: one row per session holding the full record, plus the columns used to
order, filter, count and search. A fresh process therefore reads one page with
an indexed query instead of folding the whole log first.

The ``meta`` table records how far into the log the index has applied —
the log's inode, byte offset, modification time and last sequence number. A
reader whose log has grown applies the tail; an inode change, truncation,
same-size rewrite, sequence gap or schema change makes the store rebuild the
index from the log.

Search keeps the registry's case-insensitive substring semantics, per word:
every word of a query must occur, in any order (AGX-06). SQLite's FTS5
``trigram`` tokenizer indexes the searchable text so patterns of three or more
characters use the index; without FTS5 the same ``LIKE`` runs over a plain
column.
"""

from __future__ import annotations

import enum
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agenthicc.runners.process_lease import InterProcessLock

from .model import BackgroundSession, SessionStatus

__all__ = [
    "INDEX_SCHEMA_VERSION",
    "IndexState",
    "SearchMode",
    "SessionFilter",
    "SessionIndex",
    "SessionOrder",
]

#: Bumped whenever a column, table or search representation changes; a file
#: stamped with another version is discarded and rebuilt from the log.
INDEX_SCHEMA_VERSION = 4

SearchMode = Literal["fts5", "like"]

_ATTENTION_STATUSES = (SessionStatus.WAITING_APPROVAL.value, SessionStatus.WAITING_INPUT.value)

_BASE_TABLES = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS position (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    events_inode     INTEGER NOT NULL,
    applied_offset   INTEGER NOT NULL,
    applied_mtime_ns INTEGER NOT NULL,
    applied_sequence INTEGER NOT NULL
);""",
    """CREATE TABLE IF NOT EXISTS sessions (
    rowid             INTEGER PRIMARY KEY,
    session_id        TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL,
    dispatch_state    TEXT NOT NULL,
    needs_attention   INTEGER NOT NULL,
    priority          TEXT NOT NULL,
    pinned            INTEGER NOT NULL,
    cwd               TEXT NOT NULL,
    workflow_name     TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    parent_session_id TEXT NOT NULL,
    role              TEXT NOT NULL,
    title             TEXT NOT NULL,
    last_active       REAL NOT NULL,
    created_at        REAL NOT NULL,
    worker_pid        INTEGER,
    idempotency_key   TEXT NOT NULL,
    search_text       TEXT NOT NULL,
    record            TEXT NOT NULL
);""",
)
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS sessions_order ON sessions (pinned DESC, last_active DESC, session_id)",
    "CREATE INDEX IF NOT EXISTS sessions_recent ON sessions (last_active DESC, session_id)",
    "CREATE INDEX IF NOT EXISTS sessions_created ON sessions (created_at DESC, session_id)",
    "CREATE INDEX IF NOT EXISTS sessions_status ON sessions (status, last_active DESC)",
    "CREATE INDEX IF NOT EXISTS sessions_attention ON sessions (needs_attention, last_active DESC)",
    "CREATE INDEX IF NOT EXISTS sessions_cwd ON sessions (cwd, last_active DESC)",
    "CREATE INDEX IF NOT EXISTS sessions_workflow ON sessions (workflow_name, last_active DESC)",
    "CREATE INDEX IF NOT EXISTS sessions_run ON sessions (run_id) WHERE run_id <> ''",
    "CREATE INDEX IF NOT EXISTS sessions_parent ON sessions (parent_session_id) WHERE parent_session_id <> ''",
    "CREATE INDEX IF NOT EXISTS sessions_idempotency ON sessions (idempotency_key) WHERE idempotency_key <> ''",
)
_SESSION_REQUIRED_COLUMNS = frozenset(
    {
        "session_id",
        "status",
        "dispatch_state",
        "needs_attention",
        "priority",
        "pinned",
        "cwd",
        "workflow_name",
        "run_id",
        "parent_session_id",
        "role",
        "title",
        "last_active",
        "created_at",
        "worker_pid",
        "idempotency_key",
        "search_text",
        "record",
    }
)
_POSITION_REQUIRED_COLUMNS = frozenset(
    {"id", "events_inode", "applied_offset", "applied_mtime_ns", "applied_sequence"}
)


def _is_schema_operational_error(error: sqlite3.OperationalError) -> bool:
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "no such column",
            "no such table",
            "has no column named",
            "malformed database schema",
        )
    )


# An external-content index over ``sessions.search_text``: it stores trigrams,
# not a second copy of the text. Triggers keep it in step, and an update
# re-indexes only when the searchable text changed — heartbeats do not.
_FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    search_text, content = 'sessions', content_rowid = 'rowid', tokenize = 'trigram'
);
"""
_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS sessions_fts_insert AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts (rowid, search_text) VALUES (new.rowid, new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS sessions_fts_delete AFTER DELETE ON sessions BEGIN
    INSERT INTO sessions_fts (sessions_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
END;
CREATE TRIGGER IF NOT EXISTS sessions_fts_update AFTER UPDATE OF search_text ON sessions
WHEN old.search_text IS NOT new.search_text BEGIN
    INSERT INTO sessions_fts (sessions_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO sessions_fts (rowid, search_text) VALUES (new.rowid, new.search_text);
END;
"""


class SessionOrder(enum.Enum):
    """Row orders a listing can ask for."""

    #: Pinned first, then most recent activity: the registry's own order.
    REGISTRY = "registry"
    #: Most recent activity first.
    RECENT = "recent"
    #: Most recently created first.
    CREATED = "created"
    #: Title, case-insensitively.
    TITLE = "title"
    #: Grouped by project directory, most recent activity first within one.
    PROJECT = "project"


_ORDERS: dict[SessionOrder, str] = {
    SessionOrder.REGISTRY: "ORDER BY pinned DESC, last_active DESC, session_id",
    SessionOrder.RECENT: "ORDER BY last_active DESC, session_id",
    SessionOrder.CREATED: "ORDER BY created_at DESC, session_id",
    SessionOrder.TITLE: "ORDER BY title COLLATE NOCASE, last_active DESC, session_id",
    SessionOrder.PROJECT: "ORDER BY cwd, last_active DESC, session_id",
}

_COLUMNS = (
    "session_id",
    "status",
    "dispatch_state",
    "needs_attention",
    "priority",
    "pinned",
    "cwd",
    "workflow_name",
    "run_id",
    "parent_session_id",
    "role",
    "title",
    "last_active",
    "created_at",
    "worker_pid",
    "idempotency_key",
    "search_text",
    "record",
)
# An upsert (not INSERT OR REPLACE) keeps the row id, so the FTS triggers see
# an UPDATE and can skip re-indexing unchanged text.
_UPSERT = (
    f"INSERT INTO sessions ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
    "ON CONFLICT (session_id) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in _COLUMNS[1:])
)


@dataclass(frozen=True, slots=True)
class IndexState:
    """How far into ``events.jsonl`` the index has applied."""

    inode: int
    offset: int
    mtime_ns: int
    sequence: int


@dataclass(frozen=True, slots=True)
class SessionFilter:
    """Row filters shared by listing, paging and counting; all must hold.

    ``query`` is split into words and every word must occur in the
    searchable text. ``project`` matches a full working directory or its last
    path component; ``agent`` matches the workflow or the worker role;
    ``labels`` must all be present; ``active_since`` keeps sessions active at
    or after that time.
    """

    include_archived: bool = True
    include_deleted: bool = False
    cwd: str | None = None
    workflow_name: str | None = None
    query: str = ""
    status: SessionStatus | None = None
    statuses: frozenset[SessionStatus] | None = None
    idempotency_key: str | None = None
    project: str | None = None
    agent: str | None = None
    run_id: str | None = None
    labels: frozenset[str] = frozenset()
    active_since: float | None = None
    pinned: bool | None = None


def search_text(session: BackgroundSession) -> str:
    """Return the lower-cased text a registry search matches against."""
    return " ".join(
        (
            session.session_id,
            session.title,
            session.intent,
            " ".join(session.labels),
            session.latest_activity,
            session.cwd,
            session.workflow_name,
        )
    ).lower()


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _suffix_pattern(suffix: str) -> str:
    escaped = suffix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}"


def _glob_pattern(query: str) -> str:
    # FTS5 uses the trigram index for GLOB but not for LIKE ... ESCAPE; a
    # bracket expression matches GLOB's special characters literally. The
    # stored text and the query are both lower-cased, so case-sensitive GLOB
    # still matches case-insensitively.
    escaped = "".join(f"[{character}]" if character in "*?[" else character for character in query)
    return f"*{escaped}*"


def _row(session: BackgroundSession) -> tuple[object, ...]:
    return (
        session.session_id,
        session.status.value,
        session.dispatch_state.value,
        1 if session.status.value in _ATTENTION_STATUSES else 0,
        session.priority.value,
        1 if session.pinned else 0,
        session.cwd,
        session.workflow_name,
        session.run_id,
        session.parent_session_id,
        session.role,
        session.title,
        session.last_active,
        session.created_at,
        session.worker_pid,
        session.idempotency_key,
        search_text(session),
        json.dumps(session.to_dict(), separators=(",", ":")),
    )


def _decode(record: str) -> BackgroundSession:
    value = json.loads(record)
    if not isinstance(value, dict):
        raise ValueError("index record is not an object")
    return BackgroundSession.from_mapping(value)


#: Decoded sessions kept per index; a hit requires the identical stored record.
_CACHE_LIMIT = 4096


class SessionIndex:
    """One SQLite index file; callers serialize access to an instance.

    Writes go through ``BEGIN IMMEDIATE`` transactions, so concurrent processes
    see either the old or the new state. The index is derived, so it uses
    ``synchronous=NORMAL``: a crash can lose the latest index transaction but
    never the log, and the next reader applies the tail again.
    """

    def __init__(self, path: Path, *, search_mode: SearchMode | None = None) -> None:
        self.path = path
        self._requested_search_mode = search_mode
        self._connection: sqlite3.Connection | None = None
        self._connection_identity: tuple[int, int] | None = None
        self._failure_identity: tuple[int, int] | None = None
        self._failure_observed = False
        self._search_mode: SearchMode = "like"
        # Decoding a stored record validates ~50 fields; reuse the frozen
        # session when the row still holds exactly the text it came from.
        self._decoded: dict[str, tuple[str, BackgroundSession]] = {}

    # ── connection and schema ────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is not None:
            if self.connection_matches_path():
                return connection
            self.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize connection/schema initialization with corruption recovery.
        # SQLite transactions still coordinate normal reads and writes; this
        # lock protects the short interval when the derived file is replaced.
        with InterProcessLock(self._maintenance_lock_path):
            connection = self._connection
            if connection is not None and self.connection_matches_path():
                return connection
            if connection is not None:
                self.close()
            try:
                connection = self._open()
            except sqlite3.DatabaseError:
                self._failure_identity = self._path_identity()
                self._failure_observed = True
                raise
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("BEGIN IMMEDIATE")
                if not self._schema_is_compatible(connection):
                    self._reset_schema_tables(connection)
                for statement in _BASE_TABLES:
                    connection.execute(statement)
                for statement in _INDEXES:
                    connection.execute(statement)
                mode = self._stored_search_mode(connection)
                has_fts = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions_fts'"
                ).fetchone()
                if mode == "like":
                    self._search_mode = "like"
                elif mode == "fts5" and has_fts is not None:
                    self._create_fts_objects(connection)
                    self._search_mode = "fts5"
                else:
                    self._search_mode = self._choose_search_mode(connection)
                connection.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if not _is_schema_operational_error(exc):
                    self._failure_identity = self._path_identity()
                    self._failure_observed = True
                    connection.close()
                    raise
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._reset_schema_tables(connection)
                    for statement in _BASE_TABLES:
                        connection.execute(statement)
                    for statement in _INDEXES:
                        connection.execute(statement)
                    self._search_mode = self._choose_search_mode(connection)
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    self._failure_identity = self._path_identity()
                    self._failure_observed = True
                    connection.close()
                    raise
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                self._failure_identity = self._path_identity()
                self._failure_observed = True
                connection.close()
                raise
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._connection = connection
            self._connection_identity = self._path_identity()
            self._failure_identity = None
            self._failure_observed = False
            return connection

    @property
    def _maintenance_lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.maintenance.lock")

    def _path_identity(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_dev, stat.st_ino

    def connection_matches_path(self) -> bool:
        """Whether this open connection still refers to the published index."""
        return (
            self._connection is not None
            and self._connection_identity is not None
            and self._connection_identity == self._path_identity()
        )

    def _open(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.path, timeout=30.0, check_same_thread=False, isolation_level=None
        )

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
        return frozenset(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))

    def _schema_is_compatible(self, connection: sqlite3.Connection) -> bool:
        """Migrate the known additive column gap or report that a rebuild is needed."""

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables:
            return True

        version: int | None = None
        if "meta" in tables:
            meta_columns = self._table_columns(connection, "meta")
            if not {"key", "value"} <= meta_columns:
                return False
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None:
                try:
                    version = int(row[0])
                except (TypeError, ValueError):
                    return False
                if version not in {INDEX_SCHEMA_VERSION - 1, INDEX_SCHEMA_VERSION}:
                    return False

        if "position" in tables and not _POSITION_REQUIRED_COLUMNS <= self._table_columns(
            connection, "position"
        ):
            return False

        if "sessions" in tables:
            columns = self._table_columns(connection, "sessions")
            missing = _SESSION_REQUIRED_COLUMNS - columns
            if missing:
                if missing != {"idempotency_key"}:
                    return False
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''"
                )
                self._backfill_idempotency_keys(connection)

        return True

    @staticmethod
    def _backfill_idempotency_keys(connection: sqlite3.Connection) -> None:
        """Backfill the new index column from its serialized authoritative row."""

        # Bound temporary memory for registries with many persisted sessions.
        cursor = connection.execute("SELECT session_id, record FROM sessions")
        while rows := cursor.fetchmany(512):
            updates: list[tuple[str, str]] = []
            for session_id, record in rows:
                key = ""
                try:
                    value = json.loads(record)
                except (TypeError, json.JSONDecodeError):
                    value = None
                if isinstance(value, dict):
                    candidate = value.get("idempotency_key", "")
                    if isinstance(candidate, str):
                        key = candidate[:256]
                updates.append((key, str(session_id)))
            connection.executemany(
                "UPDATE sessions SET idempotency_key = ? WHERE session_id = ?", updates
            )

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _reset_schema_tables(self, connection: sqlite3.Connection) -> None:
        """Drop only this derived database's schema, retaining the SQLite file/WAL."""

        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND type IN ('trigger', 'view', 'table')"
        ).fetchall()
        for object_type, name, _sql in objects:
            if object_type == "trigger":
                connection.execute(f"DROP TRIGGER IF EXISTS {self._quote_identifier(str(name))}")
        for object_type, name, _sql in objects:
            if object_type == "view":
                connection.execute(f"DROP VIEW IF EXISTS {self._quote_identifier(str(name))}")
        # Drop virtual tables first; SQLite removes their shadow tables too.
        virtual_tables = [
            str(name)
            for object_type, name, sql in objects
            if object_type == "table"
            and isinstance(sql, str)
            and "CREATE VIRTUAL TABLE" in sql.upper()
        ]
        for name in virtual_tables:
            connection.execute(f"DROP TABLE IF EXISTS {self._quote_identifier(name)}")
        for object_type, name, _sql in objects:
            if object_type == "table":
                connection.execute(f"DROP TABLE IF EXISTS {self._quote_identifier(str(name))}")

    def _delete_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    @staticmethod
    def _stored_search_mode(connection: sqlite3.Connection) -> SearchMode | None:
        row = connection.execute("SELECT value FROM meta WHERE key = 'search_mode'").fetchone()
        if row is None:
            return None
        value = row[0]
        return "fts5" if value == "fts5" else "like" if value == "like" else None

    def _choose_search_mode(self, connection: sqlite3.Connection) -> SearchMode:
        if self._requested_search_mode == "like":
            return "like"
        try:
            self._create_fts_objects(connection)
        except sqlite3.OperationalError as exc:
            message = str(exc).casefold()
            if any(
                marker in message
                for marker in (
                    "no such module: fts5",
                    "no such tokenizer: trigram",
                    "error in tokenizer constructor",
                )
            ):
                return "like"
            raise
        return "fts5"

    @staticmethod
    def _create_fts_objects(connection: sqlite3.Connection) -> None:
        connection.execute(_FTS_TABLE)
        for statement in _FTS_TRIGGERS.split("END;"):
            if statement.strip():
                connection.execute(statement + "END;")

    @property
    def search_mode(self) -> SearchMode:
        """Whether searches use the FTS5 trigram index or a ``LIKE`` scan."""
        self._connect()
        return self._search_mode

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        self._connection_identity = None
        if connection is not None:
            connection.close()

    def reset_schema(self) -> None:
        """Recreate the derived index schema in-place, preserving its database file."""

        connection = self._connection or self._open()
        owns_connection = self._connection is None
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._reset_schema_tables(connection)
            for statement in _BASE_TABLES:
                connection.execute(statement)
            for statement in _INDEXES:
                connection.execute(statement)
            self._search_mode = self._choose_search_mode(connection)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if owns_connection:
                connection.close()
            raise
        if owns_connection:
            self._connection = connection
            self._connection_identity = self._path_identity()
            self._failure_identity = None
            self._failure_observed = False
        self._decoded.clear()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _session(self, session_id: str, record: str) -> BackgroundSession:
        cached = self._decoded.get(session_id)
        if cached is not None and cached[0] == record:
            return cached[1]
        session = _decode(record)
        self._remember_decoded(session_id, record, session)
        return session

    def _remember_decoded(self, session_id: str, record: str, session: BackgroundSession) -> None:
        decoded = self._decoded
        decoded.pop(session_id, None)
        decoded[session_id] = (record, session)
        if len(decoded) > _CACHE_LIMIT:
            del decoded[next(iter(decoded))]

    def discard(self) -> None:
        """Discard an unusable derived file so the next access rebuilds it.

        This is reserved for SQLite-level database corruption. Compatible but
        structurally stale schemas use :meth:`reset_schema`, which preserves
        the database file and its WAL coordination.
        """
        self._decoded.clear()
        with InterProcessLock(self._maintenance_lock_path):
            path_identity = self._path_identity()
            observed_identity = (
                self._connection_identity
                if self._connection is not None
                else self._failure_identity
            )
            observed = self._connection is not None or self._failure_observed
            if observed and observed_identity != path_identity:
                # Another process already replaced this corrupt projection.
                # Close our stale handle, but never delete the new index.
                self.close()
                self._failure_identity = None
                self._failure_observed = False
                return
            self.close()
            self._failure_identity = None
            self._failure_observed = False
            self._delete_files()

    # ── state ────────────────────────────────────────────────────────────────

    def state(self) -> IndexState | None:
        """Return the applied log position, or ``None`` for an unusable index."""
        row = (
            self._connect()
            .execute(
                "SELECT (SELECT value FROM meta WHERE key = 'schema_version'), "
                "events_inode, applied_offset, applied_mtime_ns, applied_sequence "
                "FROM position WHERE id = 1"
            )
            .fetchone()
        )
        if row is None or row[0] != str(INDEX_SCHEMA_VERSION):
            return None
        return IndexState(inode=row[1], offset=row[2], mtime_ns=row[3], sequence=row[4])

    def _write_state(self, connection: sqlite3.Connection, state: IndexState) -> None:
        connection.execute(
            "INSERT INTO position (id, events_inode, applied_offset, applied_mtime_ns, "
            "applied_sequence) VALUES (1, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "events_inode = excluded.events_inode, applied_offset = excluded.applied_offset, "
            "applied_mtime_ns = excluded.applied_mtime_ns, "
            "applied_sequence = excluded.applied_sequence",
            (state.inode, state.offset, state.mtime_ns, state.sequence),
        )

    def _write_meta(self, connection: sqlite3.Connection) -> None:
        connection.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (("schema_version", str(INDEX_SCHEMA_VERSION)), ("search_mode", self._search_mode)),
        )

    # ── writes ───────────────────────────────────────────────────────────────

    def begin(self) -> sqlite3.Connection:
        """Start an exclusive write transaction and return its connection."""
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def commit(self, connection: sqlite3.Connection) -> None:
        connection.execute("COMMIT")

    def rollback(self, connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

    def replace_all(
        self,
        connection: sqlite3.Connection,
        sessions: Iterable[BackgroundSession],
        state: IndexState,
    ) -> None:
        """Replace every row inside the caller's transaction."""
        fts = self._search_mode == "fts5"
        if fts:
            # Index all rows in one pass instead of once per inserted row.
            for name in ("insert", "delete", "update"):
                connection.execute(f"DROP TRIGGER IF EXISTS sessions_fts_{name}")
        connection.execute("DELETE FROM sessions")
        self._decoded.clear()
        self._write_meta(connection)
        self.upsert(connection, sessions, state)
        if fts:
            connection.execute("INSERT INTO sessions_fts (sessions_fts) VALUES ('rebuild')")
            for statement in _FTS_TRIGGERS.split("END;"):
                if statement.strip():
                    connection.execute(statement + "END;")

    def upsert(
        self,
        connection: sqlite3.Connection,
        sessions: Iterable[BackgroundSession],
        state: IndexState,
    ) -> None:
        """Write *sessions* and the new log position inside a transaction."""
        written = [(session, _row(session)) for session in sessions]
        if written:
            connection.executemany(_UPSERT, [row for _session, row in written])
            for session, row in written:
                record = row[-1]
                assert isinstance(record, str)
                self._remember_decoded(session.session_id, record, session)
        self._write_state(connection, state)

    # ── reads ────────────────────────────────────────────────────────────────

    def get(self, session_id: str) -> BackgroundSession | None:
        row = (
            self._connect()
            .execute("SELECT record FROM sessions WHERE session_id = ?", (session_id,))
            .fetchone()
        )
        return self._session(session_id, row[0]) if row is not None else None

    def _where(self, query_filter: SessionFilter) -> tuple[str, list[object]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if not query_filter.include_deleted:
            clauses.append("status <> ?")
            parameters.append(SessionStatus.DELETED.value)
        if not query_filter.include_archived:
            clauses.append("status <> ?")
            parameters.append(SessionStatus.ARCHIVED.value)
        if query_filter.cwd is not None:
            clauses.append("cwd = ?")
            parameters.append(query_filter.cwd)
        if query_filter.workflow_name is not None:
            clauses.append("workflow_name = ?")
            parameters.append(query_filter.workflow_name)
        if query_filter.status is not None:
            clauses.append("status = ?")
            parameters.append(query_filter.status.value)
        if query_filter.statuses is not None:
            values = sorted(status.value for status in query_filter.statuses)
            clauses.append(f"status IN ({', '.join('?' for _ in values)})")
            parameters.extend(values)
        if query_filter.idempotency_key is not None:
            clauses.append("idempotency_key = ?")
            parameters.append(query_filter.idempotency_key)
        if query_filter.project is not None:
            # The full directory, or its last component under either separator.
            name = query_filter.project
            clauses.append("(cwd = ? OR cwd LIKE ? ESCAPE '\\' OR cwd LIKE ? ESCAPE '\\')")
            parameters.extend((name, _suffix_pattern("/" + name), _suffix_pattern("\\" + name)))
        if query_filter.agent is not None:
            clauses.append("(workflow_name = ? OR role = ?)")
            parameters.extend((query_filter.agent, query_filter.agent))
        if query_filter.run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(query_filter.run_id)
        for label in sorted(query_filter.labels):
            clauses.append("EXISTS (SELECT 1 FROM json_each(record, '$.labels') WHERE value = ?)")
            parameters.append(label)
        if query_filter.active_since is not None:
            clauses.append("last_active >= ?")
            parameters.append(query_filter.active_since)
        if query_filter.pinned is not None:
            clauses.append("pinned = ?")
            parameters.append(1 if query_filter.pinned else 0)
        for word in query_filter.query.lower().split():
            if self._search_mode == "fts5":
                clauses.append("rowid IN (SELECT rowid FROM sessions_fts WHERE search_text GLOB ?)")
                parameters.append(_glob_pattern(word))
            else:
                clauses.append("search_text LIKE ? ESCAPE '\\'")
                parameters.append(_like_pattern(word))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, parameters

    def sessions(
        self,
        query_filter: SessionFilter,
        *,
        order: SessionOrder = SessionOrder.REGISTRY,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[BackgroundSession]:
        """Return matching sessions in *order* (by default pinned, then newest)."""
        connection = self._connect()
        where, parameters = self._where(query_filter)
        sql = f"SELECT session_id, record FROM sessions {where} {_ORDERS[order]}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            parameters = [*parameters, limit, offset]
        return [self._session(row[0], row[1]) for row in connection.execute(sql, parameters)]

    def page(
        self,
        query_filter: SessionFilter,
        *,
        limit: int,
        offset: int,
        order: SessionOrder = SessionOrder.REGISTRY,
    ) -> tuple[list[BackgroundSession], int]:
        """Return one window of rows and the matching total from a single snapshot."""
        connection = self._connect()
        where, parameters = self._where(query_filter)
        connection.execute("BEGIN")
        try:
            rows = connection.execute(
                f"SELECT session_id, record FROM sessions {where} {_ORDERS[order]} "
                "LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
            total_row = connection.execute(
                f"SELECT count(*) FROM sessions {where}", parameters
            ).fetchone()
        finally:
            connection.execute("COMMIT")
        total = int(total_row[0]) if total_row is not None else 0
        return [self._session(row[0], row[1]) for row in rows], total

    def known(self, session_ids: Iterable[str]) -> frozenset[str]:
        """The subset of *session_ids* that have a row (deleted ones included)."""
        connection = self._connect()
        found: set[str] = set()
        ids = list(dict.fromkeys(session_ids))
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            found.update(
                row[0]
                for row in connection.execute(
                    f"SELECT session_id FROM sessions WHERE session_id IN ({placeholders})", chunk
                )
            )
        return frozenset(found)

    def count(self, query_filter: SessionFilter) -> int:
        connection = self._connect()
        where, parameters = self._where(query_filter)
        row = connection.execute(f"SELECT count(*) FROM sessions {where}", parameters).fetchone()
        return int(row[0]) if row is not None else 0

    def status_counts(self, query_filter: SessionFilter) -> dict[SessionStatus, int]:
        """Return matching sessions per status (for tabs and badges)."""
        connection = self._connect()
        where, parameters = self._where(query_filter)
        counts: dict[SessionStatus, int] = {}
        for status, count in connection.execute(
            f"SELECT status, count(*) FROM sessions {where} GROUP BY status", parameters
        ):
            try:
                counts[SessionStatus(status)] = int(count)
            except ValueError:
                continue
        return counts

    def total(self) -> int:
        row = self._connect().execute("SELECT count(*) FROM sessions").fetchone()
        return int(row[0]) if row is not None else 0
