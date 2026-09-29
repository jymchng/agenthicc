"""Detached background worker entry point (PRD-141).

Workers are deliberately thin adapters around the existing headless session
and agent-turn runners.  They never implement a second tool or workflow loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, TypedDict

from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.store import BackgroundStore, InvalidSessionTransition
from agenthicc.cli.context import CLIContext, CLIFlags
from agenthicc.config import DEFAULT_QUESTION_TIMEOUT_S


WORKER_FINALIZATION_TIMEOUT_S = 30.0
"""Maximum time allowed for owned headless resources to close."""

if TYPE_CHECKING:
    from agenthicc.background.terminals import TerminalManager
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
        attempts = current.worker_finalization_attempts + 1
        changes = _outcome_changes(
            current,
            outcome,
            lease_token=lease_token,
            worker_pid=current.worker_pid or worker_pid,
            finalization_attempts=attempts,
            cleanup_error=cleanup_error,
        )
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
                **changes,
            )
        elif current.status == SessionStatus.RUNNING and outcome.status == SessionStatus.CANCELLED:
            current = store.transition(
                request.session_id,
                SessionStatus.CANCELLING,
                expected_status=SessionStatus.RUNNING,
                expected_lease_token=lease_token,
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
                **changes,
            )
        elif current.status == SessionStatus.RUNNING:
            store.transition(
                request.session_id,
                outcome.status,
                expected_status=SessionStatus.RUNNING,
                expected_lease_token=lease_token,
                **changes,
            )
        elif current.status == outcome.status:
            # A duplicate callback must not turn a terminal result into a
            # failure. It may only add missing final metadata.
            store.update(request.session_id, **changes)
        else:
            return
        try:
            final_session = store.get(request.session_id, include_deleted=True)
            _persist_goal_run_outcome(request, final_session, outcome)
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
    if status is None:
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


async def run_worker(request: WorkerRequest, store: BackgroundStore) -> int:
    """Claim, execute, and finalize one background session."""

    lease = uuid.uuid4().hex
    session = None
    claimed = False
    outcome: _WorkerOutcome | None = None
    owner_lease: SessionOwnerLease | None = None
    processor_task: asyncio.Task[object] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    terminal_token: contextvars.Token[TerminalManager | None] | None = None
    heartbeat_stop = asyncio.Event()
    original_cwd = os.getcwd()
    try:
        os.chdir(request.cwd)
        claimed_session = store.claim(request.session_id, pid=os.getpid(), lease_token=lease)
        if claimed_session.status != SessionStatus.RUNNING:
            raise RuntimeError(f"Worker could not claim session: {claimed_session.status.value}")
        claimed = True

        async def _heartbeat() -> None:
            while not heartbeat_stop.is_set():
                await asyncio.sleep(1.0)
                if heartbeat_stop.is_set():
                    return
                try:
                    store.heartbeat(
                        request.session_id,
                        lease_token=lease,
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

        ctx = CLIContext(
            resume_id=request.session_id,
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
            owner_lease=owner_lease,
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

        async def _execute() -> _WorkerOutcome:
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

        if request.wall_timeout_s > 0:
            outcome = await asyncio.wait_for(_execute(), request.wall_timeout_s)
        else:
            outcome = await _execute()
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
