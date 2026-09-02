"""End-to-end durable resume journey for PRD-186."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.reconstruct_site.phase_impl import ReconstructState
from agenthicc.workflows.reconstruct_site.runner import (
    ReconstructContext,
    ReconstructSiteWorkflow,
)
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.e2e


def test_resume_does_not_rewind_profiled_workflow_to_init(tmp_path: Path) -> None:
    conversation = SessionConversation.open(
        "e2e-topology",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore("e2e-topology", root=tmp_path / "checkpoints")
        handle = WorkflowRunHandle.create(
            run_id="e2e-static-run",
            workflow=ReconstructSiteWorkflow,
            conversation=conversation,
            intent="continue a static reconstruction",
            checkpoint_store=store,
        )
        handle.attach_context(
            ReconstructContext(
                intent="continue a static reconstruction",
                run_id="e2e-static-run",
                state=ReconstructState.VISUAL_VALIDATION,
                phase_iteration=4,
                plan_version="reconstruct-site.v3",
                profile="static",
                shared_memory=conversation.memory,
            )
        )
        checkpoint = handle.finalize_failure(
            RuntimeError("transient provider failure"),
            boundary="visual_validation",
        )
        assert checkpoint is not None
        assert checkpoint.current_phase == "visual_validation"
        assert checkpoint.phase_index == 14

        registry = WorkflowRegistry()
        registry.register(ReconstructSiteWorkflow)
        coordinator = WorkflowRecoveryCoordinator("e2e-topology", checkpoint_store=store)
        record = coordinator.inspect(
            workflow_registry=registry,
            conversation=conversation,
        )[0]
        assert record.recoverable is True

        resumed = coordinator.rehydrate(
            record,
            workflow=ReconstructSiteWorkflow,
            conversation=conversation,
        )
        assert resumed.current_phase == "visual_validation"
        assert resumed.phase_index == 14
        assert resumed.context is not None
        assert getattr(resumed.context, "state") is ReconstructState.VISUAL_VALIDATION
        assert resumed.run_id == checkpoint.run_id
        assert resumed.conversation.conversation_id == checkpoint.conversation_id
    finally:
        conversation.close()
