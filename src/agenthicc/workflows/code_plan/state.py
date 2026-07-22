"""CodePlanState — explicit enum for the code_plan state machine."""

from __future__ import annotations

import dataclasses
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory


class CodePlanState(Enum):
    """Every possible state in the code_plan workflow.

    Transitions:
        PLAN      → EXECUTE  (plan approved + finalized)
               ↺  → PLAN     (plan not finalized — retry)
               → EXITED   (exit_code_plan called — clean early exit)
        EXECUTE   → REVIEW   (mark_execute_complete called)
        REVIEW    → SUMMARIZE (approve_review called)
               → EXECUTE   (reject_review called)
        SUMMARIZE → COMPLETE
        EXITED    (terminal — agent chose not to plan)
        FAILED    (terminal — any phase exhausted retries or error)
    """

    PLAN = auto()
    EXECUTE = auto()
    REVIEW = auto()
    SUMMARIZE = auto()
    COMPLETE = auto()  # terminal — success
    EXITED = auto()  # terminal — agent exited without planning
    FAILED = auto()  # terminal — exhausted retries or error

    @property
    def is_terminal(self) -> bool:
        return self in (CodePlanState.COMPLETE, CodePlanState.EXITED, CodePlanState.FAILED)


@dataclasses.dataclass
class CodePlanContext:
    """Data carried across phases in one code_plan run."""

    intent: str
    run_id: str
    plan: str = ""  # set after PLAN phase
    execute_summary: str = ""  # set after EXECUTE phase
    review_summary: str = ""  # set after REVIEW phase (approve)
    rejection_reason: str = ""  # set when REVIEW rejects
    fail_reason: str = ""  # set on FAILED
    shared_memory: ShortTermMemory | None = None  # shared across all phases
