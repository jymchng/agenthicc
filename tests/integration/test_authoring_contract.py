"""Workflow repair accounting across the runner and durable checkpoint store."""

from __future__ import annotations

from types import MappingProxyType
from agenthicc.tools.surface import ToolSurface, ToolSurfaceHolder
from agenthicc.runners.reload import SurfaceHolder
import asyncio
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport._mock import MockTransport

from agenthicc.agents.registry import build_agents_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.mentions.cache import MentionCache
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.tools.base import tool_name
from agenthicc.tools.workspace_access import WorkspaceScope
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.create_workflow import (
    CreateWorkflow,
    CreateWorkflowContext,
    CreateWorkflowRunner,
    CreateWorkflowState,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from agenthicc.kernel.processor import EventProcessor

pytestmark = pytest.mark.integration


async def test_checkpoint_restart_preserves_repairs_and_exhausts_the_original_budget(
    tmp_path: Path, running_processor: EventProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "authoring-session"
    run_id = "authoring-run"
    intent = "Author a review workflow"
    journal_path = tmp_path / "conversation.jsonl"
    checkpoint_root = tmp_path / "checkpoints"
    store = WorkflowCheckpointStore(session_id, root=checkpoint_root)
    app = AppState.create()
    candidate = "this is not valid Python!\n"
    generation_budgets: list[int] = []

    async def scripted_turn(_text: str, **kwargs: object) -> None:
        ctx = kwargs["ctx"]
        tools = kwargs["tools"]
        assert isinstance(ctx, CreateWorkflowContext)
        assert isinstance(tools, list)
        by_name = {tool_name(item): item for item in tools}

        async def invoke(name: str, *args: str) -> None:
            invoke_tool = by_name[name]
            assert callable(invoke_tool)
            await cast("Awaitable[object]", invoke_tool(*args))

        if ctx.state is CreateWorkflowState.DESIGN:
            await invoke("finalize_design", "Review documents", "doc_review")
        elif ctx.state is CreateWorkflowState.GENERATE:
            generation_budgets.append(ctx.repair_budget.used)
            if generation_budgets == [0, 1, 2]:
                raise asyncio.CancelledError
            await invoke("write_file", "runner.py", candidate)
            await invoke("mark_generation_complete", "Candidate written")
        elif ctx.state is CreateWorkflowState.VALIDATE:
            await invoke("approve_workflow", "Approve the candidate")
        else:
            pytest.fail(f"Unexpected provider turn in {ctx.state.name}")

    conversation = SessionConversation.open(
        session_id, max_tokens=10_000, journal_path=journal_path
    )
    handle = WorkflowRunHandle.create(
        run_id=run_id,
        workflow=CreateWorkflow,
        conversation=conversation,
        intent=intent,
        checkpoint_store=store,
    )
    config = WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=running_processor,
        agent_runner=AgentRunnerBase(transport=MockTransport()),
        approval_svc=None,
        cfg=AgenthiccConfig(),
        skills=SurfaceHolder(MappingProxyType({})),
        tool_surface=ToolSurfaceHolder(ToolSurface(0, ())),
        mcp_registry=None,
        mention_cache=MentionCache(),
        agents_registry=build_agents_registry(),
        session_memory=conversation.memory,
        conversation_id=session_id,
        workspace_scope=WorkspaceScope.create(tmp_path),
        workflow_handle=handle,
    )
    first = CreateWorkflowRunner(config)
    monkeypatch.setattr(first, "_run_turn", scripted_turn)
    try:
        with pytest.raises(asyncio.CancelledError):
            await first.run(intent)
    finally:
        conversation.close()

    restarted_store = WorkflowCheckpointStore(session_id, root=checkpoint_root)
    saved = restarted_store.load(run_id)
    assert saved is not None
    assert saved.current_phase == "generate"
    restarted_conversation = SessionConversation.open(
        session_id, max_tokens=10_000, journal_path=journal_path
    )
    try:
        restored = WorkflowRunHandle.from_checkpoint(
            saved,
            workflow=CreateWorkflow,
            conversation=restarted_conversation,
            checkpoint_store=restarted_store,
            recover_interrupted=True,
        )
        context = restored.context
        assert isinstance(context, CreateWorkflowContext)
        assert context.repair_budget.used == 2
        assert context.repair_budget.max_cycles == 3
        restored.mark_resuming()
        # An operator's setting change applies to new runs only.
        config.cfg.workflows["authoring"] = {"max_repair_cycles": 10}
        resumed = CreateWorkflowRunner(
            dataclasses.replace(
                config,
                workflow_handle=restored,
                session_memory=restarted_conversation.memory,
            )
        )
        monkeypatch.setattr(resumed, "_run_turn", scripted_turn)
        completed = await resumed.resume(context)
        assert completed.state is CreateWorkflowState.FAILED
        assert completed.repair_budget.used == 3
        assert completed.validation_report in completed.fail_reason
        assert "limit 3" in completed.fail_reason
        assert generation_budgets == [0, 1, 2, 2, 3]
        terminal = restarted_store.load(run_id)
        assert terminal is not None
        assert terminal.status == "failed"
        assert terminal.reason == "repair_budget_exhausted"
    finally:
        restarted_conversation.close()
