"""Durable goal-run identity and orchestration projections (PRD-204).

This package owns the product-level identity of a goal.  It deliberately
delegates process execution to :mod:`agenthicc.background` and Git isolation
to :mod:`agenthicc.worktrees`.
"""

from .manager import GoalRunManager, RepositoryValidationError
from .model import GoalRun, GoalRunStatus, RunAgentRecord, RunTaskRecord, RunWorktreeRecord
from .store import RunNotFound, RunStore

__all__ = [
    "GoalRun",
    "GoalRunManager",
    "GoalRunStatus",
    "RepositoryValidationError",
    "RunAgentRecord",
    "RunTaskRecord",
    "RunWorktreeRecord",
    "RunNotFound",
    "RunStore",
]
