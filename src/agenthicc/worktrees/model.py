"""Durable models for parallel worker orchestration.

The models in this module are deliberately independent from the agent runner.
They are the durable identity boundary between a coordinator, worker sessions,
and Git integration.  Every value has a JSON representation so a process can
be restarted without reconstructing state from conversational text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, TypeVar


_EnumType = TypeVar("_EnumType", bound=Enum)


class WorktreeStatus(str, Enum):
    CREATING = "creating"
    ACTIVE = "active"
    COMPLETED = "completed"
    INTEGRATING = "integrating"
    INTEGRATED = "integrated"
    CONFLICT = "conflict"
    ORPHANED = "orphaned"
    REMOVED = "removed"


class TaskStatus(str, Enum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INTEGRATING = "integrating"
    INTEGRATED = "integrated"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"
    CLEANED = "cleaned"


class OrchestrationStatus(str, Enum):
    PLANNING = "planning"
    RUNNING = "running"
    REVIEWING = "reviewing"
    INTEGRATING = "integrating"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    FAILED = "failed"
    RECOVERING = "recovering"


def _str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _enum(enum_type: type[_EnumType], value: object, default: _EnumType) -> _EnumType:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            pass
    return default


def _timestamp(value: object, default: float = 0.0) -> float:
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


@dataclass(frozen=True)
class WorktreeRecord:
    """One isolated Git worktree owned by one parent orchestration."""

    worktree_id: str
    repository: str
    path: str
    branch: str
    base_commit: str
    owner_session_id: str
    task_id: str
    status: WorktreeStatus = WorktreeStatus.CREATING
    head_commit: str = ""
    dirty: bool = False
    conflict_paths: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_error: str = ""

    def evolve(self, **changes: object) -> "WorktreeRecord":
        dirty_value = changes.get("dirty", self.dirty)
        return WorktreeRecord(
            worktree_id=self.worktree_id,
            repository=_str(changes.get("repository", self.repository), self.repository),
            path=_str(changes.get("path", self.path), self.path),
            branch=_str(changes.get("branch", self.branch), self.branch),
            base_commit=_str(changes.get("base_commit", self.base_commit), self.base_commit),
            owner_session_id=_str(
                changes.get("owner_session_id", self.owner_session_id), self.owner_session_id
            ),
            task_id=_str(changes.get("task_id", self.task_id), self.task_id),
            status=_enum(WorktreeStatus, changes.get("status", self.status), self.status),
            head_commit=_str(changes.get("head_commit", self.head_commit), self.head_commit),
            dirty=dirty_value if isinstance(dirty_value, bool) else self.dirty,
            conflict_paths=_tuple(changes.get("conflict_paths", self.conflict_paths)),
            created_at=_timestamp(changes.get("created_at", self.created_at), self.created_at),
            updated_at=_timestamp(changes.get("updated_at", time.time()), time.time()),
            last_error=_str(changes.get("last_error", self.last_error), self.last_error),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "worktree_id": self.worktree_id,
            "repository": self.repository,
            "path": self.path,
            "branch": self.branch,
            "base_commit": self.base_commit,
            "owner_session_id": self.owner_session_id,
            "task_id": self.task_id,
            "status": self.status.value,
            "head_commit": self.head_commit,
            "dirty": self.dirty,
            "conflict_paths": list(self.conflict_paths),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "WorktreeRecord":
        worktree_id = _str(value.get("worktree_id"))
        if not worktree_id:
            raise ValueError("worktree record requires worktree_id")
        return cls(
            worktree_id=worktree_id,
            repository=_str(value.get("repository")),
            path=_str(value.get("path")),
            branch=_str(value.get("branch")),
            base_commit=_str(value.get("base_commit")),
            owner_session_id=_str(value.get("owner_session_id")),
            task_id=_str(value.get("task_id")),
            status=_enum(WorktreeStatus, value.get("status"), WorktreeStatus.ORPHANED),
            head_commit=_str(value.get("head_commit")),
            dirty=bool(value.get("dirty", False)),
            conflict_paths=_tuple(value.get("conflict_paths")),
            created_at=_timestamp(value.get("created_at")),
            updated_at=_timestamp(value.get("updated_at")),
            last_error=_str(value.get("last_error")),
        )


@dataclass(frozen=True)
class WorkerResult:
    """Machine-derived completion evidence for a worker."""

    task_id: str
    worker_session_id: str
    worktree_id: str
    base_commit: str
    head_commit: str
    commits: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    clean: bool = False
    status: str = "incomplete"
    summary: str = ""
    tests: tuple[str, ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "worker_session_id": self.worker_session_id,
            "worktree_id": self.worktree_id,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "commits": list(self.commits),
            "changed_files": list(self.changed_files),
            "clean": self.clean,
            "status": self.status,
            "summary": self.summary,
            "tests": list(self.tests),
            "error": self.error,
        }


@dataclass(frozen=True)
class ParallelTask:
    """One task in the coordinator's dependency graph."""

    task_id: str
    description: str
    dependencies: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    worker_session_id: str = ""
    worktree_id: str = ""
    result: WorkerResult | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def evolve(self, **changes: object) -> "ParallelTask":
        result = changes.get("result", self.result)
        return ParallelTask(
            task_id=self.task_id,
            description=_str(changes.get("description", self.description), self.description),
            dependencies=_tuple(changes.get("dependencies", self.dependencies)),
            status=_enum(TaskStatus, changes.get("status", self.status), self.status),
            worker_session_id=_str(
                changes.get("worker_session_id", self.worker_session_id), self.worker_session_id
            ),
            worktree_id=_str(changes.get("worktree_id", self.worktree_id), self.worktree_id),
            result=result if isinstance(result, WorkerResult) or result is None else self.result,
            error=_str(changes.get("error", self.error), self.error),
            created_at=self.created_at,
            updated_at=_timestamp(changes.get("updated_at", time.time()), time.time()),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "dependencies": list(self.dependencies),
            "status": self.status.value,
            "worker_session_id": self.worker_session_id,
            "worktree_id": self.worktree_id,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ParallelManifest:
    """Crash-recoverable parent orchestration manifest."""

    orchestration_id: str
    parent_session_id: str
    repository: str
    parent_branch: str
    base_commit: str
    status: OrchestrationStatus = OrchestrationStatus.PLANNING
    tasks: tuple[ParallelTask, ...] = ()
    worktrees: tuple[WorktreeRecord, ...] = ()
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_error: str = ""
    verification: tuple[str, ...] = ()

    def task(self, task_id: str) -> ParallelTask:
        for item in self.tasks:
            if item.task_id == task_id:
                return item
        raise KeyError(task_id)

    def worktree(self, worktree_id: str) -> WorktreeRecord:
        for item in self.worktrees:
            if item.worktree_id == worktree_id:
                return item
        raise KeyError(worktree_id)

    def evolve(self, **changes: object) -> "ParallelManifest":
        tasks = changes.get("tasks", self.tasks)
        worktrees = changes.get("worktrees", self.worktrees)
        return ParallelManifest(
            orchestration_id=self.orchestration_id,
            parent_session_id=self.parent_session_id,
            repository=self.repository,
            parent_branch=self.parent_branch,
            base_commit=self.base_commit,
            status=_enum(OrchestrationStatus, changes.get("status", self.status), self.status),
            tasks=tuple(item for item in tasks if isinstance(item, ParallelTask))
            if isinstance(tasks, (list, tuple))
            else self.tasks,
            worktrees=tuple(item for item in worktrees if isinstance(item, WorktreeRecord))
            if isinstance(worktrees, (list, tuple))
            else self.worktrees,
            created_at=self.created_at,
            updated_at=_timestamp(changes.get("updated_at", time.time()), time.time()),
            last_error=_str(changes.get("last_error", self.last_error), self.last_error),
            verification=_tuple(changes.get("verification", self.verification)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "orchestration_id": self.orchestration_id,
            "parent_session_id": self.parent_session_id,
            "repository": self.repository,
            "parent_branch": self.parent_branch,
            "base_commit": self.base_commit,
            "status": self.status.value,
            "tasks": [item.to_dict() for item in self.tasks],
            "worktrees": [item.to_dict() for item in self.worktrees],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
            "verification": list(self.verification),
        }
