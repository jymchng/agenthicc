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


class ModeApplicationStatus(str, Enum):
    """Resolution state for a session's requested/effective runtime mode."""

    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"


def _mode_application_status(
    value: object, default: ModeApplicationStatus
) -> ModeApplicationStatus:
    if isinstance(value, ModeApplicationStatus):
        return value
    if isinstance(value, str):
        try:
            return ModeApplicationStatus(value)
        except ValueError:
            pass
    return default


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
ATTEMPT_HISTORY_LIMIT = 16

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
class BackgroundAttempt:
    """Bounded summary of one finished attempt for a background session."""

    attempt: int
    status: SessionStatus
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    failure_category: str = ""
    exit_reason: str = ""
    worker_exit_code: int | None = None
    latest_activity: str = ""
    requested_mode_name: str | None = None
    mode_name: str = ""
    mode_application_status: ModeApplicationStatus = ModeApplicationStatus.PENDING
    mode_application_error: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "BackgroundAttempt":
        attempt = value.get("attempt", 0)
        started_at = value.get("started_at")
        completed_at = value.get("completed_at")
        error = value.get("error")
        exit_code = value.get("worker_exit_code")
        raw_failure_category = value.get("failure_category")
        raw_exit_reason = value.get("exit_reason")
        raw_latest_activity = value.get("latest_activity")
        raw_requested_mode = value.get("requested_mode_name")
        raw_mode_error = value.get("mode_application_error")
        raw_mode_name = value.get("mode_name")
        mode_name = raw_mode_name[:128] if isinstance(raw_mode_name, str) else ""
        mode_status = _mode_application_status(
            value.get("mode_application_status"),
            ModeApplicationStatus.APPLIED if mode_name else ModeApplicationStatus.PENDING,
        )
        if mode_status is not ModeApplicationStatus.APPLIED or not mode_name:
            mode_name = ""
            if mode_status is ModeApplicationStatus.APPLIED:
                mode_status = ModeApplicationStatus.PENDING
        return cls(
            attempt=attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0,
            status=_status(value.get("status")),
            started_at=(
                float(started_at)
                if isinstance(started_at, (int, float)) and not isinstance(started_at, bool)
                else None
            ),
            completed_at=(
                float(completed_at)
                if isinstance(completed_at, (int, float)) and not isinstance(completed_at, bool)
                else None
            ),
            error=error[:2_000] if isinstance(error, str) else None,
            failure_category=raw_failure_category[:128]
            if isinstance(raw_failure_category, str)
            else "",
            exit_reason=raw_exit_reason[:128] if isinstance(raw_exit_reason, str) else "",
            worker_exit_code=(
                exit_code
                if isinstance(exit_code, int) and not isinstance(exit_code, bool)
                else None
            ),
            latest_activity=raw_latest_activity[:512]
            if isinstance(raw_latest_activity, str)
            else "",
            requested_mode_name=raw_requested_mode[:128]
            if isinstance(raw_requested_mode, str)
            else None,
            mode_name=mode_name,
            mode_application_status=mode_status,
            mode_application_error=raw_mode_error[:1_024]
            if isinstance(raw_mode_error, str)
            else "",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "failure_category": self.failure_category,
            "exit_reason": self.exit_reason,
            "worker_exit_code": self.worker_exit_code,
            "latest_activity": self.latest_activity,
            "requested_mode_name": self.requested_mode_name,
            "mode_name": self.mode_name,
            "mode_application_status": self.mode_application_status.value,
            "mode_application_error": self.mode_application_error,
        }

    @classmethod
    def from_session(cls, session: "BackgroundSession") -> "BackgroundAttempt":
        return cls(
            attempt=session.attempt,
            status=session.status,
            started_at=session.attempt_started_at or session.started_at,
            completed_at=session.completed_at,
            error=session.error,
            failure_category=session.failure_category,
            exit_reason=session.exit_reason or session.worker_exit_reason,
            worker_exit_code=session.worker_exit_code,
            latest_activity=session.latest_activity,
            requested_mode_name=session.requested_mode_name,
            mode_name=session.mode_name,
            mode_application_status=session.mode_application_status,
            mode_application_error=session.mode_application_error,
        )


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
    # PRD-205: detached goal-process lifecycle metadata.  ``worker_pid`` is
    # intentionally retained after exit so operators can correlate the run
    # with logs and the launch response; it is not an assertion that the PID
    # is still alive.
    lease_token: str = ""
    attempt: int = 0
    retry_count: int = 0
    labels: tuple[str, ...] = ()
    pinned: bool = False
    artifact_dir: str = ""
    trash_dir: str = ""
    original_artifact_dir: str = ""
    # Parallel orchestration metadata. Empty values preserve the ordinary
    # background-session contract for non-worker jobs.
    parent_session_id: str = ""
    role: str = ""
    task_id: str = ""
    worktree_id: str = ""
    branch: str = ""
    base_commit: str = ""
    # New lifecycle fields are appended so positional construction of the
    # pre-PRD-205 public dataclass remains source-compatible.
    detached_goal: bool = False
    worker_started_at: float | None = None
    worker_finished_at: float | None = None
    worker_exit_code: int | None = None
    worker_exit_reason: str = ""
    worker_finalization_attempts: int = 0
    worker_cleanup_error: str = ""
    run_id: str = ""
    # PRD-208 deletion lifecycle metadata. These fields are appended to keep
    # positional construction of the historical public dataclass compatible.
    delete_operation_id: str = ""
    delete_phase: str = ""
    delete_error: str = ""
    delete_requested_at: float | None = None
    delete_attempt: int = 0
    # Canonical mode used to construct the session runtime. Appended to retain
    # source compatibility for positional construction of older records.
    mode_name: str = ""
    # Attempt-local fields are appended for compatibility with positional
    # construction and legacy event records.
    attempt_started_at: float | None = None
    attempt_history: tuple[BackgroundAttempt, ...] = ()
    heartbeat_stale: bool = False
    # Requested invocation state is kept separate from the canonical effective
    # mode. Application state is scoped to ``mode_application_attempt``.
    requested_mode_name: str | None = None
    mode_application_status: ModeApplicationStatus = ModeApplicationStatus.PENDING
    mode_application_attempt: int = 0
    mode_application_error: str = ""

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
        parent_session_id: str = "",
        role: str = "",
        task_id: str = "",
        worktree_id: str = "",
        branch: str = "",
        base_commit: str = "",
        run_id: str = "",
        detached_goal: bool = False,
        requested_mode_name: str | None = None,
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
            parent_session_id=parent_session_id,
            run_id=run_id,
            role=role,
            detached_goal=detached_goal,
            task_id=task_id,
            worktree_id=worktree_id,
            branch=branch,
            base_commit=base_commit,
            requested_mode_name=(
                requested_mode_name[:128] if isinstance(requested_mode_name, str) else None
            ),
            mode_application_status=ModeApplicationStatus.PENDING,
        )

    def evolve(self, **changes: object) -> "BackgroundSession":
        """Return a copy with validated enum/string values normalized."""

        def _str(name: str, current: str) -> str:
            value = changes.get(name, current)
            return value if isinstance(value, str) else current

        def _bounded_str(name: str, current: str, limit: int) -> str:
            value = changes.get(name, current)
            return value[:limit] if isinstance(value, str) else current

        def _float(name: str, current: float | None) -> float | None:
            value = changes.get(name, current)
            return (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else current
            )

        def _clearable_float(name: str, current: float | None) -> float | None:
            value = changes.get(name, current)
            if value is None:
                return None
            return (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else current
            )

        def _int(name: str, current: int | None) -> int | None:
            value = changes.get(name, current)
            return int(value) if isinstance(value, int) and not isinstance(value, bool) else current

        def _optional_int(name: str, current: int | None) -> int | None:
            value = changes.get(name, current)
            if value is None:
                return None
            return int(value) if isinstance(value, int) and not isinstance(value, bool) else current

        def _optional_str(name: str, current: str | None) -> str | None:
            value = changes.get(name, current)
            return value if isinstance(value, str) or value is None else current

        def _bool(name: str, current: bool) -> bool:
            value = changes.get(name, current)
            return value if isinstance(value, bool) else current

        effective_mode = _bounded_str("mode_name", self.mode_name, 128)
        mode_status = _mode_application_status(
            changes.get("mode_application_status", self.mode_application_status),
            self.mode_application_status,
        )
        if (
            "mode_name" in changes
            and "mode_application_status" not in changes
            and effective_mode != self.mode_name
        ):
            # Changing a name is a request to change runtime state, not proof
            # that the worker resolved and durably applied it.
            mode_status = ModeApplicationStatus.PENDING
        if mode_status is not ModeApplicationStatus.APPLIED:
            effective_mode = ""
        elif not effective_mode:
            # An applied state without a canonical mode is not meaningful.
            mode_status = ModeApplicationStatus.PENDING

        def _optional_bool(name: str, current: bool | None) -> bool | None:
            value = changes.get(name, current)
            return value if isinstance(value, bool) or value is None else current

        raw_history = changes.get("attempt_history", self.attempt_history)
        history_items: list[BackgroundAttempt] = []
        if isinstance(raw_history, (list, tuple)):
            for item in raw_history:
                if isinstance(item, BackgroundAttempt):
                    history_items.append(item)
                elif isinstance(item, Mapping):
                    history_items.append(BackgroundAttempt.from_mapping(item))
        history = tuple(history_items[-ATTEMPT_HISTORY_LIMIT:])

        return BackgroundSession(
            session_id=self.session_id,
            title=_str("title", self.title),
            cwd=_str("cwd", self.cwd),
            workflow_name=_str("workflow_name", self.workflow_name),
            intent=_str("intent", self.intent),
            status=_status(changes.get("status", self.status)),
            created_at=_float("created_at", self.created_at) or 0.0,
            started_at=_clearable_float("started_at", self.started_at),
            last_active=_float("last_active", self.last_active) or 0.0,
            state_changed_at=_float("state_changed_at", self.state_changed_at) or 0.0,
            completed_at=_clearable_float("completed_at", self.completed_at),
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
            worker_pid=_optional_int("worker_pid", self.worker_pid),
            detached_goal=_bool("detached_goal", self.detached_goal),
            worker_started_at=_clearable_float("worker_started_at", self.worker_started_at),
            worker_finished_at=_clearable_float("worker_finished_at", self.worker_finished_at),
            worker_exit_code=_optional_int("worker_exit_code", self.worker_exit_code),
            worker_exit_reason=_str("worker_exit_reason", self.worker_exit_reason),
            worker_finalization_attempts=_int(
                "worker_finalization_attempts", self.worker_finalization_attempts
            )
            or 0,
            worker_cleanup_error=_str("worker_cleanup_error", self.worker_cleanup_error),
            lease_token=_str("lease_token", self.lease_token),
            attempt=_int("attempt", self.attempt) or 0,
            retry_count=_int("retry_count", self.retry_count) or 0,
            labels=_string_tuple(changes.get("labels", self.labels)),
            pinned=_bool("pinned", self.pinned),
            artifact_dir=_str("artifact_dir", self.artifact_dir),
            trash_dir=_str("trash_dir", self.trash_dir),
            original_artifact_dir=_str("original_artifact_dir", self.original_artifact_dir),
            parent_session_id=_str("parent_session_id", self.parent_session_id),
            run_id=_str("run_id", self.run_id),
            role=_str("role", self.role),
            task_id=_str("task_id", self.task_id),
            worktree_id=_str("worktree_id", self.worktree_id),
            branch=_str("branch", self.branch),
            base_commit=_str("base_commit", self.base_commit),
            delete_operation_id=_str("delete_operation_id", self.delete_operation_id),
            delete_phase=_str("delete_phase", self.delete_phase),
            delete_error=_str("delete_error", self.delete_error),
            delete_requested_at=_clearable_float("delete_requested_at", self.delete_requested_at),
            delete_attempt=_int("delete_attempt", self.delete_attempt) or 0,
            mode_name=effective_mode,
            attempt_started_at=_clearable_float("attempt_started_at", self.attempt_started_at),
            attempt_history=history,
            heartbeat_stale=_bool("heartbeat_stale", self.heartbeat_stale),
            requested_mode_name=(
                _bounded_str("requested_mode_name", self.requested_mode_name or "", 128)
                if changes.get("requested_mode_name", self.requested_mode_name) is not None
                else None
            ),
            mode_application_status=mode_status,
            mode_application_attempt=_int("mode_application_attempt", self.mode_application_attempt)
            or 0,
            mode_application_error=_bounded_str(
                "mode_application_error", self.mode_application_error, 1_024
            ),
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
            "detached_goal": self.detached_goal,
            "worker_started_at": self.worker_started_at,
            "worker_finished_at": self.worker_finished_at,
            "worker_exit_code": self.worker_exit_code,
            "worker_exit_reason": self.worker_exit_reason,
            "worker_finalization_attempts": self.worker_finalization_attempts,
            "worker_cleanup_error": self.worker_cleanup_error,
            "lease_token": self.lease_token,
            "attempt": self.attempt,
            "retry_count": self.retry_count,
            "labels": list(self.labels),
            "pinned": self.pinned,
            "artifact_dir": self.artifact_dir,
            "trash_dir": self.trash_dir,
            "original_artifact_dir": self.original_artifact_dir,
            "parent_session_id": self.parent_session_id,
            "run_id": self.run_id,
            "role": self.role,
            "task_id": self.task_id,
            "worktree_id": self.worktree_id,
            "branch": self.branch,
            "base_commit": self.base_commit,
            "delete_operation_id": self.delete_operation_id,
            "delete_phase": self.delete_phase,
            "delete_error": self.delete_error,
            "delete_requested_at": self.delete_requested_at,
            "delete_attempt": self.delete_attempt,
            "mode_name": self.mode_name,
            "attempt_started_at": self.attempt_started_at,
            "attempt_history": [item.to_dict() for item in self.attempt_history],
            "heartbeat_stale": self.heartbeat_stale,
            "requested_mode_name": self.requested_mode_name,
            "mode_application_status": self.mode_application_status.value,
            "mode_application_attempt": self.mode_application_attempt,
            "mode_application_error": self.mode_application_error,
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

        raw_attempt_history = value.get("attempt_history", ())
        attempt_history = (
            tuple(
                BackgroundAttempt.from_mapping(item)
                for item in raw_attempt_history[-ATTEMPT_HISTORY_LIMIT:]
                if isinstance(item, Mapping)
            )
            if isinstance(raw_attempt_history, (list, tuple))
            else ()
        )
        raw_mode_name = value.get("mode_name")
        mode_name = raw_mode_name[:128] if isinstance(raw_mode_name, str) else ""
        raw_requested_mode = value.get("requested_mode_name")
        raw_mode_error = value.get("mode_application_error")
        raw_mode_status = value.get("mode_application_status")
        legacy_mode_status = (
            ModeApplicationStatus.APPLIED if mode_name else ModeApplicationStatus.PENDING
        )
        mode_status = _mode_application_status(raw_mode_status, legacy_mode_status)
        if mode_status is not ModeApplicationStatus.APPLIED:
            mode_name = ""
        elif not mode_name:
            mode_status = ModeApplicationStatus.PENDING
        raw_mode_attempt = value.get("mode_application_attempt")
        mode_attempt = (
            raw_mode_attempt
            if isinstance(raw_mode_attempt, int) and not isinstance(raw_mode_attempt, bool)
            else _int_value(value.get("attempt"), 0)
            if mode_status is ModeApplicationStatus.APPLIED
            else 0
        )

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
            # ``pid`` was used by early run projections; accept it when
            # replaying a legacy background record, while emitting the typed
            # ``worker_pid`` field for new records.
            worker_pid=_int_or_none(value.get("worker_pid", value.get("pid"))),
            detached_goal=bool(value.get("detached_goal", False)),
            worker_started_at=_float_or_none(value.get("worker_started_at")),
            worker_finished_at=_float_or_none(value.get("worker_finished_at")),
            worker_exit_code=_int_or_none(value.get("worker_exit_code")),
            worker_exit_reason=str(value.get("worker_exit_reason", "")),
            worker_finalization_attempts=_int_value(value.get("worker_finalization_attempts"), 0),
            worker_cleanup_error=str(value.get("worker_cleanup_error", "")),
            lease_token=str(value.get("lease_token", "")),
            attempt=_int_value(value.get("attempt"), 0),
            retry_count=_int_value(value.get("retry_count"), 0),
            labels=_string_tuple(value.get("labels")),
            pinned=bool(value.get("pinned", False)),
            artifact_dir=str(value.get("artifact_dir", "")),
            trash_dir=str(value.get("trash_dir", "")),
            original_artifact_dir=str(value.get("original_artifact_dir", "")),
            parent_session_id=str(value.get("parent_session_id", "")),
            run_id=str(value.get("run_id", "")),
            role=str(value.get("role", "")),
            task_id=str(value.get("task_id", "")),
            worktree_id=str(value.get("worktree_id", "")),
            branch=str(value.get("branch", "")),
            base_commit=str(value.get("base_commit", "")),
            delete_operation_id=str(value.get("delete_operation_id", "")),
            delete_phase=str(value.get("delete_phase", "")),
            delete_error=str(value.get("delete_error", "")),
            delete_requested_at=_float_or_none(value.get("delete_requested_at")),
            delete_attempt=_int_value(value.get("delete_attempt"), 0),
            mode_name=mode_name,
            attempt_started_at=_float_or_none(value.get("attempt_started_at")),
            attempt_history=attempt_history,
            heartbeat_stale=bool(value.get("heartbeat_stale", False)),
            requested_mode_name=raw_requested_mode[:128]
            if isinstance(raw_requested_mode, str)
            else None,
            mode_application_status=mode_status,
            mode_application_attempt=mode_attempt,
            mode_application_error=raw_mode_error[:1_024]
            if isinstance(raw_mode_error, str)
            else "",
        )
