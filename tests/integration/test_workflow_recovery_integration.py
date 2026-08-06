"""Disk-backed process-style workflow recovery integration coverage."""

from __future__ import annotations

from pathlib import Path

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.code_plan.definition import CodePlan
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.registry import WorkflowRegistry


def test_reopen_same_session_rehydrates_checkpoint_into_one_memory(tmp_path: Path) -> None:
    session_id = "process-style-recovery"
    journal_path = tmp_path / "sessions" / session_id / "conversation-journal.jsonl"
    checkpoint_root = tmp_path / "sessions"

    first = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=journal_path,
    )
    first.memory.add_user("preserve this direct-turn history")
    expected_messages = list(first.messages)
    store = WorkflowCheckpointStore(session_id, root=checkpoint_root)
    from agenthicc.runners.workflow_handle import WorkflowRunHandle

    handle = WorkflowRunHandle.create(
        run_id="process-run",
        workflow=CodePlan,
        conversation=first,
        intent="continue after restart",
        checkpoint_store=store,
        provider_profile="profile-a",
        workspace_root="/workspace/project",
    )
    context = CodePlanContext(
        intent="continue after restart",
        run_id="process-run",
        state=CodePlanState.EXECUTE,
        phase_iteration=3,
        shared_memory=first.memory,
    )
    handle.attach_context(context)
    handle.update_phase("execute", 1, 3)
    first.close()

    reopened = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=journal_path,
    )
    try:
        registry = WorkflowRegistry()
        registry.register(CodePlan)
        coordinator = WorkflowRecoveryCoordinator(
            session_id,
            checkpoint_store=WorkflowCheckpointStore(session_id, root=checkpoint_root),
        )
        records = coordinator.inspect(
            workflow_registry=registry,
            conversation=reopened,
            provider_profile="profile-a",
            workspace_root="/workspace/project",
        )
        assert len(records) == 1
        restored = coordinator.rehydrate(
            records[0],
            workflow=CodePlan,
            conversation=reopened,
            owner_id="integration-owner",
        )
        assert restored.lifecycle == "paused"
        assert restored.run_id == "process-run"
        assert restored.current_phase == "execute"
        assert restored.context is not None
        assert isinstance(restored.context, CodePlanContext)
        assert restored.context.state is CodePlanState.EXECUTE
        assert restored.context.shared_memory is reopened.memory
        assert reopened.conversation_id == session_id
        assert reopened.messages == expected_messages
        restored.release_claim()
    finally:
        reopened.close()
