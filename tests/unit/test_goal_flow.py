"""Clean-slate tests for the copied goal_flow workflow and goal boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.workflows.goal_flow import GoalFlowWorkflow
from agenthicc.workflows.goal_flow.runner import (
    GoalContext,
    GoalFlowRunner,
    GoalState,
)
from agenthicc.workflows.loader import load_builtin_workflows

pytestmark = pytest.mark.unit


def _runner_and_context(
    tmp_path: Path,
) -> tuple[GoalFlowRunner, GoalContext, WorkflowCheckpointStore, SessionConversation]:
    conversation = SessionConversation.open(
        "goal-flow-unit",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    store = WorkflowCheckpointStore("goal-flow-unit", root=tmp_path / "checkpoints")
    handle = WorkflowRunHandle.create(
        run_id="goal-run",
        workflow=GoalFlowWorkflow,
        conversation=conversation,
        intent="implement two goals",
        checkpoint_store=store,
    )
    context = GoalContext(
        intent="implement two goals",
        run_id="goal-run",
        state=GoalState.VERIFY_GOAL,
        phase_iteration=3,
        goals=["first", "second"],
        goal_index=0,
        goal_attempts=[1, 0],
        goal_evidence=["first implementation", ""],
        goal_files=[["one.py"], []],
        shared_memory=conversation.memory,
    )
    handle.attach_context(context)
    handle.update_phase("verify_goal", 3, 3)
    config = SimpleNamespace(
        workflow_handle=handle,
        cfg=AgenthiccConfig(),
        agent_runner=SimpleNamespace(),
    )
    runner = GoalFlowRunner(config, None)
    return runner, context, store, conversation


def test_goal_flow_is_a_builtin_and_has_a_checkpoint_codec() -> None:
    assert GoalFlowWorkflow in load_builtin_workflows()
    assert GoalFlowWorkflow.name == "goal_flow"
    assert callable(GoalFlowWorkflow.checkpoint_context_to_payload)
    assert callable(GoalFlowWorkflow.checkpoint_context_from_payload)


@pytest.mark.asyncio
async def test_finalize_goals_accepts_structured_text_items() -> None:
    """Models may return the UI's ``[{"text": ...}]`` goal representation."""
    from agenthicc.workflows.goal_flow.runner import _make_decide_goals_tools

    event = asyncio.Event()
    data: dict[str, list[str]] = {}
    finalize_goals = _make_decide_goals_tools(event, data)[0]

    result = await finalize_goals(
        goals=[
            {"text": " Paginate the contents page at 20 lessons per page "},
        ]
    )

    assert result["ok"] is True
    assert data["goals"] == ["Paginate the contents page at 20 lessons per page"]
    assert event.is_set()


@pytest.mark.asyncio
async def test_finalize_goals_keeps_string_compatibility_and_rejects_bad_items() -> None:
    """The old string form remains valid and malformed objects do not crash."""
    from agenthicc.workflows.goal_flow.runner import _make_decide_goals_tools

    event = asyncio.Event()
    data: dict[str, list[str]] = {}
    finalize_goals = _make_decide_goals_tools(event, data)[0]

    result = await finalize_goals(goals=[" first goal ", "", "second goal"])
    assert result["ok"] is True
    assert data["goals"] == ["first goal", "second goal"]
    assert event.is_set()

    bad_event = asyncio.Event()
    bad_data: dict[str, list[str]] = {}
    bad_finalize_goals = _make_decide_goals_tools(bad_event, bad_data)[0]
    bad_result = await bad_finalize_goals(goals=[{"title": "missing supported text field"}])

    assert bad_result["ok"] is False
    assert "index 0" in str(bad_result["error"])
    assert not bad_event.is_set()


def test_completed_goal_checkpoint_advances_cursor_and_is_idempotent(tmp_path: Path) -> None:
    runner, context, store, conversation = _runner_and_context(tmp_path)
    try:
        runner._checkpoint_completed_goal(context, 0, GoalState.IMPLEMENT_GOAL)

        checkpoint = store.load("goal-run")
        assert checkpoint is not None
        assert checkpoint.reason == "goal_1_completed"
        assert checkpoint.status == "running"
        assert checkpoint.current_phase == "implement_goal"
        assert checkpoint.phase_index == 2
        assert checkpoint.context["kind"] == "CustomContext"
        fields = checkpoint.context["fields"]
        assert isinstance(fields, dict)
        assert fields["state"] == "IMPLEMENT_GOAL"
        assert fields["completed_goal_indices"] == [0]
        assert fields["goal_checkpoint_revisions"] == [checkpoint.revision]
        assert context.completed_goal_indices == [0]
        assert context.goal_checkpoint_revisions == [checkpoint.revision]

        revision = checkpoint.revision
        runner._checkpoint_completed_goal(context, 0, GoalState.IMPLEMENT_GOAL)
        assert store.load("goal-run").revision == revision  # type: ignore[union-attr]

        context.goal_index = 1
        runner._checkpoint_completed_goal(context, 1, GoalState.SUMMARIZE)
        latest = store.load("goal-run")
        assert latest is not None
        assert latest.reason == "goal_2_completed"
        assert latest.context["fields"]["completed_goal_indices"] == [0, 1]  # type: ignore[index]
        assert latest.context["fields"]["goal_checkpoint_revisions"] == [2, 3]  # type: ignore[index]
    finally:
        conversation.close()


def test_goal_checkpoint_codec_restores_completion_audit_and_memory(tmp_path: Path) -> None:
    runner, context, _store, conversation = _runner_and_context(tmp_path)
    try:
        context.completed_goal_indices = [0, 1]
        context.goal_checkpoint_revisions = [2, 5]
        payload = GoalFlowWorkflow.checkpoint_context_to_payload(context)
        memory = object()
        restored = GoalFlowWorkflow.checkpoint_context_from_payload(payload, memory)

        assert restored.state is GoalState.VERIFY_GOAL
        assert restored.goals == ["first", "second"]
        assert restored.completed_goal_indices == [0, 1]
        assert restored.goal_checkpoint_revisions == [2, 5]
        assert restored.shared_memory is memory
    finally:
        conversation.close()


def test_completed_goal_checkpoint_rehydrates_through_run_handle(tmp_path: Path) -> None:
    runner, context, store, conversation = _runner_and_context(tmp_path)
    try:
        context.goal_index = 1
        runner._checkpoint_completed_goal(context, 0, GoalState.IMPLEMENT_GOAL)
        checkpoint = store.load("goal-run")
        assert checkpoint is not None

        restored = WorkflowRunHandle.from_checkpoint(
            checkpoint,
            workflow=GoalFlowWorkflow,
            conversation=conversation,
            checkpoint_store=store,
        )

        assert isinstance(restored.context, GoalContext)
        assert restored.context.state is GoalState.IMPLEMENT_GOAL
        assert restored.context.goal_index == 1
        assert restored.context.completed_goal_indices == [0]
        assert restored.context.goal_checkpoint_revisions == [checkpoint.revision]
        assert restored.context.shared_memory is conversation.memory
    finally:
        conversation.close()
