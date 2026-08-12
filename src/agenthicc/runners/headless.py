"""Headless runner — emits kernel events as JSON lines to stdout."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agenthicc.runners.session_lease import (
    SessionAlreadyActiveError,
    SessionOwnerLease,
    SessionStorageError,
)

if TYPE_CHECKING:
    from agenthicc.cli.context import CLIContext
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.runners.session_lease import SessionOwnerLease

__all__ = ["WorkflowExecutionResult", "execute_workflow", "run_headless_workflow"]


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
        return result


class _HeadlessApprovalService:
    """Approval adapter for automation, defaulting to fail closed.

    Human approval overlays do not exist in headless mode.  A workflow may opt
    into automatic approval with ``--dangerously-skip-permissions``; otherwise
    approval-gated actions receive a denial instead of hanging forever waiting
    for a UI response.
    """

    def __init__(self, allow: bool) -> None:
        self._allow = allow

    async def request_approval(self, req: object) -> object:
        from agenthicc.tools.approval import ApprovalResponse  # noqa: PLC0415

        # A dangerous-permissions flag is an explicit capability approval for
        # automation, not an implicit expansion of the workspace boundary.
        # Outside-workspace access needs a caller-provided approval adapter so
        # headless runs never silently exfiltrate or mutate parent paths.
        try:
            workspace_access = object.__getattribute__(req, "workspace_access")
        except AttributeError:
            workspace_access = None
        if workspace_access:
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
    from agenthicc.runners.session_lease import SessionOpenCoordinator  # noqa: PLC0415

    selected = SessionOpenCoordinator().select_latest_for_cwd(Path.cwd(), entrypoint="headless")
    if selected is None:
        return None, None
    return selected


async def execute_workflow(
    session: "SessionContext",
    workflow_name: str,
    intent: str,
    *,
    completed_turns: int = 0,
) -> WorkflowExecutionResult:
    """Execute one registered workflow using an existing session context.

    The processor must already be running before this function is called.  It
    deliberately uses ``WorkflowPlugin.build_runner`` so specialized workflows
    such as ``code_plan`` and user-defined runners share the same construction
    path as the TUI.
    """
    from agenthicc.workflows.config import WorkflowConfig  # noqa: PLC0415
    from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore  # noqa: PLC0415
    from agenthicc.runners.workflow_handle import WorkflowRunHandle  # noqa: PLC0415

    workflow_cls = session.workflow_registry.get(workflow_name)
    if workflow_cls is None:
        available = ", ".join(sorted(session.workflow_registry.names())) or "none"
        raise ValueError(f"Unknown workflow: {workflow_name!r}. Available: {available}")
    if session.agent_runner is None:
        raise RuntimeError(
            "No LLM configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or configure Ollama."
        )
    if not intent.strip():
        raise ValueError("Workflow intent must not be empty")

    workflow_handle = None
    session_conversation = getattr(session, "session_conversation", None)
    if session_conversation is not None:
        workflow_handle = WorkflowRunHandle.create(
            run_id=uuid.uuid4().hex,
            workflow=workflow_cls,
            conversation=session_conversation,
            intent=intent,
            checkpoint_store=WorkflowCheckpointStore(session.session_id),
            browser_manager=getattr(session, "browser_manager", None),
            provider_profile=session.cfg.execution.profile,
            workspace_root=str(
                getattr(getattr(session, "workspace_scope", None), "primary_root", "") or ""
            ),
        )
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
        agent_runner=session.agent_runner,
        approval_svc=session.approval_svc,
        cfg=session.cfg,
        skills=session.skills,
        plugin_tools=session.project_plugins,
        mcp_registry=session.mcp_registry,
        mention_cache=session.mention_cache,
        agents_registry=session.agents_registry,
        memory_router=session.memory_router,
        semantic_index=session.semantic_index,
        completed_turns=completed_turns,
        session_memory=session.session_memory,
        conversation_id=session.session_id,
        usage_ledger=getattr(session, "usage_ledger", None),
        workflow_handle=workflow_handle,
        browser_manager=getattr(session, "browser_manager", None),
        browser_tools=list(getattr(session, "browser_tools", [])),
        workspace_scope=workspace_scope,
        workspace_access=workspace_access,
        params=workflow_cls.build_params(session.cfg.workflows.get(workflow_name, {})),
        terminal_wait_policies={
            phase.name: phase.terminal_wait_policy for phase in workflow_cls.phases
        },
    )
    runner = workflow_cls.build_runner(workflow_config, session.mode_manager)
    session_service = getattr(session, "session_service", None)
    if session_service is not None:
        await session_service.publish(
            session.session_id,
            source="headless",
            kind="workflow_started",
            payload={"workflow": workflow_name, "intent": intent},
        )
    if workflow_handle is not None:
        # Headless execution is another durable owner of the same run
        # namespace as the TUI. Claim only after construction and the startup
        # projection succeed so setup errors cannot strand a lease.
        workflow_handle.claim(f"headless:{os.getpid()}:{workflow_handle.run_id}")
    error: str | None = None
    runner_result: object | None = None
    try:
        runner_result = await runner.run(intent)
    except (asyncio.CancelledError, KeyboardInterrupt):
        if workflow_handle is not None:
            workflow_handle.release_claim()
        raise
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    try:
        await session.processor.drain()
    except BaseException:
        if workflow_handle is not None:
            workflow_handle.release_claim()
        raise
    workflow_run = session.app_state.workflow_run()
    status = str(getattr(workflow_run, "status", "failed") or "failed")
    if error is None and status == "failed":
        fail_reason = getattr(runner_result, "fail_reason", "")
        if isinstance(fail_reason, str) and fail_reason:
            error = fail_reason
    run_id = str(getattr(workflow_run, "run_id", "") or "")
    phases: list[str] = []
    phase_metadata: dict[str, object] = {}
    for event in getattr(session.processor, "event_log", []):
        if getattr(event, "event_type", "") != "WorkflowPhaseCompleted":
            continue
        payload = getattr(event, "payload", {})
        event_run_id = payload.get("run_id") if isinstance(payload, dict) else None
        if run_id and event_run_id != run_id:
            continue
        phase_name = payload.get("phase_name") if isinstance(payload, dict) else None
        if isinstance(phase_name, str) and phase_name:
            phases.append(phase_name)
            if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict):
                phase_metadata[phase_name] = dict(payload["metadata"])
        if not run_id:
            if isinstance(event_run_id, str):
                run_id = event_run_id
    if not phases and workflow_run is not None:
        phases = [str(record.phase_name) for record in getattr(workflow_run, "phase_history", [])]
    result = WorkflowExecutionResult(
        session_id=session.session_id,
        workflow_name=workflow_name,
        run_id=run_id,
        status=status,
        phases=tuple(phases),
        error=error,
        phase_metadata=phase_metadata,
    )
    if session_service is not None:
        try:
            await session_service.publish(
                session.session_id,
                source="headless",
                kind="workflow_run_failed"
                if result.status == "failed"
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
    return result


async def run_headless_workflow(
    ctx: "CLIContext",
    workflow_name: str,
    intent: str,
) -> WorkflowExecutionResult:
    """Build a durable session, run one workflow, and close its resources."""
    from agenthicc.runners.tui_session import _build_session_context  # noqa: PLC0415

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
    )
    terminal_token = None
    processor_task: asyncio.Task[object] | None = None
    try:
        session.app_state.cli_flags = ctx.flags
        session.approval_svc = _HeadlessApprovalService(  # type: ignore[assignment]
            ctx.flags.dangerously_skip_permissions
        )
        try:
            workspace_access = session.workspace_access
        except AttributeError:
            workspace_access = None
        if workspace_access is not None:
            workspace_access.set_approval_service(session.approval_svc)
        from agenthicc.background.terminals import (  # noqa: PLC0415
            reset_current_terminal_manager,
            set_current_terminal_manager,
        )

        terminal_manager = getattr(session, "terminal_manager", None)
        terminal_token = (
            set_current_terminal_manager(terminal_manager) if terminal_manager is not None else None
        )
        processor_task = asyncio.create_task(session.processor.run(), name="headless-processor")
        await asyncio.sleep(0)
        return await execute_workflow(session, workflow_name, intent)
    finally:
        if terminal_token is not None:
            reset_current_terminal_manager(terminal_token)
        await _close_headless_session(
            session,
            processor_task,
            cassette_base,
        )


async def _run_headless_workflow_stream(ctx: "CLIContext") -> None:
    """Run one workflow for every non-empty stdin line and emit JSON results."""
    from agenthicc.runners.tui_session import _build_session_context  # noqa: PLC0415

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
    )
    terminal_token = None
    processor_task: asyncio.Task[object] | None = None
    try:
        session.app_state.cli_flags = ctx.flags
        session.approval_svc = _HeadlessApprovalService(  # type: ignore[assignment]
            ctx.flags.dangerously_skip_permissions
        )
        try:
            workspace_access = session.workspace_access
        except AttributeError:
            workspace_access = None
        if workspace_access is not None:
            workspace_access.set_approval_service(session.approval_svc)
        from agenthicc.background.terminals import (  # noqa: PLC0415
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
            try:
                result = await execute_workflow(
                    session,
                    workflow_name,
                    intent,
                    completed_turns=completed_turns,
                )
            except Exception as exc:  # noqa: BLE001
                result = WorkflowExecutionResult(
                    session_id=session.session_id,
                    workflow_name=workflow_name,
                    run_id="",
                    status="failed",
                    phases=(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            print(json.dumps(result.to_dict()), flush=True)
            completed_turns += 1
    finally:
        if terminal_token is not None:
            reset_current_terminal_manager(terminal_token)
        await _close_headless_session(
            session,
            processor_task,
            cassette_base,
        )


async def _close_headless_session(
    session: "SessionContext",
    processor_task: asyncio.Task[object] | None,
    cassette_base: Path | None,
) -> None:
    """Close durable handles and background services for a headless session."""
    try:
        projection_task = getattr(session, "kernel_projection_task", None)
        if projection_task is not None:
            projection_task.cancel()
            await asyncio.gather(projection_task, return_exceptions=True)
        await session.processor.drain()
        await session.processor.stop()
        if processor_task is not None:
            processor_task.cancel()
            await asyncio.gather(processor_task, return_exceptions=True)
        session.session_log.close()
        close_memory = getattr(session.session_memory, "close", None)
        if callable(close_memory):
            close_memory()
        if session.mcp_registry is not None:
            await session.mcp_registry.shutdown()
        browser_manager = getattr(session, "browser_manager", None)
        if browser_manager is not None:
            await browser_manager.close_session()
        terminal_manager = getattr(session, "terminal_manager", None)
        if terminal_manager is not None:
            await terminal_manager.close()
        session_service = getattr(session, "session_service", None)
        if session_service is not None:
            await session_service.close()
        if cassette_base is not None:
            from agenthicc.runners.tui_session import _write_cassette_meta  # noqa: PLC0415

            _write_cassette_meta(cassette_base / session.session_id, session.session_id)
    finally:
        owner_lease = cast(SessionOwnerLease | None, vars(session).get("owner_lease"))
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
            from agenthicc.runners.session_lease import SessionOpenCoordinator  # noqa: PLC0415

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
