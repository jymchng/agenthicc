"""Detached background worker entry point (PRD-141).

Workers are deliberately thin adapters around the existing headless session
and agent-turn runners.  They never implement a second tool or workflow loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import logging
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, TypedDict, cast

from agenthicc.background.model import (
    BackgroundSession,
    ModeApplicationStatus,
    SessionStatus,
)
from agenthicc.background.store import BackgroundStore, InvalidSessionTransition
from agenthicc.cli.context import CLIContext, CLIFlags
from agenthicc.config import DEFAULT_QUESTION_TIMEOUT_S

logger = logging.getLogger(__name__)


WORKER_FINALIZATION_TIMEOUT_S = 30.0
"""Maximum time allowed for owned headless resources to close."""

if TYPE_CHECKING:
    from agenthicc.background.terminals import TerminalManager
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.runners.session_lease import SessionOwnerLease


@dataclass(frozen=True)
class WorkerRequest:
    session_id: str
    workflow_name: str
    intent: str
    cwd: str
    config_path: str | None
    set_overrides: tuple[str, ...]
    dangerously_skip_permissions: bool
    wall_timeout_s: float = 0.0
    max_activity_bytes: int = 64_000
    source: str = "cli"
    set_secret_overrides: tuple[str, ...] = ()
    detached_goal: bool = False
    run_id: str = ""
    mode_name: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "WorkerRequest":
        session_id = value.get("session_id")
        intent = value.get("intent")
        cwd = value.get("cwd")
        if not all(isinstance(item, str) and item for item in (session_id, intent, cwd)):
            raise ValueError("background request requires session_id, intent, and cwd")
        assert isinstance(session_id, str)
        assert isinstance(intent, str)
        assert isinstance(cwd, str)
        raw_overrides = value.get("set_overrides", ())
        overrides = (
            tuple(item for item in raw_overrides if isinstance(item, str))
            if isinstance(raw_overrides, (list, tuple))
            else ()
        )
        raw_secret_overrides = value.get("set_secret_overrides", ())
        secret_overrides = (
            tuple(item for item in raw_secret_overrides if isinstance(item, str))
            if isinstance(raw_secret_overrides, (list, tuple))
            else ()
        )
        config_path = value.get("config_path")
        raw_wall_timeout = value.get("wall_timeout_s", 0.0)
        wall_timeout_s = (
            float(raw_wall_timeout)
            if isinstance(raw_wall_timeout, (int, float)) and not isinstance(raw_wall_timeout, bool)
            else 0.0
        )
        raw_activity_bytes = value.get("max_activity_bytes", 64_000)
        max_activity_bytes = (
            int(raw_activity_bytes)
            if isinstance(raw_activity_bytes, int) and not isinstance(raw_activity_bytes, bool)
            else 64_000
        )
        raw_mode_name = value.get("mode_name")
        if raw_mode_name is not None and not isinstance(raw_mode_name, str):
            raise ValueError("background request mode_name must be a string or null")
        return cls(
            session_id=session_id,
            workflow_name=str(value.get("workflow_name", "")),
            intent=intent,
            cwd=cwd,
            config_path=config_path if isinstance(config_path, str) else None,
            set_overrides=overrides,
            dangerously_skip_permissions=bool(value.get("dangerously_skip_permissions", False)),
            set_secret_overrides=secret_overrides,
            wall_timeout_s=wall_timeout_s,
            max_activity_bytes=max_activity_bytes,
            source=str(value.get("source", "cli")),
            detached_goal=bool(value.get("detached_goal", False)),
            run_id=str(value.get("run_id", "")),
            mode_name=raw_mode_name[:128] if isinstance(raw_mode_name, str) else None,
        )


def _request_attribute(value: object, name: str, default: object = None) -> object:
    """Read optional adapter fields without requiring a concrete request type."""
    try:
        return object.__getattribute__(value, name)
    except AttributeError:
        return default


def _session_question_timeout(session: object) -> float:
    value = _request_attribute(
        _request_attribute(_request_attribute(session, "cfg", None), "tools", None),
        "question_timeout_s",
        DEFAULT_QUESTION_TIMEOUT_S,
    )
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else DEFAULT_QUESTION_TIMEOUT_S
    )


@dataclass(frozen=True)
class _WorkerOutcome:
    """The result which the worker finalizer persists exactly once."""

    status: SessionStatus
    error: str | None
    activity: str
    phase_history: tuple[str, ...] = ()
    exit_reason: str = "worker_failed"
    failure_category: str = ""
    exit_code: int = 1


class _FinalizationChanges(TypedDict, total=False):
    """Keyword fields accepted by ``BackgroundStore`` final transitions."""

    error: str | None
    latest_activity: str
    worker_pid: int
    worker_started_at: float
    worker_finished_at: float
    worker_exit_code: int
    worker_exit_reason: str
    worker_finalization_attempts: int
    worker_cleanup_error: str
    exit_reason: str
    failure_category: str
    lease_token: str
    current_phase: str
    phase_history: tuple[str, ...]
    mode_application_status: ModeApplicationStatus
    mode_application_error: str


def _signal_value(value: object, default: object = None) -> object:
    """Return a reactive signal's value, or a plain attribute value."""

    return value() if callable(value) else (value if value is not None else default)


def _name(value: object) -> str:
    raw = getattr(value, "name", value)
    return str(raw).upper()


def _confirmed_idle_after_thinking(session: object) -> bool:
    """Return whether the canonical session state describes terminal idle.

    This intentionally does not inspect rendered TUI output. A completed
    turn plus a canonical ``IDLE`` signal is the smallest reliable evidence
    available to a headless worker; pending waits, tools, compaction, child
    workers, and active workflows veto the result. The helper is kept pure
    with respect to the session so it is straightforward to exercise with
    deterministic fakes.
    """

    app_state = getattr(session, "app_state", None)
    conversation = getattr(app_state, "conversation", None)
    if conversation is None:
        return False
    if _name(_signal_value(getattr(conversation, "agent_state", None), "")) != "IDLE":
        return False
    if _signal_value(getattr(conversation, "active_tool", None), ""):
        return False
    if _signal_value(getattr(conversation, "compaction_active", None), False) is True:
        return False
    pool = _signal_value(getattr(conversation, "subagent_pool_state", None), None)
    if pool is not None:
        pool_status = _name(_signal_value(getattr(pool, "status", None), ""))
        raw_workers = getattr(pool, "workers", ())
        workers = raw_workers if isinstance(raw_workers, (list, tuple)) else ()
        active_workers = any(
            _name(getattr(worker, "status", "")) not in {"DONE", "FAILED", "CANCELLED"}
            for worker in workers
        )
        raw_total = getattr(pool, "total", 0)
        raw_done = getattr(pool, "done", 0)
        total = raw_total if isinstance(raw_total, int) else 0
        done = raw_done if isinstance(raw_done, int) else 0
        if (
            pool_status not in {"", "DONE", "COMPLETED", "FAILED", "CANCELLED"}
            or active_workers
            or total > done
        ):
            return False

    for name in (
        "pending_approval",
        "pending_question",
        "continuation_pending",
        "queued_user_input",
        "pending_user_input",
    ):
        candidate = _signal_value(getattr(app_state, name, None), None)
        if candidate not in (None, False, "", (), [], {}):
            return False

    workflow_run = _signal_value(getattr(app_state, "workflow_run", None), None)
    if workflow_run is not None:
        workflow_status = _name(_signal_value(getattr(workflow_run, "status", None), ""))
        if workflow_status not in {"", "COMPLETE", "COMPLETED", "FAILED", "EXITED"}:
            return False

    turns = _signal_value(getattr(conversation, "turns", None), ())
    if not isinstance(turns, (list, tuple)) or not turns:
        return False
    # A closed COMPLETE/ERROR turn is durable evidence that the worker was
    # active. Historical turns are acceptable here because this helper is
    # called only after the current worker operation has returned.
    return any(_name(getattr(turn, "state", "")) in {"COMPLETE", "ERROR"} for turn in turns)


def _outcome_changes(
    current: object,
    outcome: _WorkerOutcome,
    *,
    lease_token: str,
    worker_pid: int,
    finalization_attempts: int,
    cleanup_error: str = "",
) -> _FinalizationChanges:
    """Build the bounded durable fields shared by every finalization path."""

    current_phase = getattr(current, "current_phase", "")
    phase_history = getattr(current, "phase_history", ())
    combined_phases = tuple(phase_history) + outcome.phase_history
    changes: _FinalizationChanges = {
        "error": outcome.error,
        "latest_activity": outcome.activity,
        "worker_pid": worker_pid,
        "worker_started_at": getattr(current, "worker_started_at", None) or time.time(),
        "worker_finished_at": time.time(),
        "worker_exit_code": outcome.exit_code,
        "worker_exit_reason": outcome.exit_reason,
        "worker_finalization_attempts": finalization_attempts,
        "worker_cleanup_error": cleanup_error[:2_000],
        "exit_reason": outcome.activity,
        "failure_category": outcome.failure_category,
        "lease_token": "",
        "current_phase": outcome.phase_history[-1] if outcome.phase_history else current_phase,
        "phase_history": combined_phases[-64:],
    }
    # The lease argument is consumed by the caller's expected lease check;
    # naming it here documents that finalization belongs to the process that
    # claimed the session.
    _ = lease_token
    return changes


def _finalize_worker(
    store: BackgroundStore,
    request: WorkerRequest,
    *,
    lease_token: str,
    expected_attempt: int,
    outcome: _WorkerOutcome,
    worker_pid: int,
    cleanup_error: str = "",
) -> None:
    """Persist a worker outcome and an audit event idempotently.

    Normal worker termination is simply the return from ``run_worker``. No
    launcher or parent process is signalled. This makes the detached-only
    contract explicit while retaining the safer and portable process-exit
    path supplied by the module entry point.
    """

    try:
        current = store.get(request.session_id, include_deleted=True)
        if (
            current.status
            in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
            }
            and current.worker_finalization_attempts > 0
        ):
            return
        if current.attempt != expected_attempt or current.lease_token != lease_token:
            return
        attempts = current.worker_finalization_attempts + 1
        changes = _outcome_changes(
            current,
            outcome,
            lease_token=lease_token,
            worker_pid=current.worker_pid or worker_pid,
            finalization_attempts=attempts,
            cleanup_error=cleanup_error,
        )
        if (
            outcome.status is SessionStatus.FAILED
            and current.mode_application_status is ModeApplicationStatus.PENDING
            and current.mode_application_attempt == expected_attempt
        ):
            from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415

            raw_mode_error = outcome.error or "Mode resolution failed"
            safe_mode_error = str(_Redactor().value(raw_mode_error, "mode_application_error"))
            changes["mode_application_status"] = ModeApplicationStatus.FAILED
            changes["mode_application_error"] = safe_mode_error[:1_024]
        if current.status == SessionStatus.CANCELLING:
            # A supervisor cancellation may win while the provider call is
            # unwinding. Complete that durable transition without allowing a
            # successful provider result to resurrect the run.
            outcome = _WorkerOutcome(
                status=SessionStatus.CANCELLED,
                error=current.cancellation_reason or "Worker cancelled",
                activity="Worker cancelled",
                exit_reason="cancelled",
                failure_category="cancelled",
                exit_code=130,
            )
            changes = _outcome_changes(
                current,
                outcome,
                lease_token=lease_token,
                worker_pid=current.worker_pid or worker_pid,
                finalization_attempts=attempts,
                cleanup_error=cleanup_error,
            )
            store.transition(
                request.session_id,
                SessionStatus.CANCELLED,
                expected_status=SessionStatus.CANCELLING,
                expected_attempt=expected_attempt,
                expected_lease_token=lease_token,
                **changes,
            )
        elif current.status == SessionStatus.RUNNING and outcome.status == SessionStatus.CANCELLED:
            current = store.transition(
                request.session_id,
                SessionStatus.CANCELLING,
                expected_status=SessionStatus.RUNNING,
                expected_lease_token=lease_token,
                expected_attempt=expected_attempt,
                cancellation_reason=outcome.error or "Worker cancelled",
                latest_activity=outcome.activity,
            )
            changes = _outcome_changes(
                current,
                outcome,
                lease_token=lease_token,
                worker_pid=current.worker_pid or worker_pid,
                finalization_attempts=attempts,
                cleanup_error=cleanup_error,
            )
            store.transition(
                request.session_id,
                SessionStatus.CANCELLED,
                expected_status=SessionStatus.CANCELLING,
                expected_attempt=expected_attempt,
                expected_lease_token=lease_token,
                **changes,
            )
        elif current.status == SessionStatus.RUNNING:
            store.transition(
                request.session_id,
                outcome.status,
                expected_status=SessionStatus.RUNNING,
                expected_lease_token=lease_token,
                expected_attempt=expected_attempt,
                **changes,
            )
        elif current.status == outcome.status:
            # A duplicate callback must not turn a terminal result into a
            # failure. It may only add missing final metadata.
            store.update(
                request.session_id,
                expected_attempt=expected_attempt,
                expected_lease_token=lease_token,
                **changes,
            )
        else:
            return
        try:
            final_session = store.get(request.session_id, include_deleted=True)
            _persist_goal_run_outcome(
                request,
                final_session,
                outcome,
                expected_attempt=expected_attempt,
            )
        except (KeyError, RuntimeError, ValueError):
            # The background session is authoritative. A missing/legacy run
            # registry must not turn a durable session completion into a
            # worker failure.
            pass
        try:
            store.record_worker_exit(
                request.session_id,
                worker_pid=current.worker_pid or worker_pid,
                exit_reason=outcome.exit_reason,
                exit_code=outcome.exit_code,
                cleanup_error=cleanup_error,
                expected_attempt=expected_attempt,
            )
        except Exception:  # noqa: BLE001 - audit failure must not mask result
            return
    except (KeyError, InvalidSessionTransition):
        # Cancellation/recovery may have won the race. The authoritative
        # state must remain untouched rather than being resurrected.
        return


def _persist_goal_run_outcome(
    request: WorkerRequest,
    session: BackgroundSession,
    outcome: _WorkerOutcome,
    *,
    expected_attempt: int,
) -> None:
    """Project terminal worker metadata into the product-level run store."""

    if not request.run_id:
        return
    from agenthicc.runs.model import GoalRunStatus, RunAgentRecord  # noqa: PLC0415
    from agenthicc.runs.store import RunNotFound, RunStore  # noqa: PLC0415

    status = {
        SessionStatus.COMPLETED: GoalRunStatus.COMPLETED,
        SessionStatus.FAILED: GoalRunStatus.FAILED,
        SessionStatus.CANCELLED: GoalRunStatus.CANCELLED,
    }.get(outcome.status)
    if (
        status is None
        or session.attempt != expected_attempt
        or session.status is not outcome.status
    ):
        return
    run_store = RunStore()
    try:
        run_store.update(
            request.run_id,
            status=status,
            completed_at=session.completed_at,
            exit_code=outcome.exit_code,
            result_summary=(session.latest_activity if status is GoalRunStatus.COMPLETED else ""),
            failure_reason=session.error or "",
            worker_pid=session.worker_pid,
            worker_started_at=session.worker_started_at,
            worker_finished_at=session.worker_finished_at,
            worker_exit_code=session.worker_exit_code,
            worker_exit_reason=session.worker_exit_reason,
            worker_finalization_attempts=session.worker_finalization_attempts,
            worker_cleanup_error=session.worker_cleanup_error,
        )
    except RunNotFound:
        return
    run_store.link_agent(
        request.run_id,
        RunAgentRecord(
            agent_id=request.session_id,
            run_id=request.run_id,
            role=session.role or "main",
            session_id=request.session_id,
            process_id=session.worker_pid,
            status=session.status.value,
            started_at=session.started_at,
            completed_at=session.completed_at,
            last_heartbeat_at=session.last_active,
            last_activity_at=session.last_active,
            current_phase=session.current_phase,
            current_operation=session.latest_activity,
            attention_reason=session.error or "",
            exit_code=session.worker_exit_code,
            exit_reason=session.worker_exit_reason,
            attempt=session.attempt,
            requested_mode_name=session.requested_mode_name,
            mode_name=session.mode_name,
            mode_application_status=session.mode_application_status.value,
            mode_application_error=session.mode_application_error,
        ),
    )


class BackgroundApprovalService:
    """Approval adapter that waits for an explicit manager decision."""

    def __init__(
        self,
        store: BackgroundStore,
        session_id: str,
        question_timeout_s: float = DEFAULT_QUESTION_TIMEOUT_S,
        conversation_store: object | None = None,
    ) -> None:
        if (
            isinstance(question_timeout_s, bool)
            or not isinstance(question_timeout_s, (int, float))
            or not math.isfinite(float(question_timeout_s))
            or float(question_timeout_s) <= 0
        ):
            raise ValueError("tools.question_timeout_s must be a finite number greater than zero")
        self.store = store
        self.session_id = session_id
        self.question_timeout_s = float(question_timeout_s)
        self.conversation_store = conversation_store

    def _emit_question_event(
        self,
        kind: str,
        req: object,
        *,
        outcome: str,
        timeout_s: float,
        deadline_at: float,
    ) -> None:
        conversation = self.conversation_store
        append_event = _request_attribute(conversation, "append_event")
        if not callable(append_event):
            return
        raw_questions = _request_attribute(req, "tool_input", {})
        raw_questions = raw_questions.get("questions") if isinstance(raw_questions, dict) else None
        request_id = str(_request_attribute(req, "request_id", "") or "")
        fingerprint = str(_request_attribute(req, "question_fingerprint", "") or "")
        payload: dict[str, object] = {
            "request_id": request_id,
            "outcome": outcome,
            "timeout_s": timeout_s,
            "deadline_at": deadline_at,
            "question_count": min(len(raw_questions), 32) if isinstance(raw_questions, list) else 0,
            "question_fingerprint": fingerprint,
        }
        try:
            append_event(
                kind=kind,
                payload=payload,
                event_id=f"question:{request_id}:{kind}",
            )
        except Exception:  # noqa: BLE001
            return

    async def request_approval(self, req: object) -> object:
        from agenthicc.tools.approval import ApprovalResponse  # noqa: PLC0415

        if getattr(req, "kind", "tool") == "questions":
            return await self.request_input(req)

        description = str(getattr(req, "tool_name", "approval request"))[:120]
        try:
            current = self.store.get(self.session_id)
            self.store.transition(
                self.session_id,
                SessionStatus.WAITING_APPROVAL,
                expected_status=current.status,
                approval_request=description,
                approval_decision=None,
                latest_activity=f"Waiting for approval: {description}",
            )
        except (KeyError, InvalidSessionTransition):
            return ApprovalResponse(allowed=False, message="background session is no longer active")
        while True:
            await asyncio.sleep(0.2)
            try:
                current = self.store.get(self.session_id, include_deleted=True)
            except KeyError:
                return ApprovalResponse(allowed=False, message="background session was removed")
            if current.status in {
                SessionStatus.CANCELLING,
                SessionStatus.CANCELLED,
                SessionStatus.DELETED,
            }:
                return ApprovalResponse(allowed=False, message="background session was cancelled")
            if current.approval_decision is None:
                continue
            allowed = current.approval_decision
            try:
                self.store.transition(
                    self.session_id,
                    SessionStatus.RUNNING,
                    expected_status=SessionStatus.WAITING_APPROVAL,
                    approval_request="",
                    approval_decision=None,
                    latest_activity="Approval granted" if allowed else "Approval denied",
                )
            except InvalidSessionTransition:
                return ApprovalResponse(allowed=False, message="approval state changed")
            return ApprovalResponse(allowed=allowed)

    async def request_input(self, req: object) -> object:
        """Wait for explicit input to a workflow ``ask_user`` request."""

        from agenthicc.tools.approval import ApprovalResponse  # noqa: PLC0415

        description = str(getattr(req, "tool_name", "input requested"))[:120]
        try:
            timeout_s = object.__getattribute__(req, "timeout_s")
        except AttributeError:
            timeout_s = None
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
            timeout_s = self.question_timeout_s
        deadline = asyncio.get_running_loop().time() + float(timeout_s)
        deadline_wall = time.time() + float(timeout_s)
        request_id = str(
            _request_attribute(req, "request_id", "")
            or _request_attribute(req, "tool_use_id", "")
            or uuid.uuid4().hex
        )
        # Background sessions do not publish through ApprovalService, so they
        # enrich the shared request object themselves.  This keeps the
        # response/event identity stable for make_questions_tool and late-input
        # diagnostics.
        try:
            object.__setattr__(req, "request_id", request_id)
            object.__setattr__(req, "timeout_s", float(timeout_s))
            object.__setattr__(req, "deadline_at", deadline_wall)
        except (AttributeError, TypeError):
            pass
        try:
            current = self.store.get(self.session_id)
            self.store.transition(
                self.session_id,
                SessionStatus.WAITING_INPUT,
                expected_status=current.status,
                input_request=description,
                input_value=None,
                latest_activity=f"Waiting for input: {description}",
            )
        except (KeyError, InvalidSessionTransition):
            self._emit_question_event(
                "question_wait_failed",
                req,
                outcome="failed",
                timeout_s=float(timeout_s),
                deadline_at=deadline_wall,
            )
            return ApprovalResponse(
                allowed=False,
                message="background session is no longer active",
                outcome="failed",
                request_id=request_id,
                timeout_s=float(timeout_s),
            )
        self._emit_question_event(
            "question_wait_started",
            req,
            outcome="pending",
            timeout_s=float(timeout_s),
            deadline_at=deadline_wall,
        )
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                try:
                    self.store.transition(
                        self.session_id,
                        SessionStatus.RUNNING,
                        expected_status=SessionStatus.WAITING_INPUT,
                        input_request="",
                        input_value=None,
                        latest_activity="Input timed out; agent fallback required",
                    )
                except InvalidSessionTransition:
                    pass
                self._emit_question_event(
                    "question_timed_out",
                    req,
                    outcome="timed_out",
                    timeout_s=float(timeout_s),
                    deadline_at=deadline_wall,
                )
                return ApprovalResponse(
                    allowed=False,
                    message=(
                        "The user did not answer before the configured deadline. "
                        "Choose the safest reasonable option, state the assumption, "
                        "and do not ask the same question again solely because it timed out."
                    ),
                    outcome="timed_out",
                    timed_out=True,
                    decision_required=True,
                    request_id=str(_request_attribute(req, "request_id", "") or ""),
                    timeout_s=float(timeout_s),
                )
            await asyncio.sleep(min(0.2, remaining))
            try:
                current = self.store.get(self.session_id, include_deleted=True)
            except KeyError:
                return ApprovalResponse(
                    allowed=False,
                    message="background session was removed",
                    outcome="failed",
                    request_id=request_id,
                    timeout_s=float(timeout_s),
                )
            if current.status in {
                SessionStatus.CANCELLING,
                SessionStatus.CANCELLED,
                SessionStatus.DELETED,
            }:
                self._emit_question_event(
                    "question_cancelled",
                    req,
                    outcome="cancelled",
                    timeout_s=float(timeout_s),
                    deadline_at=deadline_wall,
                )
                return ApprovalResponse(
                    allowed=False,
                    message="background session was cancelled",
                    outcome="cancelled",
                    request_id=request_id,
                    timeout_s=float(timeout_s),
                )
            if current.input_value is None:
                continue
            answer = current.input_value
            try:
                self.store.transition(
                    self.session_id,
                    SessionStatus.RUNNING,
                    expected_status=SessionStatus.WAITING_INPUT,
                    input_request="",
                    input_value=None,
                    latest_activity="Input accepted",
                )
            except InvalidSessionTransition:
                self._emit_question_event(
                    "question_wait_failed",
                    req,
                    outcome="failed",
                    timeout_s=float(timeout_s),
                    deadline_at=deadline_wall,
                )
                return ApprovalResponse(
                    allowed=False,
                    message="input state changed",
                    outcome="failed",
                    request_id=request_id,
                    timeout_s=float(timeout_s),
                )
            self._emit_question_event(
                "question_answered",
                req,
                outcome="answered",
                timeout_s=float(timeout_s),
                deadline_at=deadline_wall,
            )
            return ApprovalResponse(
                allowed=True,
                message=answer,
                outcome="answered",
                request_id=request_id,
                timeout_s=float(timeout_s),
            )

    def respond(self, allowed: bool, **kwargs: object) -> None:
        self.store.update(self.session_id, approval_decision=allowed)

    def provide_input(self, value: str) -> None:
        """Deliver input for callers that hold the worker-side adapter."""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("Input must not be empty")
        current = self.store.get(self.session_id)
        if current.status != SessionStatus.WAITING_INPUT:
            raise InvalidSessionTransition("Session is not waiting for input")
        self.store.update(self.session_id, input_value=value[:8_000])

    def reset_turn_memory(self) -> None:
        return None


class BackgroundInputService(BackgroundApprovalService):
    """Named input boundary for integrations that do not need approvals."""

    async def request_input(self, req: object) -> object:
        return await super().request_input(req)


def _load_request(path: Path) -> WorkerRequest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("background request must be a JSON object")
    return WorkerRequest.from_mapping(raw)


async def _run_direct_turn(session: object, request: WorkerRequest) -> None:
    """Run a direct turn through the canonical agent-turn runner."""

    from agenthicc.runners.agent_turn import _run_agent_turn  # noqa: PLC0415

    agent_runner = getattr(session, "agent_runner", None)
    if agent_runner is None:
        raise RuntimeError("No LLM configured for background session")
    app_state = getattr(session, "app_state")
    cfg = getattr(session, "cfg")
    await _run_agent_turn(
        request.intent,
        agent_runner,
        getattr(session, "processor"),
        session_memory=getattr(session, "session_memory"),
        conversation_id=str(getattr(session, "session_id", "")),
        max_agent_turns=cfg.execution.max_agent_turns,
        conv_store=app_state.conversation,
        app_state=app_state,
        exec_cfg=cfg.execution,
        skills=getattr(session, "skills"),
        skill_permissions=cfg.agents.skill_permissions_for("default"),
        mention_cache=getattr(session, "mention_cache"),
        project_plugin_tools=list(getattr(getattr(session, "project_plugins"), "all_tools", ())),
        mcp_registry=getattr(session, "mcp_registry"),
        active_agent="default",
        completed_turns=0,
        approval_svc=getattr(session, "approval_svc"),
        workspace_access=vars(session).get("workspace_access"),
        memory_router=getattr(session, "memory_router"),
        semantic_index=getattr(session, "semantic_index"),
        browser_manager=getattr(session, "browser_manager", None),
    )


def _dispatch_background_command(
    session: SessionContext,
    text: str,
    *,
    message_id: str,
) -> tuple[str, str | None]:
    """Dispatch one slash/skill command in its owning session context.

    The manager process never executes commands. The worker uses its own
    command and skill registries and runtime state. Commands that require an
    interactive overlay or the TUI's workflow-control surface fail explicitly
    instead of being sent to the LLM as ordinary prose.

    Returns ``(error, skill_body)``; an empty error means the command was
    handled. Captured command output is projected into the target transcript.
    """
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    from agenthicc.commands import CommandContext, CommandDispatcher  # noqa: PLC0415

    normalized = text.strip()
    token = normalized.split(None, 1)[0] if normalized else ""
    if token == "/workflow":
        return (
            "/workflow requires the foreground session UI; attach to preserve its recovery controls.",
            None,
        )
    registry = session.cmd_registry
    command = registry.get(token)
    if command is None:
        if token.startswith("$"):
            return ("", None)  # Unknown $text is ordinary user text in the TUI.
        return (f"Unknown command {token!r}; the command was not sent to the model.", None)
    if command.is_skill != token.startswith("$"):
        return (f"Command namespace mismatch for {token!r}.", None)
    if command.menu_factory is not None:
        return (f"{token} opens an interactive menu; attach to the session to use it.", None)

    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    skill_bodies: list[str] = []
    context = CommandContext(
        text=normalized,
        args=" ".join(normalized.split()[1:]),
        model=session.model_label,
        console=console,
        config=session.cfg,
        session_id=session.session_id,
        skills=session.skills,
        command_registry=registry,
        workflow_registry=session.workflow_registry,
        mode_manager=session.mode_manager,
        terminal_manager=session.terminal_manager,
        set_pending_skill=skill_bodies.append,
    )
    try:
        handled = CommandDispatcher(registry).dispatch(normalized, context)
    except Exception as exc:  # noqa: BLE001
        from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415

        safe_error = _Redactor().value(f"{type(exc).__name__}: {exc}", "message")
        return (f"{token} failed: {str(safe_error)[:200]}", None)
    if not handled:
        return (f"{token} could not be handled by the owning session.", None)
    rendered = output.getvalue().strip()
    if rendered:
        from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415

        safe_output = str(_Redactor().value(rendered, "message"))[:8_000]
        session.app_state.conversation.append_event(
            "assistant_message",
            {"text": safe_output, "source": "agents-manager-command"},
            event_id=f"background-command-output:{message_id}",
        )
    return ("", skill_bodies[-1] if skill_bodies else None)


async def _compact_background_session(session: SessionContext) -> None:
    """Run the same bounded compactor used by the owning TUI session."""
    from agenthicc.memory.compactor import compact_memory  # noqa: PLC0415

    memory = session.session_memory
    if not memory._messages:
        raise ValueError("There is no session memory to compact")
    transport = cast("object", object.__getattribute__(session.agent_runner, "_transport"))
    if transport is None:
        raise RuntimeError("No provider transport is available for compaction")
    execution = session.cfg.execution
    await compact_memory(
        memory,
        transport,
        model=execution.effective_model(),
        conv_store=session.app_state.conversation,
        max_input_tokens=execution.effective_context_window(),
        usage_ledger=session.usage_ledger,
        session_id=session.session_id,
        run_id=f"compaction:{session.session_id}:{uuid.uuid4().hex[:12]}",
        max_completion_tokens=execution.max_completion_tokens,
        request_options=execution.request_options,
    )


async def run_worker(request: WorkerRequest, store: BackgroundStore) -> int:
    """Claim, execute, and finalize one background session."""

    lease = uuid.uuid4().hex
    session = None
    claimed = False
    outcome: _WorkerOutcome | None = None
    owner_lease: SessionOwnerLease | None = None
    claimed_attempt: int | None = None
    processor_task: asyncio.Task[object] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    terminal_token: contextvars.Token[TerminalManager | None] | None = None
    heartbeat_stop = asyncio.Event()
    input_inbox: object | None = None
    delivered_input_ids: set[str] = set()
    completed_command_ids: set[str] = set()
    original_cwd = os.getcwd()
    try:
        os.chdir(request.cwd)
        claimed_session = store.claim(request.session_id, pid=os.getpid(), lease_token=lease)
        if claimed_session.status != SessionStatus.RUNNING:
            raise RuntimeError(f"Worker could not claim session: {claimed_session.status.value}")
        claimed = True
        claimed_attempt = claimed_session.attempt

        async def _heartbeat() -> None:
            while not heartbeat_stop.is_set():
                await asyncio.sleep(1.0)
                if heartbeat_stop.is_set():
                    return
                try:
                    store.heartbeat(
                        request.session_id,
                        lease_token=lease,
                        attempt=claimed_attempt,
                        activity="Worker active",
                    )
                except (KeyError, InvalidSessionTransition):
                    return

        heartbeat_task = asyncio.create_task(_heartbeat(), name="background-heartbeat")
        from agenthicc.runners.headless import (  # noqa: PLC0415
            _HeadlessApprovalService,
            _close_headless_session,
            execute_workflow,
            _select_headless_workflow_resume,
        )
        from agenthicc.runners.tui_session import _build_session_context  # noqa: PLC0415
        from agenthicc.runners.session_lease import SessionOpenCoordinator  # noqa: PLC0415
        from agenthicc.background.input_inbox import BackgroundInputInbox  # noqa: PLC0415
        from agenthicc.runners.agent_turn_context import (  # noqa: PLC0415
            QueuedMessageSource,
            bind_queued_message_source,
        )

        ctx = CLIContext(
            resume_id=request.session_id,
            mode_name=request.mode_name,
            config_path=request.config_path,
            set_overrides=request.set_overrides,
            set_secret_overrides=request.set_secret_overrides,
            flags=CLIFlags(dangerously_skip_permissions=request.dangerously_skip_permissions),
        )
        # Claim the durable session before creating metadata or opening any
        # journal.  The background registry lease and the session owner lease
        # are separate layers: both must be held before execution.
        owner_lease = SessionOpenCoordinator().acquire(
            request.session_id,
            entrypoint="background",
            require_existing=False,
        )
        # A CLI-created job may be the first durable record for this session.
        # Register it only when no foreground metadata exists; resume must not
        # reset the original session's timestamps.
        from agenthicc.tui.runtime.session_log import register_session  # noqa: PLC0415

        metadata_path = (
            Path.home() / ".agenthicc" / "sessions" / request.session_id / "metadata.json"
        )
        if not metadata_path.exists():
            register_session(request.session_id, request.cwd, "")
        session = await _build_session_context(
            request.session_id,
            list(request.set_overrides),
            config_path=request.config_path,
            cli_secret_overrides=list(request.set_secret_overrides),
            headless=True,
            mode_name=request.mode_name,
            owner_lease=owner_lease,
        )
        effective_mode = session.mode_manager.active_name
        if not effective_mode:
            raise RuntimeError("Session initialization produced no effective runtime mode")
        if request.mode_name is not None:
            requested_effective_mode = session.mode_manager.resolve_name(request.mode_name)
            if requested_effective_mode != effective_mode:
                raise RuntimeError(
                    "Requested mode was not active after session construction: "
                    f"requested {requested_effective_mode!r}, got {effective_mode!r}"
                )
        store.record_mode_application(
            request.session_id,
            requested_mode_name=request.mode_name,
            effective_mode_name=effective_mode,
            attempt=claimed_attempt,
            lease_token=lease,
        )
        session.app_state.cli_flags = ctx.flags
        from agenthicc.background.terminals import set_current_terminal_manager  # noqa: PLC0415

        terminal_manager = getattr(session, "terminal_manager", None)
        if terminal_manager is not None:
            terminal_token = set_current_terminal_manager(terminal_manager)
        setattr(
            session,
            "approval_svc",
            _HeadlessApprovalService(request.dangerously_skip_permissions)
            if request.dangerously_skip_permissions
            else BackgroundApprovalService(
                store,
                request.session_id,
                question_timeout_s=_session_question_timeout(session),
                conversation_store=_request_attribute(
                    _request_attribute(session, "app_state", None), "conversation", None
                ),
            ),
        )
        workspace_access = getattr(session, "workspace_access", None)
        if workspace_access is not None:
            workspace_access.set_approval_service(session.approval_svc)
        processor_task = asyncio.create_task(session.processor.run(), name="background-processor")
        await asyncio.sleep(0)

        input_inbox = BackgroundInputInbox(store)

        def _record_input_delivery(item: object, *, starts_turn: bool) -> None:
            message_id = str(_request_attribute(item, "message_id", ""))
            text = str(_request_attribute(item, "text", ""))
            if not message_id:
                return
            delivered_input_ids.add(message_id)
            session.app_state.conversation.append_event(
                "user_message",
                {"text": text, "source": "agents-manager"},
                event_id=message_id,
            )
            try:
                store.heartbeat(
                    request.session_id,
                    lease_token=lease,
                    attempt=claimed_attempt,
                    activity=f"Input delivered · {message_id[:12]}",
                )
            except (AttributeError, InvalidSessionTransition, KeyError):
                pass
            service = _request_attribute(session, "session_service")
            publish = _request_attribute(service, "publish")
            if starts_turn and callable(publish):

                async def _publish_input_event() -> None:
                    try:
                        await publish(
                            request.session_id,
                            source="agents-manager",
                            kind="turn_queued",
                            payload={"text": text, "client_id": "agents-manager"},
                            turn_id=message_id,
                        )
                    except Exception:  # noqa: BLE001
                        logger.debug("could not publish background input event", exc_info=True)

                asyncio.get_running_loop().create_task(_publish_input_event())

        def _claim_session_input() -> str | None:
            pending = input_inbox.peek_next(
                request.session_id,
                owner_attempt=claimed_attempt or 0,
                lease_token=lease,
            )
            if pending is None:
                return None
            if pending.text.startswith(("/", "$")):
                from agenthicc.commands.busy_policy import classify_busy_command  # noqa: PLC0415

                registry = session.cmd_registry
                decision = classify_busy_command(pending.text, registry)
                if decision.policy.value == "queue":
                    return None
                if decision.policy.value == "reject":
                    item = input_inbox.claim_next(
                        request.session_id,
                        owner_attempt=claimed_attempt or 0,
                        lease_token=lease,
                    )
                    if item is None:
                        return None
                    input_inbox.reject(
                        request.session_id,
                        item.message_id,
                        owner_attempt=claimed_attempt or 0,
                        lease_token=lease,
                        reason="This command is not allowed while the worker is busy.",
                    )
                    session.app_state.conversation.append_event(
                        "user_message",
                        {"text": item.text, "source": "agents-manager"},
                        event_id=item.message_id,
                    )
                    session.app_state.conversation.append_event(
                        "assistant_message",
                        {
                            "text": "This command is not allowed while the worker is busy.",
                            "source": "agents-manager-command",
                        },
                        event_id=f"background-command-error:{item.message_id}",
                    )
                    return None
            item = input_inbox.claim_next(
                request.session_id,
                owner_attempt=claimed_attempt or 0,
                lease_token=lease,
            )
            if item is None:
                return None
            if item.text.startswith(("/", "$")):
                _record_input_delivery(item, starts_turn=False)
                error, skill_body = _dispatch_background_command(
                    session,
                    item.text,
                    message_id=item.message_id,
                )
                if skill_body is not None:
                    error = "Skill execution waits for the current turn to finish."
                if error:
                    input_inbox.reject(
                        request.session_id,
                        item.message_id,
                        owner_attempt=claimed_attempt or 0,
                        lease_token=lease,
                        reason=error,
                    )
                    delivered_input_ids.discard(item.message_id)
                    session.app_state.conversation.append_event(
                        "assistant_message",
                        {"text": error, "source": "agents-manager-command"},
                        event_id=f"background-command-error:{item.message_id}",
                    )
                    return None
                if skill_body is None:
                    completed_command_ids.add(item.message_id)
                return None
            # Provider memory receives the same text immediately after this
            # callback returns, at the runner's safe tool boundary.
            _record_input_delivery(item, starts_turn=True)
            return item.text

        async def _drain_idle_inputs() -> None:
            """Route messages left after the last active turn boundary."""
            from dataclasses import replace as dataclass_replace  # noqa: PLC0415

            while True:
                pending = input_inbox.peek_next(
                    request.session_id,
                    owner_attempt=claimed_attempt or 0,
                    lease_token=lease,
                )
                if pending is None:
                    return
                item = input_inbox.claim_next(
                    request.session_id,
                    owner_attempt=claimed_attempt or 0,
                    lease_token=lease,
                )
                if item is None:
                    return
                token = item.text.split(None, 1)[0] if item.text.strip() else ""
                registry = session.cmd_registry
                is_registered_skill = bool(
                    token.startswith("$")
                    and registry is not None
                    and registry.get(token) is not None
                )
                if item.text.startswith("/") or is_registered_skill:
                    _record_input_delivery(item, starts_turn=False)
                    if token == "/compact":
                        try:
                            await _compact_background_session(session)
                        except Exception as exc:  # noqa: BLE001
                            from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415

                            error = str(
                                _Redactor().value(f"{type(exc).__name__}: {exc}", "message")
                            )[:200]
                            input_inbox.reject(
                                request.session_id,
                                item.message_id,
                                owner_attempt=claimed_attempt or 0,
                                lease_token=lease,
                                reason=f"/compact failed: {error}",
                            )
                            delivered_input_ids.discard(item.message_id)
                            session.app_state.conversation.append_event(
                                "assistant_message",
                                {
                                    "text": f"/compact failed: {error}",
                                    "source": "agents-manager-command",
                                },
                                event_id=f"background-command-error:{item.message_id}",
                            )
                        else:
                            completed_command_ids.add(item.message_id)
                            session.app_state.conversation.append_event(
                                "assistant_message",
                                {
                                    "text": "Session memory compacted.",
                                    "source": "agents-manager-command",
                                },
                                event_id=f"background-command-output:{item.message_id}",
                            )
                        continue
                    error, skill_body = _dispatch_background_command(
                        session,
                        item.text,
                        message_id=item.message_id,
                    )
                    if error:
                        input_inbox.reject(
                            request.session_id,
                            item.message_id,
                            owner_attempt=claimed_attempt or 0,
                            lease_token=lease,
                            reason=error,
                        )
                        delivered_input_ids.discard(item.message_id)
                        session.app_state.conversation.append_event(
                            "assistant_message",
                            {"text": error, "source": "agents-manager-command"},
                            event_id=f"background-command-error:{item.message_id}",
                        )
                        continue
                    if skill_body is None:
                        completed_command_ids.add(item.message_id)
                    if skill_body is not None:
                        await _run_direct_turn(
                            session,
                            dataclass_replace(request, intent=skill_body),
                        )
                    continue
                _record_input_delivery(item, starts_turn=True)
                workflow_name = request.workflow_name
                try:
                    resume_run_id = (
                        _select_headless_workflow_resume(session, workflow_name)
                        if workflow_name
                        else None
                    )
                except Exception as exc:  # noqa: BLE001
                    from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415

                    reason = str(_Redactor().value(f"{type(exc).__name__}: {exc}", "message"))[:200]
                    delivered_input_ids.discard(item.message_id)
                    input_inbox.reject(
                        request.session_id,
                        item.message_id,
                        owner_attempt=claimed_attempt or 0,
                        lease_token=lease,
                        reason=f"Workflow continuation was not safe: {reason}",
                    )
                    session.app_state.conversation.append_event(
                        "assistant_message",
                        {
                            "text": f"Workflow continuation was not safe: {reason}",
                            "source": "agents-manager-command",
                        },
                        event_id=f"background-workflow-input-error:{item.message_id}",
                    )
                    continue
                if workflow_name and resume_run_id is not None:
                    # Match TUISession's ordinary-text continuation behavior:
                    # a late message resumes the durable workflow checkpoint
                    # instead of starting an unrelated direct turn or a new
                    # workflow from its first phase.
                    result = await execute_workflow(
                        session,
                        workflow_name,
                        item.text,
                        resume_run_id=resume_run_id,
                    )
                    if result.status != "complete":
                        delivered_input_ids.discard(item.message_id)
                        input_inbox.reject(
                            request.session_id,
                            item.message_id,
                            owner_attempt=claimed_attempt or 0,
                            lease_token=lease,
                            reason=(
                                result.error or f"Workflow continuation ended with {result.status}"
                            ),
                        )
                        continue
                    completed_command_ids.add(item.message_id)
                    continue
                await _run_direct_turn(
                    session,
                    dataclass_replace(request, intent=item.text),
                )
                completed_command_ids.add(item.message_id)

        queued_source = QueuedMessageSource(claim=_claim_session_input)

        async def _execute() -> _WorkerOutcome:
            current = store.get(request.session_id, include_deleted=True)
            if (
                current.status is not SessionStatus.RUNNING
                or current.attempt != claimed_attempt
                or current.lease_token != lease
                or current.mode_application_status is not ModeApplicationStatus.APPLIED
                or current.mode_application_attempt != claimed_attempt
                or current.mode_name != session.mode_manager.active_name
            ):
                raise RuntimeError(
                    "Worker refused agent execution because this attempt's runtime mode "
                    "was not durably attested"
                )
            if request.workflow_name:
                resume_run_id = _select_headless_workflow_resume(session, request.workflow_name)
                if resume_run_id is None:
                    result = await execute_workflow(session, request.workflow_name, request.intent)
                else:
                    result = await execute_workflow(
                        session,
                        request.workflow_name,
                        request.intent,
                        resume_run_id=resume_run_id,
                    )
                completed = result.status == "complete"
                recoverable = result.status == "paused"
                status = SessionStatus.COMPLETED if completed else SessionStatus.FAILED
                raw_phases = getattr(result, "phases", ())
                phases = (
                    tuple(phase for phase in raw_phases if isinstance(phase, str) and phase)
                    if isinstance(raw_phases, (tuple, list))
                    else ()
                )
                return _WorkerOutcome(
                    status=status,
                    error=result.error,
                    activity=f"Workflow {result.status}",
                    phase_history=phases,
                    exit_reason=(
                        "workflow_complete"
                        if completed
                        else "recoverable_error"
                        if recoverable
                        else "irrecoverable_error"
                    ),
                    failure_category=(
                        ""
                        if completed
                        else "recoverable_workflow"
                        if recoverable
                        else "irrecoverable_error"
                    ),
                    exit_code=0 if completed else 1,
                )
            await _run_direct_turn(session, request)
            await session.processor.drain()
            idle = request.detached_goal and _confirmed_idle_after_thinking(session)
            return _WorkerOutcome(
                status=SessionStatus.COMPLETED,
                error=None,
                activity="Turn complete",
                exit_reason="idle_after_thinking" if idle else "turn_complete",
                exit_code=0,
            )

        with bind_queued_message_source(queued_source):
            if request.wall_timeout_s > 0:
                outcome = await asyncio.wait_for(_execute(), request.wall_timeout_s)
            else:
                outcome = await _execute()
            if outcome.status is SessionStatus.COMPLETED:
                await _drain_idle_inputs()
        return outcome.exit_code
    except asyncio.CancelledError:
        # Ordinary background jobs retain the historical cancellation
        # contract: the supervisor owns the CANCELLING → CANCELLED transition
        # and an in-process task cancellation alone must not publish a second
        # terminal result. Detached goal workers, by contrast, must close
        # their own durable lifecycle before returning.
        if not request.detached_goal:
            outcome = None
            raise
        outcome = _WorkerOutcome(
            status=SessionStatus.CANCELLED,
            error="Worker cancelled",
            activity="Worker cancelled",
            exit_reason="cancelled",
            failure_category="cancelled",
            exit_code=130,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        outcome = _WorkerOutcome(
            status=SessionStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            activity="Worker failed",
            exit_reason="irrecoverable_error",
            failure_category="irrecoverable_error",
            exit_code=1,
        )
        return 1
    finally:
        cleanup_error = ""
        if input_inbox is not None and claimed_attempt is not None:
            settle_attempt = _request_attribute(input_inbox, "settle_attempt")
            if callable(settle_attempt):
                try:
                    settle_attempt(
                        request.session_id,
                        owner_attempt=claimed_attempt,
                        lease_token=lease,
                        delivered_ids=delivered_input_ids,
                        completed_ids=completed_command_ids,
                        succeeded=(
                            outcome is not None and outcome.status is SessionStatus.COMPLETED
                        ),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "could not settle background input receipts for %s",
                        request.session_id,
                        exc_info=True,
                    )
        if terminal_token is not None:
            from agenthicc.background.terminals import reset_current_terminal_manager  # noqa: PLC0415

            reset_current_terminal_manager(terminal_token)
        heartbeat_stop.set()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if processor_task is not None and session is not None:
            try:
                from agenthicc.runners.headless import _close_headless_session  # noqa: PLC0415

                await asyncio.wait_for(
                    _close_headless_session(session, processor_task, None),
                    timeout=WORKER_FINALIZATION_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001
                cleanup_error = f"{type(exc).__name__}: {exc}"
        if claimed and outcome is not None:
            final_outcome = (
                replace(outcome, exit_reason="cleanup_timeout")
                if cleanup_error.startswith("TimeoutError")
                else outcome
            )
            _finalize_worker(
                store,
                request,
                lease_token=lease,
                expected_attempt=claimed_attempt or 0,
                outcome=final_outcome,
                worker_pid=os.getpid(),
                cleanup_error=cleanup_error,
            )
        if owner_lease is not None:
            owner_lease.release()
        try:
            os.chdir(original_cwd)
        except OSError:  # pragma: no cover - only an unrecoverable cwd teardown
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="agenthicc detached background worker")
    parser.add_argument("--request-file", required=True)
    parser.add_argument("--store-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = _load_request(Path(args.request_file))
        return asyncio.run(run_worker(request, BackgroundStore(Path(args.store_root))))
    except Exception as exc:  # noqa: BLE001
        print(f"background worker error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
