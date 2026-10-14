"""Transactional SQLite persistence for schedules, revisions, and run receipts."""

from __future__ import annotations

import json
import builtins
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agenthicc.schedules.model import (
    CoalescePolicy,
    Occurrence,
    OccurrenceState,
    RecurrenceKind,
    Schedule,
)


class ScheduleStoreError(RuntimeError):
    """Durable schedule data could not be safely read or changed."""


class ScheduleNotFound(ScheduleStoreError):
    """No schedule with the requested stable ID exists."""


class ScheduleConflict(ScheduleStoreError):
    """A revision or occurrence changed before a compare-and-swap update."""


_SCHEMA_VERSION = 1
_ACTIVE_OCCURRENCE_STATES = (
    OccurrenceState.AWAITING_SERVICE.value,
    OccurrenceState.QUEUED.value,
    OccurrenceState.PREPARING.value,
    OccurrenceState.WAITING_CAPACITY.value,
    OccurrenceState.RUNNING.value,
)


class ScheduleStore:
    """Owner-only, cross-process database for all local user schedules.

    Repository roots are stored as canonical identities in one database under
    the Agenthicc user state directory. Prompt text and run receipts are
    sensitive local data and are never put in filenames or logs.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or Path.home() / ".agenthicc" / "schedules").expanduser()
        self.path = self.root / "schedules.sqlite3"
        self.service_lock_path = self.root / "scheduler.lock"
        self.dispatch_lock_path = self.root / "dispatch.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot open schedule database: {exc}") from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                current = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current > _SCHEMA_VERSION:
                    raise ScheduleStoreError(
                        f"schedule database schema {current} is newer than supported {_SCHEMA_VERSION}"
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schedules (
                        schedule_id TEXT PRIMARY KEY,
                        repository_root TEXT NOT NULL,
                        workflow_name TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        profile_name TEXT NOT NULL,
                        permission_ceiling TEXT NOT NULL,
                        recurrence_kind TEXT NOT NULL,
                        recurrence_spec TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        coalesce_policy TEXT NOT NULL,
                        max_in_flight INTEGER NOT NULL,
                        next_run_at REAL,
                        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                        creator TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision > 0),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        config_path TEXT NOT NULL DEFAULT '',
                        deleted_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS schedules_due_idx
                        ON schedules(enabled, deleted_at, next_run_at);
                    CREATE INDEX IF NOT EXISTS schedules_repository_idx
                        ON schedules(repository_root, deleted_at, updated_at);
                    CREATE TABLE IF NOT EXISTS schedule_revisions (
                        schedule_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        definition_json TEXT NOT NULL,
                        PRIMARY KEY(schedule_id, revision),
                        FOREIGN KEY(schedule_id) REFERENCES schedules(schedule_id)
                    );
                    CREATE TABLE IF NOT EXISTS schedule_occurrences (
                        occurrence_id TEXT PRIMARY KEY,
                        schedule_id TEXT NOT NULL,
                        schedule_revision INTEGER NOT NULL,
                        occurrence_key TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL,
                        scheduled_for REAL NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        state TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        worktree_json TEXT,
                        base_commit TEXT NOT NULL DEFAULT '',
                        retry_of TEXT NOT NULL DEFAULT '',
                        coalesced_count INTEGER NOT NULL DEFAULT 0,
                        attempt INTEGER NOT NULL DEFAULT 1,
                        receipt_json TEXT,
                        outcome_json TEXT,
                        error TEXT NOT NULL DEFAULT '',
                        FOREIGN KEY(schedule_id) REFERENCES schedules(schedule_id)
                    );
                    CREATE INDEX IF NOT EXISTS occurrences_schedule_idx
                        ON schedule_occurrences(schedule_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS occurrences_dispatch_idx
                        ON schedule_occurrences(state, scheduled_for);
                    CREATE TABLE IF NOT EXISTS schedule_audit (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        schedule_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        details_json TEXT NOT NULL
                    );
                    """
                )
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            except sqlite3.Error as exc:
                raise ScheduleStoreError(f"cannot initialize schedule database: {exc}") from exc
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _schedule_from_row(row: sqlite3.Row) -> Schedule:
        try:
            return Schedule(
                schedule_id=str(row["schedule_id"]),
                repository_root=str(row["repository_root"]),
                workflow_name=str(row["workflow_name"]),
                prompt=str(row["prompt"]),
                profile_name=str(row["profile_name"]),
                permission_ceiling=str(row["permission_ceiling"]),
                recurrence_kind=RecurrenceKind(str(row["recurrence_kind"])),
                recurrence_spec=str(row["recurrence_spec"]),
                timezone=str(row["timezone"]),
                coalesce_policy=CoalescePolicy(str(row["coalesce_policy"])),
                max_in_flight=int(row["max_in_flight"]),
                next_run_at=(float(row["next_run_at"]) if row["next_run_at"] is not None else None),
                enabled=bool(row["enabled"]),
                creator=str(row["creator"]),
                revision=int(row["revision"]),
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
                config_path=str(row["config_path"]),
                deleted_at=(float(row["deleted_at"]) if row["deleted_at"] is not None else None),
            )
        except (TypeError, ValueError) as exc:
            raise ScheduleStoreError(f"invalid persisted schedule: {exc}") from exc

    @staticmethod
    def _occurrence_from_row(row: sqlite3.Row) -> Occurrence:
        def mapping(name: str) -> Mapping[str, object] | None:
            raw = row[name]
            if raw is None:
                return None
            try:
                decoded = json.loads(str(raw))
            except json.JSONDecodeError as exc:
                raise ScheduleStoreError(f"invalid occurrence {name}: {exc}") from exc
            if not isinstance(decoded, dict):
                raise ScheduleStoreError(f"occurrence {name} is not an object")
            return decoded

        try:
            return Occurrence(
                occurrence_id=str(row["occurrence_id"]),
                schedule_id=str(row["schedule_id"]),
                schedule_revision=int(row["schedule_revision"]),
                occurrence_key=str(row["occurrence_key"]),
                kind=str(row["kind"]),
                scheduled_for=float(row["scheduled_for"]),
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
                state=OccurrenceState(str(row["state"])),
                session_id=str(row["session_id"]),
                worktree=mapping("worktree_json"),
                base_commit=str(row["base_commit"]),
                retry_of=str(row["retry_of"]),
                coalesced_count=int(row["coalesced_count"]),
                attempt=int(row["attempt"]),
                receipt=mapping("receipt_json"),
                outcome=mapping("outcome_json"),
                error=str(row["error"]),
            )
        except (TypeError, ValueError) as exc:
            raise ScheduleStoreError(f"invalid persisted occurrence: {exc}") from exc

    @staticmethod
    def _definition_json(schedule: Schedule) -> str:
        return json.dumps(
            schedule.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        schedule: Schedule,
        *,
        action: str,
        actor: str,
        now: float,
    ) -> None:
        snapshot = ScheduleStore._definition_json(schedule)
        connection.execute(
            "INSERT INTO schedule_revisions VALUES (?, ?, ?, ?, ?, ?)",
            (schedule.schedule_id, schedule.revision, action, actor, now, snapshot),
        )
        connection.execute(
            "INSERT INTO schedule_audit(schedule_id, revision, action, actor, created_at, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (schedule.schedule_id, schedule.revision, action, actor, now, snapshot),
        )

    @staticmethod
    def _insert_schedule(connection: sqlite3.Connection, schedule: Schedule) -> None:
        data = schedule.to_dict()
        connection.execute(
            """INSERT INTO schedules (
                schedule_id, repository_root, workflow_name, prompt, profile_name,
                permission_ceiling, recurrence_kind, recurrence_spec, timezone,
                coalesce_policy, max_in_flight, next_run_at, enabled, creator,
                revision, created_at, updated_at, config_path, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["schedule_id"],
                data["repository_root"],
                data["workflow_name"],
                data["prompt"],
                data["profile_name"],
                data["permission_ceiling"],
                data["recurrence_kind"],
                data["recurrence_spec"],
                data["timezone"],
                data["coalesce_policy"],
                data["max_in_flight"],
                data["next_run_at"],
                int(bool(data["enabled"])),
                data["creator"],
                data["revision"],
                data["created_at"],
                data["updated_at"],
                data["config_path"],
                data["deleted_at"],
            ),
        )

    def create(self, schedule: Schedule, *, action: str = "created") -> Schedule:
        try:
            with self._transaction() as connection:
                self._insert_schedule(connection, schedule)
                self._insert_audit(
                    connection,
                    schedule,
                    action=action,
                    actor=schedule.creator,
                    now=schedule.created_at,
                )
        except sqlite3.IntegrityError as exc:
            raise ScheduleConflict(f"schedule already exists: {schedule.schedule_id}") from exc
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot create schedule: {exc}") from exc
        return schedule

    def get(self, schedule_id: str, *, include_deleted: bool = False) -> Schedule:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM schedules WHERE schedule_id = ?"
                    + ("" if include_deleted else " AND deleted_at IS NULL"),
                    (schedule_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot read schedule: {exc}") from exc
        if row is None:
            raise ScheduleNotFound(schedule_id)
        return self._schedule_from_row(row)

    def list(
        self, *, repository_root: str | None = None, include_deleted: bool = False
    ) -> list[Schedule]:
        clauses: list[str] = []
        parameters: list[object] = []
        if repository_root is not None:
            clauses.append("repository_root = ?")
            parameters.append(repository_root)
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM schedules{where} ORDER BY updated_at DESC, schedule_id",
                    parameters,
                ).fetchall()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot list schedules: {exc}") from exc
        return [self._schedule_from_row(row) for row in rows]

    def history(
        self, schedule_id: str, *, limit: int = 50, before: float | None = None
    ) -> builtins.list[Occurrence]:
        if not 1 <= limit <= 500:
            raise ValueError("history limit must be between 1 and 500")
        clauses = ["schedule_id = ?"]
        params: list[object] = [schedule_id]
        if before is not None:
            clauses.append("created_at < ?")
            params.append(before)
        params.append(limit)
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM schedule_occurrences WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY created_at DESC, occurrence_id DESC LIMIT ?",
                    params,
                ).fetchall()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot read schedule history: {exc}") from exc
        return [self._occurrence_from_row(row) for row in rows]

    def revisions(self, schedule_id: str) -> builtins.list[dict[str, object]]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT revision, action, actor, created_at, definition_json "
                    "FROM schedule_revisions WHERE schedule_id = ? ORDER BY revision",
                    (schedule_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot read schedule revisions: {exc}") from exc
        results: list[dict[str, object]] = []
        for row in rows:
            try:
                definition = json.loads(str(row["definition_json"]))
            except json.JSONDecodeError as exc:
                raise ScheduleStoreError(f"invalid schedule revision data: {exc}") from exc
            results.append(
                {
                    "revision": int(row["revision"]),
                    "action": str(row["action"]),
                    "actor": str(row["actor"]),
                    "created_at": float(row["created_at"]),
                    "definition": definition,
                }
            )
        return results

    def get_revision(self, schedule_id: str, revision: int) -> Schedule:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT definition_json FROM schedule_revisions WHERE schedule_id=? AND revision=?",
                    (schedule_id, revision),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot read schedule revision: {exc}") from exc
        if row is None:
            raise ScheduleNotFound(f"revision {revision} of schedule {schedule_id}")
        try:
            value = json.loads(str(row["definition_json"]))
            if not isinstance(value, dict):
                raise ValueError("revision is not an object")
            return Schedule.from_mapping(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ScheduleStoreError(f"invalid schedule revision: {exc}") from exc

    def revise(
        self,
        schedule_id: str,
        *,
        expected_revision: int,
        action: str,
        actor: str,
        now: float,
        **changes: object,
    ) -> Schedule:
        allowed = {
            "workflow_name",
            "prompt",
            "profile_name",
            "permission_ceiling",
            "recurrence_kind",
            "recurrence_spec",
            "timezone",
            "coalesce_policy",
            "max_in_flight",
            "next_run_at",
            "enabled",
            "config_path",
            "deleted_at",
        }
        if set(changes) - allowed:
            raise ValueError(
                "unsupported schedule fields: " + ", ".join(sorted(set(changes) - allowed))
            )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_id = ? AND deleted_at IS NULL",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise ScheduleNotFound(schedule_id)
            current = self._schedule_from_row(row)
            if current.revision != expected_revision:
                raise ScheduleConflict(
                    f"schedule revision changed: expected {expected_revision}, found {current.revision}"
                )
            payload = current.to_dict()
            payload.update(changes)
            payload.update(revision=current.revision + 1, updated_at=now)
            updated = Schedule.from_mapping(payload)
            data = updated.to_dict()
            connection.execute(
                """UPDATE schedules SET workflow_name=?, prompt=?, profile_name=?,
                permission_ceiling=?, recurrence_kind=?, recurrence_spec=?, timezone=?,
                coalesce_policy=?, max_in_flight=?, next_run_at=?, enabled=?, revision=?,
                updated_at=?, config_path=?, deleted_at=? WHERE schedule_id=? AND revision=?""",
                (
                    data["workflow_name"],
                    data["prompt"],
                    data["profile_name"],
                    data["permission_ceiling"],
                    data["recurrence_kind"],
                    data["recurrence_spec"],
                    data["timezone"],
                    data["coalesce_policy"],
                    data["max_in_flight"],
                    data["next_run_at"],
                    int(bool(data["enabled"])),
                    data["revision"],
                    data["updated_at"],
                    data["config_path"],
                    data["deleted_at"],
                    schedule_id,
                    expected_revision,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ScheduleConflict("schedule changed during update")
            self._insert_audit(connection, updated, action=action, actor=actor, now=now)
        return updated

    def update_cursor(
        self, schedule_id: str, *, expected_revision: int, next_run_at: float | None, now: float
    ) -> None:
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "UPDATE schedules SET next_run_at=?, updated_at=? "
                    "WHERE schedule_id=? AND revision=? AND enabled=1 AND deleted_at IS NULL",
                    (next_run_at, now, schedule_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise ScheduleConflict(
                        "schedule changed before occurrence cursor could advance"
                    )
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot advance schedule cursor: {exc}") from exc

    def create_occurrence(
        self,
        *,
        schedule: Schedule,
        occurrence_id: str,
        occurrence_key: str,
        kind: str,
        scheduled_for: float,
        session_id: str,
        state: OccurrenceState,
        now: float,
        receipt: Mapping[str, object] | None = None,
        retry_of: str = "",
        attempt: int = 1,
    ) -> Occurrence:
        receipt_json = (
            json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"))
            if receipt is not None
            else None
        )
        try:
            with self._transaction() as connection:
                connection.execute(
                    """INSERT INTO schedule_occurrences (
                    occurrence_id, schedule_id, schedule_revision, occurrence_key, kind,
                    scheduled_for, created_at, updated_at, state, session_id, retry_of,
                    attempt, receipt_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        occurrence_id,
                        schedule.schedule_id,
                        schedule.revision,
                        occurrence_key,
                        kind,
                        scheduled_for,
                        now,
                        now,
                        state.value,
                        session_id,
                        retry_of,
                        attempt,
                        receipt_json,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            try:
                return self.get_occurrence_by_key(occurrence_key)
            except ScheduleStoreError:
                raise ScheduleConflict("schedule occurrence was already claimed") from exc
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot record schedule occurrence: {exc}") from exc
        return self.get_occurrence(occurrence_id)

    def create_manual_occurrence(
        self,
        *,
        schedule: Schedule,
        occurrence_id: str,
        occurrence_key: str,
        scheduled_for: float,
        session_id: str,
        now: float,
        actor: str,
        receipt: Mapping[str, object],
    ) -> Occurrence:
        """Atomically admit a run-now request under its schedule's policy.

        The active-count check and insert share one SQLite write transaction.
        This matters for simultaneous CLI/TUI requests and multiple processes:
        a check in ``ScheduleManager`` followed by an independent insert could
        otherwise over-admit work or strand multiple ``queue_one`` entries.
        """

        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM schedules WHERE schedule_id=? AND deleted_at IS NULL",
                    (schedule.schedule_id,),
                ).fetchone()
                if row is None:
                    raise ScheduleNotFound(schedule.schedule_id)
                current = self._schedule_from_row(row)
                if current.revision != schedule.revision:
                    raise ScheduleConflict("schedule definition changed before run-now admission")
                rows = connection.execute(
                    "SELECT state FROM schedule_occurrences WHERE schedule_id=? AND state IN (?, ?, ?, ?, ?)",
                    (schedule.schedule_id, *_ACTIVE_OCCURRENCE_STATES),
                ).fetchall()
                inflight_states = [OccurrenceState(str(item["state"])) for item in rows]

                if schedule.coalesce_policy is CoalescePolicy.SKIP:
                    if inflight_states:
                        raise ValueError(
                            "this schedule already has an in-flight occurrence (policy: skip)"
                        )
                    state = OccurrenceState.AWAITING_SERVICE
                elif schedule.coalesce_policy is CoalescePolicy.QUEUE_ONE:
                    if any(
                        item in {OccurrenceState.AWAITING_SERVICE, OccurrenceState.QUEUED}
                        for item in inflight_states
                    ):
                        raise ValueError(
                            "this schedule already has its one pending queued occurrence"
                        )
                    has_active_run = any(
                        item
                        in {
                            OccurrenceState.PREPARING,
                            OccurrenceState.WAITING_CAPACITY,
                            OccurrenceState.RUNNING,
                        }
                        for item in inflight_states
                    )
                    state = (
                        OccurrenceState.QUEUED
                        if has_active_run
                        else OccurrenceState.AWAITING_SERVICE
                    )
                else:
                    if len(inflight_states) >= schedule.max_in_flight:
                        raise ValueError("schedule max_in_flight has been reached")
                    state = OccurrenceState.AWAITING_SERVICE

                durable_receipt = dict(receipt)
                durable_receipt.update(
                    scheduler_state=(
                        "queued" if state is OccurrenceState.QUEUED else "awaiting_service"
                    ),
                    accepted_by=actor,
                    session_id=session_id,
                )
                connection.execute(
                    """INSERT INTO schedule_occurrences (
                    occurrence_id, schedule_id, schedule_revision, occurrence_key, kind,
                    scheduled_for, created_at, updated_at, state, session_id, retry_of,
                    attempt, receipt_json) VALUES (?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?, '', 1, ?)""",
                    (
                        occurrence_id,
                        schedule.schedule_id,
                        schedule.revision,
                        occurrence_key,
                        scheduled_for,
                        now,
                        now,
                        state.value,
                        session_id,
                        json.dumps(durable_receipt, sort_keys=True, separators=(",", ":")),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            try:
                return self.get_occurrence_by_key(occurrence_key)
            except ScheduleStoreError:
                raise ScheduleConflict("schedule occurrence was already claimed") from exc
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot record manual schedule occurrence: {exc}") from exc
        return self.get_occurrence(occurrence_id)

    def claim_due_occurrence(
        self,
        *,
        schedule: Schedule,
        expected_due_at: float,
        next_run_at: float | None,
        occurrence_id: str,
        occurrence_key: str,
        session_id: str,
        state: OccurrenceState,
        now: float,
        coalesce_target_id: str = "",
    ) -> Occurrence:
        """Atomically advance a due cursor and persist its occurrence receipt."""

        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT revision, next_run_at, enabled, deleted_at FROM schedules WHERE schedule_id=?",
                    (schedule.schedule_id,),
                ).fetchone()
                if (
                    row is None
                    or int(row["revision"]) != schedule.revision
                    or row["next_run_at"] is None
                    or float(row["next_run_at"]) != expected_due_at
                    or not bool(row["enabled"])
                    or row["deleted_at"] is not None
                ):
                    existing = connection.execute(
                        "SELECT * FROM schedule_occurrences WHERE occurrence_key=?",
                        (occurrence_key,),
                    ).fetchone()
                    if existing is not None:
                        return self._occurrence_from_row(existing)
                    raise ScheduleConflict("schedule cursor changed before due occurrence claim")
                updated = connection.execute(
                    "UPDATE schedules SET next_run_at=?, updated_at=? WHERE schedule_id=? "
                    "AND revision=? AND next_run_at=?",
                    (next_run_at, now, schedule.schedule_id, schedule.revision, expected_due_at),
                )
                if updated.rowcount != 1:
                    raise ScheduleConflict("schedule cursor changed before it could advance")
                if coalesce_target_id:
                    coalesced = connection.execute(
                        "UPDATE schedule_occurrences SET coalesced_count=coalesced_count+1, "
                        "scheduled_for=?, updated_at=? WHERE occurrence_id=? AND state=?",
                        (
                            expected_due_at,
                            now,
                            coalesce_target_id,
                            OccurrenceState.QUEUED.value,
                        ),
                    )
                    if coalesced.rowcount != 1:
                        raise ScheduleConflict("queued occurrence changed before coalescing")
                    selected = connection.execute(
                        "SELECT * FROM schedule_occurrences WHERE occurrence_id=?",
                        (coalesce_target_id,),
                    ).fetchone()
                    if selected is None:
                        raise ScheduleConflict("coalesced occurrence disappeared")
                    result = self._occurrence_from_row(selected)
                else:
                    connection.execute(
                        """INSERT INTO schedule_occurrences (
                        occurrence_id, schedule_id, schedule_revision, occurrence_key, kind,
                        scheduled_for, created_at, updated_at, state, session_id,
                        receipt_json) VALUES (?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?)""",
                        (
                            occurrence_id,
                            schedule.schedule_id,
                            schedule.revision,
                            occurrence_key,
                            expected_due_at,
                            now,
                            now,
                            state.value,
                            session_id,
                            json.dumps(
                                {"session_id": session_id, "claimed_at": now},
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                    selected = connection.execute(
                        "SELECT * FROM schedule_occurrences WHERE occurrence_id=?",
                        (occurrence_id,),
                    ).fetchone()
                    if selected is None:
                        raise ScheduleConflict("claimed occurrence was not persisted")
                    result = self._occurrence_from_row(selected)
            return result
        except sqlite3.IntegrityError as exc:
            try:
                return self.get_occurrence_by_key(occurrence_key)
            except ScheduleStoreError:
                raise ScheduleConflict("schedule occurrence was already claimed") from exc
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot claim due schedule occurrence: {exc}") from exc

    def get_occurrence(self, occurrence_id: str) -> Occurrence:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM schedule_occurrences WHERE occurrence_id=?", (occurrence_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot read occurrence: {exc}") from exc
        if row is None:
            raise ScheduleNotFound(f"occurrence {occurrence_id}")
        return self._occurrence_from_row(row)

    def get_occurrence_by_key(self, occurrence_key: str) -> Occurrence:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM schedule_occurrences WHERE occurrence_key=?", (occurrence_key,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot read occurrence: {exc}") from exc
        if row is None:
            raise ScheduleNotFound(f"occurrence key {occurrence_key}")
        return self._occurrence_from_row(row)

    def list_occurrences(
        self, *, states: tuple[OccurrenceState, ...] | None = None, limit: int = 200
    ) -> builtins.list[Occurrence]:
        if not 1 <= limit <= 5000:
            raise ValueError("occurrence list limit must be between 1 and 5000")
        parameters: list[object] = []
        sql = "SELECT * FROM schedule_occurrences"
        if states:
            sql += " WHERE state IN (" + ",".join("?" for _ in states) + ")"
            parameters.extend(state.value for state in states)
        sql += " ORDER BY created_at, occurrence_id LIMIT ?"
        parameters.append(limit)
        try:
            with self._connection() as connection:
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot list occurrences: {exc}") from exc
        return [self._occurrence_from_row(row) for row in rows]

    def update_occurrence(
        self,
        occurrence_id: str,
        *,
        now: float,
        expected_states: tuple[OccurrenceState, ...] | None = None,
        **changes: object,
    ) -> Occurrence:
        allowed = {
            "state",
            "session_id",
            "worktree",
            "base_commit",
            "coalesced_count",
            "attempt",
            "receipt",
            "outcome",
            "error",
        }
        if set(changes) - allowed:
            raise ValueError("unsupported occurrence fields")
        assignments: list[str] = []
        parameters: list[object] = []
        for name, value in changes.items():
            column = {
                "worktree": "worktree_json",
                "receipt": "receipt_json",
                "outcome": "outcome_json",
            }.get(name, name)
            if name in {"worktree", "receipt", "outcome"}:
                if value is not None and not isinstance(value, Mapping):
                    raise ValueError(f"occurrence {name} must be a mapping or null")
                value = (
                    json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
                    if value is not None
                    else None
                )
            elif isinstance(value, OccurrenceState):
                value = value.value
            assignments.append(f"{column}=?")
            parameters.append(value)
        assignments.append("updated_at=?")
        parameters.append(now)
        parameters.append(occurrence_id)
        sql = "UPDATE schedule_occurrences SET " + ",".join(assignments) + " WHERE occurrence_id=?"
        if expected_states:
            sql += " AND state IN (" + ",".join("?" for _ in expected_states) + ")"
            parameters.extend(state.value for state in expected_states)
        try:
            with self._transaction() as connection:
                cursor = connection.execute(sql, parameters)
                if cursor.rowcount != 1:
                    raise ScheduleConflict(f"occurrence {occurrence_id} changed before update")
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot update occurrence: {exc}") from exc
        return self.get_occurrence(occurrence_id)

    def coalesce_pending_occurrence(self, occurrence_id: str, *, now: float) -> Occurrence:
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "UPDATE schedule_occurrences SET coalesced_count=coalesced_count+1, updated_at=? "
                    "WHERE occurrence_id=? AND state=?",
                    (now, occurrence_id, OccurrenceState.QUEUED.value),
                )
                if cursor.rowcount != 1:
                    raise ScheduleConflict("queued coalesced occurrence is no longer pending")
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot coalesce occurrence: {exc}") from exc
        return self.get_occurrence(occurrence_id)

    def inflight_for(self, schedule_id: str) -> builtins.list[Occurrence]:
        placeholders = ",".join("?" for _ in _ACTIVE_OCCURRENCE_STATES)
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM schedule_occurrences WHERE schedule_id=? AND state IN ({placeholders})",
                    (schedule_id, *_ACTIVE_OCCURRENCE_STATES),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot inspect in-flight occurrences: {exc}") from exc
        return [self._occurrence_from_row(row) for row in rows]

    def due_schedules(self, *, now: float, limit: int = 500) -> builtins.list[Schedule]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM schedules WHERE enabled=1 AND deleted_at IS NULL "
                    "AND next_run_at IS NOT NULL AND next_run_at<=? "
                    "ORDER BY next_run_at, schedule_id LIMIT ?",
                    (now, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot inspect due schedules: {exc}") from exc
        return [self._schedule_from_row(row) for row in rows]

    def update_coalesced(
        self, occurrence_id: str, *, scheduled_for: float, now: float
    ) -> Occurrence:
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "UPDATE schedule_occurrences SET coalesced_count=coalesced_count+1, "
                    "scheduled_for=?, updated_at=? WHERE occurrence_id=? AND state=?",
                    (scheduled_for, now, occurrence_id, OccurrenceState.QUEUED.value),
                )
                if cursor.rowcount != 1:
                    raise ScheduleConflict("queued occurrence changed before coalescing")
        except sqlite3.Error as exc:
            raise ScheduleStoreError(f"cannot coalesce schedule occurrence: {exc}") from exc
        return self.get_occurrence(occurrence_id)
