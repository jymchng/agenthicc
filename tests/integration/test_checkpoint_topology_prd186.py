"""Integration coverage for profile-aware checkpoint persistence and recovery."""

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

pytestmark = pytest.mark.integration


def test_profile_cursor_survives_failure_and_restart(tmp_path: Path) -> None:
    conversation = SessionConversation.open(
        "integration-topology",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore("integration-topology", root=tmp_path / "checkpoints")
        handle = WorkflowRunHandle.create(
            run_id="reconstruct-static-run",
            workflow=ReconstructSiteWorkflow,
            conversation=conversation,
            intent="reconstruct a static site",
            checkpoint_store=store,
        )
        context = ReconstructContext(
            intent="reconstruct a static site",
            run_id="reconstruct-static-run",
            state=ReconstructState.FINAL_VALIDATION,
            phase_iteration=3,
            plan_version="reconstruct-site.v3",
            profile="static",
            active_phase_names=[
                "init",
                "recon",
                "visual_research",
                "interaction_analysis",
                "content_assets",
                "architecture",
                "design_system",
                "research_gate",
                "bootstrap",
                "global_shell",
                "component_system",
                "page",
                "responsive_pass",
                "visual_validation",
                "interaction_validation",
                "accessibility",
                "performance",
                "fidelity_pass",
                "final_validation",
            ],
            shared_memory=conversation.memory,
        )
        handle.attach_context(context)

        checkpoint = handle.finalize_failure(
            TimeoutError("provider timed out in final validation"),
            boundary="final_validation",
        )
        assert checkpoint is not None
        assert checkpoint.current_phase == "final_validation"
        assert checkpoint.phase_index == 19
        assert checkpoint.topology_profile == "static"
        assert checkpoint.topology_phase_names[-1] == "final_validation"

        registry = WorkflowRegistry()
        registry.register(ReconstructSiteWorkflow)
        coordinator = WorkflowRecoveryCoordinator("integration-topology", checkpoint_store=store)
        record = coordinator.inspect(
            workflow_registry=registry,
            conversation=conversation,
        )[0]
        assert record.recoverable is True

        restored = coordinator.rehydrate(
            record,
            workflow=ReconstructSiteWorkflow,
            conversation=conversation,
        )
        assert restored.run_id == "reconstruct-static-run"
        assert restored.current_phase == "final_validation"
        assert restored.phase_index == 19
        assert restored.topology_phase_names[-1] == "final_validation"
    finally:
        conversation.close()
