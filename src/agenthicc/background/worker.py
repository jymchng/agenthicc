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
from typing import TYPE_CHECKING, Literal, Mapping, TypedDict, cast

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
    from lauren_ai._transport import Transport

    from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse

    from agenthicc.memory.compactor import CompactionOutcome
    from agenthicc.background.recovery import RecoveryAssessment
    from agenthicc.background.terminals import TerminalManager
    from agenthicc.runners.workflow_recovery import WorkflowRecoveryRecord
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.runners.session_conversation import SessionConversation
    from agenthicc.runners.session_lease import SessionOwnerLease
    from agenthicc.runners.run_coordinator import ResumePlan
    from agenthicc.workflows.registry import WorkflowRegistry


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
    resume_existing_only: bool = False
    dispatch_token: str = ""

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
        raw_dispatch_token = value.get("dispatch_token", "")
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
            resume_existing_only=value.get("resume_existing_only") is True,
            dispatch_token=raw_dispatch_token[:128] if isinstance(raw_dispatch_token, str) else "",
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


@dataclass(frozen=True)
class _BackgroundWorkflowCommandResult:
    """Target-router result for a workflow command handled without a TUI."""

    workflow_name: str | None = None
    message: str = ""
    error: str = ""
    selection_only: bool = False
    fatal: bool = False


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


def _confirmed_idle_after_thinking(session: SessionContext) -> bool:
    """Return whether the canonical session state describes terminal idle.

    This intentionally does not inspect rendered TUI output. A completed
    turn plus a canonical ``IDLE`` signal is the smallest reliable evidence
    available to a headless worker; a pending approval, a running tool,
    compaction, unfinished subagent workers and an active workflow veto it.
    """
    from agenthicc.tui.conversation_store import AgentState

    app_state = session.app_state
    conversation = app_state.conversation
    if conversation.agent_state() is not AgentState.IDLE:
        return False
    if conversation.active_tool() or conversation.compaction_active():
        return False
    pool = conversation.subagent_pool_state()
    if pool is not None and (
        any(worker.status.upper() not in {"DONE", "FAILED", "CANCELLED"} for worker in pool.workers)
        or pool.total > pool.done
    ):
        return False
    if app_state.pending_approval() is not None:
        return False
    workflow_run = app_state.workflow_run()
    if workflow_run is not None and workflow_run.status.upper() not in {
        "",
        "COMPLETE",
        "COMPLETED",
        "FAILED",
        "EXITED",
    }:
        return False
    # A closed COMPLETE/ERROR turn is durable evidence that the worker was
    # active. Historical turns are acceptable here because this helper is
    # called only after the current worker operation has returned.
    return any(
        turn.state in (AgentState.COMPLETE, AgentState.ERROR) for turn in conversation.turns()
    )


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
            from agenthicc.tui.runtime.session_export import _Redactor

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
    from agenthicc.runs.model import GoalRunStatus, RunAgentRecord
    from agenthicc.runs.store import RunNotFound, RunStore

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
        inbox: object | None = None,
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
        if inbox is None:
            from agenthicc.background.inbox import BackgroundInteractionInbox

            inbox = BackgroundInteractionInbox(store)
        self.inbox = inbox

    @staticmethod
    def _approval_target(req: object) -> dict[str, object]:
        workspace = _request_attribute(req, "workspace_access", ())
        accesses: list[dict[str, str]] = []
        if isinstance(workspace, (list, tuple)):
            for item in workspace[:32]:
                canonical = _request_attribute(item, "canonical", "")
                operation = _request_attribute(item, "operation", "")
                display = _request_attribute(item, "display", "")
                if isinstance(canonical, str) and isinstance(operation, str):
                    accesses.append(
                        {
                            "canonical": canonical[:2_048],
                            "operation": operation[:80],
                            "display": display[:500] if isinstance(display, str) else "",
                        }
                    )
        capabilities = _request_attribute(req, "capabilities", ())
        safe_capabilities = (
            sorted(item[:128] for item in capabilities if isinstance(item, str))
            if isinstance(capabilities, (set, frozenset, tuple, list))
            else []
        )
        tool_name = _request_attribute(req, "tool_name", "approval request")
        tool_use_id = _request_attribute(req, "tool_use_id", "")
        mode_options = _request_attribute(req, "mode_options", ())
        tool_input = _request_attribute(req, "tool_input", {})
        from agenthicc.tui.runtime.session_export import _Redactor

        safe_input = _Redactor().value(tool_input, "tool_input")
        if not isinstance(safe_input, dict):
            safe_input = {}
        return {
            "tool_name": tool_name[:160] if isinstance(tool_name, str) else "approval request",
            "approval_id": tool_use_id[:256] if isinstance(tool_use_id, str) else "",
            "request_kind": str(_request_attribute(req, "kind", "tool"))[:40],
            "capabilities": safe_capabilities,
            "workspace_access": accesses,
            "mode_options": [item[:80] for item in mode_options if isinstance(item, str)]
            if isinstance(mode_options, (list, tuple))
            else [],
            "tool_input": safe_input,
        }

    @staticmethod
    def _question_target(req: object) -> tuple[str, dict[str, object]]:
        tool_input = _request_attribute(req, "tool_input", {})
        raw_questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
        questions: list[dict[str, object]] = []
        if isinstance(raw_questions, list):
            for question in raw_questions[:32]:
                if not isinstance(question, dict):
                    continue
                identifier = question.get("id")
                if not isinstance(identifier, str) or not identifier:
                    continue
                options = question.get("options", [])
                questions.append(
                    {
                        "id": identifier[:128],
                        "text": str(question.get("text", ""))[:4_000],
                        "options": [str(option)[:1_000] for option in options[:64]]
                        if isinstance(options, list)
                        else [],
                    }
                )
        if questions:
            request_id = str(
                _request_attribute(req, "request_id", "")
                or _request_attribute(req, "tool_use_id", "")
                or uuid.uuid4().hex
            )
            return request_id, {
                "tool_name": str(_request_attribute(req, "tool_name", "ask_user"))[:160],
                "question_ids": [item["id"] for item in questions],
                "questions": questions,
            }
        request_id = str(
            _request_attribute(req, "request_id", "")
            or _request_attribute(req, "tool_use_id", "")
            or uuid.uuid4().hex
        )
        return request_id, {
            "tool_name": str(_request_attribute(req, "tool_name", "input requested"))[:160],
            "question_ids": [],
            "questions": [],
        }

    async def _wait_for_inbox_item(
        self,
        *,
        req: object,
        kind: Literal["approval", "question", "input"],
        request_id: str,
        target: dict[str, object],
        timeout_s: float | None,
    ) -> tuple[object | None, str]:
        from agenthicc.background.inbox import BackgroundInteractionInbox

        inbox = self.inbox
        if not isinstance(inbox, BackgroundInteractionInbox):
            raise TypeError("background approval service requires an interaction inbox")
        current = self.store.get(self.session_id)
        expires_at = time.time() + timeout_s if timeout_s is not None else None
        item = inbox.register(
            session_id=self.session_id,
            attempt=current.attempt,
            request_id=request_id,
            kind=kind,
            target=target,
            expires_at=expires_at,
        )
        waiting_status = (
            SessionStatus.WAITING_APPROVAL if kind == "approval" else SessionStatus.WAITING_INPUT
        )
        current = self.store.transition(
            self.session_id,
            waiting_status,
            expected_status=SessionStatus.RUNNING,
            approval_request=(
                str(target.get("tool_name", "approval request")) if kind == "approval" else ""
            ),
            approval_decision=None,
            approval_item_id=item.item_id if kind == "approval" else "",
            input_request=(
                str(target.get("tool_name", "input requested")) if kind != "approval" else ""
            ),
            input_value=None,
            input_item_id=item.item_id if kind != "approval" else "",
            latest_activity=f"Waiting for {'approval' if kind == 'approval' else 'input'}: {target.get('tool_name', '')}",
        )
        try:
            inbox.activate(item.item_id)
        except Exception:
            inbox.set_terminal(item.item_id, "failed", "could not publish inbox request")
            raise
        deadline = expires_at
        answer = await inbox.wait_for_answer(item.item_id, deadline=deadline)
        if answer is not None:
            response = inbox.answer_response(answer)
            try:
                self.store.transition(
                    self.session_id,
                    SessionStatus.RUNNING,
                    expected_status=waiting_status,
                    expected_attempt=current.attempt,
                    expected_lease_token=current.lease_token,
                    approval_request="",
                    approval_decision=None,
                    approval_item_id="",
                    input_request="",
                    input_value=None,
                    input_item_id="",
                    latest_activity=(
                        "Approval resolved" if kind == "approval" else "Input accepted"
                    ),
                )
            except InvalidSessionTransition:
                inbox.set_terminal(item.item_id, "stale", "session changed before delivery")
                return None, "stale"
            inbox.mark_delivered(item.item_id)
            return response, "answered"
        latest_item = inbox.get(item.item_id)
        try:
            current = self.store.get(self.session_id, include_deleted=True)
        except KeyError:
            return None, "stale"
        if current.status in {
            SessionStatus.CANCELLING,
            SessionStatus.CANCELLED,
            SessionStatus.DELETED,
        }:
            inbox.set_terminal(item.item_id, "stale", "session was cancelled or deleted")
            return None, "cancelled"
        if current.status is waiting_status and current.attempt == item.attempt:
            try:
                self.store.transition(
                    self.session_id,
                    SessionStatus.RUNNING,
                    expected_status=waiting_status,
                    expected_attempt=item.attempt,
                    expected_lease_token=current.lease_token,
                    approval_request="",
                    approval_decision=None,
                    approval_item_id="",
                    input_request="",
                    input_value=None,
                    input_item_id="",
                    latest_activity=(
                        "Approval expired"
                        if kind == "approval"
                        else "Input timed out; agent fallback required"
                    ),
                )
            except InvalidSessionTransition:
                pass
        if latest_item.status == "accepted":
            # Accepted responses remain durable across interruption. The next
            # worker will consume only if the exact attempt/item still owns it.
            return None, "accepted"
        return None, latest_item.status

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

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        from agenthicc.tools.approval import ApprovalResponse

        if req.kind == "questions":
            return await self.request_input(req)

        request_id = str(
            _request_attribute(req, "request_id", "")
            or _request_attribute(req, "tool_use_id", "")
            or uuid.uuid4().hex
        )
        target = self._approval_target(req)
        try:
            value, outcome = await self._wait_for_inbox_item(
                req=req,
                kind="approval",
                request_id=request_id,
                target=target,
                timeout_s=None,
            )
        except KeyError:
            return ApprovalResponse(allowed=False, message="background session was removed")
        except (InvalidSessionTransition, ValueError):
            return ApprovalResponse(allowed=False, message="background session is no longer active")
        if isinstance(value, dict):
            return ApprovalResponse(
                allowed=value.get("allowed") is True,
                remember=value.get("remember") is True,
                remember_all=value.get("remember_all") is True,
                message=value.get("message", "") if isinstance(value.get("message"), str) else "",
                mode=value.get("mode") if isinstance(value.get("mode"), str) else None,
                scope_grant=value.get("scope_grant")
                if isinstance(value.get("scope_grant"), str)
                else None,
                outcome=str(value.get("outcome", outcome)),
                request_id=request_id,
            )
        return ApprovalResponse(
            allowed=False,
            message="approval state changed" if outcome == "stale" else f"approval {outcome}",
            outcome=outcome,
            request_id=request_id,
        )

    async def request_input(self, req: ApprovalRequest) -> ApprovalResponse:
        """Wait for explicit input to a workflow ``ask_user`` request."""

        from agenthicc.tools.approval import ApprovalResponse

        try:
            timeout_s = object.__getattribute__(req, "timeout_s")
        except AttributeError:
            timeout_s = None
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
            timeout_s = self.question_timeout_s
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
        request_id, target = self._question_target(req)
        self._emit_question_event(
            "question_wait_started",
            req,
            outcome="pending",
            timeout_s=float(timeout_s),
            deadline_at=deadline_wall,
        )
        try:
            value, outcome = await self._wait_for_inbox_item(
                req=req,
                kind="question" if target.get("question_ids") else "input",
                request_id=request_id,
                target=target,
                timeout_s=float(timeout_s),
            )
        except KeyError:
            self._emit_question_event(
                "question_wait_failed",
                req,
                outcome="failed",
                timeout_s=float(timeout_s),
                deadline_at=deadline_wall,
            )
            return ApprovalResponse(
                allowed=False,
                message="background session was removed",
                outcome="failed",
                request_id=request_id,
                timeout_s=float(timeout_s),
            )
        except (InvalidSessionTransition, ValueError):
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
        if isinstance(value, dict):
            self._emit_question_event(
                "question_answered",
                req,
                outcome="answered",
                timeout_s=float(timeout_s),
                deadline_at=deadline_wall,
            )
            return ApprovalResponse(
                allowed=value.get("allowed") is True,
                message=value.get("message", "") if isinstance(value.get("message"), str) else "",
                outcome=str(value.get("outcome", "answered")),
                request_id=request_id,
                timeout_s=float(timeout_s),
            )
        if outcome == "expired":
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
                request_id=request_id,
                timeout_s=float(timeout_s),
            )
        if outcome == "cancelled":
            self._emit_question_event(
                "question_cancelled",
                req,
                outcome="cancelled",
                timeout_s=float(timeout_s),
                deadline_at=deadline_wall,
            )
        return ApprovalResponse(
            allowed=False,
            message=(
                "background session was removed"
                if outcome == "stale"
                else f"background input {outcome}"
            ),
            outcome="cancelled" if outcome == "cancelled" else "failed",
            request_id=request_id,
            timeout_s=float(timeout_s),
        )

    def respond(self, allowed: bool, **kwargs: object) -> None:
        current = self.store.get(self.session_id)
        if current.approval_item_id:
            from agenthicc.background.inbox import BackgroundInteractionInbox

            inbox = self.inbox
            if not isinstance(inbox, BackgroundInteractionInbox):
                raise TypeError("background approval service requires an interaction inbox")
            item = inbox.get(current.approval_item_id)
            response: dict[str, object] = {"allowed": allowed}
            for key in ("remember", "remember_all", "scope_grant", "message", "mode"):
                if key in kwargs and kwargs[key] is not None:
                    response[key] = kwargs[key]
            if allowed and item.target.get("workspace_access") and "scope_grant" not in response:
                response["scope_grant"] = "target_once"
            inbox.respond(
                item.item_id,
                response,
                expected_version=item.version,
                idempotency_key=uuid.uuid4().hex,
            )
            return
        self.store.update(self.session_id, approval_decision=allowed)

    def provide_input(self, value: str) -> None:
        """Deliver input for callers that hold the worker-side adapter."""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("Input must not be empty")
        if len(value) > 8_000:
            raise ValueError("Input exceeds 8000 characters")
        current = self.store.get(self.session_id)
        if current.status != SessionStatus.WAITING_INPUT:
            raise InvalidSessionTransition("Session is not waiting for input")
        if current.input_item_id:
            from agenthicc.background.inbox import (
                BackgroundInteractionInbox,
                response_from_text,
            )

            inbox = self.inbox
            if not isinstance(inbox, BackgroundInteractionInbox):
                raise TypeError("background input service requires an interaction inbox")
            item = inbox.get(current.input_item_id)
            inbox.respond(
                item.item_id,
                response_from_text(item, value),
                expected_version=item.version,
                idempotency_key=uuid.uuid4().hex,
            )
            return
        self.store.update(self.session_id, input_value=value)

    def reset_turn_memory(self) -> None:
        return None


class BackgroundInputService(BackgroundApprovalService):
    """Named input boundary for integrations that do not need approvals."""

    async def request_input(self, req: ApprovalRequest) -> ApprovalResponse:
        return await super().request_input(req)


def _load_request(path: Path) -> WorkerRequest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("background request must be a JSON object")
    return WorkerRequest.from_mapping(raw)


async def _run_direct_turn(
    session: SessionContext,
    request: WorkerRequest,
    *,
    resume_plan: "ResumePlan | None" = None,
) -> None:
    """Run a direct turn through the canonical agent-turn runner."""

    from agenthicc.runners.agent_turn import _run_agent_turn

    agent_runner = session.agent_runner
    if agent_runner is None:
        config_error = _request_attribute(session, "llm_config_error", None)
        if isinstance(config_error, str) and config_error:
            raise RuntimeError(f"LLM configuration error: {config_error}")
        raise RuntimeError("No LLM configured for background session")
    app_state = session.app_state
    cfg = session.cfg
    await _run_agent_turn(
        request.intent,
        agent_runner,
        session.processor,
        session_memory=session.session_memory,
        conversation_id=str(session.session_id),
        max_agent_turns=cfg.execution.max_agent_turns,
        conv_store=app_state.conversation,
        app_state=app_state,
        exec_cfg=cfg.execution,
        skills=session.skills,
        skill_permissions=cfg.agents.skill_permissions_for("default"),
        mention_cache=session.mention_cache,
        project_plugin_tools=list(getattr(session.project_plugins, "all_tools", ())),
        mcp_registry=session.mcp_registry,
        active_agent="default",
        completed_turns=0,
        approval_svc=session.approval_svc,
        workspace_access=session.workspace_access,
        memory_router=session.memory_router,
        semantic_index=session.semantic_index,
        browser_manager=session.browser_manager,
        resume_turn_id=resume_plan.turn_id if resume_plan is not None else None,
        resume_ledger=resume_plan.ledger if resume_plan is not None else None,
    )


def _assess_direct_turn_recovery(
    session: object,
    pending_resume: object,
    *,
    configured_workflow_name: str = "",
    session_intent: str = "",
) -> "RecoveryAssessment":
    """Correlate an interrupted journal turn with durable workflow recovery.

    The mere presence of a workflow record is not ownership evidence. New
    journal entries carry the workflow run ID; historical entries without it
    are held for an explicit recovery decision whenever workflow state exists.
    """
    from agenthicc.background.recovery import assess_recovery_owner

    processor = _request_attribute(session, "processor", None)
    get_state = _request_attribute(processor, "get_state", None)
    active_workflow_run_ids: list[str] = []
    inspection_error = ""
    if callable(get_state):
        try:
            state = get_state()
            workflows = _request_attribute(state, "workflows", {})
            from agenthicc.kernel.state import NodeStatus

            if isinstance(workflows, Mapping):
                for key, workflow in workflows.items():
                    status = _request_attribute(workflow, "status")
                    if status in {NodeStatus.complete, NodeStatus.failed}:
                        continue
                    if not _request_attribute(workflow, "name", ""):
                        continue
                    run_id = _request_attribute(workflow, "run_id", None)
                    active_workflow_run_ids.append(str(run_id or key)[:128])
        except Exception as exc:  # noqa: BLE001 — do not run both recovery paths
            inspection_error = f"active workflow projection: {type(exc).__name__}"

    conversation = _request_attribute(session, "session_conversation", None)
    recoverable_run_ids: list[str] = []
    invalid_run_ids: list[str] = []
    correlated_workflow_run_ids: list[str] = []
    if conversation is not None:
        from agenthicc.runners.workflow_recovery import (
            WorkflowRecoveryCoordinator,
        )

        config = _request_attribute(session, "cfg", None)
        execution = _request_attribute(config, "execution", None)
        profile = _request_attribute(execution, "profile", "")
        registry = _request_attribute(session, "workflow_registry", None)
        workspace_scope = _request_attribute(session, "workspace_scope", None)
        workspace_root = _request_attribute(workspace_scope, "primary_root", "")
        try:
            records = WorkflowRecoveryCoordinator(
                str(_request_attribute(session, "session_id", ""))
            ).inspect(
                workflow_registry=cast("WorkflowRegistry | None", registry),
                conversation=cast("SessionConversation | None", conversation),
                provider_profile=str(profile or ""),
                workspace_root=str(workspace_root or ""),
            )
            recoverable_run_ids = [record.run_id for record in records if record.recoverable]
            invalid_run_ids = [record.run_id for record in records if not record.recoverable]
            session_id = str(_request_attribute(session, "session_id", ""))
            if configured_workflow_name and session_intent:
                correlated_workflow_run_ids = [
                    record.run_id
                    for record in records
                    if record.workflow_name == configured_workflow_name
                    and record.conversation_id == session_id
                    and record.intent == session_intent
                ]
        except Exception as exc:  # noqa: BLE001 — preserve the pending input
            inspection_error = (
                inspection_error or f"workflow checkpoint inspection: {type(exc).__name__}"
            )

    raw_turn_workflow_run_id = _request_attribute(pending_resume, "workflow_run_id", None)
    return assess_recovery_owner(
        turn_id=str(_request_attribute(pending_resume, "turn_id", "")) or None,
        turn_workflow_run_id=(
            raw_turn_workflow_run_id
            if isinstance(raw_turn_workflow_run_id, str) and raw_turn_workflow_run_id
            else None
        ),
        turn_execution_kind=(
            raw_execution_kind
            if isinstance(
                raw_execution_kind := _request_attribute(pending_resume, "execution_kind", None),
                str,
            )
            else None
        ),
        recoverable_run_ids=recoverable_run_ids,
        invalid_run_ids=invalid_run_ids,
        active_workflow_run_ids=active_workflow_run_ids,
        correlated_workflow_run_ids=correlated_workflow_run_ids,
        inspection_error=inspection_error,
    )


def _is_workflow_recovery_control(text: str) -> bool:
    """Identify only target-routed commands that can resolve recovery state."""
    parts = text.strip().split()
    return len(parts) >= 2 and parts[0] == "/workflow" and parts[1] in {"resume", "reset"}


def _safe_provider_failure_message(
    session: object,
    exc: BaseException,
    *,
    request: WorkerRequest,
    attempt: int,
    diagnostic_context: Mapping[str, str | int] | None,
) -> str | None:
    """Project HTTP provider errors into bounded, payload-free status text."""
    from agenthicc.runners.agent_turn import (
        _http_status_code,
        _missing_opencode_session_diagnostic,
        _provider_error_identifiers,
        _safe_provider_identifier,
    )

    status = _http_status_code(exc)
    if status is None:
        return None
    config = _request_attribute(session, "cfg", None)
    execution = _request_attribute(config, "execution", None)
    provider = _safe_provider_identifier(_request_attribute(execution, "provider", ""))
    model = _safe_provider_identifier(_request_attribute(execution, "model", ""))
    profile = _safe_provider_identifier(_request_attribute(execution, "profile", "")) or "default"
    parts = [f"HTTP {status}", f"session={request.session_id}", f"attempt={attempt}"]
    if provider:
        parts.append(f"provider={provider}")
    if model:
        parts.append(f"model={model}")
    parts.append(f"profile={profile}")
    identifiers = _provider_error_identifiers(exc)
    for key, label in (
        ("provider_error_type", "type"),
        ("provider_error_code", "code"),
        ("provider_request_id", "request_id"),
    ):
        value = identifiers.get(key)
        if value:
            parts.append(f"{label}={value}")
    if diagnostic_context is not None:
        for key, label in (
            ("phase", "phase"),
            ("interrupted_turn_id", "interrupted_turn"),
            ("input_message_id", "input"),
        ):
            context_value = diagnostic_context.get(key)
            if (
                isinstance(context_value, (str, int))
                and not isinstance(context_value, bool)
                and context_value
            ):
                safe_value = _safe_provider_identifier(context_value)
                if safe_value:
                    parts.append(f"{label}={safe_value}")
    diagnostic = _missing_opencode_session_diagnostic(exc)
    if diagnostic is not None:
        return f"Provider session identity is missing: {diagnostic} ({'; '.join(parts)})"
    return f"Provider request failed ({'; '.join(parts)})"


def _dispatch_background_command(
    session: SessionContext,
    text: str,
    *,
    message_id: str,
) -> tuple[str, str | None]:
    """Dispatch one slash/skill command in its owning session context.

    The manager process never executes commands. The worker uses its own
    command and skill registries and runtime state. Commands that require an
    interactive overlay fail explicitly instead of being sent to the LLM as
    ordinary prose. `/workflow` is handled by the stateful async controller in
    the worker loop, not by this synchronous dispatcher.

    Returns ``(error, skill_body)``; an empty error means the command was
    handled. Captured command output is projected into the target transcript.
    """
    from io import StringIO

    from rich.console import Console

    from agenthicc.commands import CommandContext, CommandDispatcher

    normalized = text.strip()
    token = normalized.split(None, 1)[0] if normalized else ""
    if token == "/workflow":
        return (
            "/workflow must be routed through the background workflow controller.",
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
        from agenthicc.tui.runtime.session_export import _Redactor

        safe_error = _Redactor().value(f"{type(exc).__name__}: {exc}", "message")
        return (f"{token} failed: {str(safe_error)[:200]}", None)
    if not handled:
        return (f"{token} could not be handled by the owning session.", None)
    rendered = output.getvalue().strip()
    if rendered:
        from agenthicc.tui.runtime.session_export import _Redactor

        safe_output = str(_Redactor().value(rendered, "message"))[:8_000]
        session.app_state.conversation.append_event(
            "assistant_message",
            {"text": safe_output, "source": "agents-manager-command"},
            event_id=f"background-command-output:{message_id}",
        )
    return ("", skill_bodies[-1] if skill_bodies else None)


def _persist_background_workflow_selection(
    store: BackgroundStore,
    session_id: str,
    *,
    workflow_name: str,
    attempt: int,
    lease_token: str,
    activity: str,
) -> str:
    """Persist a workflow selector only while this worker still owns the run."""
    try:
        store.update(
            session_id,
            expected_status=SessionStatus.RUNNING,
            expected_attempt=attempt,
            expected_lease_token=lease_token,
            workflow_name=workflow_name,
            latest_activity=activity,
        )
    except (KeyError, InvalidSessionTransition) as exc:
        return f"Cannot update workflow selection: {exc}"
    return ""


async def _handle_background_workflow_command(
    session: SessionContext,
    args: str,
    *,
    store: BackgroundStore,
    attempt: int,
    lease_token: str,
    input_resume_plan: "ResumePlan | None" = None,
) -> _BackgroundWorkflowCommandResult:
    """Handle one forwarded `/workflow` input in the target session runtime.

    The manager and inbox treat input as opaque text. This is the background
    equivalent of ``TUISession.route()``: command feedback is a handled target
    response, while only a lost/stale worker lease is a delivery failure.
    """
    from agenthicc.runners.headless import execute_workflow
    from agenthicc.runners.workflow_recovery import (
        WorkflowRecoveryCoordinator,
    )

    parts = args.strip().split()
    subcommand = parts[0] if parts else ""
    registry = session.workflow_registry
    conversation = session.session_conversation
    profile = str(session.cfg.execution.profile or "")
    workspace_scope = session.workspace_scope
    workspace_root = str(workspace_scope.primary_root or "") if workspace_scope else ""

    def default_workflow_name() -> tuple[str, str]:
        active_mode = session.app_state.active_mode()
        default_name = str(active_mode.default_workflow or "")
        if not default_name:
            return "", ""
        workflow = registry.get(default_name)
        if workflow is None:
            return "", f"Mode default workflow {default_name!r} is not loaded."
        return str(workflow.name), ""

    async def persist_selection(name: str, activity: str) -> str:
        return _persist_background_workflow_selection(
            store,
            session.session_id,
            workflow_name=name,
            attempt=attempt,
            lease_token=lease_token,
            activity=activity,
        )

    def inspect_run_records(
        *,
        include_terminal: bool,
    ) -> tuple[list[WorkflowRecoveryRecord], str]:
        if conversation is None:
            return [], ""
        coordinator = WorkflowRecoveryCoordinator(session.session_id)
        try:
            records = coordinator.inspect(
                workflow_registry=registry,
                conversation=conversation,
                provider_profile=profile,
                workspace_root=workspace_root,
                include_terminal=include_terminal,
            )
        except Exception as exc:  # noqa: BLE001
            return [], f"Cannot inspect workflow runs: {type(exc).__name__}: {exc}"
        return records, ""

    def find_run_record(
        run_id: str,
        *,
        include_terminal: bool,
    ) -> tuple[WorkflowRecoveryRecord | None, str]:
        records, error = inspect_run_records(include_terminal=include_terminal)
        if error:
            return None, error
        requested = run_id.strip().strip("`'\"")
        exact = [record for record in records if record.run_id == requested]
        matches = exact or [
            record
            for record in records
            if record.run_id.casefold().startswith(requested.casefold())
        ]
        if len(matches) > 1:
            choices = ", ".join(record.run_id for record in matches[:8])
            return None, f"Workflow run ID {requested!r} is ambiguous: {choices}."
        if not matches:
            return None, f"No workflow run matches {requested!r}."
        return matches[0], ""

    try:
        # Match TUISession: a bare reset clears the selector immediately, but
        # does not discard a saved run unless its ID is explicitly supplied.
        if subcommand in {"", "reset"}:
            if subcommand == "reset" and len(parts) > 2:
                return _BackgroundWorkflowCommandResult(error="Usage: /workflow reset [run-id]")
            workflow_name, error = default_workflow_name()
            if error:
                return _BackgroundWorkflowCommandResult(error=error)
            if subcommand == "reset" and len(parts) == 2:
                record, error = find_run_record(parts[1], include_terminal=True)
                if error:
                    return _BackgroundWorkflowCommandResult(error=error)
                if record is None:
                    return _BackgroundWorkflowCommandResult(
                        error=f"No workflow run matches {parts[1]!r}."
                    )
                checkpoint = record.checkpoint
                if checkpoint is None:
                    diagnostic = record.display_error
                    return _BackgroundWorkflowCommandResult(
                        error=f"Cannot discard workflow run {parts[1]!r}: {diagnostic}"
                    )
                coordinator = WorkflowRecoveryCoordinator(session.session_id)
                discarded = coordinator.discard(
                    record,
                    owner_id=f"background:{os.getpid()}:{record.run_id}",
                )
                from agenthicc.kernel import Event

                try:
                    await session.processor.emit(
                        Event.create(
                            "WorkflowRunDiscarded",
                            {
                                "run_id": discarded.run_id,
                                "workflow_name": discarded.workflow_name,
                            },
                        )
                    )
                except Exception:  # noqa: BLE001 - checkpoint is authoritative
                    logger.debug("could not project workflow discard event", exc_info=True)
                selection_error = await persist_selection(
                    workflow_name,
                    f"Workflow reset to mode default · {workflow_name or 'none'}",
                )
                if selection_error:
                    return _BackgroundWorkflowCommandResult(
                        error=selection_error,
                        fatal=True,
                    )
                return _BackgroundWorkflowCommandResult(
                    workflow_name=workflow_name,
                    message=(
                        f"Workflow run {discarded.run_id} discarded; selector reset to "
                        f"{workflow_name or 'the mode default'}."
                    ),
                    selection_only=True,
                )
            selection_error = await persist_selection(
                workflow_name,
                f"Workflow reset to mode default · {workflow_name or 'none'}",
            )
            if selection_error:
                return _BackgroundWorkflowCommandResult(error=selection_error, fatal=True)
            records, inspection_error = inspect_run_records(include_terminal=False)
            if inspection_error:
                return _BackgroundWorkflowCommandResult(
                    workflow_name=workflow_name,
                    error=inspection_error,
                    selection_only=True,
                )
            recoverable_records = [record for record in records if record.recoverable]
            if recoverable_records:
                choices = ", ".join(record.run_id for record in recoverable_records[:8])
                suffix = " …" if len(recoverable_records) > 8 else ""
                return _BackgroundWorkflowCommandResult(
                    workflow_name=workflow_name,
                    error=(
                        "Select a recoverable workflow explicitly before resetting: "
                        f"/workflow reset <run-id> ({choices}{suffix})"
                    ),
                    selection_only=True,
                )
            return _BackgroundWorkflowCommandResult(
                workflow_name=workflow_name,
                message=f"Workflow reset to {workflow_name or 'the mode default'}.",
                selection_only=True,
            )

        if subcommand == "resume":
            if len(parts) > 2:
                return _BackgroundWorkflowCommandResult(error="Usage: /workflow resume [run-id]")
            if conversation is None:
                return _BackgroundWorkflowCommandResult(
                    error="Workflow resume requires the durable session conversation.",
                    selection_only=True,
                )
            coordinator = WorkflowRecoveryCoordinator(session.session_id)
            try:
                if len(parts) == 2:
                    record, error = find_run_record(parts[1], include_terminal=False)
                    if error:
                        return _BackgroundWorkflowCommandResult(error=error)
                else:
                    record = coordinator.select_latest_for_resume(
                        workflow_registry=registry,
                        conversation=conversation,
                        provider_profile=profile,
                        workspace_root=workspace_root,
                    )
            except Exception as exc:  # noqa: BLE001
                return _BackgroundWorkflowCommandResult(
                    error=f"Cannot select a workflow to resume: {type(exc).__name__}: {exc}",
                    selection_only=True,
                )
            if record is None:
                return _BackgroundWorkflowCommandResult(
                    error=(
                        "No recoverable workflow run is available. Use /workflow reset only "
                        "if you intend to discard a saved run."
                    ),
                    selection_only=True,
                )
            if not record.recoverable:
                return _BackgroundWorkflowCommandResult(
                    error=(
                        f"Workflow run {record.run_id!r} is not recoverable: {record.display_error}"
                    ),
                    selection_only=True,
                )
            workflow_name = record.workflow_name
            if not workflow_name:
                return _BackgroundWorkflowCommandResult(
                    error="The selected workflow checkpoint has no workflow name.",
                    selection_only=True,
                )
            pending_resume = _request_attribute(session, "pending_resume", None)
            if pending_resume is not None:
                raw_linked_run_id = _request_attribute(pending_resume, "workflow_run_id", None)
                linked_run_id = (
                    raw_linked_run_id
                    if isinstance(raw_linked_run_id, str) and raw_linked_run_id
                    else None
                )
                execution_kind = _request_attribute(pending_resume, "execution_kind", None)
                if linked_run_id is not None and linked_run_id != record.run_id:
                    return _BackgroundWorkflowCommandResult(
                        workflow_name=workflow_name,
                        error=(
                            "The interrupted turn is linked to a different workflow run. "
                            "The selected checkpoint and queued input are preserved."
                        ),
                        selection_only=True,
                    )
                if execution_kind == "direct":
                    return _BackgroundWorkflowCommandResult(
                        workflow_name=workflow_name,
                        error=(
                            "The interrupted journal turn is a direct-agent turn, not part of "
                            "this workflow. The workflow command was handled, but the direct "
                            "turn must recover first."
                        ),
                        selection_only=True,
                    )
                # An explicit target-side `/workflow resume [run-id]` is the
                # operator's recovery-owner decision for legacy journals that
                # predate turn-to-run provenance. The re-drive records this
                # run ID on the next durable turn_started event.
                input_resume_plan = cast("ResumePlan", pending_resume)
            selection_error = await persist_selection(
                workflow_name,
                f"Resuming workflow · {workflow_name}",
            )
            if selection_error:
                return _BackgroundWorkflowCommandResult(error=selection_error, fatal=True)
            intent = record.intent
            if not intent.strip():
                return _BackgroundWorkflowCommandResult(
                    error="The selected workflow checkpoint has no resumable intent."
                )
            result = await execute_workflow(
                session,
                workflow_name,
                intent,
                resume_run_id=record.run_id,
                resume_plan=input_resume_plan,
            )
            # The workflow resume may have completed the exact interrupted
            # journal turn. Re-fold the authoritative journal before deciding
            # whether deferred ordinary input can be delivered.
            if input_resume_plan is not None and conversation is not None:
                from agenthicc.runners.run_coordinator import RunCoordinator

                incomplete = RunCoordinator.detect_incomplete_turn(conversation.journal)
                session.pending_resume = (
                    RunCoordinator.build_resume_plan(conversation.journal, incomplete)
                    if incomplete is not None
                    else None
                )
            status = result.status
            if status == "complete":
                return _BackgroundWorkflowCommandResult(
                    workflow_name=workflow_name,
                    message=f"Workflow {workflow_name} resumed and completed.",
                )
            if status == "paused":
                return _BackgroundWorkflowCommandResult(
                    workflow_name=workflow_name,
                    message=f"Workflow {workflow_name} remains paused and recoverable.",
                    selection_only=True,
                )
            return _BackgroundWorkflowCommandResult(
                workflow_name=workflow_name,
                error=result.error or f"Workflow resume ended with {status}.",
                selection_only=True,
            )

        if len(parts) != 1:
            return _BackgroundWorkflowCommandResult(
                error="Usage: /workflow <name> | resume [run-id] | reset [run-id]"
            )
        definition = registry.get(subcommand)
        if definition is None:
            available = ", ".join(sorted(registry.names())) or "none"
            return _BackgroundWorkflowCommandResult(
                error=f"Unknown workflow: {subcommand!r} (available: {available})"
            )
        workflow_name = str(definition.name)
        records, inspection_error = inspect_run_records(include_terminal=False)
        if inspection_error:
            return _BackgroundWorkflowCommandResult(error=inspection_error)
        paused_workflows = [
            record
            for record in records
            if record.recoverable and record.workflow_name != workflow_name
        ]
        if paused_workflows:
            current_names = ", ".join(sorted({record.workflow_name for record in paused_workflows}))
            return _BackgroundWorkflowCommandResult(
                error=(
                    f"{current_names!r} is paused. Resume or reset its saved run before "
                    "switching workflows."
                ),
                selection_only=True,
            )
        selection_error = await persist_selection(
            workflow_name,
            f"Workflow selected · {workflow_name}",
        )
        if selection_error:
            return _BackgroundWorkflowCommandResult(error=selection_error, fatal=True)
        return _BackgroundWorkflowCommandResult(
            workflow_name=workflow_name,
            message=f"Workflow → {workflow_name}",
            selection_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        from agenthicc.tui.runtime.session_export import _Redactor

        error = str(_Redactor().value(f"{type(exc).__name__}: {exc}", "message"))[:400]
        return _BackgroundWorkflowCommandResult(
            error=f"/workflow failed: {error}",
            selection_only=True,
        )


async def _compact_background_session(session: SessionContext) -> CompactionOutcome:
    """Run the same bounded compactor used by the owning TUI session."""
    from agenthicc.memory.compactor import (
        CompactionRecoveryRequired,
        compact_memory_with_result,
    )

    memory = session.session_memory
    if not memory._messages:
        raise ValueError("There is no session memory to compact")
    transport = cast(
        "Transport | None", object.__getattribute__(session.agent_runner, "_transport")
    )
    if transport is None:
        raise RuntimeError("No provider transport is available for compaction")
    execution = session.cfg.execution
    outcome = await compact_memory_with_result(
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
    if outcome.status == "blocked_durability_error":
        raise CompactionRecoveryRequired(outcome)
    return outcome


async def run_worker(request: WorkerRequest, store: BackgroundStore) -> int:
    """Claim, execute, and finalize one background session."""

    from agenthicc.background.settings import (
        BackgroundSettings,
        load_background_settings,
    )

    try:
        background_settings = load_background_settings(
            config_path=request.config_path,
            overrides=request.set_overrides,
            cwd=Path(request.cwd),
        )
        store.configure_notifications(background_settings.notifications)
    except Exception as exc:  # noqa: BLE001
        # Notifications are strictly non-critical to worker execution. Config
        # validation is repeated by session construction and will still fail
        # the run if the underlying background configuration is invalid.
        logger.warning("background notifications unavailable (%s)", type(exc).__name__)
        background_settings = BackgroundSettings()

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
    provider_diagnostic_context: dict[str, str | int] | None = None
    original_cwd = os.getcwd()
    try:
        os.chdir(request.cwd)
        claimed_session = store.claim(
            request.session_id,
            pid=os.getpid(),
            lease_token=lease,
            dispatch_token=request.dispatch_token or None,
        )
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
        from agenthicc.runners.headless import (
            _HeadlessApprovalService,
            _close_headless_session,
            execute_workflow,
            _select_headless_workflow_resume,
        )
        from agenthicc.runners.tui_session import _build_session_context
        from agenthicc.runners.session_lease import SessionOpenCoordinator
        from agenthicc.background.input_inbox import BackgroundInputInbox
        from agenthicc.runners.agent_turn_context import (
            QueuedMessageSource,
            bind_provider_diagnostic_context,
            bind_queued_message_source,
            current_provider_diagnostic_context,
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
        from agenthicc.tui.runtime.session_log import register_session

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
        from agenthicc.background.terminals import set_current_terminal_manager

        terminal_manager = getattr(session, "terminal_manager", None)
        if terminal_manager is not None:
            terminal_token = set_current_terminal_manager(terminal_manager)
        session.approval_svc = (
            _HeadlessApprovalService(request.dangerously_skip_permissions)
            if request.dangerously_skip_permissions
            else BackgroundApprovalService(
                store,
                request.session_id,
                question_timeout_s=_session_question_timeout(session),
                conversation_store=_request_attribute(
                    _request_attribute(session, "app_state", None), "conversation", None
                ),
            )
        )
        workspace_access = getattr(session, "workspace_access", None)
        if workspace_access is not None:
            workspace_access.set_approval_service(session.approval_svc)
        processor_task = asyncio.create_task(session.processor.run(), name="background-processor")
        await asyncio.sleep(0)

        input_inbox = BackgroundInputInbox(store)
        active_workflow_name = request.workflow_name
        session_waiting_for_next_input = False
        workflow_command_failure: str | None = None

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
            nonlocal session_waiting_for_next_input
            pending = input_inbox.peek_next(
                request.session_id,
                owner_attempt=claimed_attempt or 0,
                lease_token=lease,
            )
            if pending is None:
                return None
            if pending.text.startswith(("/", "$")):
                from agenthicc.commands.busy_policy import classify_busy_command

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
                    _record_input_delivery(item, starts_turn=False)
                    completed_command_ids.add(item.message_id)
                    session_waiting_for_next_input = True
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
            diagnostic_context = current_provider_diagnostic_context()
            if diagnostic_context is not None:
                diagnostic_context["phase"] = "processing_input"
                diagnostic_context["input_message_id"] = item.message_id
            if item.text.startswith(("/", "$")):
                _record_input_delivery(item, starts_turn=False)
                session_waiting_for_next_input = True
                error, skill_body = _dispatch_background_command(
                    session,
                    item.text,
                    message_id=item.message_id,
                )
                if skill_body is not None:
                    error = "Skill execution waits for the current turn to finish."
                if error:
                    # This is a target-side routing result (for example an
                    # unknown command or a command requiring an interactive
                    # overlay), not a transport failure. The owning session
                    # received and handled the user's exact input.
                    completed_command_ids.add(item.message_id)
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

        async def _dispatch_workflow_input(item: object) -> None:
            """Route a claimed workflow command through the target session."""
            nonlocal active_workflow_name
            nonlocal session_waiting_for_next_input
            nonlocal workflow_command_failure
            diagnostic_context = current_provider_diagnostic_context()
            message_id = str(_request_attribute(item, "message_id", ""))
            text = str(_request_attribute(item, "text", ""))
            if diagnostic_context is not None:
                diagnostic_context["phase"] = "processing_input"
                diagnostic_context["input_message_id"] = message_id
            _record_input_delivery(item, starts_turn=False)
            workflow_parts = text.strip().split(None, 1)
            result = await _handle_background_workflow_command(
                session,
                workflow_parts[1] if len(workflow_parts) == 2 else "",
                store=store,
                attempt=claimed_attempt or 0,
                lease_token=lease,
            )
            if result.fatal:
                input_inbox.reject(
                    request.session_id,
                    message_id,
                    owner_attempt=claimed_attempt or 0,
                    lease_token=lease,
                    reason=result.error,
                )
                delivered_input_ids.discard(message_id)
                workflow_command_failure = result.error
                session.app_state.conversation.append_event(
                    "assistant_message",
                    {"text": result.error, "source": "agents-manager-command"},
                    event_id=f"background-command-error:{message_id}",
                )
                return
            if result.workflow_name is not None:
                active_workflow_name = result.workflow_name
            session_waiting_for_next_input = result.selection_only or bool(result.error)
            completed_command_ids.add(message_id)
            response = result.error or result.message
            if response:
                session.app_state.conversation.append_event(
                    "assistant_message",
                    {"text": response, "source": "agents-manager-command"},
                    event_id=(
                        f"background-command-error:{message_id}"
                        if result.error
                        else f"background-command-output:{message_id}"
                    ),
                )

        async def _drain_idle_inputs() -> None:
            """Route every queued message through the target session's input path."""
            nonlocal session_waiting_for_next_input
            from dataclasses import replace as dataclass_replace

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
                    if token == "/workflow":
                        await _dispatch_workflow_input(item)
                        continue
                    _record_input_delivery(item, starts_turn=False)
                    if token == "/compact":
                        try:
                            compaction_outcome = await _compact_background_session(session)
                        except Exception as exc:  # noqa: BLE001
                            from agenthicc.memory.compactor import CompactionRecoveryRequired

                            if isinstance(exc, CompactionRecoveryRequired):
                                raise
                            from agenthicc.tui.runtime.session_export import _Redactor

                            error = str(
                                _Redactor().value(f"{type(exc).__name__}: {exc}", "message")
                            )[:200]
                            completed_command_ids.add(item.message_id)
                            session_waiting_for_next_input = True
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
                            session_waiting_for_next_input = True
                            outcome_status = compaction_outcome.status
                            compacted = compaction_outcome.applied
                            message = (
                                "Session memory compacted."
                                if compacted
                                else "Compaction did not change session memory; history was preserved."
                            )
                            session.app_state.conversation.append_event(
                                "assistant_message",
                                {
                                    "text": f"{message} (status: {outcome_status})",
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
                        completed_command_ids.add(item.message_id)
                        session_waiting_for_next_input = True
                        session.app_state.conversation.append_event(
                            "assistant_message",
                            {"text": error, "source": "agents-manager-command"},
                            event_id=f"background-command-error:{item.message_id}",
                        )
                        continue
                    if skill_body is None:
                        completed_command_ids.add(item.message_id)
                        session_waiting_for_next_input = True
                    if skill_body is not None:
                        diagnostic_context = current_provider_diagnostic_context()
                        if diagnostic_context is not None:
                            diagnostic_context["phase"] = "processing_input"
                            diagnostic_context["input_message_id"] = item.message_id
                        await _run_direct_turn(
                            session,
                            dataclass_replace(request, intent=skill_body),
                        )
                        session_waiting_for_next_input = False
                    continue
                # Claiming the inbox entry is the delivery boundary. Ordinary
                # text is always a normal turn in this target session; do not
                # infer workflow continuation or inspect/select checkpoints.
                _record_input_delivery(item, starts_turn=True)
                diagnostic_context = current_provider_diagnostic_context()
                if diagnostic_context is not None:
                    diagnostic_context["phase"] = "processing_input"
                    diagnostic_context["input_message_id"] = item.message_id
                await _run_direct_turn(
                    session,
                    dataclass_replace(request, intent=item.text),
                )
                session_waiting_for_next_input = False
                completed_command_ids.add(item.message_id)

        async def _route_recovery_control_inputs() -> int:
            """Route explicit workflow recovery commands without overtaking turns.

            Ordinary messages stay accepted at the FIFO head. Only the target
            session's explicit ``/workflow resume`` and ``/workflow reset``
            controls may be claimed by stable ID to unblock recovery.
            """
            nonlocal active_workflow_name
            nonlocal session_waiting_for_next_input
            nonlocal workflow_command_failure
            routed = 0
            for receipt in input_inbox.receipts(request.session_id):
                if receipt.state != "accepted" or not _is_workflow_recovery_control(receipt.text):
                    continue
                item = input_inbox.claim_by_id(
                    request.session_id,
                    receipt.message_id,
                    owner_attempt=claimed_attempt or 0,
                    lease_token=lease,
                )
                if item is None:
                    continue
                routed += 1
                await _dispatch_workflow_input(item)
            return routed

        queued_source = QueuedMessageSource(claim=_claim_session_input)
        provider_diagnostic_context = {
            "session_id": request.session_id,
            "background_attempt": claimed_attempt or 0,
            "phase": "background_turn",
        }

        async def _execute() -> _WorkerOutcome:
            nonlocal active_workflow_name
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
            deferred_inputs = tuple(
                item
                for item in input_inbox.receipts(request.session_id)
                if item.state == "accepted" and item.deferred
            )
            pending_resume = _request_attribute(session, "pending_resume", None)
            if pending_resume is not None:
                from agenthicc.background.recovery import RecoveryOwner

                assessment = _assess_direct_turn_recovery(
                    session,
                    pending_resume,
                    configured_workflow_name=active_workflow_name,
                    session_intent=request.intent,
                )
                # A target-side recovery control must not be hidden behind a
                # direct-turn replay either. Ordinary text remains queued; the
                # explicit command is handled first, then ownership is assessed
                # again before any provider turn is allowed.
                routed_recovery_controls = await _route_recovery_control_inputs()
                refreshed_resume = _request_attribute(session, "pending_resume", None)
                if refreshed_resume is not None:
                    pending_resume = refreshed_resume
                    assessment = _assess_direct_turn_recovery(
                        session,
                        pending_resume,
                        configured_workflow_name=active_workflow_name,
                        session_intent=request.intent,
                    )
                else:
                    from agenthicc.background.recovery import RecoveryAssessment

                    assessment = RecoveryAssessment(owner=RecoveryOwner.NONE)

                if (
                    assessment.owner is RecoveryOwner.WORKFLOW
                    and assessment.workflow_run_id
                    and not routed_recovery_controls
                ):
                    recovery_run_id = assessment.workflow_run_id
                    # This resumes the uniquely identified existing workflow
                    # as part of restoring the target session. It does not
                    # reinterpret or replace any inbox message: accepted user
                    # input remains pending until the prior workflow turn has
                    # reached a safe journal boundary.
                    provider_diagnostic_context["phase"] = "recovering_workflow_turn"
                    provider_diagnostic_context["workflow_run_id"] = recovery_run_id
                    resume_result = await _handle_background_workflow_command(
                        session,
                        f"resume {recovery_run_id}",
                        store=store,
                        attempt=claimed_attempt or 0,
                        lease_token=lease,
                    )
                    if resume_result.workflow_name is not None:
                        active_workflow_name = resume_result.workflow_name
                    refreshed_resume = _request_attribute(session, "pending_resume", None)
                    if refreshed_resume is None:
                        from agenthicc.background.recovery import RecoveryAssessment

                        assessment = RecoveryAssessment(owner=RecoveryOwner.NONE)
                    else:
                        from agenthicc.background.recovery import RecoveryAssessment

                        failure_detail = resume_result.error or resume_result.message
                        if not failure_detail:
                            failure_detail = "The saved workflow remains paused."
                        assessment = RecoveryAssessment(
                            owner=RecoveryOwner.UNRESOLVED,
                            turn_id=str(_request_attribute(pending_resume, "turn_id", "")) or None,
                            workflow_run_id=recovery_run_id,
                            related_run_ids=(recovery_run_id,),
                            reason=(
                                "Automatic recovery of the identified workflow did not reach "
                                f"a safe turn boundary: {failure_detail}"
                            ),
                        )
                if assessment.owner in {RecoveryOwner.WORKFLOW, RecoveryOwner.UNRESOLVED}:
                    if assessment.owner is RecoveryOwner.WORKFLOW:
                        owner_run_id = assessment.workflow_run_id or "<run-id>"
                        reason = (
                            "This interrupted turn belongs to workflow run "
                            f"{owner_run_id}. The queued input is preserved; send "
                            f"`/workflow resume {owner_run_id}` to retry recovery, or "
                            f"`/workflow reset {owner_run_id}` to explicitly discard it."
                        )
                    elif assessment.reason.startswith("Automatic recovery"):
                        owner_run_id = assessment.workflow_run_id or "<run-id>"
                        reason = (
                            f"{assessment.reason} The queued input is preserved; retry with "
                            f"`/workflow resume {owner_run_id}` or explicitly discard with "
                            f"`/workflow reset {owner_run_id}`."
                        )
                    elif assessment.related_run_ids:
                        choices = ", ".join(assessment.related_run_ids[:4])
                        suffix = " …" if len(assessment.related_run_ids) > 4 else ""
                        reason = (
                            "Recovery ownership is ambiguous. Select the intended run with "
                            f"`/workflow resume <run-id>` or discard a specific run with "
                            f"`/workflow reset <run-id>`. Related run(s): {choices}{suffix}."
                        )
                    else:
                        reason = (
                            "Workflow recovery state could not be inspected safely. Review the "
                            "saved workflow state, then send `/workflow resume <run-id>` or "
                            "`/workflow reset <run-id>`."
                        )
                    recovery_reason = reason[:240]
                    try:
                        input_inbox.mark_waiting_recovery(
                            request.session_id,
                            owner_attempt=claimed_attempt or 0,
                            lease_token=lease,
                            reason=recovery_reason,
                        )
                    except (InvalidSessionTransition, KeyError):
                        logger.info(
                            "could not persist recovery-wait receipt for %s",
                            request.session_id,
                            exc_info=True,
                        )
                    return _WorkerOutcome(
                        status=SessionStatus.ORPHANED,
                        error=recovery_reason,
                        activity="Waiting for workflow recovery",
                        exit_reason="workflow_recovery_waiting",
                        failure_category="",
                        exit_code=0,
                    )

                if assessment.owner is RecoveryOwner.DIRECT_TURN:
                    provider_diagnostic_context["phase"] = "recovering_prior_turn"
                    provider_diagnostic_context["interrupted_turn_id"] = str(
                        _request_attribute(pending_resume, "turn_id", "")
                    )
                    if deferred_inputs:
                        provider_diagnostic_context["input_message_id"] = deferred_inputs[
                            0
                        ].message_id
                    from dataclasses import replace as dataclass_replace

                    recovery_request = dataclass_replace(
                        request,
                        intent=str(_request_attribute(pending_resume, "user_message", "")),
                    )
                    await _run_direct_turn(
                        session,
                        recovery_request,
                        resume_plan=cast("ResumePlan", pending_resume),
                    )
                    await session.processor.drain()
                    return _WorkerOutcome(
                        status=SessionStatus.COMPLETED,
                        error=None,
                        activity=(
                            "Interrupted turn recovered; queued input pending delivery"
                            if deferred_inputs
                            else "Interrupted turn recovered"
                        ),
                        exit_reason="turn_recovered",
                        exit_code=0,
                    )
            if deferred_inputs:
                # A user submitted against a stale/recoverable session. Forward
                # accepted text as target-session turns before considering the
                # original launch intent; never reinterpret it as a workflow
                # continuation or replay old work first.
                await _drain_idle_inputs()
                failed_inputs = tuple(
                    pending
                    for pending in deferred_inputs
                    if pending.message_id not in delivered_input_ids
                )
                if failed_inputs:
                    return _WorkerOutcome(
                        status=SessionStatus.FAILED,
                        error=("One or more queued recovery inputs could not be safely processed"),
                        activity="Background recovery input needs attention",
                        exit_reason="recovery_input_failed",
                        failure_category="recovery_input_failed",
                        exit_code=1,
                    )
                if workflow_command_failure is not None:
                    return _WorkerOutcome(
                        status=SessionStatus.FAILED,
                        error=workflow_command_failure,
                        activity="Background workflow command needs attention",
                        exit_reason="workflow_command_failed",
                        failure_category="workflow_command_failed",
                        exit_code=1,
                    )
                if session_waiting_for_next_input:
                    return _WorkerOutcome(
                        status=SessionStatus.ORPHANED,
                        error=None,
                        activity="Input handled by target session · awaiting next message",
                        exit_reason="target_input_handled",
                        exit_code=0,
                    )
                return _WorkerOutcome(
                    status=SessionStatus.COMPLETED,
                    error=None,
                    activity="Background recovery input complete",
                    exit_reason="recovery_input_complete",
                    exit_code=0,
                )
            if active_workflow_name:
                resume_run_id = _select_headless_workflow_resume(session, active_workflow_name)
                if resume_run_id is None:
                    if request.resume_existing_only:
                        return _WorkerOutcome(
                            status=SessionStatus.COMPLETED,
                            error=None,
                            activity="No resumable workflow work",
                            exit_reason="no_resumable_work",
                            exit_code=0,
                        )
                    result = await execute_workflow(session, active_workflow_name, request.intent)
                else:
                    result = await execute_workflow(
                        session,
                        active_workflow_name,
                        request.intent,
                        resume_run_id=resume_run_id,
                    )
                completed = result.status == "complete"
                recoverable = result.status == "paused"
                category = str(_request_attribute(result, "failure_category", "") or "")
                compaction_recovery = recoverable and category in {
                    "compaction_persistence_failed",
                    "context_limit_unresolved",
                }
                history_recovery = (
                    result.status == "blocked" and category == "tool_history_recovery_required"
                )
                recovery_blocked = compaction_recovery or history_recovery
                status = (
                    SessionStatus.COMPLETED
                    if completed
                    else SessionStatus.ORPHANED
                    if recovery_blocked
                    else SessionStatus.FAILED
                )
                raw_phases = getattr(result, "phases", ())
                phases = (
                    tuple(phase for phase in raw_phases if isinstance(phase, str) and phase)
                    if isinstance(raw_phases, (tuple, list))
                    else ()
                )
                return _WorkerOutcome(
                    status=status,
                    error=result.error,
                    activity=(
                        "Conversation history needs recovery"
                        if history_recovery
                        else "Workflow compaction needs recovery"
                        if compaction_recovery
                        else f"Workflow {result.status}"
                    ),
                    phase_history=phases,
                    exit_reason=(
                        "workflow_complete"
                        if completed
                        else category
                        if recovery_blocked
                        else "recoverable_error"
                        if recoverable
                        else "irrecoverable_error"
                    ),
                    failure_category=(
                        ""
                        if completed
                        else category
                        if recovery_blocked
                        else "recoverable_workflow"
                        if recoverable
                        else "irrecoverable_error"
                    ),
                    exit_code=0 if completed or recovery_blocked else 1,
                )
            if request.resume_existing_only:
                return _WorkerOutcome(
                    status=SessionStatus.COMPLETED,
                    error=None,
                    activity="No resumable work",
                    exit_reason="no_resumable_work",
                    exit_code=0,
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

        with (
            bind_queued_message_source(queued_source),
            bind_provider_diagnostic_context(provider_diagnostic_context),
        ):
            if request.wall_timeout_s > 0:
                outcome = await asyncio.wait_for(_execute(), request.wall_timeout_s)
            else:
                outcome = await _execute()
        if outcome.status is SessionStatus.COMPLETED:
            await _drain_idle_inputs()
            if workflow_command_failure is not None:
                outcome = replace(
                    outcome,
                    status=SessionStatus.FAILED,
                    error=workflow_command_failure,
                    activity="Background workflow command needs attention",
                    exit_reason="workflow_command_failed",
                    failure_category="workflow_command_failed",
                    exit_code=1,
                )
            elif session_waiting_for_next_input:
                outcome = replace(
                    outcome,
                    status=SessionStatus.ORPHANED,
                    error=None,
                    activity="Input handled by target session · awaiting next message",
                    exit_reason="target_input_handled",
                    failure_category="",
                    exit_code=0,
                )
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
        from agenthicc.memory.tool_history import ToolHistoryRecoveryRequired

        if isinstance(exc, ToolHistoryRecoveryRequired):
            outcome = _WorkerOutcome(
                status=SessionStatus.ORPHANED,
                error=str(exc),
                activity="Conversation history needs recovery",
                exit_reason="tool_history_recovery_required",
                failure_category="tool_history_recovery_required",
                exit_code=0,
            )
            return 0
        from agenthicc.memory.compactor import CompactionRecoveryRequired

        if isinstance(exc, CompactionRecoveryRequired) and request.detached_goal:
            outcome = _WorkerOutcome(
                status=SessionStatus.ORPHANED,
                error=str(exc),
                activity="Conversation compaction needs recovery",
                exit_reason=exc.failure_category,
                failure_category=exc.failure_category,
                exit_code=0,
            )
            return 0
        safe_provider_error = (
            _safe_provider_failure_message(
                session,
                exc,
                request=request,
                attempt=claimed_attempt or 0,
                diagnostic_context=provider_diagnostic_context,
            )
            if session is not None
            else None
        )
        outcome = _WorkerOutcome(
            status=SessionStatus.FAILED,
            error=safe_provider_error or f"{type(exc).__name__}: {exc}",
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
            from agenthicc.background.terminals import reset_current_terminal_manager

            reset_current_terminal_manager(terminal_token)
        heartbeat_stop.set()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if processor_task is not None and session is not None:
            try:
                from agenthicc.runners.headless import _close_headless_session

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
        try:
            from agenthicc.background.supervisor import BackgroundSupervisor

            BackgroundSupervisor(
                store,
                max_workers=background_settings.max_workers,
                max_workers_per_project=background_settings.max_workers_per_project,
                max_workers_per_user=background_settings.max_workers_per_user,
                queue_enabled=background_settings.queue_enabled,
                max_queue_length=background_settings.max_queue_length,
                priority_aging_interval_s=background_settings.priority_aging_interval_s,
                dispatch_lease_s=background_settings.dispatch_lease_s,
                notification_settings=background_settings.notifications,
            ).dispatch_pending()
        except Exception:  # noqa: BLE001
            # The durable queue remains authoritative; a manager refresh or
            # the next accepted request retries dispatch after a failure.
            logger.warning(
                "could not dispatch the next queued background request after %s",
                request.session_id,
                exc_info=True,
            )


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
