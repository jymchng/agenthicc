"""Recovery ownership arbitration for interrupted background turns."""

from __future__ import annotations

import pytest

from agenthicc.background.recovery import RecoveryOwner, assess_recovery_owner

pytestmark = pytest.mark.unit


def test_no_interrupted_turn_needs_no_recovery_owner() -> None:
    result = assess_recovery_owner(
        turn_id=None,
        turn_workflow_run_id=None,
        recoverable_run_ids=("workflow-a",),
    )
    assert result.owner is RecoveryOwner.NONE


def test_unrelated_workflow_checkpoint_does_not_block_proven_direct_turn() -> None:
    result = assess_recovery_owner(
        turn_id="turn-direct",
        turn_workflow_run_id=None,
        turn_execution_kind="direct",
        recoverable_run_ids=("workflow-unrelated",),
    )
    assert result.owner is RecoveryOwner.DIRECT_TURN
    assert result.workflow_run_id is None
    assert result.related_run_ids == ("workflow-unrelated",)


def test_matching_workflow_run_owns_the_interrupted_turn() -> None:
    result = assess_recovery_owner(
        turn_id="turn-workflow",
        turn_workflow_run_id="workflow-a",
        turn_execution_kind="workflow",
        recoverable_run_ids=("workflow-a", "workflow-b"),
    )
    assert result.owner is RecoveryOwner.WORKFLOW
    assert result.related_run_ids == ("workflow-a",)


def test_legacy_turn_uses_one_exactly_correlated_recoverable_checkpoint() -> None:
    result = assess_recovery_owner(
        turn_id="legacy-turn",
        turn_workflow_run_id=None,
        recoverable_run_ids=("workflow-a", "workflow-b"),
        correlated_workflow_run_ids=("workflow-a",),
    )
    assert result.owner is RecoveryOwner.WORKFLOW
    assert result.workflow_run_id == "workflow-a"
    assert "uniquely matches" in result.reason


def test_multiple_correlated_legacy_checkpoints_remain_unresolved() -> None:
    result = assess_recovery_owner(
        turn_id="legacy-turn",
        turn_workflow_run_id=None,
        recoverable_run_ids=("workflow-a", "workflow-b"),
        correlated_workflow_run_ids=("workflow-a", "workflow-b"),
    )
    assert result.owner is RecoveryOwner.UNRESOLVED
    assert result.related_run_ids == ("workflow-a", "workflow-b")


def test_correlated_but_invalid_checkpoint_does_not_become_recovery_owner() -> None:
    result = assess_recovery_owner(
        turn_id="legacy-turn",
        turn_workflow_run_id=None,
        invalid_run_ids=("workflow-a",),
        correlated_workflow_run_ids=("workflow-a",),
    )
    assert result.owner is RecoveryOwner.UNRESOLVED
    assert result.workflow_run_id == "workflow-a"


@pytest.mark.parametrize(
    ("kwargs", "reason_fragment"),
    [
        (
            {"recoverable_run_ids": ("workflow-a",)},
            "no workflow ownership metadata",
        ),
        (
            {"invalid_run_ids": ("workflow-a",)},
            "no workflow ownership metadata",
        ),
        (
            {"active_workflow_run_ids": ("workflow-a",)},
            "no workflow ownership metadata",
        ),
        (
            {"inspection_error": "CheckpointReadError"},
            "inspection failed",
        ),
    ],
)
def test_legacy_or_uninspectable_state_fails_closed(
    kwargs: dict[str, object], reason_fragment: str
) -> None:
    recovery_arguments: dict[str, object] = {"recoverable_run_ids": ()}
    recovery_arguments.update(kwargs)
    result = assess_recovery_owner(
        turn_id="legacy-turn",
        turn_workflow_run_id=None,
        **recovery_arguments,  # type: ignore[arg-type]
    )
    assert result.owner is RecoveryOwner.UNRESOLVED
    assert reason_fragment in result.reason


def test_link_to_missing_workflow_record_is_not_guessed_as_direct() -> None:
    result = assess_recovery_owner(
        turn_id="turn-with-stale-link",
        turn_workflow_run_id="workflow-missing",
        recoverable_run_ids=(),
    )
    assert result.owner is RecoveryOwner.UNRESOLVED
    assert "no recovery record" in result.reason


def test_link_to_invalid_workflow_checkpoint_waits_for_explicit_resolution() -> None:
    result = assess_recovery_owner(
        turn_id="turn-with-invalid-link",
        turn_workflow_run_id="workflow-invalid",
        recoverable_run_ids=(),
        invalid_run_ids=("workflow-invalid",),
    )
    assert result.owner is RecoveryOwner.UNRESOLVED
    assert result.related_run_ids == ("workflow-invalid",)
