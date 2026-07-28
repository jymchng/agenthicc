"""Explicit lifecycle state for the built-in ``create_*`` workflows.

The authoring workflows have a deliberately small state machine.  Artifact
side effects remain owned by the runner, while transitions are represented by
typed states so a resume or a future UI does not have to infer lifecycle state
from free-form transcript text.
"""

from __future__ import annotations

import dataclasses
from enum import Enum, auto
from typing import TYPE_CHECKING

from agenthicc.workflows.authoring.artifact import (
    AuthoringArtifact,
    AuthoringResult,
    ValidationReport,
    WorkflowCandidate,
)

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory


class AuthoringState(Enum):
    """States used by all three built-in authoring workflows."""

    INTERPRET = auto()
    DESIGN = auto()
    EXECUTE = auto()
    STAGE = auto()
    REVIEW = auto()
    PUBLISH = auto()
    SUMMARIZE = auto()
    COMPLETE = auto()
    REJECTED = auto()
    FAILED = auto()

    @property
    def phase_name(self) -> str | None:
        """Return the persisted phase name, or ``None`` for a terminal state."""

        names = {
            AuthoringState.INTERPRET: "interpret",
            AuthoringState.DESIGN: "design",
            AuthoringState.EXECUTE: "execute",
            AuthoringState.STAGE: "stage",
            AuthoringState.REVIEW: "review",
            AuthoringState.PUBLISH: "publish",
            AuthoringState.SUMMARIZE: "summarize",
        }
        return names.get(self)

    @property
    def is_terminal(self) -> bool:
        """Whether the state ends the authoring lifecycle."""

        return self in {
            AuthoringState.COMPLETE,
            AuthoringState.REJECTED,
            AuthoringState.FAILED,
        }


@dataclasses.dataclass
class AuthoringContext:
    """Mutable data carried by the explicit authoring state machine."""

    intent: str
    run_id: str
    shared_memory: "ShortTermMemory | None" = None
    interpreted_intent: str = ""
    design_summary: str = ""
    candidate: WorkflowCandidate | None = None
    report: ValidationReport = dataclasses.field(default_factory=ValidationReport)
    attempts: int = 0
    generation_text: str = ""
    artifact: AuthoringArtifact | None = None
    approval_granted: bool = False
    result: AuthoringResult | None = None
    phase_text: str = ""
    phase_approved: bool | None = None
    phase_structured: dict[str, object] = dataclasses.field(default_factory=dict)

    def set_phase_output(
        self,
        text: str,
        *,
        approved: bool | None = None,
        structured: dict[str, object] | None = None,
    ) -> None:
        """Set the transcript/effect payload emitted when the current phase ends."""

        self.phase_text = text
        self.phase_approved = approved
        self.phase_structured = structured or {}

    def clear_phase_output(self) -> None:
        """Clear the previous phase's completion payload before a new phase."""

        self.phase_text = ""
        self.phase_approved = None
        self.phase_structured = {}


_PHASE_TO_STATE: dict[str, AuthoringState] = {
    "interpret": AuthoringState.INTERPRET,
    "design": AuthoringState.DESIGN,
    "execute": AuthoringState.EXECUTE,
    "stage": AuthoringState.STAGE,
    "review": AuthoringState.REVIEW,
    "publish": AuthoringState.PUBLISH,
    "summarize": AuthoringState.SUMMARIZE,
}


def state_for_phase(name: str) -> AuthoringState:
    """Resolve a definition phase name to an explicit authoring state."""

    try:
        return _PHASE_TO_STATE[name]
    except KeyError as exc:
        raise ValueError(f"unknown authoring phase: {name!r}") from exc
