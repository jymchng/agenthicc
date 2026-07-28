"""Explicit lifecycle state for the built-in ``create_*`` workflows.

The authoring workflows have a deliberately small state machine.  Artifact
side effects remain owned by the runner, while transitions are represented by
typed states so a resume or a future UI does not have to infer lifecycle state
from free-form transcript text.
"""

from __future__ import annotations

from enum import Enum, auto


class AuthoringState(Enum):
    """States used by all three built-in authoring workflows."""

    INTERPRET = auto()
    DESIGN = auto()
    STAGE = auto()
    VALIDATE = auto()
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
            AuthoringState.STAGE: "stage",
            AuthoringState.VALIDATE: "validate",
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


_PHASE_TO_STATE: dict[str, AuthoringState] = {
    "interpret": AuthoringState.INTERPRET,
    "design": AuthoringState.DESIGN,
    "stage": AuthoringState.STAGE,
    "validate": AuthoringState.VALIDATE,
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
