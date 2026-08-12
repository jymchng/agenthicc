"""Exclusive ownership of durable agenthicc sessions (PRD-171).

The session lease is the outer process boundary for a durable conversation.
It is acquired before session construction opens journals, providers, tools, or
the visual transcript.  Workflow-run claims remain a separate, nested guard
for workflow recovery and side effects.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path
from typing import Callable, Literal

from agenthicc.runners.process_lease import (
    InterProcessLock,
    InterProcessLockError,
    atomic_publish,
    directory_fsync,
    owner_alive,
    process_identity,
    read_json_object,
    remove_if_owner,
)

__all__ = [
    "SessionAlreadyActiveError",
    "SessionIndexError",
    "SessionLeaseError",
    "SessionLeaseInspection",
    "SessionOpenCoordinator",
    "SessionOwnerInfo",
    "SessionOwnerLease",
    "SessionStorageError",
    "default_sessions_dir",
    "format_session_conflict",
]

SESSION_OWNER_SCHEMA_VERSION = 1
SESSION_CONFLICT_EXIT_CODE = 3


def default_sessions_dir() -> Path:
    """Return the user-wide durable session root."""

    return Path.home() / ".agenthicc" / "sessions"


def _safe_display(value: object, *, limit: int = 128) -> str:
    """Bound and neutralize control characters from user-editable records."""

    text = str(value)
    return "".join(character if character.isprintable() else "?" for character in text)[:limit]


class SessionLeaseError(RuntimeError):
    """Base class for session-owner acquisition and release failures."""


class SessionStorageError(SessionLeaseError):
    """Raised when session state cannot be safely inspected or persisted."""

    code = "session_storage_error"


class SessionIndexError(SessionStorageError):
    """Raised when the canonical session index is missing or invalid."""

    code = "session_index_error"


@dataclass(frozen=True)
class SessionOwnerInfo:
    """Safe diagnostic metadata for one session owner."""

    schema_version: int
    session_id: str
    owner_id: str
    pid: int
    host: str
    process_start_token: str | None
    acquired_at: float
    entrypoint: str

    @classmethod
    def from_mapping(cls, value: object, *, session_id: str) -> "SessionOwnerInfo | None":
        if not isinstance(value, dict):
            return None
        schema = value.get("schema_version")
        record_session = value.get("session_id")
        owner_id = value.get("owner_id")
        pid = value.get("pid")
        host = value.get("host")
        acquired_at = value.get("acquired_at")
        entrypoint = value.get("entrypoint")
        start_token = value.get("process_start_token")
        if (
            schema != SESSION_OWNER_SCHEMA_VERSION
            or record_session != session_id
            or not isinstance(owner_id, str)
            or not owner_id
            or len(owner_id) > 256
            or not owner_id.isprintable()
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(host, str)
            or not host
            or len(host) > 255
            or not host.isprintable()
            or not isinstance(acquired_at, (int, float))
            or isinstance(acquired_at, bool)
            or not math.isfinite(float(acquired_at))
            or not isinstance(entrypoint, str)
            or not entrypoint
            or len(entrypoint) > 32
            or not entrypoint.isprintable()
        ):
            return None
        if start_token is not None and (not isinstance(start_token, str) or len(start_token) > 512):
            return None
        return cls(
            schema_version=SESSION_OWNER_SCHEMA_VERSION,
            session_id=session_id,
            owner_id=owner_id,
            pid=pid,
            host=host,
            process_start_token=start_token,
            acquired_at=float(acquired_at),
            entrypoint=entrypoint,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the redaction-safe on-disk representation."""

        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "pid": self.pid,
            "host": self.host,
            "acquired_at": self.acquired_at,
            "entrypoint": self.entrypoint,
        }
        if self.process_start_token:
            payload["process_start_token"] = self.process_start_token
        return payload

    @property
    def age_seconds(self) -> float:
        """Return non-negative diagnostic age without affecting liveness."""

        return max(0.0, time.time() - self.acquired_at)


class SessionAlreadyActiveError(SessionLeaseError):
    """Raised when another live or unverifiable process owns a session."""

    code = "session_already_active"
    exit_code = SESSION_CONFLICT_EXIT_CODE

    def __init__(
        self,
        session_id: str,
        *,
        owner: SessionOwnerInfo | None = None,
        reason: str = "live_owner",
    ) -> None:
        self.session_id = session_id
        self.owner = owner
        self.reason = reason
        owner_text = "unknown owner"
        if owner is not None:
            owner_text = (
                f"pid={owner.pid}, host={_safe_display(owner.host)}, "
                f"entrypoint={_safe_display(owner.entrypoint)}"
            )
        super().__init__(
            f"session {session_id!r} is already active ({owner_text}; reason={reason})"
        )

    def to_dict(self) -> dict[str, object]:
        """Return the bounded machine-readable conflict diagnostic."""

        result: dict[str, object] = {
            "status": "error",
            "code": self.code,
            "session_id": self.session_id,
            "reason": self.reason,
        }
        if self.owner is not None:
            result["owner"] = {
                "pid": self.owner.pid,
                "host": self.owner.host,
                "entrypoint": self.owner.entrypoint,
                "acquired_at": self.owner.acquired_at,
                "age_seconds": self.owner.age_seconds,
            }
        return result


def format_session_conflict(exc: SessionAlreadyActiveError) -> str:
    """Render the bounded human-readable conflict diagnostic."""

    owner = exc.owner
    if owner is None:
        owner_text = "owner identity is unavailable"
    else:
        owner_text = (
            f"pid={owner.pid}, host={_safe_display(owner.host)}, "
            f"entrypoint={_safe_display(owner.entrypoint)}, "
            f"age={owner.age_seconds:.0f}s"
        )
    return (
        f"error: {exc.code}\n"
        f"Session {exc.session_id} is already open ({owner_text}).\n"
        "Close that agenthicc process or continue the session there. "
        "No transcript was loaded and no new session was created."
    )


LeaseState = Literal["available", "active", "recoverable", "unknown"]


@dataclass(frozen=True)
class SessionLeaseInspection:
    """Non-mutating owner classification used by list/inspect surfaces."""

    session_id: str
    state: LeaseState
    owner: SessionOwnerInfo | None = None
    reason: str | None = None


@dataclass
class _LocalLeaseState:
    owner_id: str
    info: SessionOwnerInfo
    references: int = 0


_PROCESS_LEASES: dict[tuple[Path, str], _LocalLeaseState] = {}


class SessionOwnerLease:
    """One process-local reference to a durable session owner lease."""

    def __init__(
        self, store: "SessionOwnerStore", state_key: tuple[Path, str], state: _LocalLeaseState
    ) -> None:
        self._store = store
        self._state_key = state_key
        self._state = state
        self._released = False

    @property
    def session_id(self) -> str:
        return self._state.info.session_id

    @property
    def owner_id(self) -> str:
        return self._state.owner_id

    @property
    def info(self) -> SessionOwnerInfo:
        return self._state.info

    @property
    def path(self) -> Path:
        return self._store.owner_path(self.session_id)

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Release one reference; remove the record after the final reference."""

        if self._released:
            return
        self._released = True
        self._store._release(self._state_key, self._state)

    def __enter__(self) -> "SessionOwnerLease":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


class SessionOwnerStore:
    """Durable owner records for one user-wide session root."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        identity_resolver: Callable[[int], tuple[str, str] | None] = process_identity,
    ) -> None:
        self.root = Path(root or default_sessions_dir()).expanduser().resolve()
        self._identity_resolver = identity_resolver

    def session_path(self, session_id: str) -> Path:
        self._validate_identifier(session_id)
        return self.root / session_id

    def owner_path(self, session_id: str) -> Path:
        return self.session_path(session_id) / ".owner"

    def owner_lock_path(self, session_id: str) -> Path:
        """Return the short-lived lock used to serialize owner changes."""

        return self.session_path(session_id) / ".owner.lock"

    @contextmanager
    def _owner_guard(self, session_id: str) -> Iterator[None]:
        """Serialize publication, stale recovery, and release for one session.

        The owner record is published atomically, but release still performs
        a read/compare/unlink sequence.  This lock closes that TOCTOU window so
        late cleanup cannot remove a newer owner's record.
        """

        try:
            with InterProcessLock(self.owner_lock_path(session_id)):
                yield
        except InterProcessLockError as exc:
            raise SessionStorageError(
                f"cannot safely coordinate owner for session {session_id!r}: {exc}"
            ) from exc

    def inspect(self, session_id: str) -> SessionLeaseInspection:
        """Classify ownership without deleting or acquiring anything."""

        path = self.owner_path(session_id)
        payload = read_json_object(path)
        if payload is None:
            return SessionLeaseInspection(session_id, "available")
        owner = SessionOwnerInfo.from_mapping(payload, session_id=session_id)
        if owner is None:
            return SessionLeaseInspection(session_id, "unknown", reason="invalid_owner_record")
        if owner_alive(payload, identity_resolver=self._identity_resolver):
            return SessionLeaseInspection(session_id, "active", owner=owner)
        return SessionLeaseInspection(session_id, "recoverable", owner=owner, reason="owner_dead")

    def acquire(
        self,
        session_id: str,
        *,
        entrypoint: str = "tui",
        require_existing: bool = False,
    ) -> SessionOwnerLease:
        """Acquire an exclusive owner lease for *session_id*."""

        self._validate_identifier(session_id)
        self._validate_entrypoint(entrypoint)
        session_path = self.session_path(session_id)
        if require_existing and not session_path.is_dir():
            raise SessionStorageError(f"session {session_id!r} was not found")
        try:
            session_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionStorageError(
                f"cannot create session directory for {session_id!r}: {exc}"
            ) from exc
        try:
            os.chmod(self.root, 0o700)
            os.chmod(session_path, 0o700)
        except OSError:
            pass

        state_key = (self.root.resolve(), session_id)
        local = _PROCESS_LEASES.get(state_key)
        if local is not None:
            current = read_json_object(self.owner_path(session_id))
            if current is not None and current.get("owner_id") == local.owner_id:
                local.references += 1
                return SessionOwnerLease(self, state_key, local)
            _PROCESS_LEASES.pop(state_key, None)

        owner_id = f"{entrypoint}:{os.getpid()}:{uuid.uuid4().hex}"
        try:
            start_identity = self._identity_resolver(os.getpid())
        except Exception:  # noqa: BLE001
            # A new owner can still be published without a start token.  The
            # missing token is retained as an uncertainty on later conflict
            # checks, which then fail closed instead of reclaiming by age.
            start_identity = None
        info = SessionOwnerInfo(
            schema_version=SESSION_OWNER_SCHEMA_VERSION,
            session_id=session_id,
            owner_id=owner_id,
            pid=os.getpid(),
            host=socket.gethostname(),
            process_start_token=start_identity[1] if start_identity is not None else None,
            acquired_at=time.time(),
            entrypoint=entrypoint,
        )
        encoded = (json.dumps(info.to_mapping(), sort_keys=True) + "\n").encode("utf-8")
        path = self.owner_path(session_id)

        with self._owner_guard(session_id):
            for _attempt in range(3):
                try:
                    atomic_publish(path, encoded, prefix=".owner-")
                except FileExistsError:
                    existing_payload = read_json_object(path)
                    if existing_payload is None:
                        continue
                    existing = SessionOwnerInfo.from_mapping(
                        existing_payload, session_id=session_id
                    )
                    if existing is None:
                        raise SessionAlreadyActiveError(
                            session_id,
                            reason="owner_unverifiable",
                        )
                    if owner_alive(
                        existing_payload,
                        identity_resolver=self._identity_resolver,
                    ):
                        raise SessionAlreadyActiveError(session_id, owner=existing)
                    # The compare and unlink are serialized relative to every
                    # other acquisition and release for this session.
                    if not remove_if_owner(path, existing.owner_id):
                        continue
                    continue
                except OSError as exc:
                    raise SessionStorageError(
                        f"cannot publish owner for {session_id!r}: {exc}"
                    ) from exc
                directory_fsync(session_path)
                local = _LocalLeaseState(owner_id=owner_id, info=info, references=1)
                _PROCESS_LEASES[state_key] = local
                return SessionOwnerLease(self, state_key, local)

        raise SessionStorageError(f"could not acquire owner lease for {session_id!r}")

    def _release(self, state_key: tuple[Path, str], state: _LocalLeaseState) -> None:
        current = _PROCESS_LEASES.get(state_key)
        if current is not state:
            return
        current.references -= 1
        if current.references > 0:
            return
        _PROCESS_LEASES.pop(state_key, None)
        try:
            with self._owner_guard(state.info.session_id):
                remove_if_owner(self.owner_path(state.info.session_id), state.owner_id)
                directory_fsync(self.session_path(state.info.session_id))
        except SessionStorageError:
            # Failing closed leaves the record for a later safe reclaim.  Never
            # unlink without the coordination lock.
            return

    @staticmethod
    def _validate_identifier(value: str) -> None:
        if (
            not value
            or value in {".", ".."}
            or any(separator in value for separator in ("/", "\\"))
            or "\x00" in value
            or not value.isprintable()
            or len(value) > 256
        ):
            raise ValueError("session_id must be a non-empty safe identifier")

    @staticmethod
    def _validate_entrypoint(value: str) -> None:
        if (
            not value
            or len(value) > 32
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in value)
        ):
            raise ValueError("entrypoint must be a short lowercase identifier")


class SessionOpenCoordinator:
    """Resolve and claim sessions through one canonical attach boundary."""

    def __init__(self, sessions_dir: Path | str | None = None) -> None:
        self.sessions_dir = Path(sessions_dir or default_sessions_dir()).expanduser()
        self.owner_store = SessionOwnerStore(self.sessions_dir)
        self.index_path = self.sessions_dir / "index.json"
        self.index_lock_path = self.sessions_dir / "index.lock"

    def acquire_existing(
        self,
        session_id: str,
        *,
        entrypoint: str = "tui",
    ) -> SessionOwnerLease:
        """Claim an explicitly selected, existing session."""

        return self.owner_store.acquire(
            session_id,
            entrypoint=entrypoint,
            require_existing=True,
        )

    def acquire(
        self,
        session_id: str,
        *,
        entrypoint: str = "tui",
        require_existing: bool = False,
    ) -> SessionOwnerLease:
        """Acquire an ID supplied by a trusted caller, optionally requiring it."""

        return self.owner_store.acquire(
            session_id,
            entrypoint=entrypoint,
            require_existing=require_existing,
        )

    def acquire_new(self, session_id: str, *, entrypoint: str = "tui") -> SessionOwnerLease:
        """Claim a newly allocated session before registration."""

        return self.owner_store.acquire(session_id, entrypoint=entrypoint)

    def select_latest_for_cwd(
        self,
        cwd: str | Path,
        *,
        entrypoint: str = "tui",
    ) -> tuple[str, SessionOwnerLease] | None:
        """Select and claim the newest matching session atomically."""

        canonical_cwd = str(Path(cwd).expanduser().resolve())
        try:
            with InterProcessLock(self.index_lock_path):
                index = self._load_index()
                candidates = [
                    (session_id, metadata)
                    for session_id, metadata in index.items()
                    if metadata.get("cwd") == canonical_cwd
                ]
                if not candidates:
                    return None
                session_id = max(
                    candidates,
                    key=lambda item: (self._timestamp(item[1].get("last_active")), item[0]),
                )[0]
                lease = self.acquire_existing(session_id, entrypoint=entrypoint)
                return session_id, lease
        except InterProcessLockError as exc:
            raise SessionStorageError(
                f"cannot safely coordinate session index {self.index_path}: {exc}"
            ) from exc

    def inspect(self, session_id: str) -> SessionLeaseInspection:
        """Inspect one session owner without changing it."""

        return self.owner_store.inspect(session_id)

    def _load_index(self) -> dict[str, dict[str, object]]:
        if not self.index_path.exists():
            return {}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SessionIndexError(f"cannot read session index {self.index_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SessionIndexError(f"session index {self.index_path} must contain an object")
        index: dict[str, dict[str, object]] = {}
        for session_id, metadata in raw.items():
            if not isinstance(session_id, str) or not isinstance(metadata, dict):
                raise SessionIndexError(f"invalid record in session index {self.index_path}")
            try:
                SessionOwnerStore._validate_identifier(session_id)
            except ValueError as exc:
                raise SessionIndexError(
                    f"invalid session ID in session index {self.index_path}: {exc}"
                ) from exc
            index[session_id] = {str(key): value for key, value in metadata.items()}
        return index

    @staticmethod
    def _timestamp(value: object) -> float:
        return (
            float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        )
