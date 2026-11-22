"""Headless runner — emits kernel events as JSON lines to stdout."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agenthicc.runners.session_lease import (
    SessionAlreadyActiveError,
    SessionOwnerLease,
    SessionStorageError,
)
from agenthicc.runners.failure_kinds import failure_reason, reported_failure
from agenthicc.runners.workflow_handle import classify_workflow_failure

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase

    from agenthicc.cli.context import CLIContext
    from agenthicc.kernel.events import Event
    from agenthicc.tui.conversation_store import ConversationEvent
    from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse
    from agenthicc.runners.run_coordinator import ResumePlan
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.runners.session_lease import SessionOwnerLease
    from agenthicc.runners.workflow_profile import ResumeProviderProfile
    from agenthicc.runners.workflow_stack import WorkflowDelivery, WorkflowFrame
    from agenthicc.workflows.plugin import WorkflowPlugin

__all__ = ["WorkflowExecutionResult", "execute_workflow", "run_headless_workflow"]


def _workflow_failure_kind(exc: object) -> str:
    """Map common headless workflow failures to stable recovery categories."""
    disposition = classify_workflow_failure(exc)
    text = str(exc).lower()
    # Preserve the pre-provider setup category for compatibility. Once a
    # provider HTTP status is present, the shared classifier's more precise
    # provider_configuration/provider_request category wins.
    if disposition.kind == "workflow_error" and any(
        marker in text for marker in ("config", "profile")
    ):
        return "configuration"
    return disposition.kind


class _WorkflowResumeTransitionError(RuntimeError):
    """A resume could not durably transition before dispatch."""


def _emit_authored_json(event: Event) -> None:
    if event.event_type == "AuthoredPublished":
        print(json.dumps({**event.payload, "event": "authored_published"}), flush=True)


@dataclass(frozen=True)
class WorkflowExecutionResult:
    """Small, JSON-safe outcome returned by one headless workflow run."""

    session_id: str
    workflow_name: str
    run_id: str
    status: str
    phases: tuple[str, ...]
    error: str | None = None
    phase_metadata: dict[str, object] = field(default_factory=dict)
    failure_category: str = ""
    #: The failed turn's ``kind``/``message``/``action`` (TUX-08), if one failed.
    failure: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation used by CLI and stdin modes."""
        result: dict[str, object] = {
            "event_type": "WorkflowRunCompleted",
            "session_id": self.session_id,
            "workflow": self.workflow_name,
            "run_id": self.run_id,
            "status": self.status,
            "phases": list(self.phases),
            "error": self.error,
        }
        if self.phase_metadata:
            result["phase_metadata"] = self.phase_metadata
        if self.failure_category:
            result["failure_category"] = self.failure_category
        if self.failure is not None:
            result["error_kind"] = self.failure.get("kind")
        return result

    def error_record(self) -> dict[str, object] | None:
        """The ``{"type": "error", "kind": …}`` line for a failed turn (TUX-08)."""
        if self.failure is None:
            return None
        return {
            "type": "error",
            **self.failure,
            "session_id": self.session_id,
            "workflow": self.workflow_name,
            "run_id": self.run_id,
        }


def _optional_request_field(value: object, name: str, default: object = None) -> object:
    """Read an optional approval-request field from legacy adapters."""
    try:
        return object.__getattribute__(value, name)
    except AttributeError:
        return default


class _HeadlessApprovalService:
    """Approval adapter for automation, defaulting to fail closed.

    Human approval overlays do not exist in headless mode.  A workflow may opt
    into automatic approval with ``--dangerously-skip-permissions``; otherwise
    approval-gated actions receive a denial instead of hanging forever waiting
    for a UI response.
    """

    def __init__(self, allow: bool) -> None:
        self._allow = allow

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        from agenthicc.tools.approval import ApprovalResponse

        if req.kind == "questions":
            return ApprovalResponse(
                allowed=False,
                outcome="cancelled",
                message="headless mode has no interactive question adapter",
                request_id=req.request_id,
            )

        # A dangerous-permissions flag is an explicit capability approval for
        # automation, not an implicit expansion of the workspace boundary.
        # Outside-workspace access needs a caller-provided approval adapter so
        # headless runs never silently exfiltrate or mutate parent paths.
        if req.workspace_access:
            return ApprovalResponse(
                allowed=False,
                message="headless workspace access is denied without an explicit scope policy",
            )
        message = (
            "headless approval granted"
            if self._allow
            else ("headless approval denied; pass --dangerously-skip-permissions to allow it")
        )
        return ApprovalResponse(allowed=self._allow, message=message)

    def respond(self, allowed: bool, **kwargs: object) -> None:
        return None

    def reset_turn_memory(self) -> None:
        return None


def _resolve_headless_session(
    ctx: "CLIContext",
) -> tuple[str | None, "SessionOwnerLease | None"]:
    """Resolve `--continue` and claim it before headless construction."""

    if ctx.resume_id is not None or not ctx.continue_session:
        return ctx.resume_id, None
    from agenthicc.runners.session_lease import SessionOpenCoordinator

    selected = SessionOpenCoordinator().select_latest_for_cwd(Path.cwd(), entrypoint="headless")
    if selected is None:
        return None, None
    return selected


def _select_headless_workflow_resume(
    session: "SessionContext",
    workflow_name: str,
) -> str | None:
    """Resolve one durable workflow run for a resumed headless session.

    A session resume restores the conversation journal, but that is not enough
    to restore a workflow cursor.  This resolver deliberately returns a
    ``run_id`` selection rather than a checkpoint snapshot; ``execute_workflow``
    reloads and claims the latest bytes immediately before dispatch.  Returning
    ``None`` means there is no workflow recovery record and a caller that
    explicitly requested a new run may use ``runner.run(intent)``.
    """
    from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator

    conversation = session.session_conversation
    if conversation is None:
        return None
    from agenthicc.memory.tool_history import ToolHistoryRecoveryRequired

    retry_recovery = getattr(conversation, "retry_tool_history_recovery", None)
    if callable(retry_recovery):
        recovery = retry_recovery()
        if not recovery.provider_ready:
            raise ToolHistoryRecoveryRequired(recovery)
    workspace_scope = session.workspace_scope
    workspace_root = str(getattr(workspace_scope, "primary_root", "") or "")
    coordinator = WorkflowRecoveryCoordinator(session.session_id)
    record = coordinator.select_for_resume(
        workflow_name=workflow_name,
        workflow_registry=session.workflow_registry,
        conversation=conversation,
        provider_profile=None,
        workspace_root=workspace_root,
    )
    return record.run_id if record is not None else None


async def execute_workflow(
    session: "SessionContext",
    workflow_name: str,
    intent: str,
    *,
    completed_turns: int = 0,
    resume_run_id: str | None = None,
    continuation: str | None = None,
    resume_plan: ResumePlan | None = None,
    profile_override: str | None = None,
) -> WorkflowExecutionResult:
    """Execute a named workflow or default agent turn, including child returns."""
    return await _execute_workflow_turn(
        session,
        workflow_name,
        intent,
        completed_turns=completed_turns,
        resume_run_id=resume_run_id,
        continuation=continuation,
        resume_plan=resume_plan,
        profile_override=profile_override,
    )


async def _execute_workflow_turn(
    session: SessionContext,
    workflow_name: str,
    intent: str,
    *,
    completed_turns: int,
    resume_run_id: str | None = None,
    continuation: str | None = None,
    resume_plan: ResumePlan | None = None,
    profile_override: str | None = None,
    deliver_parent: bool = True,
) -> WorkflowExecutionResult:
    from agenthicc.runners.workflow_stack import (
        WorkflowDelivery,
        WorkflowStackError,
        workflow_return,
    )
    from agenthicc.workflows.checkpoint import workflow_fingerprint
    from agenthicc.workflows.stack_tools import WorkflowToolScope, bind_workflow_tools

    stack = session.workflow_stack
    frame = stack.top
    registered = session.workflow_registry.get(workflow_name)
    if (
        workflow_name == "default"
        and registered is None
        and not frame.depth
        and not frame.awaiting_child
        and resume_run_id is None
    ):
        if session.agent_runner is None:
            raise RuntimeError("No LLM configured for the default turn.")
        if not intent.strip():
            raise ValueError("Workflow intent must not be empty")
        stack.set_base("default")
        session.app_state.conversation.workflow_stack.set(stack.summaries())
        await _run_headless_agent_turn(session, intent, completed_turns)
        return WorkflowExecutionResult(session.session_id, "default", "", "complete", ())
    pinned = bool(frame.depth or frame.awaiting_child)
    workflow = frame.definition if pinned else registered
    if workflow is None:
        available = ", ".join(sorted(session.workflow_registry.names())) or "none"
        raise ValueError(f"Unknown workflow: {workflow_name!r}. Available: {available}")
    if session.agent_runner is None and resume_run_id is None:
        raise RuntimeError(
            "No LLM configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or configure Ollama."
        )
    if not intent.strip():
        raise ValueError("Workflow intent must not be empty")
    if resume_run_id is not None and session.session_conversation is None:
        raise ValueError("workflow resume requires the durable session conversation")
    if pinned:
        if frame.workflow_name != workflow_name:
            raise WorkflowStackError("frame_not_current", "The called workflow must finish first.")
        if resume_run_id is not None and resume_run_id != frame.run_id:
            raise WorkflowStackError(
                "frame_not_current", "Resume the current child workflow first."
            )
        if (
            resume_run_id is not None
            and registered is not None
            and workflow_fingerprint(workflow) != workflow_fingerprint(registered)
        ):
            raise ValueError(
                f"PLUGIN_INCOMPATIBLE: Workflow {workflow_name!r} changed since it paused."
            )
        if frame.awaiting_child:
            if resume_run_id != frame.run_id:
                raise WorkflowStackError(
                    "frame_not_running", "Resume the suspended workflow first."
                )
        if frame.status == "paused":
            if resume_run_id != frame.run_id:
                raise WorkflowStackError(
                    "frame_not_running", "Resume the current child workflow first."
                )
            frame = stack.resume(frame.frame_id)
    else:
        stack.set_base(workflow.name)
        frame = stack.top

    conversation = session.app_state.conversation
    conversation.workflow_stack.set(stack.summaries())
    tools = WorkflowToolScope(
        stack,
        session.workflow_registry,
        session.cfg,
        active_mode=lambda: session.app_state.active_mode(),
        approval=session.approval_svc,
        headless=True,
    )
    try:
        with bind_workflow_tools(tools):
            result, outcome = await _execute_registered_workflow(
                session,
                workflow,
                workflow_name,
                intent,
                frame,
                completed_turns=completed_turns,
                resume_run_id=resume_run_id,
                continuation=continuation,
                resume_plan=resume_plan,
                profile_override=profile_override,
            )
    except BaseException:
        if tools.take_pending() is not None:
            conversation.append_event(
                "system", {"text": "The pending workflow call was dropped when the turn ended."}
            )
        if frame.depth:
            stack.pause(frame.frame_id)
            conversation.workflow_stack.set(stack.summaries())
        raise
    pending = tools.take_pending()
    if pending is not None:
        if result.status == "suspended":
            stack.park_current_run(workflow)
            child = stack.push(pending)
            child_result = await _execute_workflow_turn(
                session,
                child.workflow_name,
                child.intent,
                completed_turns=completed_turns + 1,
                deliver_parent=False,
            )
            delivery = stack.take_return()
            if delivery is None:
                return child_result
            resumed = await _deliver_headless_return(
                session,
                delivery,
                completed_turns + 2,
                deliver_parent=deliver_parent,
            )
            return resumed if resumed is not None else child_result
        conversation.append_event(
            "system", {"text": "The pending workflow call was dropped when the turn ended."}
        )
    if frame.awaiting_child and result.status == "complete":
        stack.resume_parked_run()
        conversation.workflow_stack.set(stack.summaries())
    publishes = bool(workflow.invocation.publishes)
    if frame.depth:
        done = result.status == "complete"
        if done or (publishes and result.status == "failed"):
            returned = workflow_return(
                frame,
                outcome,
                session.app_state.workflow_run(),
                status="completed" if done else "failed",
            )
            parent = stack.complete(frame.frame_id, returned)
            conversation.workflow_stack.set(stack.summaries())
            if done:
                returned = await session.authored.publish_return(
                    returned, outcome, workflow, session.workflow_registry
                )
            stack.queue_return(WorkflowDelivery(parent.frame_id, frame.run_id, returned))
            if deliver_parent:
                delivery = stack.take_return()
                if delivery is not None:
                    await _deliver_headless_return(session, delivery, completed_turns + 1)
        else:
            stack.pause(frame.frame_id)
            conversation.workflow_stack.set(stack.summaries())
    elif publishes and result.status in {"complete", "failed"}:
        if result.status == "complete":
            returned = workflow_return(frame, outcome, session.app_state.workflow_run())
            await session.authored.publish_return(
                returned, outcome, workflow, session.workflow_registry
            )
        stack.clear_base()
        conversation.workflow_stack.set(stack.summaries())
    return result


async def _deliver_headless_return(
    session: SessionContext,
    delivery: WorkflowDelivery,
    completed_turns: int,
    *,
    deliver_parent: bool = True,
) -> WorkflowExecutionResult | None:
    """Give the now-current parent one turn carrying its child's result."""
    stack = session.workflow_stack
    parent = stack.top
    name = (
        parent.workflow_name if stack.depth or parent.awaiting_child else stack.base_dispatch_name
    )
    text = delivery.result.as_system_block()
    service = session.session_service
    if service is not None:
        await service.publish(
            session.session_id,
            source="headless",
            kind="turn_queued",
            payload={"text": text, "client_id": "workflow-return", "idempotency_key": delivery.key},
            turn_id=delivery.key,
        )
    if name is not None:
        return await _execute_workflow_turn(
            session,
            name,
            (parent.intent or text) if parent.awaiting_child else text,
            completed_turns=completed_turns,
            resume_run_id=parent.run_id if parent.awaiting_child else None,
            continuation=text if parent.awaiting_child else None,
            deliver_parent=deliver_parent,
        )
    await _run_headless_agent_turn(session, text, completed_turns)
    return None


async def _run_headless_agent_turn(
    session: SessionContext, text: str, completed_turns: int
) -> None:
    from agenthicc.agents.selection import turn_agent
    from agenthicc.memory.tool_history import ToolHistoryRecoveryRequired
    from agenthicc.runners.agent_turn import _run_agent_turn
    from agenthicc.runners.session_tools import session_tools
    from agenthicc.workflows.stack_tools import WorkflowToolScope, bind_workflow_tools

    agent = turn_agent(session.agents_registry, session.app_state.agent(), session.cfg)
    tools = WorkflowToolScope(
        session.workflow_stack,
        session.workflow_registry,
        session.cfg,
        active_mode=session.app_state.active_mode,
        approval=session.approval_svc,
        headless=True,
    )
    conversation = session.session_conversation
    owner = (
        conversation.owned(f"headless-turn:{uuid.uuid4().hex}") if conversation else nullcontext()
    )
    async with owner:
        if conversation is not None:
            recovery = conversation.retry_tool_history_recovery()
            if not recovery.provider_ready:
                raise ToolHistoryRecoveryRequired(recovery)
        session.approval_svc.reset_turn_memory()
        try:
            with bind_workflow_tools(tools):
                await _run_agent_turn(
                    text,
                    session.agent_runner,
                    session.processor,
                    session_memory=session.session_memory,
                    conversation_id=session.session_id,
                    max_agent_turns=agent.max_agent_turns,
                    conv_store=session.app_state.conversation,
                    app_state=session.app_state,
                    exec_cfg=agent.exec_cfg,
                    skills=session.reload.skills,
                    skill_permissions=session.cfg.agents.skill_permissions_for(agent.spec.name),
                    mention_cache=session.mention_cache,
                    extra_tools=list(session_tools(session)),
                    tool_surface=session.reload.tools,
                    mcp_registry=session.mcp_registry,
                    agents_registry=session.agents_registry,
                    subagent_registry=session.subagent_registry,
                    active_agent=agent.spec.name,
                    completed_turns=completed_turns,
                    approval_svc=session.approval_svc,
                    agent_spec=agent.spec,
                    agent_config=session.cfg,
                    workspace_access=session.workspace_access,
                    memory_router=session.memory_router,
                    semantic_index=session.semantic_index,
                    usage_ledger=session.usage_ledger,
                    browser_manager=session.browser_manager,
                )
        except BaseException:
            if tools.take_pending() is not None:
                session.app_state.conversation.append_event(
                    "system", {"text": "The pending workflow call was dropped when the turn ended."}
                )
            raise
    pending = tools.take_pending()
    if pending is not None:
        child = session.workflow_stack.push(pending)
        await execute_workflow(
            session, child.workflow_name, child.intent, completed_turns=completed_turns + 1
        )


async def _execute_registered_workflow(
    session: SessionContext,
    workflow_cls: type[WorkflowPlugin],
    workflow_name: str,
    intent: str,
    frame: WorkflowFrame,
    *,
    completed_turns: int,
    resume_run_id: str | None,
    continuation: str | None,
    resume_plan: ResumePlan | None,
    profile_override: str | None,
) -> tuple[WorkflowExecutionResult, object | None]:
    """Drive one workflow run without dispatching its parent's return turn.

    The processor must already be running before this function is called. A
    *continuation* is new user text supplied while resuming a checkpoint; it is
    added to the resumed workflow context exactly as the foreground TUI does.
    It is ignored for fresh runs. The function deliberately uses
    ``WorkflowPlugin.build_runner`` so specialized workflows such as
    ``code_plan`` and user-defined runners share the same construction path as
    the TUI.
    """
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
    from agenthicc.runners.workflow_handle import WorkflowRunHandle
    from agenthicc.workflows.stack_tools import WorkflowHandoff

    workflow_handle = None
    resuming = resume_run_id is not None
    session_conversation = session.session_conversation
    session_service = session.session_service
    if session_conversation is not None:
        # Detached execution and foreground workflow resume share the same
        # provider-history gate. Try deterministic local journal repair before
        # creating/claiming a workflow handle; an irreparable transcript must
        # not turn into an ordinary provider/workflow failure or reach an LLM.
        recovery = session_conversation.retry_tool_history_recovery()
        if not recovery.provider_ready:
            codes = ", ".join(code[:64] for code in recovery.issue_codes[:4])
            detail = codes or "validation_unavailable"
            return WorkflowExecutionResult(
                session_id=session.session_id,
                workflow_name=workflow_name,
                run_id=resume_run_id or "",
                status="blocked",
                phases=(),
                error=(
                    "Conversation history is preserved but is not provider-ready "
                    f"(issues: {detail}); workflow execution was not started."
                ),
                failure_category="tool_history_recovery_required",
            ), None
        # The turn boundary: announce background pools that finished since the
        # session last ran (recovery already marked unfinished ones interrupted).
        from agenthicc.subagents.delivery import deliver_session_background_notices

        deliver_session_background_notices(session)
        checkpoint_store = WorkflowCheckpointStore(session.session_id)
        if resuming:
            from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
            from agenthicc.workflows.registry import WorkflowRegistry

            coordinator = WorkflowRecoveryCoordinator(
                session.session_id,
                checkpoint_store=checkpoint_store,
            )
            resume_registry = session.workflow_registry
            if frame.depth or frame.awaiting_child:
                resume_registry = WorkflowRegistry()
                resume_registry.register(workflow_cls)
            records = coordinator.inspect(
                workflow_registry=resume_registry,
                conversation=session_conversation,
                provider_profile=None,
                workspace_root=str(getattr(session.workspace_scope, "primary_root", "") or ""),
            )
            record = next(
                (candidate for candidate in records if candidate.run_id == resume_run_id),
                None,
            )
            if record is None or not record.recoverable:
                reason = record.display_error if record is not None else "run was not found"
                raise ValueError(f"workflow run {resume_run_id!r} is not recoverable: {reason}")
            if record.workflow_name != workflow_cls.name:
                raise ValueError(
                    f"workflow run {resume_run_id!r} belongs to {record.workflow_name!r}, "
                    f"not {workflow_cls.name!r}"
                )
            workflow_handle = coordinator.rehydrate(
                record,
                workflow=workflow_cls,
                conversation=session_conversation,
                browser_manager=session.browser_manager,
                owner_id=f"headless:{os.getpid()}:{resume_run_id}",
            )
        else:
            from agenthicc.config import AgenthiccConfig
            from agenthicc.runners.workflow_profile import provider_profile_fingerprint

            provider_profile = session.cfg.execution.profile
            profile_fingerprint = (
                provider_profile_fingerprint(session.cfg, provider_profile)
                if isinstance(session.cfg, AgenthiccConfig)
                else ""
            )
            workflow_handle = WorkflowRunHandle.create(
                run_id=frame.run_id if frame.depth else uuid.uuid4().hex,
                workflow=workflow_cls,
                conversation=session_conversation,
                intent=intent,
                checkpoint_store=checkpoint_store,
                browser_manager=session.browser_manager,
                provider_profile=provider_profile,
                provider_profile_fingerprint=profile_fingerprint,
                workspace_root=str(getattr(session.workspace_scope, "primary_root", "") or ""),
            )
    provider_selection: ResumeProviderProfile | None = None
    selected_config = session.cfg
    selected_runner: object | None = session.agent_runner
    if resuming and workflow_handle is not None:
        from agenthicc.runners.workflow_profile import (
            WorkflowProfileResolutionError,
            resolve_resume_provider_profile,
            workflow_resume_agent_runner,
        )

        try:
            provider_selection = resolve_resume_provider_profile(
                session.cfg,
                saved_profile=workflow_handle.provider_profile,
                saved_binding_fingerprint=workflow_handle.provider_profile_fingerprint,
                explicit_profile=profile_override,
            )
            selected_config = provider_selection.config
            selected_runner = workflow_resume_agent_runner(
                session.agent_runner,
                provider_selection,
                conversation_id=workflow_handle.conversation.conversation_id,
            )
        except WorkflowProfileResolutionError as exc:
            workflow_handle.release_claim()
            return WorkflowExecutionResult(
                session_id=session.session_id,
                workflow_name=workflow_name,
                run_id=workflow_handle.run_id,
                status="blocked",
                phases=(),
                error=str(exc),
                failure_category=exc.code,
            ), None
        except Exception as exc:  # noqa: BLE001 - never dispatch an unresolved provider
            workflow_handle.release_claim()
            return WorkflowExecutionResult(
                session_id=session.session_id,
                workflow_name=workflow_name,
                run_id=workflow_handle.run_id,
                status="blocked",
                phases=(),
                error=(
                    "provider_profile_invalid: could not prepare workflow provider "
                    f"({type(exc).__name__})."
                ),
                failure_category="provider_profile_invalid",
            ), None
    effective_intent = (
        workflow_handle.original_intent if resuming and workflow_handle is not None else intent
    )
    error: str | None = None
    failure_category = ""
    runner_result: object | None = None
    suspended_for_child = False
    completed_phases: list[Event] = []

    def _record_completed_phase(event: Event) -> None:
        if event.event_type == "WorkflowPhaseCompleted":
            completed_phases.append(event)

    remove_phase_sink = session.processor.add_sink(_record_completed_phase)
    # TUX-08: a failed turn publishes a classified error event; headless
    # reports the last one as {"type": "error", "kind": …}.
    turn_failures: list[dict[str, object]] = []

    def _record_turn_failure(event: ConversationEvent) -> None:
        if event.kind == "error" and isinstance(event.payload.get("kind"), str):
            turn_failures.append(
                {
                    key: event.payload[key]
                    for key in ("kind", "message", "action")
                    if key in event.payload
                }
            )

    remove_failure_subscription = session.app_state.conversation.on_event(_record_turn_failure)
    try:
        # Establish a durable identity before any plugin setup.  A generic
        # bootstrap context is deliberately diagnostic-only until the runner
        # attaches its typed context, but it lets build_params/build_runner
        # failures retain a run id and a durable explanation.
        if workflow_handle is not None and not resuming:
            from agenthicc.workflows.plugin import WorkflowContext

            _phase_specs = getattr(workflow_cls, "phases", ())
            workflow_handle.attach_bootstrap_context(
                WorkflowContext(
                    intent=effective_intent,
                    run_id=workflow_handle.run_id,
                    workflow_name=workflow_cls.name,
                    current_phase=(
                        getattr(_phase_specs[0], "name", None) if _phase_specs else None
                    ),
                )
            )
            workflow_handle.claim(f"headless:{os.getpid()}:{workflow_handle.run_id}")
            session.workflow_stack.attach_run(workflow_handle.run_id, workflow_handle.current_phase)
            session.app_state.conversation.workflow_stack.set(session.workflow_stack.summaries())
            if _phase_specs:
                workflow_handle.update_phase(_phase_specs[0].name, 0, 0)
            else:
                workflow_handle.save_checkpoint(reason="started")
            initializer = getattr(workflow_cls, "create_initial_context", None)
            if callable(initializer):
                initial_context = initializer(
                    effective_intent,
                    workflow_handle.run_id,
                    session.session_memory,
                )
                if initial_context is not None:
                    workflow_handle.attach_context(initial_context)
                    if _phase_specs:
                        workflow_handle.update_phase(_phase_specs[0].name, 0, 0)
        elif workflow_handle is not None:
            assert provider_selection is not None
            previous_profile_state = (
                workflow_handle.provider_profile,
                workflow_handle.provider_profile_fingerprint,
                workflow_handle.provider_profile_origin,
                workflow_handle.provider_profile_history,
            )
            try:
                workflow_handle.select_provider_profile(
                    provider_selection.profile_name,
                    provider_selection.binding_fingerprint,
                )
                workflow_handle.mark_resuming()
                workflow_handle.persist_checkpoint(reason="resuming")
            except Exception as exc:  # noqa: BLE001 - preserve the prior durable pause
                (
                    workflow_handle.provider_profile,
                    workflow_handle.provider_profile_fingerprint,
                    workflow_handle.provider_profile_origin,
                    workflow_handle.provider_profile_history,
                ) = previous_profile_state
                workflow_handle.lifecycle = "paused"
                workflow_handle.pause_requested = True
                workflow_handle.release_claim()
                raise _WorkflowResumeTransitionError(
                    f"could not persist the resuming checkpoint: {type(exc).__name__}: {exc}"
                ) from exc
            memory = session.session_memory
            if memory is not None:
                from agenthicc.runners.agent_turn import (
                    _preserve_interrupted_memory,
                )

                _preserve_interrupted_memory(memory)
                phase = workflow_handle.current_phase or "current"
                resume_prompt = (
                    "[WORKFLOW RESUME]\n"
                    f"Workflow: {workflow_cls.name}\n"
                    f"Current phase: {phase}\n"
                    "The prior agent turn was interrupted safely; preserve completed work "
                    "and continue from the saved phase.\n"
                    f"Original intent: {workflow_handle.original_intent}\n"
                    + (f"User continuation: {continuation}" if continuation else "Continue.")
                )
                if continuation:
                    workflow_handle.append_continuation(continuation)
                memory.add_user(resume_prompt)

        try:
            workspace_scope = session.workspace_scope
            workspace_access = session.workspace_access
        except AttributeError:
            # Keep lightweight SessionContext test doubles and third-party callers
            # compatible with the pre-PRD-168 shape.
            workspace_scope = None
            workspace_access = None
        workflow_config = WorkflowConfig(
            conv_store=session.app_state.conversation,
            app_state=session.app_state,
            processor=session.processor,
            agent_runner=cast("AgentRunnerBase", selected_runner),
            approval_svc=session.approval_svc,
            cfg=selected_config,
            skills=session.reload.skills,
            tool_surface=session.reload.tools,
            reload_service=session.reload,
            mcp_registry=session.mcp_registry,
            mention_cache=session.mention_cache,
            agents_registry=session.agents_registry,
            subagent_registry=session.subagent_registry,
            memory_router=session.memory_router,
            semantic_index=session.semantic_index,
            completed_turns=completed_turns,
            session_memory=session.session_memory,
            conversation_id=session.session_id,
            usage_ledger=session.usage_ledger,
            workflow_handle=workflow_handle,
            browser_manager=session.browser_manager,
            browser_tools=list(session.browser_tools),
            workspace_scope=workspace_scope,
            workspace_access=workspace_access,
            params=frame.params
            or workflow_cls.build_params(selected_config.workflows.get(workflow_name, {})),
            terminal_wait_policies={
                phase.name: phase.terminal_wait_policy for phase in workflow_cls.phases
            },
        )
        runner = workflow_cls.build_runner(workflow_config, session.mode_manager)
        if session_service is not None:
            await session_service.publish(
                session.session_id,
                source="headless",
                kind="workflow_started",
                payload={"workflow": workflow_name, "intent": effective_intent},
            )
        if workflow_handle is not None:
            from agenthicc.runners.workflow_execution_context import (
                bind_workflow_execution,
            )

            with bind_workflow_execution(
                workflow_handle.run_id,
                resume_plan=resume_plan,
                policy=frame.policy if frame.depth else None,
                model_request=frame.model_request(),
            ):
                runner_result = (
                    await runner.resume(workflow_handle.context)
                    if resuming
                    else await runner.run(intent)
                )
        else:
            runner_result = await runner.run(intent)
        # Specialized runners may intentionally return a typed FAILED state
        # after a phase-local agent/provider exception. That is not successful
        # completion: convert it to the same durable failure boundary used by
        # the exception path before the normal-return safety net can mark the
        # handle complete.
        result_state_value = _optional_request_field(runner_result, "state", "")
        result_state = _optional_request_field(result_state_value, "name", "")
        result_failure = _optional_request_field(runner_result, "fail_reason", "")
        if (
            workflow_handle is not None
            and isinstance(result_state, str)
            and result_state.lower() in {"failed", "failure"}
        ):
            error = str(result_failure or "workflow returned unsuccessfully")
            checkpoint = workflow_handle.finalize_failure(
                error,
                kind=_workflow_failure_kind(error),
            )
            workflow_run = session.app_state.workflow_run()
            if is_dataclass(workflow_run):
                session.app_state.workflow_run.set(
                    replace(
                        workflow_run,
                        status="paused" if checkpoint is not None else "failed",
                        current_phase=workflow_handle.current_phase if checkpoint else None,
                    )
                )
        # Built-in runners persist this boundary themselves, but a plugin
        # runner is allowed to return a successful result without knowing
        # about the session owner's lifecycle bookkeeping.  Do not leave a
        # successfully returned run in ``running``/``resuming``: that would
        # make the next ``--continue`` discover it as an interrupted run and
        # could replay work.  This mirrors the TUI's normal-return safety net.
        if workflow_handle is not None and workflow_handle.lifecycle in {
            "running",
            "resuming",
        }:
            workflow_run_after_return = session.app_state.workflow_run()
            if not (
                workflow_run_after_return is not None
                and workflow_run_after_return.run_id == workflow_handle.run_id
                and workflow_run_after_return.status == "failed"
            ):
                workflow_handle.mark_terminal("complete")
                if workflow_handle.checkpoint_supported:
                    workflow_handle.save_checkpoint(reason="complete")
    except WorkflowHandoff:
        assert workflow_handle is not None
        try:
            workflow_handle.suspend_for_child()
        except (OSError, ValueError) as exc:
            error = "The parent workflow could not save its handoff checkpoint."
            workflow_handle.finalize_failure(exc, kind="checkpoint_storage")
            failure_category = "checkpoint_storage"
        else:
            suspended_for_child = True
    except (asyncio.CancelledError, KeyboardInterrupt):
        remove_phase_sink()
        remove_failure_subscription()
        if workflow_handle is not None:
            workflow_handle.finalize_failure("cancelled", kind="user_cancelled")
            workflow_handle.release_claim()
        raise
    except Exception as exc:  # noqa: BLE001
        # A failed agent turn reports a fixed kind and agenthicc's own copy;
        # the provider's exception text never reaches the JSON (TUX-08).
        error = failure_reason(exc)
        from agenthicc.memory.compactor import CompactionRecoveryRequired

        if isinstance(exc, _WorkflowResumeTransitionError):
            failure_category = "workflow_resume_transition_failed"
            if workflow_handle is not None:
                workflow_handle.lifecycle = "paused"
                workflow_handle.pause_requested = True
                workflow_handle.release_claim()
        else:
            if isinstance(exc, CompactionRecoveryRequired):
                failure_category = exc.failure_category
            if workflow_handle is not None:
                # A failed turn's exception keeps its status and provider
                # metadata for the checkpoint, which persists only its copy.
                checkpoint = workflow_handle.finalize_failure(
                    exc if reported_failure(exc) is not None else error,
                    kind=_workflow_failure_kind(exc),
                )
                workflow_run = session.app_state.workflow_run()
                if is_dataclass(workflow_run):
                    session.app_state.workflow_run.set(
                        replace(
                            workflow_run,
                            status="paused" if checkpoint is not None else "failed",
                            current_phase=workflow_handle.current_phase if checkpoint else None,
                        )
                    )
    try:
        await session.processor.drain()
    except BaseException as exc:
        if workflow_handle is not None:
            failure_kind = (
                "user_cancelled"
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
                else "workflow_error"
            )
            workflow_handle.finalize_failure(
                f"processor drain failed: {type(exc).__name__}: {exc}",
                kind=failure_kind,
            )
            workflow_handle.release_claim()
        raise
    finally:
        remove_phase_sink()
        remove_failure_subscription()
    workflow_run = session.app_state.workflow_run()
    if (
        workflow_handle is not None
        and workflow_run is not None
        and workflow_run.run_id != workflow_handle.run_id
    ):
        workflow_run = None
    status = (workflow_run.status if workflow_run is not None else "") or "failed"
    if error is None and workflow_handle is not None and workflow_handle.lifecycle == "complete":
        status = "complete"
    if error is None and status == "failed":
        fail_reason = getattr(runner_result, "fail_reason", "")
        if isinstance(fail_reason, str) and fail_reason:
            error = fail_reason
        else:
            error = "workflow returned unsuccessfully"
        if workflow_handle is not None:
            checkpoint = workflow_handle.finalize_failure(
                error,
                kind=_workflow_failure_kind(error),
            )
            status = "paused" if checkpoint is not None else "failed"
            if is_dataclass(workflow_run):
                session.app_state.workflow_run.set(
                    replace(
                        workflow_run,
                        status=status,
                        current_phase=workflow_handle.current_phase if checkpoint else None,
                    )
                )
    if workflow_handle is not None and workflow_handle.lifecycle == "paused":
        status = "suspended" if suspended_for_child else "paused"
        # The handle owns the redacted, bounded diagnostic. Do not expose a
        # provider exception's raw request dump in the JSON result. A failed
        # resume transition is not checkpointed, so keep its immediate error.
        if (
            not suspended_for_child
            and workflow_handle.last_error
            and failure_category != "workflow_resume_transition_failed"
        ):
            error = workflow_handle.last_error
    run_id = (workflow_run.run_id if workflow_run is not None else "") or (
        workflow_handle.run_id if workflow_handle is not None else ""
    )
    phases: list[str] = []
    phase_metadata: dict[str, object] = {}
    for event in completed_phases:
        event_run_id = event.payload.get("run_id")
        if run_id and event_run_id != run_id:
            continue
        phase_name = event.payload.get("phase_name")
        if isinstance(phase_name, str) and phase_name:
            phases.append(phase_name)
            metadata = event.payload.get("metadata")
            if isinstance(metadata, dict):
                phase_metadata[phase_name] = dict(metadata)
        if not run_id and isinstance(event_run_id, str):
            run_id = event_run_id
    if not phases and workflow_run is not None:
        phases = [str(record.phase_name) for record in workflow_run.phase_history]
    result = WorkflowExecutionResult(
        session_id=session.session_id,
        workflow_name=workflow_name,
        run_id=run_id,
        status=status,
        phases=tuple(phases),
        error=error,
        phase_metadata=phase_metadata,
        failure_category=failure_category,
        failure=turn_failures[-1] if turn_failures and status in {"failed", "paused"} else None,
    )
    handle_phase = (workflow_handle.current_phase if workflow_handle is not None else None) or ""
    handle_failure_kind = workflow_handle.failure_kind if workflow_handle is not None else None
    if session_service is not None and not suspended_for_child:
        try:
            if result.status == "paused":
                await session_service.publish(
                    session.session_id,
                    source="headless",
                    kind="workflow_paused_after_error",
                    payload={
                        "workflow": result.workflow_name,
                        "run_id": result.run_id,
                        "phase": handle_phase,
                        "failure_kind": handle_failure_kind
                        or result.failure_category
                        or "workflow_error",
                        "revision": (
                            workflow_handle.checkpoint_revision
                            if workflow_handle is not None
                            else 0
                        ),
                        "resumable": True,
                    },
                )
            elif result.error is not None:
                await session_service.publish(
                    session.session_id,
                    source="headless",
                    kind="workflow_failure_diagnostic",
                    payload={
                        "workflow": result.workflow_name,
                        "run_id": result.run_id,
                        "phase": handle_phase,
                        "failure_kind": handle_failure_kind or "workflow_error",
                        "resumable": False,
                    },
                )
            await session_service.publish(
                session.session_id,
                source="headless",
                kind="workflow_run_failed"
                if result.status in {"failed", "paused"}
                else "workflow_run_completed",
                payload={
                    "workflow": result.workflow_name,
                    "run_id": result.run_id,
                    "status": result.status,
                    "phases": list(result.phases),
                    "error": result.error,
                },
            )
        except BaseException:
            if workflow_handle is not None:
                workflow_handle.release_claim()
            raise
    if workflow_handle is not None:
        workflow_handle.release_claim()
    outcome = runner_result
    if outcome is None and workflow_handle is not None:
        outcome = workflow_handle.context
    return result, outcome


async def run_headless_workflow(
    ctx: "CLIContext",
    workflow_name: str,
    intent: str,
) -> WorkflowExecutionResult:
    """Build a durable session, run one workflow, and close its resources."""
    from agenthicc.runners.tui_session import _build_session_context

    cassette_base = Path(ctx.record_cassette) if ctx.record_cassette else None
    resume_id, owner_lease = _resolve_headless_session(ctx)
    session = await _build_session_context(
        resume_id,
        list(ctx.set_overrides),
        cassette_base,
        config_path=ctx.config_path,
        cli_secret_overrides=list(ctx.set_secret_overrides),
        mode_name=ctx.mode_name,
        workflow_name=workflow_name,
        headless=True,
        owner_lease=owner_lease,
        config=ctx.config,
    )
    terminal_token = None
    processor_task: asyncio.Task[object] | None = None
    remove_authored_sink = session.processor.add_sink(_emit_authored_json)
    try:
        session.app_state.cli_flags = ctx.flags
        session.approval_svc = _HeadlessApprovalService(ctx.flags.dangerously_skip_permissions)
        try:
            workspace_access = session.workspace_access
        except AttributeError:
            workspace_access = None
        if workspace_access is not None:
            workspace_access.set_approval_service(session.approval_svc)
        from agenthicc.background.terminals import (
            reset_current_terminal_manager,
            set_current_terminal_manager,
        )

        terminal_manager = getattr(session, "terminal_manager", None)
        terminal_token = (
            set_current_terminal_manager(terminal_manager) if terminal_manager is not None else None
        )
        processor_task = asyncio.create_task(session.processor.run(), name="headless-processor")
        await asyncio.sleep(0)
        resume_run_id = (
            _select_headless_workflow_resume(session, workflow_name)
            if resume_id is not None
            else None
        )
        from agenthicc.runners.workflow_profile import execution_profile_override

        return await execute_workflow(
            session,
            workflow_name,
            intent,
            resume_run_id=resume_run_id,
            profile_override=execution_profile_override(ctx.set_overrides),
        )
    finally:
        if terminal_token is not None:
            reset_current_terminal_manager(terminal_token)
        try:
            await _close_headless_session(session, processor_task, cassette_base)
        finally:
            remove_authored_sink()


async def _run_headless_workflow_stream(ctx: "CLIContext") -> None:
    """Run one workflow for every non-empty stdin line and emit JSON results."""
    from agenthicc.runners.tui_session import _build_session_context
    from agenthicc.runners.workflow_profile import execution_profile_override

    workflow_name = ctx.workflow_name
    if not workflow_name:
        raise ValueError("--workflow requires a workflow name")
    cassette_base = Path(ctx.record_cassette) if ctx.record_cassette else None
    resume_id, owner_lease = _resolve_headless_session(ctx)
    session = await _build_session_context(
        resume_id,
        list(ctx.set_overrides),
        cassette_base,
        config_path=ctx.config_path,
        cli_secret_overrides=list(ctx.set_secret_overrides),
        mode_name=ctx.mode_name,
        workflow_name=workflow_name,
        headless=True,
        owner_lease=owner_lease,
        config=ctx.config,
    )
    terminal_token = None
    processor_task: asyncio.Task[object] | None = None
    remove_authored_sink = session.processor.add_sink(_emit_authored_json)
    try:
        session.app_state.cli_flags = ctx.flags
        session.approval_svc = _HeadlessApprovalService(ctx.flags.dangerously_skip_permissions)
        try:
            workspace_access = session.workspace_access
        except AttributeError:
            workspace_access = None
        if workspace_access is not None:
            workspace_access.set_approval_service(session.approval_svc)
        from agenthicc.background.terminals import (
            reset_current_terminal_manager,
            set_current_terminal_manager,
        )

        terminal_manager = getattr(session, "terminal_manager", None)
        terminal_token = (
            set_current_terminal_manager(terminal_manager) if terminal_manager is not None else None
        )
        processor_task = asyncio.create_task(session.processor.run(), name="headless-processor")
        await asyncio.sleep(0)
        print(
            json.dumps(
                {
                    "status": "ready",
                    "mode": "headless",
                    "workflow": workflow_name,
                    "session_id": session.session_id,
                }
            ),
            flush=True,
        )
        completed_turns = 0
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            intent = line.strip()
            if not intent:
                continue
            command = intent.split(maxsplit=1)
            if command[0] == "/reload":
                from agenthicc.runners.reload import parse_reload_surfaces

                try:
                    surfaces = parse_reload_surfaces(command[1] if len(command) == 2 else "")
                except ValueError as exc:
                    print(
                        json.dumps(
                            {"event": "error", "code": "invalid_reload", "message": str(exc)}
                        ),
                        flush=True,
                    )
                else:
                    report = await session.reload.reload(surfaces, reason="command")
                    print(json.dumps({"event": "reload", **report.to_dict()}), flush=True)
                continue
            if intent == "/loops" or intent == "/loop" or intent.startswith("/loop "):
                print(
                    json.dumps(
                        {
                            "event_type": "Error",
                            "code": "loop_interactive_required",
                            "message": "Loop scheduling and job management require an interactive TUI session.",
                        }
                    ),
                    flush=True,
                )
                continue
            try:
                resume_run_id = (
                    _select_headless_workflow_resume(session, workflow_name)
                    if resume_id is not None or completed_turns > 0
                    else None
                )
                result = await execute_workflow(
                    session,
                    workflow_name,
                    intent,
                    completed_turns=completed_turns,
                    resume_run_id=resume_run_id,
                    profile_override=execution_profile_override(ctx.set_overrides),
                )
            except Exception as exc:  # noqa: BLE001
                from agenthicc.memory.tool_history import ToolHistoryRecoveryRequired

                if isinstance(exc, ToolHistoryRecoveryRequired):
                    result = WorkflowExecutionResult(
                        session_id=session.session_id,
                        workflow_name=workflow_name,
                        run_id="",
                        status="blocked",
                        phases=(),
                        error=str(exc),
                        failure_category="tool_history_recovery_required",
                    )
                else:
                    result = WorkflowExecutionResult(
                        session_id=session.session_id,
                        workflow_name=workflow_name,
                        run_id="",
                        status="failed",
                        phases=(),
                        error=failure_reason(exc),
                        failure=reported_failure(exc),
                    )
            record = result.error_record()
            if record is not None:
                print(json.dumps(record), flush=True)
            print(json.dumps(result.to_dict()), flush=True)
            completed_turns += 1
    finally:
        if terminal_token is not None:
            reset_current_terminal_manager(terminal_token)
        try:
            await _close_headless_session(session, processor_task, cassette_base)
        finally:
            remove_authored_sink()


async def _close_headless_session(
    session: "SessionContext",
    processor_task: asyncio.Task[object] | None,
    cassette_base: Path | None,
) -> None:
    """Close durable handles and background services for a headless session."""
    session_service = session.session_service
    try:
        await session.reload.close()
        projection_task = session.kernel_projection_task
        if projection_task is not None:
            projection_task.cancel()
            await asyncio.gather(projection_task, return_exceptions=True)
        await session.processor.drain()
        await session.processor.stop()
        if processor_task is not None:
            # A stopped processor finishes its effects and flushes its log
            # before run() returns; cancel only one that does not stop.
            await asyncio.wait({processor_task}, timeout=10)
            if not processor_task.done():
                processor_task.cancel()
            await asyncio.gather(processor_task, return_exceptions=True)
        if session.semantic_index is not None:
            await session.semantic_index.aclose()
        session.session_log.close()
        close_memory = getattr(session.session_memory, "close", None)
        if callable(close_memory):
            close_memory()
        if session.mcp_registry is not None:
            await session.mcp_registry.shutdown()
        browser_manager = session.browser_manager
        if browser_manager is not None:
            await browser_manager.close_session()
        terminal_manager = session.terminal_manager
        if terminal_manager is not None:
            await terminal_manager.close()
        if session_service is not None:
            await session_service.close()
        if cassette_base is not None:
            from agenthicc.runners.tui_session import _write_cassette_meta

            _write_cassette_meta(cassette_base / session.session_id, session.session_id)
    finally:
        owner_lease = session.owner_lease
        if owner_lease is not None:
            owner_lease.release()


async def _run_headless(ctx: CLIContext | None = None) -> None:
    if ctx is not None and ctx.workflow_name:
        try:
            await _run_headless_workflow_stream(ctx)
        except SessionAlreadyActiveError as exc:
            print(json.dumps(exc.to_dict(), sort_keys=True), flush=True)
            raise SystemExit(exc.exit_code) from exc
        except SessionStorageError as exc:
            print(
                json.dumps(
                    {"status": "error", "code": exc.code, "message": str(exc)},
                    sort_keys=True,
                ),
                flush=True,
            )
            raise SystemExit(1) from exc
        return

    from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings

    # The client-neutral stdin runner historically did not construct a full
    # TUI SessionContext, but it still creates a durable SessionService
    # projection when callers supply --resume/--continue.  Claim that ID here
    # so those flags cannot silently bypass the single-owner boundary.
    owner_lease = None
    durable_session_id: str | None = None
    if ctx is not None and (ctx.resume_id is not None or ctx.continue_session):
        try:
            durable_session_id, owner_lease = _resolve_headless_session(ctx)
            from agenthicc.runners.session_lease import SessionOpenCoordinator

            coordinator = SessionOpenCoordinator()
            if durable_session_id is None:
                durable_session_id = uuid.uuid4().hex
                owner_lease = coordinator.acquire_new(
                    durable_session_id,
                    entrypoint="headless",
                )
            elif owner_lease is None:
                owner_lease = coordinator.acquire_existing(
                    durable_session_id,
                    entrypoint="headless",
                )
        except SessionAlreadyActiveError as exc:
            print(json.dumps(exc.to_dict(), sort_keys=True), flush=True)
            raise SystemExit(exc.exit_code) from exc
        except SessionStorageError as exc:
            print(
                json.dumps(
                    {"status": "error", "code": exc.code, "message": str(exc)},
                    sort_keys=True,
                ),
                flush=True,
            )
            raise SystemExit(1) from exc

    state = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
    if durable_session_id is not None:
        state = replace(state, session_id=durable_session_id)

    from agenthicc.session_service import SessionCommand, SessionService

    session_service: SessionService | None = None
    proc_task: asyncio.Task[object] | None = None
    try:
        session_service = SessionService()
        await session_service.ensure_session(
            state.session_id,
            project_root=Path.cwd(),
            capabilities=frozenset({"read", "control", "workspace"}),
        )
        processor = EventProcessor(initial_state=state, persist=False)
        sub = processor.subscribe()
        proc_task = asyncio.create_task(processor.run())
    except BaseException:
        if proc_task is not None:
            proc_task.cancel()
            await asyncio.gather(proc_task, return_exceptions=True)
        if session_service is not None:
            await session_service.close()
        if owner_lease is not None:
            owner_lease.release()
        raise
    assert session_service is not None
    assert proc_task is not None
    print(
        json.dumps({"status": "ready", "mode": "headless", "session_id": state.session_id}),
        flush=True,
    )
    try:
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            if text == "/loops" or text == "/loop" or text.startswith("/loop "):
                print(
                    json.dumps(
                        {
                            "event_type": "Error",
                            "code": "loop_interactive_required",
                            "message": "Loop scheduling and job management require an interactive TUI session.",
                        }
                    ),
                    flush=True,
                )
                continue
            intent_id = uuid.uuid4().hex
            command_result = await session_service.submit(
                SessionCommand(
                    kind="submit_message",
                    session_id=state.session_id,
                    client_id="headless",
                    idempotency_key=f"headless-{intent_id}",
                    payload={"text": text},
                    capabilities=frozenset({"read", "control", "workspace"}),
                )
            )
            await processor.emit(
                Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text})
            )
            turn_id = command_result.data.get("turn_id")
            await session_service.publish(
                state.session_id,
                source="headless",
                kind="intent_created",
                payload={"intent_id": intent_id, "raw_text": text},
                turn_id=turn_id if isinstance(turn_id, str) else None,
            )
            try:
                snap = await asyncio.wait_for(sub.get(), timeout=2.0)
                intent = snap.intents.get(intent_id)
                print(
                    json.dumps(
                        {
                            "event_type": "IntentCreated",
                            "intent_id": intent_id,
                            "status": intent.status.value if intent else "pending",
                        }
                    ),
                    flush=True,
                )
            except asyncio.TimeoutError:
                print(json.dumps({"event_type": "Error", "message": "timeout"}), flush=True)
    finally:
        await session_service.close()
        proc_task.cancel()
        await asyncio.gather(proc_task, return_exceptions=True)
        if owner_lease is not None:
            owner_lease.release()
