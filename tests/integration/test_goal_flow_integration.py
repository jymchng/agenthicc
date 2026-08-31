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
from agenthicc.workflows.goal_flow.runner import GoalFlowRunner, GoalState, GoalStatus

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


@pytest.mark.asyncio
async def test_dynamic_goal_mutations_preserve_identity_and_schedule_pending_work(
    tmp_path: Path,
) -> None:
    """A real runner loop can add work without replaying the active goal."""
    conversation = SessionConversation.open(
        "goal-flow-dynamic-integration",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore(
            conversation.conversation_id,
            root=tmp_path / "checkpoints",
        )
        handle = WorkflowRunHandle.create(
            run_id="goal-dynamic-integration-run",
            workflow=GoalFlowWorkflow,
            conversation=conversation,
            intent="discover and complete follow-up work",
            checkpoint_store=store,
        )
        app = AppState.create()
        ctx = SimpleNamespace(
            app_state=app,
            processor=SimpleNamespace(),
            agent_runner=SimpleNamespace(),
            cfg=AgenthiccConfig(),
            workflow_handle=handle,
            session_memory=conversation.memory,
            params=None,
            conv_store=app.conversation,
        )
        runner = GoalFlowRunner(ctx, None)
        mutation_results: list[dict[str, object]] = []
        completed_active_ids: list[str] = []
        insertion_done = False
        append_done = False

        async def fake_run_phase(**kwargs: object) -> None:
            nonlocal append_done, insertion_done
            prompt = str(kwargs["system_prompt"])
            tools = kwargs["tools"]
            assert isinstance(tools, list)
            by_name = {str(getattr(tool, "__name__", "")): tool for tool in tools}
            assert isinstance(handle.context, object)
            active_context = handle.context
            if "CLARIFY" in prompt:
                await by_name["complete_clarification"](
                    notes="The follow-up work may be discovered during implementation."
                )
            elif "DECIDE_GOALS" in prompt:
                await by_name["finalize_goals"](goals=["first", "second"])
            elif "IMPLEMENT_GOAL" in prompt:
                active = active_context.active_record()  # type: ignore[attr-defined]
                if active.text == "first" and not append_done:
                    mutation_results.append(await by_name["append_goal"](goal="follow-up"))
                    append_done = True
                completed_active_ids.append(active.goal_id)
                await by_name["goal_implemented"](summary=f"Implemented {active.text}.", files=[])
            elif "VERIFY_GOAL" in prompt:
                active = active_context.active_record()  # type: ignore[attr-defined]
                if active.text == "first" and not insertion_done:
                    mutation_results.append(
                        await by_name["insert_goal"](index=0, goal="prerequisite")
                    )
                    insertion_done = True
                await by_name["verify_goal"](satisfied=True, evidence=f"Verified {active.text}.")
            elif "SUMMARIZE" in prompt:
                await by_name["complete_workflow"](
                    summary="All discovered work is complete.", files=[]
                )
            else:  # pragma: no cover - protects the fake from silently drifting
                raise AssertionError(f"unexpected phase prompt: {prompt}")

        runner.run_phase = fake_run_phase  # type: ignore[method-assign]
        result = await runner.run("discover and complete follow-up work")

        assert result.state is GoalState.COMPLETE
        assert [record.text for record in result.goal_records] == [
            "prerequisite",
            "first",
            "second",
            "follow-up",
        ]
        assert all(record.status is GoalStatus.VERIFIED for record in result.goal_records)
        assert len(set(completed_active_ids)) == 4
        assert mutation_results[0]["goal_list_revision"] == 1
        assert mutation_results[1]["goal_list_revision"] == 2
        assert result.active_goal_id == ""

        reloaded = store.load("goal-dynamic-integration-run")
        assert reloaded is not None
        fields = reloaded.context["fields"]
        assert isinstance(fields, dict)
        assert fields["goal_list_revision"] == 2
        assert len(fields["goal_mutation_receipts"]) == 2  # type: ignore[arg-type]
    finally:
        conversation.close()


@pytest.mark.asyncio
async def test_dynamic_goal_mutation_survives_failure_checkpoint_and_rehydration(
    tmp_path: Path,
) -> None:
    """A provider failure after mutation resumes the same dynamic context."""
    conversation = SessionConversation.open(
        "goal-flow-recovery-integration",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore(
            conversation.conversation_id,
            root=tmp_path / "checkpoints",
        )
        handle = WorkflowRunHandle.create(
            run_id="goal-recovery-integration-run",
            workflow=GoalFlowWorkflow,
            conversation=conversation,
            intent="recover dynamic goals",
            checkpoint_store=store,
        )
        app = AppState.create()
        config = SimpleNamespace(
            app_state=app,
            processor=SimpleNamespace(),
            agent_runner=SimpleNamespace(),
            cfg=AgenthiccConfig(),
            workflow_handle=handle,
            session_memory=conversation.memory,
            params=None,
            conv_store=app.conversation,
        )
        runner = GoalFlowRunner(config, None)
        from agenthicc.workflows.goal_flow.runner import GoalContext

        context = GoalContext(
            intent="recover dynamic goals",
            run_id=handle.run_id,
            state=GoalState.IMPLEMENT_GOAL,
            goals=["current", "later"],
            shared_memory=conversation.memory,
        )
        handle.attach_context(context)
        handle.update_phase("implement_goal", 2, 1)
        mutation = await runner._append_goal(context, "discovered")
        assert mutation["ok"] is True
        active_id = context.active_goal_id

        handle.finalize_failure(RuntimeError("provider returned 429"), kind="provider_transient")
        failed = store.load(handle.run_id)
        assert failed is not None
        assert failed.status == "paused"
        assert failed.current_phase == "implement_goal"

        fresh_store = WorkflowCheckpointStore(
            conversation.conversation_id,
            root=tmp_path / "checkpoints",
        )
        restored_handle = WorkflowRunHandle.from_checkpoint(
            failed,
            workflow=GoalFlowWorkflow,
            conversation=conversation,
            checkpoint_store=fresh_store,
        )
        restored = restored_handle.context
        assert isinstance(restored, GoalContext)
        assert restored_handle.run_id == handle.run_id
        assert restored_handle.conversation.conversation_id == conversation.conversation_id
        assert restored.state is GoalState.IMPLEMENT_GOAL
        assert restored.active_goal_id == active_id
        assert [record.text for record in restored.goal_records] == [
            "current",
            "later",
            "discovered",
        ]
        assert restored.goal_list_revision == 1
        assert len(restored.goal_mutation_receipts) == 1
    finally:
        conversation.close()
