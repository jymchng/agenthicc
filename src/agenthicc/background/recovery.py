"""Typed arbitration for direct-turn and workflow checkpoint recovery."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class RecoveryOwner(str, Enum):
    """The single recovery path authorized to advance a session conversation."""

    NONE = "none"
    DIRECT_TURN = "direct_turn"
    WORKFLOW = "workflow"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class RecoveryAssessment:
    """Durable-evidence result used before a worker dispatches provider input."""

    owner: RecoveryOwner
    turn_id: str | None = None
    workflow_run_id: str | None = None
    related_run_ids: tuple[str, ...] = ()
    reason: str = ""


def assess_recovery_owner(
    *,
    turn_id: str | None,
    turn_workflow_run_id: str | None,
    turn_execution_kind: str | None = None,
    recoverable_run_ids: Iterable[str] = (),
    invalid_run_ids: Iterable[str] = (),
    active_workflow_run_ids: Iterable[str] = (),
    correlated_workflow_run_ids: Iterable[str] = (),
    inspection_error: str = "",
) -> RecoveryAssessment:
    """Choose one recovery owner without treating any session checkpoint as related.

    A new journal turn records its workflow run ID. For older turns without
    that provenance, only one checkpoint that exactly matches the session's
    configured workflow, conversation identity, and original intent can own
    the turn automatically. Other legacy combinations remain unresolved.
    """

    if turn_id is None:
        return RecoveryAssessment(RecoveryOwner.NONE)

    recoverable = tuple(dict.fromkeys(item for item in recoverable_run_ids if item))
    invalid = tuple(dict.fromkeys(item for item in invalid_run_ids if item))
    active = tuple(dict.fromkeys(item for item in active_workflow_run_ids if item))
    correlated = tuple(dict.fromkeys(item for item in correlated_workflow_run_ids if item))
    known = tuple(dict.fromkeys((*recoverable, *invalid, *active)))

    if turn_execution_kind == "direct":
        if turn_workflow_run_id:
            return RecoveryAssessment(
                RecoveryOwner.UNRESOLVED,
                turn_id=turn_id,
                workflow_run_id=turn_workflow_run_id,
                related_run_ids=known,
                reason="journal execution kind conflicts with its workflow run link",
            )
        return RecoveryAssessment(
            RecoveryOwner.DIRECT_TURN,
            turn_id=turn_id,
            related_run_ids=known,
            reason="the journal explicitly records this as a direct-agent turn",
        )
    if turn_execution_kind == "workflow" and not turn_workflow_run_id:
        return RecoveryAssessment(
            RecoveryOwner.UNRESOLVED,
            turn_id=turn_id,
            reason="journal marks a workflow turn but has no workflow run ID",
        )

    if inspection_error:
        return RecoveryAssessment(
            RecoveryOwner.UNRESOLVED,
            turn_id=turn_id,
            workflow_run_id=turn_workflow_run_id,
            related_run_ids=known,
            reason=f"workflow recovery inspection failed: {inspection_error}",
        )

    if turn_workflow_run_id:
        if turn_workflow_run_id in recoverable:
            return RecoveryAssessment(
                RecoveryOwner.WORKFLOW,
                turn_id=turn_id,
                workflow_run_id=turn_workflow_run_id,
                related_run_ids=(turn_workflow_run_id,),
                reason="the interrupted journal turn is linked to this recoverable workflow",
            )
        if turn_workflow_run_id in known:
            return RecoveryAssessment(
                RecoveryOwner.UNRESOLVED,
                turn_id=turn_id,
                workflow_run_id=turn_workflow_run_id,
                related_run_ids=(turn_workflow_run_id,),
                reason="the linked workflow run is active or has invalid recovery state",
            )
        if not known:
            return RecoveryAssessment(
                RecoveryOwner.UNRESOLVED,
                turn_id=turn_id,
                workflow_run_id=turn_workflow_run_id,
                reason="journal turn references a workflow run with no recovery record",
            )
        # The turn has explicit provenance for another run; those records do
        # not own it, so direct-turn replay is safe and leaves them untouched.
        return RecoveryAssessment(
            RecoveryOwner.DIRECT_TURN,
            turn_id=turn_id,
            workflow_run_id=turn_workflow_run_id,
            related_run_ids=known,
            reason="the interrupted turn belongs to a different workflow run",
        )

    if len(correlated) == 1:
        correlated_run_id = correlated[0]
        if correlated_run_id in recoverable:
            return RecoveryAssessment(
                RecoveryOwner.WORKFLOW,
                turn_id=turn_id,
                workflow_run_id=correlated_run_id,
                related_run_ids=(correlated_run_id,),
                reason=(
                    "the legacy turn uniquely matches the session's configured workflow, "
                    "conversation, and original intent"
                ),
            )
        return RecoveryAssessment(
            RecoveryOwner.UNRESOLVED,
            turn_id=turn_id,
            workflow_run_id=correlated_run_id,
            related_run_ids=(correlated_run_id,),
            reason="the uniquely correlated workflow run is not safely recoverable",
        )
    if len(correlated) > 1:
        return RecoveryAssessment(
            RecoveryOwner.UNRESOLVED,
            turn_id=turn_id,
            related_run_ids=correlated,
            reason="multiple workflow checkpoints match this legacy turn",
        )

    if known:
        return RecoveryAssessment(
            RecoveryOwner.UNRESOLVED,
            turn_id=turn_id,
            related_run_ids=known,
            reason="historical journal turn has no workflow ownership metadata",
        )

    return RecoveryAssessment(
        RecoveryOwner.DIRECT_TURN,
        turn_id=turn_id,
        reason="the interrupted turn has no workflow recovery owner",
    )
