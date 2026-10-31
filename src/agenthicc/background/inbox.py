"""Durable, versioned operator inbox for background approval and questions.

The inbox stores the exact operation identity and a private answer receipt.
Opaque messages sent from the agents hub remain in ``input_inbox.py`` and
are deliberately not interpreted here.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Literal, Mapping

from .model import BackgroundSession, SessionStatus
from .store import BackgroundStore, InvalidSessionTransition

InboxKind = Literal["approval", "question", "input"]
InboxStatus = Literal[
    "publishing",
    "pending",
    "accepted",
    "delivered",
    "rejected",
    "expired",
    "stale",
    "failed",
]
_INBOX_SCHEMA_VERSION = 1
_MAX_TARGET_BYTES = 64_000
_MAX_RESPONSE_BYTES = 64_000


@dataclass(frozen=True)
class BackgroundInboxItem:
    """Safe client projection of one pending or resolved operation."""

    item_id: str
    session_id: str
    attempt: int
    request_id: str
    kind: InboxKind
    created_at: float
    expires_at: float | None
    target: Mapping[str, object]
    status: InboxStatus
    version: int
    required_scope: str
    project_root: str
    workspace: str
    resolution: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialize metadata without ever including an accepted answer."""

        return {
            "item_id": self.item_id,
            "session_id": self.session_id,
            "attempt": self.attempt,
            "request_id": self.request_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "target": dict(self.target),
            "status": self.status,
            "version": self.version,
            "required_scope": self.required_scope,
            "project_root": self.project_root,
            "workspace": self.workspace,
            "resolution": self.resolution,
        }


@dataclass(frozen=True)
class InboxAnswer:
    """Private worker-side answer; never included in the public item view."""

    item: BackgroundInboxItem
    response: Mapping[str, object]


class InboxConflict(InvalidSessionTransition):
    """A versioned inbox action lost a race or targets a stale operation."""

    def __init__(self, message: str, current: BackgroundInboxItem | None = None) -> None:
        super().__init__(message)
        self.current = current


def stable_item_id(session_id: str, attempt: int, request_id: str) -> str:
    """Derive a stable opaque item ID for one exact session operation."""

    identity = f"{session_id}\0{attempt}\0{request_id}".encode("utf-8")
    return "inbox_" + hashlib.sha256(identity).hexdigest()[:32]


def workspace_name(project_root: str) -> str:
    """Return only the workspace directory name for list views."""

    return Path(project_root.rstrip("/\\")).name or "workspace"


def response_from_text(item: BackgroundInboxItem, text: str) -> dict[str, object]:
    """Map an explicitly selected question/input answer to its result shape.

    A single ask_user question accepts free text verbatim. Multiple questions
    require a JSON object keyed by the exact question IDs, matching the
    existing ``make_questions_tool`` result contract.
    """

    if not isinstance(text, str) or not text.strip():
        raise ValueError("answer must not be empty")
    if item.kind == "input":
        return {"answer": text}
    if item.kind != "question":
        raise ValueError("text answers are only valid for question or input items")
    raw_ids = item.target.get("question_ids")
    ids = (
        [value for value in raw_ids if isinstance(value, str)] if isinstance(raw_ids, list) else []
    )
    if len(ids) == 1:
        return {"answers": {ids[0]: text}}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("multiple questions require a JSON object keyed by question ID") from exc
    if not isinstance(value, dict):
        raise ValueError("question answer must be a JSON object")
    return {"answers": value}


class BackgroundInteractionInbox:
    """SQLite-backed pending interaction store with CAS and idempotency.

    The session lifecycle journal remains authoritative for current ownership.
    An item is first persisted as ``publishing``, then the session transition
    publishes its stable item ID, and finally the item becomes ``pending``.
    Readers never show the intermediate state. On restart, ``reconcile`` joins
    those two durable records and either activates the item or marks it stale.
    """

    def __init__(self, store: BackgroundStore) -> None:
        self.store = store
        self.root = store.root
        self.path = self.root / "operator-inbox.sqlite3"
        # The database is created on first use, so opening an interactive
        # session does no inbox I/O before its first frame (AGX-02).
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        if not self._initialized:
            self._initialize()
        return self._open()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def prepare(self) -> None:
        """Create the database now rather than on first use (blocking).

        The agents hub prepares its inbox on a worker thread as it opens,
        so the first approval, input or deletion does not pay for it.
        """
        self._initialize()

    def _initialize(self) -> None:
        with self._init_lock:
            if not self._initialized:
                self._create_schema()
                self._initialized = True

    def _create_schema(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        with closing(self._open()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, _INBOX_SCHEMA_VERSION}:
                raise RuntimeError(f"unsupported operator inbox schema version: {version}")
            if version == 0:
                self._write_schema(connection)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _write_schema(connection: sqlite3.Connection) -> None:
        """Create the tables of a new database; opening an existing one writes nothing."""
        connection.executescript(
            """
                CREATE TABLE IF NOT EXISTS inbox_items (
                    item_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    request_id TEXT NOT NULL,
                    owner_hash TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    target_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    required_scope TEXT NOT NULL,
                    project_root TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    response_json TEXT,
                    response_hash TEXT,
                    resolution TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    UNIQUE(session_id, attempt, request_id)
                );
                CREATE INDEX IF NOT EXISTS inbox_items_visible
                    ON inbox_items(status, created_at, item_id);
                CREATE INDEX IF NOT EXISTS inbox_items_project
                    ON inbox_items(project_root, status, created_at);
                CREATE TABLE IF NOT EXISTS inbox_idempotency (
                    item_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    response_hash TEXT NOT NULL,
                    PRIMARY KEY(item_id, idempotency_key),
                    FOREIGN KEY(item_id) REFERENCES inbox_items(item_id)
                );
                """
        )
        connection.execute(f"PRAGMA user_version={_INBOX_SCHEMA_VERSION}")

    @staticmethod
    def _parse(row: sqlite3.Row) -> BackgroundInboxItem:
        try:
            target_value = json.loads(row["target_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("inbox item target is corrupt") from exc
        if not isinstance(target_value, dict):
            raise ValueError("inbox item target is corrupt")
        expires = row["expires_at"]
        kind = row["kind"]
        status = row["status"]
        if kind not in {"approval", "question", "input"}:
            raise ValueError("inbox item kind is corrupt")
        if status not in {
            "publishing",
            "pending",
            "accepted",
            "delivered",
            "rejected",
            "expired",
            "stale",
            "failed",
        }:
            raise ValueError("inbox item status is corrupt")
        return BackgroundInboxItem(
            item_id=str(row["item_id"]),
            session_id=str(row["session_id"]),
            attempt=int(row["attempt"]),
            request_id=str(row["request_id"]),
            kind=kind,
            created_at=float(row["created_at"]),
            expires_at=float(expires) if isinstance(expires, (int, float)) else None,
            target=target_value,
            status=status,
            version=int(row["version"]),
            required_scope=str(row["required_scope"]),
            project_root=str(row["project_root"]),
            workspace=str(row["workspace"]),
            resolution=str(row["resolution"] or "")[:240],
        )

    @staticmethod
    def _owner_hash(session: BackgroundSession) -> str:
        return hashlib.sha256(session.lease_token.encode("utf-8")).hexdigest()

    def register(
        self,
        *,
        session_id: str,
        attempt: int,
        request_id: str,
        kind: InboxKind,
        target: Mapping[str, object],
        expires_at: float | None,
        now: float | None = None,
    ) -> BackgroundInboxItem:
        """Persist an invisible item before changing visible session status."""

        if not request_id or len(request_id) > 256:
            raise ValueError("request_id must be a non-empty bounded identifier")
        if not session_id or len(session_id) > 256:
            raise ValueError("session_id must be a non-empty bounded identifier")
        if kind not in {"approval", "question", "input"}:
            raise ValueError("unsupported background inbox item type")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if expires_at is not None and (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
        ):
            raise ValueError("expires_at must be a finite timestamp or None")
        target_json = json.dumps(dict(target), ensure_ascii=False, sort_keys=True)
        if len(target_json.encode("utf-8")) > _MAX_TARGET_BYTES:
            raise ValueError("inbox target exceeds the size limit")
        identifier = stable_item_id(session_id, attempt, request_id)
        current = self.store.get(session_id, include_deleted=True)
        expected_status = (
            SessionStatus.WAITING_APPROVAL if kind == "approval" else SessionStatus.WAITING_INPUT
        )
        current_item_id = current.approval_item_id if kind == "approval" else current.input_item_id
        if current.attempt != attempt or (
            current.status is not SessionStatus.RUNNING
            and not (current.status is expected_status and current_item_id == identifier)
        ):
            raise InboxConflict("session attempt is no longer running")
        created = time.time() if now is None else now
        project_root = str(Path(current.cwd).expanduser().resolve())
        owner_hash = self._owner_hash(current)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inbox_items WHERE item_id=?", (identifier,)
            ).fetchone()
            if row is not None:
                item = self._parse(row)
                if (
                    item.session_id != session_id
                    or item.attempt != attempt
                    or item.request_id != request_id
                    or item.kind != kind
                    or dict(item.target) != dict(target)
                ):
                    connection.rollback()
                    raise InboxConflict("stable inbox item ID was reused for different content")
                connection.commit()
                return item
            connection.execute(
                """INSERT INTO inbox_items(
                    item_id, session_id, attempt, request_id, owner_hash, kind,
                    created_at, expires_at, target_json, status, version,
                    required_scope, project_root, workspace, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'publishing', 1,
                          'control', ?, ?, ?)""",
                (
                    identifier,
                    session_id,
                    attempt,
                    request_id,
                    owner_hash,
                    kind,
                    created,
                    expires_at,
                    target_json,
                    project_root,
                    workspace_name(project_root),
                    created,
                ),
            )
            row = connection.execute(
                "SELECT * FROM inbox_items WHERE item_id=?", (identifier,)
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("operator inbox failed to persist a new item")
        return self._parse(row)

    def activate(self, item_id: str) -> BackgroundInboxItem:
        """Make a registered item visible after its session transition commits."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(item_id)
            if row["status"] == "publishing":
                connection.execute(
                    "UPDATE inbox_items SET status='pending', version=version+1, updated_at=? "
                    "WHERE item_id=? AND status='publishing'",
                    (time.time(), item_id),
                )
                row = connection.execute(
                    "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                ).fetchone()
            connection.commit()
        if row is None:
            raise KeyError(item_id)
        return self._parse(row)

    def get(self, item_id: str) -> BackgroundInboxItem:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return self._parse(row)

    def pending_summary(
        self,
    ) -> tuple[int, frozenset[str], frozenset[str], BackgroundInboxItem | None]:
        """Return the exact pending count, IDs, owning sessions and oldest request."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            count_row = connection.execute(
                "SELECT COUNT(*) FROM inbox_items WHERE status='pending'"
            ).fetchone()
            session_rows = connection.execute(
                "SELECT DISTINCT session_id FROM inbox_items WHERE status='pending'"
            ).fetchall()
            item_rows = connection.execute(
                "SELECT item_id FROM inbox_items WHERE status='pending'"
            ).fetchall()
            oldest_row = connection.execute(
                "SELECT * FROM inbox_items WHERE status='pending' "
                "ORDER BY created_at ASC, item_id ASC LIMIT 1"
            ).fetchone()
            connection.commit()
        count = int(count_row[0]) if count_row is not None else 0
        session_ids = frozenset(str(row[0]) for row in session_rows)
        item_ids = frozenset(str(row[0]) for row in item_rows)
        oldest = self._parse(oldest_row) if oldest_row is not None else None
        return count, session_ids, item_ids, oldest

    def list_items(
        self,
        *,
        project_root: str | Path | None = None,
        item_type: str | None = None,
        older_than_s: float | None = None,
        status: str | None = None,
        limit: int = 100,
        now: float | None = None,
    ) -> tuple[BackgroundInboxItem, ...]:
        """List oldest pending work first, with bounded exact filters."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if item_type is not None and item_type not in {"approval", "question", "input"}:
            raise ValueError("item_type must be approval, question, or input")
        if status is not None and status not in {
            "publishing",
            "pending",
            "accepted",
            "delivered",
            "rejected",
            "expired",
            "stale",
            "failed",
        }:
            raise ValueError("unsupported inbox status")
        conditions: list[str] = []
        params: list[object] = []
        if project_root is not None:
            normalized = str(Path(project_root).expanduser().resolve())
            conditions.append("project_root=?")
            params.append(normalized)
        if item_type is not None:
            conditions.append("kind=?")
            params.append(item_type)
        if older_than_s is not None:
            if (
                isinstance(older_than_s, bool)
                or not isinstance(older_than_s, (int, float))
                or not math.isfinite(float(older_than_s))
                or older_than_s < 0
            ):
                raise ValueError("older_than_s must be a non-negative number")
            cutoff = (time.time() if now is None else now) - float(older_than_s)
            conditions.append("created_at<=?")
            params.append(cutoff)
        if status is None:
            conditions.append("status IN ('pending','accepted')")
        else:
            conditions.append("status=?")
            params.append(status)
        where = " AND ".join(conditions)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM inbox_items WHERE {where} "
                "ORDER BY created_at ASC, item_id ASC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(self._parse(row) for row in rows)

    def respond(
        self,
        item_id: str,
        response: Mapping[str, object],
        *,
        expected_version: int,
        idempotency_key: str,
        now: float | None = None,
    ) -> tuple[BackgroundInboxItem, bool]:
        """Accept exactly one response against the exact live operation.

        The background registry lock and SQLite immediate transaction form a
        compare-and-set boundary: a response cannot win after the session has
        advanced, been deleted, or changed worker lease.
        """

        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 256
        ):
            raise ValueError("idempotency_key must be a non-empty bounded string")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError("expected_version must be a positive integer")
        response_json = json.dumps(dict(response), ensure_ascii=False, sort_keys=True)
        response_bytes = response_json.encode("utf-8")
        if len(response_bytes) > _MAX_RESPONSE_BYTES:
            raise ValueError("inbox response exceeds the size limit")
        response_hash = hashlib.sha256(response_bytes).hexdigest()
        current_time = time.time() if now is None else now

        with self.store._lock():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise KeyError(item_id)
                item = self._parse(row)
                prior = connection.execute(
                    "SELECT response_hash FROM inbox_idempotency "
                    "WHERE item_id=? AND idempotency_key=?",
                    (item_id, idempotency_key),
                ).fetchone()
                if prior is not None:
                    if prior["response_hash"] != response_hash:
                        connection.rollback()
                        raise InboxConflict(
                            "idempotency key was already used for a different response", item
                        )
                    connection.commit()
                    return item, True
                if item.status != "pending" or item.version != expected_version:
                    connection.rollback()
                    raise InboxConflict(
                        f"inbox item is {item.status} at version {item.version}", item
                    )
                if item.expires_at is not None and current_time >= item.expires_at:
                    connection.execute(
                        "UPDATE inbox_items SET status='expired', version=version+1, "
                        "resolution='expired before response', updated_at=? WHERE item_id=?",
                        (current_time, item_id),
                    )
                    row = connection.execute(
                        "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                    ).fetchone()
                    connection.commit()
                    raise InboxConflict(
                        "inbox item expired before the response was accepted",
                        self._parse(row) if row is not None else None,
                    )
                self._validate_response(item, response)
                try:
                    session = self.store.get(item.session_id, include_deleted=True)
                    self._assert_current(session, item, row["owner_hash"])
                except (KeyError, InboxConflict) as exc:
                    reason = str(exc) or "owning session no longer exists"
                    connection.execute(
                        "UPDATE inbox_items SET status='stale', version=version+1, "
                        "resolution=?, updated_at=? WHERE item_id=? AND status='pending'",
                        (reason[:240], current_time, item_id),
                    )
                    stale = connection.execute(
                        "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                    ).fetchone()
                    connection.commit()
                    raise InboxConflict(
                        reason,
                        self._parse(stale) if stale is not None else item,
                    ) from exc
                connection.execute(
                    "INSERT INTO inbox_idempotency(item_id,idempotency_key,response_hash) "
                    "VALUES (?,?,?)",
                    (item_id, idempotency_key, response_hash),
                )
                connection.execute(
                    "UPDATE inbox_items SET status='accepted', version=version+1, "
                    "response_json=?, response_hash=?, updated_at=? "
                    "WHERE item_id=? AND status='pending' AND version=?",
                    (response_json, response_hash, current_time, item_id, expected_version),
                )
                updated = connection.execute(
                    "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                ).fetchone()
                if updated is None or updated["status"] != "accepted":
                    connection.rollback()
                    raise InboxConflict("another client answered this item", item)
                connection.commit()
            return self._parse(updated), False

    @staticmethod
    def _validate_response(item: BackgroundInboxItem, response: Mapping[str, object]) -> None:
        if item.kind == "approval":
            if set(response) - {
                "allowed",
                "remember",
                "remember_all",
                "scope_grant",
                "message",
                "mode",
            }:
                raise ValueError("approval response contains unsupported fields")
            if not isinstance(response.get("allowed"), bool):
                raise ValueError("approval response requires boolean allowed")
            if any(
                key in response and not isinstance(response[key], bool)
                for key in ("remember", "remember_all")
            ):
                raise ValueError("approval remember flags must be booleans")
            scope = response.get("scope_grant")
            if scope is not None and scope not in {"target_once", "target_turn", "target_session"}:
                raise ValueError("approval scope_grant is invalid")
            if not response["allowed"] and (
                response.get("remember") or response.get("remember_all")
            ):
                raise ValueError("a rejected approval cannot be remembered")
            message = response.get("message", "")
            if not isinstance(message, str) or len(message) > 8_000:
                raise ValueError("approval feedback must be a string up to 8000 characters")
            mode = response.get("mode")
            options = item.target.get("mode_options")
            if mode is not None and (
                not isinstance(mode, str)
                or (isinstance(options, list) and options and mode not in options)
            ):
                raise ValueError("approval mode is not one of the advertised choices")
            if mode is not None and not response["allowed"]:
                raise ValueError("a rejected approval cannot select an execution mode")
            return
        if item.kind == "input":
            answer = response.get("answer")
            if set(response) != {"answer"} or not isinstance(answer, str):
                raise ValueError("input response must contain exactly one string answer")
            return
        answers = response.get("answers")
        question_ids = item.target.get("question_ids")
        if set(response) != {"answers"} or not isinstance(answers, Mapping):
            raise ValueError("question response must contain an answers object")
        if not isinstance(question_ids, list) or not all(
            isinstance(question_id, str) for question_id in question_ids
        ):
            raise ValueError("question item has invalid question IDs")
        if set(answers) != set(question_ids) or not all(
            isinstance(question_id, str) and isinstance(value, str)
            for question_id, value in answers.items()
        ):
            raise ValueError("answers must contain one string for every requested question ID")

    @staticmethod
    def _assert_current(
        session: BackgroundSession, item: BackgroundInboxItem, owner_hash: str
    ) -> None:
        expected_status = (
            SessionStatus.WAITING_APPROVAL
            if item.kind == "approval"
            else SessionStatus.WAITING_INPUT
        )
        current_item_id = (
            session.approval_item_id if item.kind == "approval" else session.input_item_id
        )
        if session.status is SessionStatus.DELETED:
            raise InboxConflict("deleted sessions cannot receive inbox answers", item)
        if (
            session.status is not expected_status
            or session.attempt != item.attempt
            or current_item_id != item.item_id
            or BackgroundInteractionInbox._owner_hash(session) != owner_hash
        ):
            raise InboxConflict("inbox item is stale; its operation is no longer waiting", item)

    def accepted_answer(self, item_id: str) -> InboxAnswer | None:
        """Load an accepted response for the worker that owns this exact item."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
            ).fetchone()
        if row is None or row["status"] != "accepted":
            return None
        item = self._parse(row)
        try:
            current = self.store.get(item.session_id, include_deleted=True)
            self._assert_current(current, item, str(row["owner_hash"]))
        except (KeyError, InboxConflict) as exc:
            self.set_terminal(item_id, "stale", str(exc) or "owning session is stale")
            return None
        response = row["response_json"]
        if not isinstance(response, str):
            return None
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, dict):
            return None
        return InboxAnswer(item, decoded)

    def mark_delivered(self, item_id: str, *, now: float | None = None) -> BackgroundInboxItem:
        """Acknowledge answer consumption after the waiting operation resumes."""

        current_time = time.time() if now is None else now
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(item_id)
            if row["status"] == "accepted":
                response: object = None
                if isinstance(row["response_json"], str):
                    try:
                        response = json.loads(row["response_json"])
                    except json.JSONDecodeError:
                        response = None
                status = (
                    "rejected"
                    if isinstance(response, dict) and response.get("allowed") is False
                    else "delivered"
                )
                connection.execute(
                    "UPDATE inbox_items SET status=?, version=version+1, updated_at=? "
                    "WHERE item_id=? AND status='accepted'",
                    (status, current_time, item_id),
                )
                row = connection.execute(
                    "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                ).fetchone()
            connection.commit()
        if row is None:
            raise KeyError(item_id)
        return self._parse(row)

    def expire(
        self,
        item_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        reason: str = "expired by operator",
        now: float | None = None,
    ) -> BackgroundInboxItem:
        """Expire a pending operation without injecting a response elsewhere."""

        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 256
        ):
            raise ValueError("idempotency_key must be a non-empty bounded string")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError("expected_version must be a positive integer")
        if not isinstance(reason, str):
            raise ValueError("reason must be a string")
        current_time = time.time() if now is None else now
        expiry_hash = hashlib.sha256(
            json.dumps({"expire": True, "reason": reason[:240]}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self.store._lock():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise KeyError(item_id)
                item = self._parse(row)
                prior = connection.execute(
                    "SELECT response_hash FROM inbox_idempotency "
                    "WHERE item_id=? AND idempotency_key=?",
                    (item_id, idempotency_key),
                ).fetchone()
                if prior is not None:
                    if prior["response_hash"] != expiry_hash:
                        connection.rollback()
                        raise InboxConflict(
                            "idempotency key was already used for a different operation", item
                        )
                    connection.commit()
                    return item
                if item.status != "pending" or item.version != expected_version:
                    connection.rollback()
                    raise InboxConflict(
                        f"inbox item is {item.status} at version {item.version}", item
                    )
                try:
                    session = self.store.get(item.session_id, include_deleted=True)
                    self._assert_current(session, item, str(row["owner_hash"]))
                except (KeyError, InboxConflict) as exc:
                    stale_reason = str(exc) or "owning session no longer exists"
                    connection.execute(
                        "UPDATE inbox_items SET status='stale', version=version+1, "
                        "resolution=?, updated_at=? WHERE item_id=? AND status='pending'",
                        (stale_reason[:240], current_time, item_id),
                    )
                    stale = connection.execute(
                        "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                    ).fetchone()
                    connection.commit()
                    raise InboxConflict(
                        stale_reason,
                        self._parse(stale) if stale is not None else item,
                    ) from exc
                connection.execute(
                    "INSERT INTO inbox_idempotency(item_id,idempotency_key,response_hash) "
                    "VALUES (?,?,?)",
                    (item_id, idempotency_key, expiry_hash),
                )
                connection.execute(
                    "UPDATE inbox_items SET status='expired', version=version+1, "
                    "resolution=?, updated_at=? WHERE item_id=? AND status='pending' AND version=?",
                    (reason[:240], current_time, item_id, expected_version),
                )
                updated = connection.execute(
                    "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                ).fetchone()
                connection.commit()
        if updated is None:
            raise KeyError(item_id)
        return self._parse(updated)

    def set_terminal(
        self,
        item_id: str,
        status: Literal["expired", "stale", "failed"],
        reason: str,
        *,
        now: float | None = None,
    ) -> BackgroundInboxItem:
        """Record worker timeout, stale operation, or failed publication."""

        current_time = time.time() if now is None else now
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(item_id)
            allowed_statuses = {"pending", "publishing"}
            if status == "stale":
                allowed_statuses.add("accepted")
            if row["status"] in allowed_statuses:
                placeholders = ",".join("?" for _ in allowed_statuses)
                connection.execute(
                    "UPDATE inbox_items SET status=?, version=version+1, resolution=?, updated_at=? "
                    f"WHERE item_id=? AND status IN ({placeholders})",
                    (status, reason[:240], current_time, item_id, *sorted(allowed_statuses)),
                )
                row = connection.execute(
                    "SELECT * FROM inbox_items WHERE item_id=?", (item_id,)
                ).fetchone()
            connection.commit()
        if row is None:
            raise KeyError(item_id)
        return self._parse(row)

    def reconcile(self, *, now: float | None = None) -> int:
        """Repair interrupted publication and mark answers for stale attempts."""

        current_time = time.time() if now is None else now
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM inbox_items WHERE status IN ('publishing','pending','accepted') "
                "ORDER BY created_at ASC"
            ).fetchall()
        changed = 0
        for row in rows:
            item = self._parse(row)
            try:
                session = self.store.get(item.session_id, include_deleted=True)
            except KeyError:
                self.set_terminal(
                    item.item_id, "stale", "owning session no longer exists", now=current_time
                )
                changed += 1
                continue
            try:
                self._assert_current(session, item, str(row["owner_hash"]))
            except InboxConflict as exc:
                self.set_terminal(item.item_id, "stale", str(exc), now=current_time)
                changed += 1
                continue
            if (
                item.expires_at is not None
                and current_time >= item.expires_at
                and item.status == "pending"
            ):
                self.set_terminal(
                    item.item_id, "expired", "request deadline elapsed", now=current_time
                )
                changed += 1
                continue
            if item.status == "publishing":
                self.activate(item.item_id)
                changed += 1
        return changed

    async def wait_for_answer(
        self,
        item_id: str,
        *,
        deadline: float | None,
        poll_interval_s: float = 0.2,
    ) -> InboxAnswer | None:
        """Wait for one accepted answer, expiry, or stale owner without blocking."""

        while True:
            # Migrate decisions written by older supervisors that still use
            # BackgroundSession.approval_decision/input_value.  Adoption is
            # fenced by the exact current item and the normal CAS transaction;
            # new clients always write directly to this inbox.
            try:
                item = self.get(item_id)
                current = self.store.get(item.session_id, include_deleted=True)
                expected_status = (
                    SessionStatus.WAITING_APPROVAL
                    if item.kind == "approval"
                    else SessionStatus.WAITING_INPUT
                )
                current_item_id = (
                    current.approval_item_id if item.kind == "approval" else current.input_item_id
                )
                if current.status in {
                    SessionStatus.CANCELLING,
                    SessionStatus.CANCELLED,
                    SessionStatus.DELETED,
                }:
                    self.set_terminal(item_id, "stale", "owning session was cancelled or deleted")
                    return None
                if (
                    current.status is not expected_status
                    or current.attempt != item.attempt
                    or current_item_id != item.item_id
                ):
                    self.set_terminal(item_id, "stale", "owning operation is no longer waiting")
                    return None
                legacy_response: dict[str, object] | None = None
                if item.kind == "approval" and current.approval_decision is not None:
                    legacy_response = {"allowed": current.approval_decision}
                elif current.input_value is not None and item.kind in {"input", "question"}:
                    try:
                        legacy_response = response_from_text(item, current.input_value)
                    except ValueError:
                        legacy_response = None
                if legacy_response is not None and item.status == "pending":
                    self.respond(
                        item_id,
                        legacy_response,
                        expected_version=item.version,
                        idempotency_key=f"legacy-{item_id}",
                    )
            except KeyError:
                return None
            except (InboxConflict, InvalidSessionTransition, ValueError):
                pass
            answer = self.accepted_answer(item_id)
            if answer is not None:
                return answer
            try:
                item = self.get(item_id)
            except KeyError:
                return None
            if item.status in {"expired", "stale", "failed", "rejected", "delivered"}:
                return None
            if deadline is not None and time.time() >= deadline:
                self.set_terminal(item_id, "expired", "request deadline elapsed")
                return None
            delay = poll_interval_s
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - time.time()))
            await asyncio.sleep(max(0.01, delay))

    def answer_response(self, answer: InboxAnswer) -> dict[str, object]:
        """Map stored operator response to the existing ApprovalResponse contract."""

        response = dict(answer.response)
        common: dict[str, object] = {
            "allowed": True,
            "outcome": "answered",
            "request_id": answer.item.request_id,
        }
        if answer.item.kind == "approval":
            allowed = response.get("allowed")
            if not isinstance(allowed, bool):
                return {"allowed": False, "outcome": "failed", "request_id": answer.item.request_id}
            return {
                "allowed": allowed,
                "remember": response.get("remember") is True,
                "remember_all": response.get("remember_all") is True,
                "scope_grant": response.get("scope_grant"),
                "message": response.get("message", ""),
                "mode": response.get("mode"),
                "outcome": "answered" if allowed else "rejected",
                "request_id": answer.item.request_id,
            }
        if answer.item.kind == "input":
            value = response.get("answer")
            if not isinstance(value, str):
                return {"allowed": False, "outcome": "failed", "request_id": answer.item.request_id}
            return {**common, "message": value}
        question_ids = answer.item.target.get("question_ids", [])
        answers = response.get("answers")
        if not isinstance(question_ids, list) or not isinstance(answers, dict):
            return {"allowed": False, "outcome": "failed", "request_id": answer.item.request_id}
        if set(answers) != set(item for item in question_ids if isinstance(item, str)):
            return {"allowed": False, "outcome": "failed", "request_id": answer.item.request_id}
        if any(not isinstance(value, str) for value in answers.values()):
            return {"allowed": False, "outcome": "failed", "request_id": answer.item.request_id}
        return {**common, "message": json.dumps(answers, ensure_ascii=False)}
