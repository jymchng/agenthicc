"""Clean-slate unit coverage for PRD-170 durable workflow recovery."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import (
    WorkflowClaimError,
    WorkflowCheckpointStore,
)
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.code_plan.definition import CodePlan
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.checkpoint import context_from_payload, context_to_payload
from agenthicc.workflows.plugin import WorkflowContext
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.unit


def _conversation(tmp_path: Path) -> SessionConversation:
    return SessionConversation.open(
        "session-recovery",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )


def _running_checkpoint(
    tmp_path: Path,
    conversation: SessionConversation,
    *,
    state: CodePlanState = CodePlanState.PLAN,
    phase: str = "plan",
    phase_index: int = 0,
    phase_iteration: int = 1,
) -> tuple[WorkflowCheckpointStore, WorkflowRunHandle]:
    store = WorkflowCheckpointStore("session-recovery", root=tmp_path)
    handle = WorkflowRunHandle.create(
        run_id="run-1",
        workflow=CodePlan,
        conversation=conversation,
        intent="implement recovery",
        checkpoint_store=store,
        provider_profile="default",
    )
    context = CodePlanContext(
        intent="implement recovery",
        run_id="run-1",
        state=state,
        phase_iteration=phase_iteration,
        shared_memory=conversation.memory,
    )
    handle.attach_context(context)
    handle.update_phase(phase, phase_index, phase_iteration)
    return store, handle


def test_process_interrupted_checkpoint_is_rehydrated_at_exact_typed_state(
    tmp_path: Path,
) -> None:
    conversation = _conversation(tmp_path)
    try:
        store, handle = _running_checkpoint(tmp_path, conversation)
        coordinator = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store)

        records = coordinator.inspect(
            workflow_registry=None,
            conversation=conversation,
            provider_profile="default",
        )
        assert len(records) == 1
        assert records[0].interrupted is True
        assert records[0].recoverable is True

        restored = coordinator.rehydrate(
            records[0],
            workflow=CodePlan,
            conversation=conversation,
            owner_id="test-owner",
        )
        assert restored.lifecycle == "paused"
        assert restored.current_phase == "plan"
        assert restored.phase_iteration == 1
        assert restored.context is not None
        assert isinstance(restored.context, CodePlanContext)
        assert restored.context.state is CodePlanState.PLAN
        assert restored.context.shared_memory is conversation.memory
        assert store.claim_owner("run-1") == "test-owner"
        restored.release_claim()
        assert store.claim_owner("run-1") is None
        # The original in-memory handle was never the durable owner.
        assert handle.claim_owner_id is None
    finally:
        conversation.close()


def test_live_claim_prevents_duplicate_resume_and_release_is_owner_checked(
    tmp_path: Path,
) -> None:
    store = WorkflowCheckpointStore("session-recovery", root=tmp_path)
    first = store.acquire_claim("run-1", "owner-a")
    assert first.owner_id == "owner-a"
    assert store.acquire_claim("run-1", "owner-a") == first
    with pytest.raises(WorkflowClaimError, match="already claimed"):
        store.acquire_claim("run-1", "owner-b")
    store.release_claim("run-1", "owner-b")
    assert store.claim_owner("run-1") == "owner-a"
    store.release_claim("run-1", "owner-a")
    assert store.claim_owner("run-1") is None


def test_dead_local_claim_can_be_reclaimed_but_malformed_claim_fails_closed(
    tmp_path: Path,
) -> None:
    store = WorkflowCheckpointStore("session-recovery", root=tmp_path)
    claim_path = store.claim_path_for("run-1")
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim_path.write_text(
        json.dumps(
            {
                "owner_id": "dead",
                "pid": 2_147_483_647,
                "host": socket.gethostname(),
            }
        ),
        encoding="utf-8",
    )
    # The intentionally invalid high PID is provably absent on the test host.
    reclaimed = store.acquire_claim("run-1", "new-owner")
    assert reclaimed.owner_id == "new-owner"
    store.release_claim("run-1", "new-owner")

    claim_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(WorkflowClaimError):
        store.acquire_claim("run-1", "another-owner")


def test_recovery_rejects_phase_context_mismatch(tmp_path: Path) -> None:
    conversation = _conversation(tmp_path)
    try:
        store, handle = _running_checkpoint(tmp_path, conversation)
        assert handle.context is not None
        assert isinstance(handle.context, CodePlanContext)
        handle.context.state = CodePlanState.EXECUTE
        # Deliberately preserve the old phase cursor while the typed state has
        # moved. The checkpoint is syntactically valid but semantically unsafe.
        handle.save_checkpoint(reason="mismatch")
        coordinator = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store)
        registry = WorkflowRegistry()
        registry.register(CodePlan)
        record = coordinator.inspect(conversation=conversation, workflow_registry=registry)[0]
        assert record.recoverable is False
        assert record.error_code == "checkpoint_phase_mismatch"
    finally:
        conversation.close()


def test_recovery_fails_closed_for_profile_and_cursor_mismatch(tmp_path: Path) -> None:
    conversation = _conversation(tmp_path)
    try:
        store, handle = _running_checkpoint(tmp_path, conversation)
        checkpoint = handle.save_checkpoint(reason="profile")
        store.save(replace(checkpoint, provider_profile="modal-prod", conversation_cursor=4))
        coordinator = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store)
        profile_record = coordinator.inspect(conversation=conversation, provider_profile="local")[0]
        # Cursor validation is intentionally ordered before provider selection:
        # the session cannot safely rehydrate an older conversation in any profile.
        assert profile_record.error_code == "conversation_cursor_mismatch"
    finally:
        conversation.close()


def test_recovery_rejects_a_different_workspace_identity(tmp_path: Path) -> None:
    conversation = _conversation(tmp_path)
    try:
        store, handle = _running_checkpoint(tmp_path, conversation)
        checkpoint = handle.save_checkpoint(reason="workspace")
        store.save(replace(checkpoint, workspace_root="/project/old"))
        registry = WorkflowRegistry()
        registry.register(CodePlan)
        record = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store).inspect(
            workflow_registry=registry,
            conversation=conversation,
            workspace_root="/project/new",
        )[0]
        assert record.error_code == "workspace_mismatch"
    finally:
        conversation.close()


def test_incompatible_checkpoint_can_be_audited_as_discarded(tmp_path: Path) -> None:
    conversation = _conversation(tmp_path)
    try:
        store, handle = _running_checkpoint(tmp_path, conversation)
        checkpoint = handle.save_checkpoint(reason="incompatible")
        store.save(replace(checkpoint, provider_profile="removed-profile"))
        coordinator = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store)
        registry = WorkflowRegistry()
        registry.register(CodePlan)
        record = coordinator.inspect(
            workflow_registry=registry,
            conversation=conversation,
            provider_profile="current-profile",
        )[0]
        assert record.recoverable is False
        discarded = coordinator.discard(record, owner_id="reset-owner")
        assert discarded.status == "discarded"
        assert store.load("run-1") == discarded
        assert store.claim_owner("run-1") is None
    finally:
        conversation.close()


def test_generic_context_round_trip_preserves_graph_edge_and_iterations() -> None:
    context = WorkflowContext(
        intent="graph",
        run_id="graph-run",
        workflow_name="graph-workflow",
        current_phase="review",
        phase_iteration=4,
        phase_iterations={"plan": 2, "review": 4},
        next_phase="execute",
    )
    restored = context_from_payload(context_to_payload(context))
    assert isinstance(restored, WorkflowContext)
    assert restored.current_phase == "review"
    assert restored.phase_iteration == 4
    assert restored.phase_iterations == {"plan": 2, "review": 4}
    assert restored.next_phase == "execute"
