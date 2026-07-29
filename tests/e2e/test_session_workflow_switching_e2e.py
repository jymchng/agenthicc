"""End-to-end session journey: direct history → code_plan → create_workflow."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState as KernelAppState
from agenthicc.kernel import EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.code_plan import CodePlan, CodePlanRunner, CodePlanState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.create_workflow import (
    CreateWorkflow,
    CreateWorkflowRunner,
    CreateWorkflowState,
    PhaseArtifact,
)
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.create_workflow.validation import validate_workflow_file

pytestmark = pytest.mark.e2e


@pytest.fixture
async def processor(tmp_path: Path):
    kernel = KernelAppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snapshot.json"),
        ),
        policy=SecurityPolicy(),
    )
    processor = EventProcessor(initial_state=kernel, persist=False)
    task = asyncio.create_task(processor.run())
    await asyncio.sleep(0)
    yield processor
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _config(
    app: TUIAppState,
    processor: EventProcessor,
    conversation: SessionConversation,
    handle: WorkflowRunHandle,
) -> WorkflowConfig:
    return WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=processor,
        agent_runner=SimpleNamespace(
            _transport=SimpleNamespace(_config=SimpleNamespace(model="e2e-model"))
        ),  # type: ignore[arg-type]
        approval_svc=None,
        cfg=AgenthiccConfig(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=MagicMock(),
        session_memory=conversation.memory,
        conversation_id=conversation.conversation_id,
        workflow_handle=handle,
    )


async def test_session_history_survives_workflow_switching(
    tmp_path: Path,
    processor: EventProcessor,
) -> None:
    conversation = SessionConversation.open(
        "e2e-session",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    conversation.memory.add_user("Discuss the repository architecture")
    conversation.memory.add_assistant({"role": "assistant", "content": "Architecture notes"})
    initial_messages = list(conversation.messages)
    store = WorkflowCheckpointStore("e2e-session", root=tmp_path / "checkpoints")
    app = TUIAppState.create()

    code_handle = WorkflowRunHandle.create(
        run_id="code-plan-run",
        workflow=CodePlan,
        conversation=conversation,
        intent="Implement the session workflow support",
        checkpoint_store=store,
    )
    code_runner = CodePlanRunner(_config(app, processor, conversation, code_handle))

    async def plan(ctx):
        code_runner._set_phase("plan", 0, ctx)
        ctx.plan = "implement it"
        return CodePlanState.EXECUTE

    async def execute(ctx):
        code_runner._set_phase("execute", 1, ctx)
        ctx.execute_summary = "implemented"
        return CodePlanState.REVIEW

    async def review(ctx):
        code_runner._set_phase("review", 2, ctx)
        ctx.review_summary = "looks good"
        return CodePlanState.SUMMARIZE

    async def summarize(ctx):
        code_runner._set_phase("summarize", 3, ctx)
        return CodePlanState.COMPLETE

    code_runner._plan = plan  # type: ignore[method-assign]
    code_runner._execute = execute  # type: ignore[method-assign]
    code_runner._review = review  # type: ignore[method-assign]
    code_runner._summarize = summarize  # type: ignore[method-assign]
    await code_runner.run(code_handle.original_intent)

    assert code_handle.lifecycle == "complete"
    assert code_handle.conversation.memory is conversation.memory
    assert conversation.conversation_id == "e2e-session"

    author_handle = WorkflowRunHandle.create(
        run_id="author-run",
        workflow=CreateWorkflow,
        conversation=conversation,
        intent="Create a review workflow",
        checkpoint_store=store,
    )
    author_runner = CreateWorkflowRunner(_config(app, processor, conversation, author_handle))

    async def dispatch(state, ctx):
        if state is CreateWorkflowState.DESIGN:
            ctx.design = "design"
            ctx.add_artifact(PhaseArtifact("design", "design", "design"))
            return CreateWorkflowState.GENERATE
        if state is CreateWorkflowState.GENERATE:
            ctx.generated_path = ".agenthicc/workflows/review.py"
            ctx.add_artifact(PhaseArtifact("generate", "workflow_file", "generated"))
            return CreateWorkflowState.VALIDATE
        if state is CreateWorkflowState.VALIDATE:
            ctx.validation_summary = "valid"
            ctx.add_artifact(PhaseArtifact("validate", "validation_report", "pass"))
            return CreateWorkflowState.SUMMARIZE
        ctx.add_artifact(PhaseArtifact("summarize", "summary", "done"))
        return CreateWorkflowState.COMPLETE

    author_runner._dispatch = dispatch  # type: ignore[method-assign]
    await author_runner.run(author_handle.original_intent)

    assert author_handle.lifecycle == "complete"
    assert author_handle.conversation is code_handle.conversation
    assert author_handle.conversation.memory is conversation.memory
    assert conversation.messages == initial_messages
    assert store.load("code-plan-run").conversation_id == "e2e-session"  # type: ignore[union-attr]
    assert store.load("author-run").conversation_id == "e2e-session"  # type: ignore[union-attr]
    conversation.close()


async def test_generated_workflow_checkpoint_survives_session_restart(
    tmp_path: Path,
    processor: EventProcessor,
) -> None:
    """A generated custom workflow restores its typed state after reopening the journal."""
    inspection = make_inspection_tools()
    show_example = next(
        tool for tool in inspection if getattr(tool, "__name__", "") == "show_example_workflow"
    )
    result = await show_example()
    source = result["source"]
    assert isinstance(source, str)

    workflow_path = tmp_path / ".agenthicc" / "workflows" / "release_check.py"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(source, encoding="utf-8")
    report = validate_workflow_file(
        str(workflow_path), expected_name="release_check", root=tmp_path
    )
    assert report.ok, report.render()

    spec = importlib.util.spec_from_file_location("e2e_generated_release_check", workflow_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    journal_path = tmp_path / "restart-conversation.jsonl"
    conversation = SessionConversation.open(
        "restart-session",
        max_tokens=10_000,
        journal_path=journal_path,
    )
    conversation.memory.add_user("Earlier direct request")
    store = WorkflowCheckpointStore("restart-session", root=tmp_path / "checkpoints")
    context = module.ReleaseContext(
        intent="check the release",
        run_id="restart-run",
        plan="run tests",
        state=module.ReleaseState.VERIFY,
        phase_iteration=2,
        shared_memory=conversation.memory,
    )
    handle = WorkflowRunHandle.create(
        run_id="restart-run",
        workflow=module.ReleaseCheckWorkflow,
        conversation=conversation,
        intent=context.intent,
        checkpoint_store=store,
    )
    handle.attach_context(context)
    handle.request_pause()
    handle.mark_paused()
    checkpoint = handle.save_checkpoint(reason="escape")
    conversation.close()

    reopened = SessionConversation.open(
        "restart-session",
        max_tokens=10_000,
        journal_path=journal_path,
    )
    restored = WorkflowRunHandle.from_checkpoint(
        checkpoint,
        workflow=module.ReleaseCheckWorkflow,
        conversation=reopened,
        checkpoint_store=store,
    )
    assert restored.context.state is module.ReleaseState.VERIFY
    assert restored.context.phase_iteration == 2
    assert restored.context.shared_memory is reopened.memory
    assert reopened.messages[0]["content"] == "Earlier direct request"
    reopened.close()
