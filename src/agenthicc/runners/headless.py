"""Headless runner — emits kernel events as JSON lines to stdout."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.cli.context import CLIContext
    from agenthicc.runners.session_context import SessionContext

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

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation used by CLI and stdin modes."""
        return {
            "event_type": "WorkflowRunCompleted",
            "session_id": self.session_id,
            "workflow": self.workflow_name,
            "run_id": self.run_id,
            "status": self.status,
            "phases": list(self.phases),
            "error": self.error,
        }


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

    workflow_config = WorkflowConfig(
        conv_store=session.app_state.conversation,
        app_state=session.app_state,
        processor=session.processor,
        agent_runner=session.agent_runner,
        approval_svc=session.approval_svc,
        cfg=session.cfg,
        skills=session.skills,
        plugin_tools=session.project_plugins.all_tools,
        mcp_registry=session.mcp_registry,
        mention_cache=session.mention_cache,
        agents_registry=session.agents_registry,
        memory_router=session.memory_router,
        semantic_index=session.semantic_index,
        completed_turns=completed_turns,
        params=workflow_cls.build_params(session.cfg.workflows.get(workflow_name, {})),
    )
    runner = workflow_cls.build_runner(workflow_config, session.mode_manager)
    error: str | None = None
    runner_result: object | None = None
    try:
        runner_result = await runner.run(intent)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    await session.processor.drain()
    workflow_run = session.app_state.workflow_run()
    status = str(getattr(workflow_run, "status", "failed") or "failed")
    if error is None and status == "failed":
        fail_reason = getattr(runner_result, "fail_reason", "")
        if isinstance(fail_reason, str) and fail_reason:
            error = fail_reason
    run_id = str(getattr(workflow_run, "run_id", "") or "")
    phases: list[str] = []
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
        if not run_id:
            if isinstance(event_run_id, str):
                run_id = event_run_id
    if not phases and workflow_run is not None:
        phases = [str(record.phase_name) for record in getattr(workflow_run, "phase_history", [])]
    return WorkflowExecutionResult(
        session_id=session.session_id,
        workflow_name=workflow_name,
        run_id=run_id,
        status=status,
        phases=tuple(phases),
        error=error,
    )


async def run_headless_workflow(
    ctx: "CLIContext",
    workflow_name: str,
    intent: str,
) -> WorkflowExecutionResult:
    """Build a durable session, run one workflow, and close its resources."""
    from agenthicc.runners.tui_session import _build_session_context  # noqa: PLC0415

    cassette_base = Path(ctx.record_cassette) if ctx.record_cassette else None
    session = await _build_session_context(
        ctx.resume_id,
        list(ctx.set_overrides),
        cassette_base,
        config_path=ctx.config_path,
        headless=True,
    )
    session.app_state.cli_flags = ctx.flags
    session.approval_svc = _HeadlessApprovalService(ctx.flags.dangerously_skip_permissions)  # type: ignore[assignment]
    processor_task = asyncio.create_task(session.processor.run(), name="headless-processor")
    await asyncio.sleep(0)
    try:
        return await execute_workflow(session, workflow_name, intent)
    finally:
        await _close_headless_session(session, processor_task, cassette_base)


async def _run_headless_workflow_stream(ctx: "CLIContext") -> None:
    """Run one workflow for every non-empty stdin line and emit JSON results."""
    from agenthicc.runners.tui_session import _build_session_context  # noqa: PLC0415

    workflow_name = ctx.workflow_name
    if not workflow_name:
        raise ValueError("--workflow requires a workflow name")
    cassette_base = Path(ctx.record_cassette) if ctx.record_cassette else None
    session = await _build_session_context(
        ctx.resume_id,
        list(ctx.set_overrides),
        cassette_base,
        config_path=ctx.config_path,
        headless=True,
    )
    session.app_state.cli_flags = ctx.flags
    session.approval_svc = _HeadlessApprovalService(ctx.flags.dangerously_skip_permissions)  # type: ignore[assignment]
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
    try:
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
        await _close_headless_session(session, processor_task, cassette_base)


async def _close_headless_session(
    session: "SessionContext",
    processor_task: asyncio.Task[object],
    cassette_base: Path | None,
) -> None:
    """Close durable handles and background services for a headless session."""
    await session.processor.drain()
    await session.processor.stop()
    processor_task.cancel()
    await asyncio.gather(processor_task, return_exceptions=True)
    session.session_log.close()
    close_memory = getattr(session.session_memory, "close", None)
    if callable(close_memory):
        close_memory()
    if session.mcp_registry is not None:
        await session.mcp_registry.shutdown()
    if cassette_base is not None:
        from agenthicc.runners.tui_session import _write_cassette_meta  # noqa: PLC0415

        _write_cassette_meta(cassette_base / session.session_id, session.session_id)


async def _run_headless(ctx: CLIContext | None = None) -> None:
    if ctx is not None and ctx.workflow_name:
        await _run_headless_workflow_stream(ctx)
        return

    from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings

    state = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())

    # PRD-79: apply CLIFlags from the CLI context.
    if ctx is not None:
        state.cli_flags = ctx.flags

    processor = EventProcessor(initial_state=state, persist=False)
    sub = processor.subscribe()
    proc_task = asyncio.create_task(processor.run())
    print(json.dumps({"status": "ready", "mode": "headless"}), flush=True)
    try:
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            intent_id = uuid.uuid4().hex
            await processor.emit(
                Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text})
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
        proc_task.cancel()
        await asyncio.gather(proc_task, return_exceptions=True)
