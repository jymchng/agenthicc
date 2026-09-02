"""Clean-slate coverage for PRD-186 checkpoint topology coordinates."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.checkpoint import (
    CheckpointValidationError,
    WorkflowCheckpoint,
    WorkflowCheckpointTopology,
    topology_from_phase_specs,
)
from agenthicc.workflows.plugin import PhaseSpec, WorkflowContext, WorkflowPlugin
from agenthicc.workflows.reconstruct_site.evidence_plan import (
    RECONSTRUCT_PHASE_PLAN,
)
from agenthicc.workflows.reconstruct_site.runner import ReconstructSiteWorkflow
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.unit


def test_topology_fingerprint_includes_order_and_edges() -> None:
    first = WorkflowCheckpointTopology(
        workflow_name="demo",
        topology_version="demo.v1",
        profile="small",
        phase_names=("init", "build"),
        next_phases=("build", None),
        reject_phases=("init", "build"),
    )
    reordered = WorkflowCheckpointTopology(
        workflow_name="demo",
        topology_version="demo.v1",
        profile="small",
        phase_names=("build", "init"),
        next_phases=("init", None),
        reject_phases=("build", "init"),
    )
    changed_edge = WorkflowCheckpointTopology(
        workflow_name="demo",
        topology_version="demo.v1",
        profile="small",
        phase_names=("init", "build"),
        next_phases=(None, None),
        reject_phases=("init", "build"),
    )

    assert first.index_for("build") == 1
    assert first.topology_fingerprint != reordered.topology_fingerprint
    assert first.topology_fingerprint != changed_edge.topology_fingerprint
    with pytest.raises(CheckpointValidationError, match="absent"):
        first.index_for("missing")


def test_topology_adapter_supports_phase_spec_edges() -> None:
    topology = topology_from_phase_specs(
        "demo",
        (
            PhaseSpec(name="init", next="work", on_reject="init"),
            PhaseSpec(name="work", next="complete"),
        ),
        topology_version="demo.v2",
        profile="default",
    )

    assert topology.phase_names == ("init", "work")
    assert topology.next_phases == ("work", None)
    assert topology.reject_phases == ("init", None)


def test_reconstruct_profile_resolver_uses_active_not_registry_index() -> None:
    topology = ReconstructSiteWorkflow.resolve_checkpoint_topology(
        {
            "kind": "CustomContext",
            "fields": {
                "profile": "static",
                "plan_version": "reconstruct-site.v3",
            },
        }
    )
    active = RECONSTRUCT_PHASE_PLAN.active("static")

    assert topology.phase_names == active.names
    assert topology.index_for("final_validation") == 19
    assert topology.index_for("final_validation") != ReconstructSiteWorkflow.phases.index(
        ReconstructSiteWorkflow.get_phase("final_validation")
    )


def _profiled_workflow() -> type[WorkflowPlugin]:
    class ProfiledWorkflow(WorkflowPlugin):
        name = "profiled_topology"
        phases = [
            PhaseSpec(name="init", next="skipped", on_reject="init"),
            PhaseSpec(name="skipped", next="target", on_reject="skipped"),
            PhaseSpec(name="target", on_reject="target"),
        ]

        @classmethod
        def resolve_checkpoint_topology(cls, context_payload: dict[str, object]):
            del context_payload
            return topology_from_phase_specs(
                cls.name,
                (
                    PhaseSpec(name="init", next="target", on_reject="init"),
                    PhaseSpec(name="target", on_reject="target"),
                ),
                topology_version="profiled.v1",
                profile="reduced",
            )

    return ProfiledWorkflow


def _conversation(tmp_path: Path) -> SessionConversation:
    return SessionConversation.open(
        "session-topology",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )


def test_handle_persists_profile_local_index_and_recovery_accepts_it(tmp_path: Path) -> None:
    workflow = _profiled_workflow()
    conversation = _conversation(tmp_path)
    try:
        store = WorkflowCheckpointStore("session-topology", root=tmp_path / "checkpoints")
        handle = WorkflowRunHandle.create(
            run_id="run-topology",
            workflow=workflow,
            conversation=conversation,
            intent="resume the reduced workflow",
            checkpoint_store=store,
        )
        handle.attach_context(
            WorkflowContext(
                intent="resume the reduced workflow",
                run_id="run-topology",
                workflow_name=workflow.name,
                current_phase="target",
                phase_iteration=2,
            )
        )

        # The caller supplies the full-registry index (2), but the handle
        # derives the active reduced index (1) from the workflow contract.
        checkpoint = (
            handle.update_phase("target", index=2, iteration=2) or handle.build_checkpoint()
        )
        assert checkpoint is not None
        saved = store.load("run-topology")
        assert saved is not None
        assert saved.phase_index == 1
        assert saved.topology_phase_names == ("init", "target")
        assert saved.topology_profile == "reduced"

        registry = WorkflowRegistry()
        registry.register(workflow)
        record = WorkflowRecoveryCoordinator("session-topology", checkpoint_store=store).inspect(
            workflow_registry=registry, conversation=conversation
        )[0]
        assert record.recoverable is True
    finally:
        conversation.close()


def test_old_checkpoint_is_migratable_when_resolver_is_unambiguous(tmp_path: Path) -> None:
    workflow = _profiled_workflow()
    conversation = _conversation(tmp_path)
    try:
        store = WorkflowCheckpointStore("session-topology", root=tmp_path / "checkpoints")
        handle = WorkflowRunHandle.create(
            run_id="old-run",
            workflow=workflow,
            conversation=conversation,
            intent="migrate",
            checkpoint_store=store,
        )
        handle.attach_context(
            WorkflowContext(
                intent="migrate",
                run_id="old-run",
                workflow_name=workflow.name,
                current_phase="target",
                phase_iteration=1,
            )
        )
        current = handle.update_phase("target", index=1, iteration=1) or handle.build_checkpoint()
        old = replace(
            current,
            topology_version="",
            topology_fingerprint="",
            topology_profile="",
            topology_phase_names=(),
            revision=current.revision + 1,
        )
        store.save(old)

        registry = WorkflowRegistry()
        registry.register(workflow)
        record = WorkflowRecoveryCoordinator("session-topology", checkpoint_store=store).inspect(
            workflow_registry=registry, conversation=conversation
        )[0]
        assert record.recoverable is True

        restored = WorkflowRecoveryCoordinator(
            "session-topology", checkpoint_store=store
        ).rehydrate(record, workflow=workflow, conversation=conversation)
        migrated = restored.save_checkpoint(reason="migrated")
        assert migrated.topology_phase_names == ("init", "target")
        assert migrated.phase_index == 1
        assert migrated.run_id == "old-run"
    finally:
        conversation.close()


def test_recovery_reports_active_index_mismatch_separately(tmp_path: Path) -> None:
    workflow = _profiled_workflow()
    conversation = _conversation(tmp_path)
    try:
        store = WorkflowCheckpointStore("session-topology", root=tmp_path / "checkpoints")
        handle = WorkflowRunHandle.create(
            run_id="bad-index",
            workflow=workflow,
            conversation=conversation,
            intent="reject bad index",
            checkpoint_store=store,
        )
        handle.attach_context(
            WorkflowContext(
                intent="reject bad index",
                run_id="bad-index",
                workflow_name=workflow.name,
                current_phase="target",
                phase_iteration=1,
            )
        )
        current = handle.update_phase("target", index=1, iteration=1) or handle.build_checkpoint()
        store.save(replace(current, phase_index=0, revision=current.revision + 1))
        registry = WorkflowRegistry()
        registry.register(workflow)

        record = WorkflowRecoveryCoordinator("session-topology", checkpoint_store=store).inspect(
            workflow_registry=registry, conversation=conversation
        )[0]
        assert record.recoverable is False
        assert record.error_code == "checkpoint_phase_index_mismatch"
        assert "saved=0" in (record.error or "")
        assert "expected=1" in (record.error or "")
    finally:
        conversation.close()


def test_checkpoint_topology_fields_round_trip() -> None:
    checkpoint = WorkflowCheckpoint(
        run_id="run",
        workflow_name="demo",
        conversation_id="session",
        intent="intent",
        status="paused",
        current_phase="target",
        phase_index=1,
        phase_iteration=1,
        conversation_cursor=0,
        context={"kind": "WorkflowContext", "fields": {}},
        plugin_fingerprint="plugin",
        topology_version="demo.v1",
        topology_fingerprint="fingerprint",
        topology_profile="reduced",
        topology_phase_names=("init", "target"),
    )

    restored = WorkflowCheckpoint.from_dict(checkpoint.to_dict())
    assert restored == checkpoint


def test_checkpoint_rejects_partial_topology_metadata() -> None:
    checkpoint = WorkflowCheckpoint(
        run_id="run",
        workflow_name="demo",
        conversation_id="session",
        intent="intent",
        status="paused",
        current_phase="target",
        phase_index=0,
        phase_iteration=0,
        conversation_cursor=0,
        context={"kind": "WorkflowContext", "fields": {}},
        plugin_fingerprint="plugin",
    )
    raw = checkpoint.to_dict()
    raw["topology_version"] = "demo.v1"
    unsigned = dict(raw)
    unsigned.pop("content_hash", None)
    import hashlib
    import json

    raw["content_hash"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    with pytest.raises(CheckpointValidationError, match="incomplete"):
        WorkflowCheckpoint.from_dict(raw)
