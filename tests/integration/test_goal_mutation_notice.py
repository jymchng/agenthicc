"""Integration coverage for goal mutation events and their TUI projection."""

from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.config import AgenthiccConfig
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.workflows.goal_flow import GoalFlowWorkflow
from agenthicc.workflows.goal_flow.runner import GoalContext, GoalFlowRunner, GoalState

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_goal_flow_mutation_projects_its_committed_event_to_the_tui(
    tmp_path: Path,
) -> None:
    conversation = SessionConversation.open(
        "goal-mutation-runner",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore(
            conversation.conversation_id,
            root=tmp_path / "checkpoints",
        )
        handle = WorkflowRunHandle.create(
            run_id="goal-mutation-run",
            workflow=GoalFlowWorkflow,
            conversation=conversation,
            intent="implement the current goal",
            checkpoint_store=store,
        )
        context = GoalContext(
            intent="implement the current goal",
            run_id="goal-mutation-run",
            state=GoalState.IMPLEMENT_GOAL,
            phase_iteration=1,
            goals=["current", "later"],
            goal_index=0,
            goal_attempts=[1, 0],
            goal_evidence=["", ""],
            goal_files=[[], []],
            shared_memory=conversation.memory,
        )
        handle.attach_context(context)
        handle.update_phase("implement_goal", 1, 1)

        app = AppState.create()
        runner = GoalFlowRunner(
            SimpleNamespace(
                workflow_handle=handle,
                cfg=AgenthiccConfig(),
                agent_runner=SimpleNamespace(),
                conv_store=app.conversation,
                params=None,
            ),
            None,
        )
        output = StringIO()
        appender = ScrollBufferAppender(
            app,
            Console(file=output, force_terminal=False, color_system=None),
        )
        appender.mount()
        try:
            result = await runner._append_goal(context, "follow-up")
            await asyncio.sleep(0)
        finally:
            appender.unmount()

        assert result["ok"] is True
        checkpoint = store.load("goal-mutation-run")
        assert checkpoint is not None
        assert checkpoint.context["fields"]["goal_list_revision"] == 1  # type: ignore[index]
        rendered = output.getvalue()
        assert rendered.count("Plan updated") == 1
        assert "Added a goal to the end · position 3 of 3" in rendered
    finally:
        conversation.close()


@pytest.mark.asyncio
async def test_committed_goal_mutation_event_reaches_tui_once() -> None:
    state = AppState.create()
    output = StringIO()
    appender = ScrollBufferAppender(
        state,
        Console(file=output, force_terminal=False, color_system=None),
    )
    appender.mount()
    try:
        state.conversation.begin_turn("assistant", turn_id="goal-mutation")
        event = state.conversation.append_event(
            "goal_list_mutated",
            {"operation": "insert", "index": 7, "goal_count": 13, "goal_id": "opaque"},
            event_id="goal-mutation-event",
        )
        duplicate = state.conversation.append_event(
            "goal_list_mutated",
            {"operation": "insert", "index": 7, "goal_count": 13, "goal_id": "opaque"},
            event_id="goal-mutation-event",
        )
        await asyncio.sleep(0)
    finally:
        appender.unmount()

    rendered = output.getvalue()
    assert event is duplicate
    assert rendered.count("Plan updated") == 1
    assert "Inserted a goal · position 8 of 13" in rendered
    assert "↳ Current goal continues" in rendered
    assert "opaque" not in rendered


def test_replayed_goal_mutation_has_deterministic_presentation() -> None:
    def render() -> str:
        state = AppState.create()
        output = StringIO()
        appender = ScrollBufferAppender(
            state,
            Console(file=output, force_terminal=False, color_system=None),
        )
        appender.replay(
            [
                ConversationEvent(
                    event_id="replayed-goal-mutation",
                    kind="goal_list_mutated",
                    payload={"operation": "append", "index": 12, "goal_count": 13},
                )
            ]
        )
        appender.flush()
        return output.getvalue()

    first = render()
    second = render()
    assert first == second
    assert first.count("Plan updated") == 1
    assert "Added a goal to the end · position 13 of 13" in first
