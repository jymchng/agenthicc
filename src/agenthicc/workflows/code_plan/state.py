"""CodePlanState — explicit enum for the code_plan state machine."""
from __future__ import annotations

import dataclasses
from enum import Enum, auto
from typing import Any


class CodePlanState(Enum):
    """Every possible state in the code_plan workflow.

    Transitions:
        PLAN      → EXECUTE (plan approved + finalized)
               ↺  → PLAN    (plan not finalized — retry)
        EXECUTE   → REVIEW   (mark_execute_complete called)
        REVIEW    → SUMMARIZE (approve_review called)
               → EXECUTE   (reject_review called)
        SUMMARIZE → COMPLETE
        FAILED    (any phase exhausted retries)
    """

    PLAN      = auto()
    EXECUTE   = auto()
    REVIEW    = auto()
    SUMMARIZE = auto()
    COMPLETE  = auto()   # terminal — success
    FAILED    = auto()   # terminal — exhausted retries or error

    @property
    def is_terminal(self) -> bool:
        return self in (CodePlanState.COMPLETE, CodePlanState.FAILED)


@dataclasses.dataclass
class CodePlanContext:
    """Data carried across phases in one code_plan run."""

    intent:           str
    run_id:           str
    plan:             str       = ""   # set after PLAN phase
    execute_summary:  str       = ""   # set after EXECUTE phase
    review_summary:   str       = ""   # set after REVIEW phase (approve)
    rejection_reason: str       = ""   # set when REVIEW rejects
    fail_reason:      str       = ""   # set on FAILED
    shared_memory:    Any       = None # ShortTermMemory shared across all phases
