"""Drive and checkpoint a staged authoring run under the session's permissions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar

from lauren_ai._memory import ShortTermMemory

from agenthicc.kernel import Event
from agenthicc.plugins.authored import AuthoredArtifactStore
from agenthicc.runners.agent_turn import _run_agent_turn
from agenthicc.runners.prompt_contract import build_workflow_prompt_contract
from agenthicc.tools.base import tool_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from enum import StrEnum

    from agenthicc.tools.base import ToolLike
    from agenthicc.workflows.config import WorkflowConfig


class AuthoringCursor(Protocol):
    """The common mutable cursor for authoring sets."""

    run_id: str
    intent: str
    phase_iteration: int
    fail_reason: str
    shared_memory: ShortTermMemory | None

    @property
    def state(self) -> StrEnum: ...


_Cursor = TypeVar("_Cursor", bound=AuthoringCursor)


async def drive_staged_run(
    config: WorkflowConfig,
    context: _Cursor,
    phases: tuple[str, ...],
    advance: Callable[[_Cursor, AuthoredArtifactStore], Awaitable[None]],
    *,
    workflow_name: str,
) -> _Cursor:
    """Advance through scoped phases, preserving the exact checkpoint cursor."""
    if context.shared_memory is None:
        context.shared_memory = config.session_memory or ShortTermMemory(
            max_tokens=config.cfg.execution.effective_usable_budget()
        )
    workspace = config.workspace_scope
    store = AuthoredArtifactStore(workspace.primary_root if workspace else Path.cwd())
    handle = config.workflow_handle
    identity = {"run_id": context.run_id, "workflow_name": workflow_name}
    if context.phase_iteration == 0:
        await config.processor.emit(
            Event.create(
                "WorkflowRunStarted",
                {**identity, "intent": context.intent, "phase_names": list(phases)},
            )
        )
    while context.state.value in phases:
        phase = context.state.value
        context.phase_iteration += 1
        if handle is not None:
            handle.attach_context(context)
            handle.update_phase(phase, phases.index(phase), context.phase_iteration)
        await config.processor.emit(
            Event.create("WorkflowPhaseStarted", {**identity, "phase_name": phase})
        )
        await advance(context, store)
        if handle is not None:
            handle.attach_context(context)
            following = context.state.value
            if following in phases:
                handle.update_phase(
                    following, phases.index(following), context.phase_iteration, persist=False
                )
            handle.persist_context_transition()
        await config.processor.emit(
            Event.create(
                "WorkflowPhaseCompleted",
                {
                    **identity,
                    "phase_name": phase,
                    "role": "auto",
                    "metadata": {
                        "phase_iteration": context.phase_iteration,
                        "next_state": context.state.value,
                    },
                },
            )
        )
    if context.state.value == "failed":
        await asyncio.to_thread(store.discard_staging, context.run_id)
        if handle is not None:
            handle.failure_kind = (
                "repair_budget_exhausted"
                if context.fail_reason.startswith("repair budget exhausted")
                else "phase_execution"
            )
            handle.mark_terminal("failed", error=context.fail_reason)
            if handle.checkpoint_supported:
                handle.save_checkpoint(reason=handle.failure_kind)
    await config.processor.emit(
        Event.create(
            "WorkflowRunCompleted",
            {**identity, "status": context.state.value, "phases_run": context.phase_iteration},
        )
    )
    return context


async def run_authoring_phase(
    config: WorkflowConfig,
    context: AuthoringCursor,
    *,
    workflow_name: str,
    cache_contract: str,
    text: str,
    instructions: str,
    tools: Sequence[ToolLike],
) -> None:
    """Send only the phase's scoped tools through the standard turn gates."""
    execution = config.cfg.execution
    if config.params is not None:
        model = config.params.model_for_phase(context.state.value, execution.model)
        execution = replace(execution, model=model)
    contract = build_workflow_prompt_contract(
        workflow_name=workflow_name,
        stable_system_prefix=cache_contract,
        phase_prompt=instructions,
        stable_tools=(),
        phase_tools=tools,
        execution=execution,
    )
    await _run_agent_turn(
        text,
        runner=config.agent_runner,
        processor=config.processor,
        session_memory=context.shared_memory,
        conversation_id=config.conversation_id,
        max_agent_turns=10,
        conv_store=config.conv_store,
        app_state=config.app_state,
        exec_cfg=execution,
        extra_tools=list(tools),
        allowed_tool_names=frozenset(map(tool_name, tools)),
        approval_svc=config.approval_svc,
        workspace_access=config.workspace_access,
        next_queued_message=config.next_queued_message,
        prompt_contract=contract,
        tool_surface_generation=config.tool_surface.current().generation,
        usage_ledger=config.usage_ledger,
    )
