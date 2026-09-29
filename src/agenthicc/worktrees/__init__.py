"""Parallel coding orchestration with isolated Git worktrees (PRD-203)."""

from .coordinator import CoordinatorError, ParallelCoordinator, worker_prompt
from .manager import (
    IntegrationResult,
    RepositoryError,
    WorktreeConflictError,
    WorktreeDirtyError,
    WorktreeError,
    WorktreeInspection,
    WorktreeManager,
    WorktreeNotFound,
)
from .model import (
    OrchestrationStatus,
    ParallelManifest,
    ParallelTask,
    TaskStatus,
    WorktreeRecord,
    WorktreeStatus,
    WorkerResult,
)
from .store import ManifestNotFound, ManifestStore

__all__ = [
    "CoordinatorError",
    "IntegrationResult",
    "ManifestNotFound",
    "ManifestStore",
    "OrchestrationStatus",
    "ParallelCoordinator",
    "ParallelManifest",
    "ParallelTask",
    "RepositoryError",
    "TaskStatus",
    "WorktreeConflictError",
    "WorktreeDirtyError",
    "WorktreeError",
    "WorktreeInspection",
    "WorktreeManager",
    "WorktreeNotFound",
    "WorktreeRecord",
    "WorktreeStatus",
    "WorkerResult",
    "worker_prompt",
]
