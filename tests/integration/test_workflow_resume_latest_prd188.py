"""Disk-backed integration coverage for PRD-188 latest-run selection."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.code_plan.definition import CodePlan
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.integration


def _save_run(
    store: WorkflowCheckpointStore,
    conversation: SessionConversation,
    run_id: str,
    intent: str,
) -> WorkflowRunHandle:
    handle = WorkflowRunHandle.create(
        run_id=run_id,
        workflow=CodePlan,
        conversation=conversation,
        intent=intent,
        checkpoint_store=store,
        provider_profile="default",
    )
    handle.attach_context(
        CodePlanContext(
            intent=intent,
            run_id=run_id,
            state=CodePlanState.EXECUTE,
            phase_iteration=2,
            shared_memory=conversation.memory,
        )
    )
    handle.update_phase("execute", 1, 2)
    handle.save_checkpoint(reason="integration")
    return handle


def test_latest_selection_reads_durable_checkpoint_activity(tmp_path: Path) -> None:
    session_id = "prd188-integration"
    conversation = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore(session_id, root=tmp_path / "sessions")
        older = _save_run(store, conversation, "run-a", "older")
        newer = _save_run(store, conversation, "run-b", "newer")
        older_checkpoint = store.load(older.run_id)
        newer_checkpoint = store.load(newer.run_id)
        assert older_checkpoint is not None
        assert newer_checkpoint is not None
        store.save(
            replace(
                older_checkpoint,
                updated_at=100.0,
                revision=older_checkpoint.revision + 1,
            )
        )
        store.save(
            replace(
                newer_checkpoint,
                updated_at=200.0,
                revision=newer_checkpoint.revision + 1,
            )
        )

        registry = WorkflowRegistry()
        registry.register(CodePlan)
        coordinator = WorkflowRecoveryCoordinator(session_id, checkpoint_store=store)
        selected = coordinator.select_latest_for_resume(
            workflow_registry=registry,
            conversation=conversation,
            provider_profile="default",
        )

        assert selected is not None
        assert selected.run_id == "run-b"
        assert selected.current_phase == "execute"
        assert selected.checkpoint_revision == newer_checkpoint.revision + 1
    finally:
        conversation.close()
