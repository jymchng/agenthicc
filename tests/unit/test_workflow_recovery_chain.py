"""Parent-link validation and schema migration for foreground recovery."""

from dataclasses import replace
from pathlib import Path

import pytest

from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_recovery import (
    WorkflowRecoveryCoordinator,
    linked_workflow_recovery,
    workflow_recovery_leaves,
)
from agenthicc.workflows.checkpoint import CheckpointValidationError, WorkflowCheckpoint


def _checkpoint(run_id: str, parent_run_id: str | None) -> WorkflowCheckpoint:
    return WorkflowCheckpoint(
        run_id=run_id,
        workflow_name="inspect",
        conversation_id="session",
        intent="inspect the sample",
        status="paused",
        current_phase="inspect",
        phase_index=0,
        phase_iteration=0,
        conversation_cursor=0,
        context={},
        plugin_fingerprint="fixture",
        frame_id=f"frame-{run_id}",
        parent_run_id=parent_run_id,
        origin="agent" if parent_run_id else "selection",
    )


def test_linked_resume_offers_only_the_child(tmp_path: Path) -> None:
    store = WorkflowCheckpointStore("session", root=tmp_path)
    store.save(_checkpoint("parent", None))
    store.save(_checkpoint("child", "parent"))
    coordinator = WorkflowRecoveryCoordinator("session", checkpoint_store=store)
    records = coordinator.inspect()

    leaves = workflow_recovery_leaves(records)
    assert [leaf.run_id for leaf in leaves] == ["child"]
    chain = linked_workflow_recovery(records, leaves[0], max_depth=1)
    assert [record.run_id for record in chain.records] == ["parent", "child"]
    assert chain.missing_parent_run_id is None


def test_orphan_is_offered_and_link_cycles_and_excess_depth_are_diagnostic(tmp_path: Path) -> None:
    store = WorkflowCheckpointStore("session", root=tmp_path)
    coordinator = WorkflowRecoveryCoordinator("session", checkpoint_store=store)
    store.save(_checkpoint("child", "missing"))
    selected = coordinator.select_latest_for_resume()
    assert selected is not None
    chain = linked_workflow_recovery([selected], selected, max_depth=0)
    assert chain.records == (selected,)
    assert chain.missing_parent_run_id == "missing"

    store.save(_checkpoint("missing", None))
    diagnostics = coordinator.inspect(max_stack_depth=0)
    assert all(record.error_code == "workflow_stack_depth" for record in diagnostics)
    store.save(replace(_checkpoint("missing", "child"), revision=1))
    records = coordinator.inspect()
    assert len(records) == 2 and {record.error_code for record in records} == {
        "workflow_link_cycle"
    }
    with pytest.raises(ValueError, match="parent-link cycle"):
        coordinator.select_latest_for_resume()


@pytest.mark.parametrize("field", ["schema_version", "frame_id", "parent_run_id", "origin"])
def test_checkpoint_predating_stack_is_rejected_before_recovery(field: str) -> None:
    payload = _checkpoint("run", None).to_dict()
    if field == "schema_version":
        payload[field] = 1
    else:
        del payload[field]
    with pytest.raises(CheckpointValidationError, match="predates workflow stacks"):
        WorkflowCheckpoint.from_dict(payload)
