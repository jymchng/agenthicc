"""Integration coverage for goal_flow's per-goal durable boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.goal_flow import GoalFlowWorkflow
from agenthicc.workflows.goal_flow.runner import GoalFlowRunner, GoalState

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_goal_flow_saves_checkpoint_after_each_verified_goal(tmp_path: Path) -> None:
    conversation = SessionConversation.open(
        "goal-flow-integration",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore(
            conversation.conversation_id,
            root=tmp_path / "checkpoints",
        )
        handle = WorkflowRunHandle.create(
            run_id="goal-integration-run",
            workflow=GoalFlowWorkflow,
            conversation=conversation,
            intent="implement two goals",
            checkpoint_store=store,
        )
        ctx = SimpleNamespace(
            app_state=AppState.create(),
            processor=SimpleNamespace(),
            agent_runner=SimpleNamespace(),
            cfg=AgenthiccConfig(),
            workflow_handle=handle,
            session_memory=conversation.memory,
        )
        runner = GoalFlowRunner(ctx, None)

        async def fake_run_phase(**kwargs: object) -> None:
            prompt = str(kwargs["system_prompt"])
            tools = kwargs["tools"]
            assert isinstance(tools, list)
            transition = tools[0]
            if "CLARIFY" in prompt:
                await transition(notes="The implementation and verification contract is clear.")
            elif "DECIDE_GOALS" in prompt:
                await transition(goals=["first goal", "second goal"])
            elif "IMPLEMENT_GOAL" in prompt:
                await transition(summary="Implemented the current goal.", files=["one.py"])
            elif "VERIFY_GOAL" in prompt:
                await transition(satisfied=True, evidence="Focused integration checks passed.")
            elif "SUMMARIZE" in prompt:
                await transition(
                    summary="Both goals were implemented and verified.", files=["one.py"]
                )
            else:  # pragma: no cover - protects the fake from silently drifting
                raise AssertionError(f"unexpected phase prompt: {prompt}")

        runner.run_phase = fake_run_phase  # type: ignore[method-assign]
        reasons: list[str] = []
        original_save = handle.save_checkpoint

        def save_checkpoint(*, reason: str = ""):
            reasons.append(reason)
            return original_save(reason=reason)

        handle.save_checkpoint = save_checkpoint  # type: ignore[method-assign]
        result = await runner.run("implement two goals")

        assert result.state is GoalState.COMPLETE
        assert result.completed_goal_indices == [0, 1]
        assert len(result.goal_checkpoint_revisions) == 2
        assert reasons.count("goal_1_completed") == 1
        assert reasons.count("goal_2_completed") == 1
        assert result.goal_checkpoint_revisions[0] < result.goal_checkpoint_revisions[1]

        # Session ownership normally writes this terminal boundary after the
        # runner returns. Preserve it here to verify the final durable context
        # still carries the per-goal audit.
        handle.mark_terminal("complete")
        terminal = handle.save_checkpoint(reason="complete")
        assert terminal.status == "complete"
        assert terminal.context["fields"]["completed_goal_indices"] == [0, 1]  # type: ignore[index]
    finally:
        conversation.close()
