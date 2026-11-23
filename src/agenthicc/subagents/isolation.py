"""Per-worker git worktrees for parallel subagent pools (SUBAGENT-15).

Workers that can change files run in one shared directory by default, so two of
them editing the same path race and the last write silently wins. A
:class:`PoolIsolation` gives each working worker its own git worktree, based on
one snapshot of the parent's *current* working state (uncommitted and untracked
files included), and afterwards applies each worker's changes back to the
parent's workspace atomically and in task order.

Nothing here changes a tool: every tool family derives its root from the
task-local workspace policy, so a worker that binds a policy over its worktree
reads, writes, runs commands and uses git there. The parent's index, ``HEAD``
and branch are never touched; results come back as patches, not merge commits.

The git work is blocking, so every public coroutine runs it in a thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agenthicc.worktrees.manager import RepositoryError, WorktreeError, WorktreeManager
from agenthicc.worktrees.model import WorktreeRecord

__all__ = [
    "IsolationError",
    "IsolationMode",
    "PoolIsolation",
    "WorkerHarvest",
    "WorkerIntegration",
    "WorkerWorkspace",
    "git_repository_for",
]

log = logging.getLogger(__name__)

#: ``subagents.isolation``: isolate when it helps, always, or never.
IsolationMode = Literal["auto", "worktree", "shared"]

#: What happened to a worker's changes when the pool finished.
IntegrationState = Literal["applied", "conflict", "kept", "none"]


class IsolationError(RuntimeError):
    """The pool's worktrees could not be created; no worker was started."""


@dataclass(frozen=True, slots=True)
class WorkerWorkspace:
    """One worker's isolated working directory."""

    task_id: str
    #: The directory the worker's tools resolve paths against (the worktree,
    #: plus the parent's own subdirectory when it was not the repository root).
    path: Path
    #: The worktree's top-level directory.
    root: Path
    branch: str
    base_commit: str


@dataclass(frozen=True, slots=True)
class WorkerHarvest:
    """What git recorded a worker changing, whatever tool changed it."""

    changed_paths: tuple[str, ...]
    head_commit: str


@dataclass(frozen=True, slots=True)
class WorkerIntegration:
    """The fate of one worker's changes."""

    state: IntegrationState
    conflict_paths: tuple[str, ...] = ()
    message: str = ""
    #: Where the work is preserved, for ``kept`` and ``conflict``.
    branch: str = ""
    workspace: str = ""


def git_repository_for(workspace_root: Path) -> Path | None:
    """The git top level containing *workspace_root*, or ``None`` outside a repository."""
    try:
        return WorktreeManager._resolve_repository(workspace_root)
    except RepositoryError:
        return None


class PoolIsolation:
    """The worktrees of one subagent pool: prepare, harvest, integrate, clean up."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        session_id: str,
        pool_id: str,
        worktree_root: Path | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._session_id = session_id or "session"
        self._pool_id = pool_id
        try:
            self._manager = WorktreeManager(self._workspace_root, root=worktree_root)
        except RepositoryError as exc:
            raise IsolationError(str(exc)) from exc
        try:
            self._subdirectory = self._workspace_root.relative_to(self._manager.repository)
        except ValueError:  # symlinked checkout: fall back to the repository root
            self._subdirectory = Path()
        self._records: dict[str, WorktreeRecord] = {}
        self._harvests: dict[str, WorkerHarvest] = {}
        self._preserved: set[str] = set()
        #: Workers apply their patches one at a time, each on top of the last.
        self._apply_lock = asyncio.Lock()

    @property
    def repository(self) -> Path:
        """The parent repository's top-level directory."""
        return self._manager.repository

    async def prepare(self, task_ids: Sequence[str]) -> dict[str, WorkerWorkspace]:
        """Snapshot the parent's working state and create one worktree per task.

        All-or-nothing: when any worktree cannot be created, those already
        created are removed and :class:`IsolationError` names the git error.
        """
        try:
            base = await asyncio.to_thread(self._manager.snapshot_commit)
        except WorktreeError as exc:
            raise IsolationError(f"could not snapshot the workspace: {exc}") from exc
        workspaces: dict[str, WorkerWorkspace] = {}
        try:
            for task_id in task_ids:
                record = await asyncio.to_thread(
                    self._manager.create,
                    parent_session_id=self._session_id,
                    task_id=f"{self._pool_id[:8]}-{task_id}",
                    base_commit=base,
                    require_clean=False,
                )
                self._records[task_id] = record
                root = Path(record.path)
                workspaces[task_id] = WorkerWorkspace(
                    task_id=task_id,
                    path=root / self._subdirectory,
                    root=root,
                    branch=record.branch,
                    base_commit=base,
                )
        except Exception as exc:
            await self.cleanup(frozenset())
            raise IsolationError(f"could not create a worker worktree: {exc}") from exc
        return workspaces

    async def harvest(self, task_id: str) -> WorkerHarvest:
        """Commit a worker's changes on its branch and list what changed."""
        record = self._records[task_id]
        path = Path(record.path)
        head = await asyncio.to_thread(
            self._manager.commit_all, path, f"agenthicc subagent {task_id}"
        )
        changed = await asyncio.to_thread(
            self._manager.changed_files, record.base_commit, head, path
        )
        harvest = WorkerHarvest(changed_paths=tuple(changed), head_commit=head)
        self._harvests[task_id] = harvest
        return harvest

    def preserve(self, task_id: str) -> None:
        """Keep *task_id*'s worktree and branch through :meth:`cleanup`."""
        self._preserved.add(task_id)

    async def release_if_clean(self, task_id: str) -> None:
        """Release a consumed authoring worktree after its remaining edits are checked."""
        record = self._records[task_id]
        if await asyncio.to_thread(self._manager.is_clean, cwd=Path(record.path)):
            self._preserved.discard(task_id)

    async def integrate_one(self, task_id: str, succeeded: bool) -> WorkerIntegration:
        """Apply one finished worker's changes to the parent's workspace.

        Called as each worker finishes, so the worker's own result already
        carries the outcome. The patch applies on top of whatever earlier
        workers applied, so edits to different parts of one file compose and
        only genuine overlaps conflict; when two overlap, the one that finishes
        first is applied. A patch that does not apply is reported with its
        paths, and its worktree and branch are kept; so are those of a failed
        worker that changed something.
        """
        record = self._records.get(task_id)
        harvest = self._harvests.get(task_id)
        if record is None or harvest is None or not harvest.changed_paths:
            return WorkerIntegration(state="none")
        if not succeeded:
            self._preserved.add(task_id)
            return WorkerIntegration(
                state="kept",
                message="the worker failed; its changes were not applied",
                branch=record.branch,
                workspace=record.path,
            )
        async with self._apply_lock:
            patch = await asyncio.to_thread(
                self._manager.diff_patch, record.base_commit, harvest.head_commit
            )
            applied = await asyncio.to_thread(self._manager.apply_patch, patch)
        if applied.ok:
            return WorkerIntegration(state="applied")
        self._preserved.add(task_id)
        return WorkerIntegration(
            state="conflict",
            conflict_paths=applied.conflict_paths,
            message=applied.message,
            branch=record.branch,
            workspace=record.path,
        )

    async def integrate(
        self,
        outcomes: Sequence[tuple[str, bool]],
    ) -> dict[str, WorkerIntegration]:
        """Integrate several workers in the order given (see :meth:`integrate_one`)."""
        return {task_id: await self.integrate_one(task_id, ok) for task_id, ok in outcomes}

    async def cleanup(self, preserve: AbstractSet[str] | None = None) -> None:
        """Remove every worktree and branch this pool made except preserved ones.

        With no argument, the preserved set is the workers whose changes were
        kept or conflicted (:meth:`integrate_one`).
        """
        keep = self._preserved if preserve is None else preserve
        for task_id, record in list(self._records.items()):
            if task_id in keep:
                continue
            try:
                await asyncio.to_thread(self._manager.remove, record, force=True)
            except WorktreeError:
                log.warning("could not remove worktree %s", record.path, exc_info=True)
            self._records.pop(task_id, None)
