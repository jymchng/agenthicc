"""Typed background-session lifecycle models for PRD-141."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class SessionStatus(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    RETRYING = "retrying"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"
    ARCHIVED = "archived"
    DELETED = "deleted"


ACTIVE_STATUSES = frozenset(
    {
        SessionStatus.QUEUED,
        SessionStatus.STARTING,
        SessionStatus.RUNNING,
        SessionStatus.WAITING_APPROVAL,
        SessionStatus.WAITING_INPUT,
        SessionStatus.RETRYING,
        SessionStatus.CANCELLING,
    }
)
TERMINAL_STATUSES = frozenset(
    {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.ORPHANED,
        SessionStatus.ARCHIVED,
        SessionStatus.DELETED,
    }
)

_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.QUEUED: frozenset(
        {
            SessionStatus.STARTING,
            SessionStatus.CANCELLING,
            SessionStatus.ORPHANED,
            SessionStatus.FAILED,
        }
    ),
    SessionStatus.STARTING: frozenset(
        {SessionStatus.RUNNING, SessionStatus.CANCELLING, SessionStatus.ORPHANED}
    ),
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.WAITING_APPROVAL,
            SessionStatus.WAITING_INPUT,
            SessionStatus.RETRYING,
            SessionStatus.CANCELLING,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.ORPHANED,
        }
    ),
    SessionStatus.WAITING_APPROVAL: frozenset({SessionStatus.RUNNING, SessionStatus.CANCELLING}),
    SessionStatus.WAITING_INPUT: frozenset({SessionStatus.RUNNING, SessionStatus.CANCELLING}),
    SessionStatus.RETRYING: frozenset(
        {SessionStatus.STARTING, SessionStatus.CANCELLING, SessionStatus.ORPHANED}
    ),
    SessionStatus.CANCELLING: frozenset({SessionStatus.CANCELLED, SessionStatus.ORPHANED}),
    SessionStatus.COMPLETED: frozenset({SessionStatus.ARCHIVED}),
    SessionStatus.FAILED: frozenset(
        {SessionStatus.RETRYING, SessionStatus.STARTING, SessionStatus.ARCHIVED}
    ),
    SessionStatus.CANCELLED: frozenset({SessionStatus.STARTING, SessionStatus.ARCHIVED}),
    SessionStatus.ORPHANED: frozenset(
        {SessionStatus.STARTING, SessionStatus.CANCELLING, SessionStatus.ARCHIVED}
    ),
    SessionStatus.ARCHIVED: frozenset(
        {SessionStatus.STARTING, SessionStatus.COMPLETED, SessionStatus.FAILED}
    ),
    SessionStatus.DELETED: frozenset(),
}


def legal_transition(current: SessionStatus, target: SessionStatus) -> bool:
    """Return whether *current* may move to *target*."""

    return target in _TRANSITIONS.get(current, frozenset())


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _status(value: object) -> SessionStatus:
    if isinstance(value, SessionStatus):
        return value
    if isinstance(value, str):
        try:
            return SessionStatus(value)
        except ValueError:
            pass
    return SessionStatus.FAILED


@dataclass(frozen=True)
class BackgroundSession:
    """One durable background execution and its recoverable metadata."""

    session_id: str
    title: str
    cwd: str
    workflow_name: str
    intent: str
    status: SessionStatus = SessionStatus.QUEUED
    created_at: float = 0.0
    started_at: float | None = None
    last_active: float = 0.0
    state_changed_at: float = 0.0
    completed_at: float | None = None
    provider: str = ""
    model: str = ""
    source: str = "cli"
    current_phase: str = ""
    phase_history: tuple[str, ...] = ()
    latest_activity: str = "Accepted"
    error: str | None = None
    failure_category: str = ""
    cancellation_reason: str = ""
    exit_reason: str = ""
    resume_marker: str = ""
    approval_request: str = ""
    approval_decision: bool | None = None
    input_request: str = ""
    input_value: str | None = None
    worker_pid: int | None = None
    lease_token: str = ""
    attempt: int = 0
    retry_count: int = 0
    labels: tuple[str, ...] = ()
    pinned: bool = False
    artifact_dir: str = ""
    trash_dir: str = ""
    original_artifact_dir: str = ""

    @classmethod
    def create(
        cls,
        session_id: str,
        *,
        title: str,
        cwd: str,
        workflow_name: str,
        intent: str,
        artifact_dir: str = "",
        now: float | None = None,
    ) -> "BackgroundSession":
        timestamp = time.time() if now is None else now
        return cls(
            session_id=session_id,
            title=title or intent[:80] or "Background session",
            cwd=cwd,
            workflow_name=workflow_name,
            intent=intent,
            created_at=timestamp,
            last_active=timestamp,
            state_changed_at=timestamp,
            artifact_dir=artifact_dir,
            original_artifact_dir=artifact_dir,
        )

    def evolve(self, **changes: object) -> "BackgroundSession":
        """Return a copy with validated enum/string values normalized."""

        def _str(name: str, current: str) -> str:
            value = changes.get(name, current)
            return value if isinstance(value, str) else current

        def _float(name: str, current: float | None) -> float | None:
            value = changes.get(name, current)
            return (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else current
            )

        def _int(name: str, current: int | None) -> int | None:
            value = changes.get(name, current)
            return int(value) if isinstance(value, int) and not isinstance(value, bool) else current

        def _optional_str(name: str, current: str | None) -> str | None:
            value = changes.get(name, current)
            return value if isinstance(value, str) or value is None else current

        def _bool(name: str, current: bool) -> bool:
            value = changes.get(name, current)
            return value if isinstance(value, bool) else current

        def _optional_bool(name: str, current: bool | None) -> bool | None:
            value = changes.get(name, current)
            return value if isinstance(value, bool) or value is None else current

        return BackgroundSession(
            session_id=self.session_id,
            title=_str("title", self.title),
            cwd=_str("cwd", self.cwd),
            workflow_name=_str("workflow_name", self.workflow_name),
            intent=_str("intent", self.intent),
            status=_status(changes.get("status", self.status)),
            created_at=_float("created_at", self.created_at) or 0.0,
            started_at=_float("started_at", self.started_at),
            last_active=_float("last_active", self.last_active) or 0.0,
            state_changed_at=_float("state_changed_at", self.state_changed_at) or 0.0,
            completed_at=_float("completed_at", self.completed_at),
            provider=_str("provider", self.provider),
            model=_str("model", self.model),
            source=_str("source", self.source),
            current_phase=_str("current_phase", self.current_phase),
            phase_history=_string_tuple(changes.get("phase_history", self.phase_history)),
            latest_activity=_str("latest_activity", self.latest_activity),
            error=_optional_str("error", self.error),
            failure_category=_str("failure_category", self.failure_category),
            cancellation_reason=_str("cancellation_reason", self.cancellation_reason),
            exit_reason=_str("exit_reason", self.exit_reason),
            resume_marker=_str("resume_marker", self.resume_marker),
            approval_request=_str("approval_request", self.approval_request),
            approval_decision=_optional_bool("approval_decision", self.approval_decision),
            input_request=_str("input_request", self.input_request),
            input_value=_optional_str("input_value", self.input_value),
            worker_pid=_int("worker_pid", self.worker_pid),
            lease_token=_str("lease_token", self.lease_token),
            attempt=_int("attempt", self.attempt) or 0,
            retry_count=_int("retry_count", self.retry_count) or 0,
            labels=_string_tuple(changes.get("labels", self.labels)),
            pinned=_bool("pinned", self.pinned),
            artifact_dir=_str("artifact_dir", self.artifact_dir),
            trash_dir=_str("trash_dir", self.trash_dir),
            original_artifact_dir=_str("original_artifact_dir", self.original_artifact_dir),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "cwd": self.cwd,
            "workflow_name": self.workflow_name,
            "intent": self.intent,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "last_active": self.last_active,
            "state_changed_at": self.state_changed_at,
            "completed_at": self.completed_at,
            "provider": self.provider,
            "model": self.model,
            "source": self.source,
            "current_phase": self.current_phase,
            "phase_history": list(self.phase_history),
            "latest_activity": self.latest_activity,
            "error": self.error,
            "failure_category": self.failure_category,
            "cancellation_reason": self.cancellation_reason,
            "exit_reason": self.exit_reason,
            "resume_marker": self.resume_marker,
            "approval_request": self.approval_request,
            "approval_decision": self.approval_decision,
            "input_request": self.input_request,
            "input_value": self.input_value,
            "worker_pid": self.worker_pid,
            "lease_token": self.lease_token,
            "attempt": self.attempt,
            "retry_count": self.retry_count,
            "labels": list(self.labels),
            "pinned": self.pinned,
            "artifact_dir": self.artifact_dir,
            "trash_dir": self.trash_dir,
            "original_artifact_dir": self.original_artifact_dir,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "BackgroundSession":
        session_id = value.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("background session record has no session_id")

        def _float_or_none(item: object, default: float | None = None) -> float | None:
            return (
                float(item)
                if isinstance(item, (int, float)) and not isinstance(item, bool)
                else default
            )

        def _float_value(item: object, default: float) -> float:
            value = _float_or_none(item)
            return default if value is None else value

        def _int_or_none(item: object, default: int | None = None) -> int | None:
            return int(item) if isinstance(item, int) and not isinstance(item, bool) else default

        def _int_value(item: object, default: int) -> int:
            value = _int_or_none(item)
            return default if value is None else value

        def _optional_str(item: object) -> str | None:
            return item if isinstance(item, str) or item is None else None

        def _optional_bool(item: object) -> bool | None:
            return item if isinstance(item, bool) or item is None else None

        return cls(
            session_id=session_id,
            title=str(value.get("title", "Background session")),
            cwd=str(value.get("cwd", "")),
            workflow_name=str(value.get("workflow_name", "")),
            intent=str(value.get("intent", "")),
            status=_status(value.get("status")),
            created_at=_float_value(value.get("created_at"), 0.0),
            started_at=_float_or_none(value.get("started_at")),
            last_active=_float_value(value.get("last_active"), 0.0),
            state_changed_at=_float_value(value.get("state_changed_at"), 0.0),
            completed_at=_float_or_none(value.get("completed_at")),
            provider=str(value.get("provider", "")),
            model=str(value.get("model", "")),
            source=str(value.get("source", "cli")),
            current_phase=str(value.get("current_phase", "")),
            phase_history=_string_tuple(value.get("phase_history")),
            latest_activity=str(value.get("latest_activity", "")),
            error=_optional_str(value.get("error")),
            failure_category=str(value.get("failure_category", "")),
            cancellation_reason=str(value.get("cancellation_reason", "")),
            exit_reason=str(value.get("exit_reason", "")),
            resume_marker=str(value.get("resume_marker", "")),
            approval_request=str(value.get("approval_request", "")),
            approval_decision=_optional_bool(value.get("approval_decision")),
            input_request=str(value.get("input_request", "")),
            input_value=_optional_str(value.get("input_value")),
            worker_pid=_int_or_none(value.get("worker_pid")),
            lease_token=str(value.get("lease_token", "")),
            attempt=_int_value(value.get("attempt"), 0),
            retry_count=_int_value(value.get("retry_count"), 0),
            labels=_string_tuple(value.get("labels")),
            pinned=bool(value.get("pinned", False)),
            artifact_dir=str(value.get("artifact_dir", "")),
            trash_dir=str(value.get("trash_dir", "")),
            original_artifact_dir=str(value.get("original_artifact_dir", "")),
        )
