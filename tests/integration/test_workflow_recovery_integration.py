"""Disk-backed process-style workflow recovery integration coverage."""

from __future__ import annotations

from pathlib import Path

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.code_plan.definition import CodePlan
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.create_workflow.definition import CreateWorkflow
from agenthicc.workflows.create_workflow.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
    PhaseArtifact,
)
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


def test_reopen_create_workflow_with_distinct_authored_slug_is_non_destructive(
    tmp_path: Path,
) -> None:
    session_id = "create-workflow-slug-recovery"
    run_id = "create-workflow-run"
    checkpoint_root = tmp_path / "sessions"
    journal_path = checkpoint_root / session_id / "conversation-journal.jsonl"
    draft = tmp_path / ".agenthicc" / "workflows" / ".drafts" / run_id / "tailor_resume"
    runner_file = draft / "runner.py"
    runner_file.parent.mkdir(parents=True)
    runner_file.write_text("# draft created before restart\n", encoding="utf-8")
    original_draft = runner_file.read_bytes()
    authored_path = str(runner_file)

    first = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=journal_path,
    )
    from agenthicc.runners.workflow_handle import WorkflowRunHandle

    initial_handle = WorkflowRunHandle.create(
        run_id=run_id,
        workflow=CreateWorkflow,
        conversation=first,
        intent="author a resume workflow",
        checkpoint_store=WorkflowCheckpointStore(session_id, root=checkpoint_root),
    )
    initial_handle.attach_context(
        CreateWorkflowContext(
            intent="author a resume workflow",
            run_id=run_id,
            design="A workflow that resumes from its generated draft.",
            workflow_name="tailor_resume",
            generated_path=authored_path,
            generation_summary="Initial package is written.",
            artifacts={
                "generate": PhaseArtifact(
                    phase="generate",
                    kind="workflow_file",
                    content="Initial runner draft.",
                    metadata={"path": authored_path},
                )
            },
            state=CreateWorkflowState.GENERATE,
            phase_iteration=2,
            phase_attempts={"design": 1, "generate": 2},
            completed_phases=["design"],
        )
    )
    initial_handle.update_phase("generate", 1, 2)
    checkpoint_before_restart = WorkflowCheckpointStore(session_id, root=checkpoint_root).load(
        run_id
    )
    assert checkpoint_before_restart is not None
    first.close()

    reopened = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=journal_path,
    )
    try:
        store = WorkflowCheckpointStore(session_id, root=checkpoint_root)
        registry = WorkflowRegistry()
        registry.register(CreateWorkflow)
        coordinator = WorkflowRecoveryCoordinator(session_id, checkpoint_store=store)
        record = coordinator.inspect(
            workflow_registry=registry,
            conversation=reopened,
        )[0]

        assert record.recoverable is True
        restored_handle = coordinator.rehydrate(
            record,
            workflow=CreateWorkflow,
            conversation=reopened,
            owner_id="create-workflow-integration-owner",
        )
        restored = restored_handle.context
        assert isinstance(restored, CreateWorkflowContext)
        assert restored.run_id == run_id
        assert restored.state is CreateWorkflowState.GENERATE
        assert restored.workflow_name == "tailor_resume"
        assert restored.generated_path == authored_path
        assert restored.artifacts["generate"].metadata["path"] == authored_path
        assert runner_file.read_bytes() == original_draft
        checkpoint_after_recovery = store.load(run_id)
        assert checkpoint_after_recovery is not None
        assert checkpoint_after_recovery.revision == checkpoint_before_restart.revision
        restored_handle.release_claim()
    finally:
        reopened.close()
