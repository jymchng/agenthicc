"""Client-neutral session contracts (PRD-150).

These models are deliberately independent of Rich, prompt-toolkit, HTTP, and
the provider runtime.  Adapters may serialize them, but they remain the
single vocabulary for session commands, snapshots, and projected events.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

__all__ = [
    "CommandResult",
    "EventDurability",
    "SessionCommand",
    "SessionError",
    "SessionEvent",
    "SessionSnapshot",
    "SessionState",
]


class SessionState(StrEnum):
    """Lifecycle states visible to every client adapter."""

    CREATED = "created"
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_QUESTION = "waiting_question"
    BACKGROUND = "background"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class EventDurability(StrEnum):
    """Whether an event is retained for replay."""

    DURABLE = "durable"
    EPHEMERAL = "ephemeral"


class SessionError(RuntimeError):
    """Structured service error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "status": self.status}


@dataclass(frozen=True)
class SessionSnapshot:
    """Policy-filtered, client-neutral read model for one session."""

    schema_version: int
    session_id: str
    project_root: str
    created_at: float
    updated_at: float
    state: SessionState
    active_turn_id: str | None = None
    parent_session_id: str | None = None
    workflow: dict[str, object] = field(default_factory=dict)
    agent: dict[str, object] = field(default_factory=dict)
    queue: dict[str, object] = field(default_factory=lambda: {"depth": 0, "accepting_input": True})
    approvals_pending: int = 0
    questions_pending: int = 0
    background_jobs_running: int = 0
    terminals_running: int = 0
    last_event_sequence: int = 0
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> SessionSnapshot:
        state = data.get("state", SessionState.CREATED.value)
        if not isinstance(state, str):
            raise ValueError("snapshot state must be a string")

        def number(key: str, default: float = 0.0) -> float:
            value = data.get(key, default)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"snapshot field {key!r} must be numeric")
            return float(value)

        def integer(key: str, default: int = 0) -> int:
            value = data.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"snapshot field {key!r} must be an integer")
            return value

        def mapping(key: str) -> dict[str, object]:
            value = data.get(key, {})
            if not isinstance(value, Mapping):
                raise ValueError(f"snapshot field {key!r} must be an object")
            return dict(value)

        session_id = data.get("session_id")
        project_root = data.get("project_root")
        if not isinstance(session_id, str) or not isinstance(project_root, str):
            raise ValueError("snapshot session_id and project_root must be strings")
        raw_capabilities = data.get("capabilities", [])
        if not isinstance(raw_capabilities, (list, tuple)):
            raise ValueError("snapshot capabilities must be a list")
        active_turn_id = data.get("active_turn_id")
        parent_session_id = data.get("parent_session_id")
        return cls(
            schema_version=integer("schema_version", 1),
            session_id=session_id,
            project_root=project_root,
            created_at=number("created_at"),
            updated_at=number("updated_at"),
            state=SessionState(state),
            active_turn_id=active_turn_id if isinstance(active_turn_id, str) else None,
            parent_session_id=parent_session_id if isinstance(parent_session_id, str) else None,
            workflow=mapping("workflow"),
            agent=mapping("agent"),
            queue=mapping("queue"),
            approvals_pending=integer("approvals_pending"),
            questions_pending=integer("questions_pending"),
            background_jobs_running=integer("background_jobs_running"),
            terminals_running=integer("terminals_running"),
            last_event_sequence=integer("last_event_sequence"),
            capabilities=tuple(item for item in raw_capabilities if isinstance(item, str)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "project_root": self.project_root,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state": self.state.value,
            "active_turn_id": self.active_turn_id,
            "parent_session_id": self.parent_session_id,
            "workflow": dict(self.workflow),
            "agent": dict(self.agent),
            "queue": dict(self.queue),
            "approvals_pending": self.approvals_pending,
            "questions_pending": self.questions_pending,
            "background_jobs_running": self.background_jobs_running,
            "terminals_running": self.terminals_running,
            "last_event_sequence": self.last_event_sequence,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class SessionEvent:
    """Versioned event envelope delivered to all supported clients."""

    schema_version: int
    event_id: str
    sequence: int
    session_id: str
    turn_id: str | None
    source: str
    kind: str
    occurred_at: float
    durability: EventDurability
    visibility: str
    payload: dict[str, object]

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        sequence: int,
        source: str,
        kind: str,
        payload: Mapping[str, object] | None = None,
        turn_id: str | None = None,
        durability: EventDurability = EventDurability.DURABLE,
        visibility: str = "session",
    ) -> SessionEvent:
        return cls(
            schema_version=1,
            event_id=f"evt_{uuid.uuid4().hex}",
            sequence=sequence,
            session_id=session_id,
            turn_id=turn_id,
            source=source,
            kind=kind,
            occurred_at=time.time(),
            durability=durability,
            visibility=visibility,
            payload=dict(payload or {}),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "source": self.source,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "durability": self.durability.value,
            "visibility": self.visibility,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> SessionEvent:
        def text(key: str, default: str | None = None) -> str | None:
            value = data.get(key, default)
            if value is None or isinstance(value, str):
                return value
            raise ValueError(f"event field {key!r} must be a string or null")

        def integer(key: str) -> int:
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            raise ValueError(f"event field {key!r} must be an integer")

        payload = data.get("payload", {})
        if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
            raise ValueError("event payload must be an object")
        occurred_at = data.get("occurred_at")
        if not isinstance(occurred_at, (int, float)) or isinstance(occurred_at, bool):
            raise ValueError("event occurred_at must be a number")
        schema_version = data.get("schema_version", 1)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("event schema_version must be an integer")
        event_id = text("event_id")
        session_id = text("session_id")
        source = text("source")
        kind = text("kind")
        durability = text("durability", EventDurability.DURABLE.value)
        visibility = text("visibility", "session")
        if not all(
            isinstance(value, str) and value for value in (event_id, session_id, source, kind)
        ):
            raise ValueError("event identity fields must be non-empty strings")
        if durability not in {item.value for item in EventDurability}:
            raise ValueError("unknown event durability")
        if not isinstance(visibility, str) or not visibility:
            raise ValueError("event visibility must be a non-empty string")
        assert isinstance(event_id, str)
        assert isinstance(session_id, str)
        assert isinstance(source, str)
        assert isinstance(kind, str)
        return cls(
            schema_version=schema_version,
            event_id=event_id,
            sequence=integer("sequence"),
            session_id=session_id,
            turn_id=text("turn_id"),
            source=source,
            kind=kind,
            occurred_at=float(occurred_at),
            durability=EventDurability(durability),
            visibility=visibility,
            payload=dict(payload),
        )


@dataclass(frozen=True)
class SessionCommand:
    """A mutation submitted by any client adapter."""

    kind: str
    session_id: str | None = None
    client_id: str = "local"
    command_id: str = ""
    idempotency_key: str = ""
    expected_sequence: int | None = None
    payload: dict[str, object] = field(default_factory=dict)
    capabilities: frozenset[str] = frozenset({"read", "control"})

    def __post_init__(self) -> None:
        if not self.command_id:
            object.__setattr__(self, "command_id", f"cmd_{uuid.uuid4().hex}")
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", self.command_id)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, object],
        *,
        client_id: str = "local",
        capabilities: frozenset[str] | None = None,
    ) -> SessionCommand:
        kind = data.get("kind")
        if not isinstance(kind, str) or not kind:
            raise SessionError("invalid_command", "command kind must be a non-empty string")
        payload = data.get("payload", {})
        if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
            raise SessionError("invalid_command", "command payload must be an object")
        expected = data.get("expected_sequence")
        if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
            raise SessionError("invalid_command", "expected_sequence must be an integer or null")
        command_id = data.get("command_id", "")
        idempotency = data.get("idempotency_key", "")
        session_id = data.get("session_id")
        if not isinstance(command_id, str) or not isinstance(idempotency, str):
            raise SessionError("invalid_command", "command IDs must be strings")
        if session_id is not None and not isinstance(session_id, str):
            raise SessionError("invalid_command", "session_id must be a string or null")
        return cls(
            kind=kind,
            session_id=session_id,
            client_id=client_id,
            command_id=command_id,
            idempotency_key=idempotency,
            expected_sequence=expected,
            payload=dict(payload),
            capabilities=(
                capabilities if capabilities is not None else frozenset({"read", "control"})
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": self.kind,
            "session_id": self.session_id,
            "client_id": self.client_id,
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "expected_sequence": self.expected_sequence,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class CommandResult:
    """Stable result returned after a command is accepted or rejected."""

    ok: bool
    command_id: str
    session_id: str | None
    code: str = "ok"
    message: str = ""
    data: dict[str, object] = field(default_factory=dict)
    replayed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "command_id": self.command_id,
            "session_id": self.session_id,
            "code": self.code,
            "message": self.message,
            "data": dict(self.data),
            "replayed": self.replayed,
        }
