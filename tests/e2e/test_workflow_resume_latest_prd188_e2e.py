"""End-to-end recovery journey for PRD-188."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.code_plan.definition import CodePlan
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.e2e


def test_omitted_id_rehydrates_same_run_at_saved_phase(tmp_path: Path) -> None:
    session_id = "prd188-e2e"
    conversation = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore(session_id, root=tmp_path / "sessions")
        handle = WorkflowRunHandle.create(
            run_id="saved-run",
            workflow=CodePlan,
            conversation=conversation,
            intent="continue the implementation",
            checkpoint_store=store,
        )
        handle.attach_context(
            CodePlanContext(
                intent="continue the implementation",
                run_id="saved-run",
                state=CodePlanState.EXECUTE,
                phase_iteration=3,
                shared_memory=conversation.memory,
            )
        )
        handle.update_phase("execute", 1, 3)
        handle.request_pause()
        handle.mark_paused(reason="e2e")
        checkpoint = handle.save_checkpoint(reason="e2e")
        assert checkpoint.current_phase == "execute"

        registry = WorkflowRegistry()
        registry.register(CodePlan)
        coordinator = WorkflowRecoveryCoordinator(session_id, checkpoint_store=store)
        selected = coordinator.select_latest_for_resume(
            workflow_registry=registry,
            conversation=conversation,
        )
        assert selected is not None
        assert selected.run_id == "saved-run"

        restored = coordinator.rehydrate(
            selected,
            workflow=CodePlan,
            conversation=conversation,
            owner_id="e2e-owner",
        )
        assert restored.run_id == "saved-run"
        assert restored.current_phase == "execute"
        assert restored.context is not None
        assert isinstance(restored.context, CodePlanContext)
        assert restored.context.state is CodePlanState.EXECUTE
        assert restored.context.phase_iteration == 3
        restored.release_claim()
    finally:
        conversation.close()
