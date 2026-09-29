"""Shared workflow tool for durable Git-isolated worker agents.

Unlike ``spawn_subagents``, this tool creates durable background Agenthicc
sessions.  It is injected at the canonical agent-turn boundary, so built-in
and custom workflows receive the same worker capability without importing a
particular workflow runner.
"""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from .coordinator import CoordinatorError, ParallelCoordinator
from .manager import RepositoryError, WorktreeError
from .store import ManifestStore

if TYPE_CHECKING:
    from agenthicc.background import BackgroundSupervisor
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy

__all__ = ["make_spawn_worker_agents_tool"]


class _WorkerTaskInput(TypedDict):
    """One task accepted by the workflow-wide worker tool."""

    task_id: str
    description: str
    dependencies: list[str] | None


def make_spawn_worker_agents_tool(
    *,
    repository: Path | str,
    parent_session_id: str,
    run_id: str = "",
    max_parallel_tasks: int = 4,
    workspace_access: "WorkspaceAccessPolicy | None" = None,
    store: ManifestStore | None = None,
    supervisor: "BackgroundSupervisor | None" = None,
) -> object:
    """Build the durable ``spawn_worker_agents`` tool for one parent session.

    The tool creates a new orchestration per invocation, records every task
    before dispatching it, and returns durable IDs rather than worker prose.
    Workers run a direct Agenthicc turn in their assigned worktree.  They are
    not given the parent's conversation history; the task description is their
    explicit context boundary.
    """

    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_control  # noqa: PLC0415

    effective_limit = max(1, int(max_parallel_tasks))
    manifest_store = store or ManifestStore()

    if workspace_access is not None:
        repository = workspace_access.scope.primary_root

    if supervisor is None:
        from agenthicc.background import BackgroundStore as _BackgroundStore  # noqa: PLC0415
        from agenthicc.background import BackgroundSupervisor as _BackgroundSupervisor  # noqa: PLC0415

        supervisor = _BackgroundSupervisor(
            _BackgroundStore(),
            max_workers=effective_limit,
            max_workers_per_project=effective_limit,
        )

    @tool_control
    @_tool()
    async def spawn_worker_agents(
        tasks: list[_WorkerTaskInput],
        max_concurrent: int = effective_limit,
        dangerously_skip_permissions: bool = False,
    ) -> dict[str, object]:
        """Create durable isolated worker agents for independent coding tasks.

        Each task receives a dedicated Git worktree and branch. Workers commit
        their changes; the parent later reviews and integrates them through
        the durable worktree controls. Set ``dangerously_skip_permissions``
        only when the parent has explicitly approved autonomous worker tool
        execution; otherwise workers use the normal background approval path.
        """

        if not isinstance(tasks, list) or not tasks:
            return {
                "ok": False,
                "error": "tasks must be a non-empty list",
                "message": "Provide at least one independent worker task.",
            }
        if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool):
            return {"ok": False, "error": "max_concurrent must be an integer"}
        if max_concurrent < 1:
            return {"ok": False, "error": "max_concurrent must be at least 1"}
        limit = min(max_concurrent, effective_limit)
        coordinator = ParallelCoordinator(
            repository,
            parent_session_id=parent_session_id,
            run_id=run_id,
            store=manifest_store,
            supervisor=supervisor,
            max_parallel_tasks=limit,
            worker_workflow_name="",
            worker_dangerously_skip_permissions=dangerously_skip_permissions,
        )
        try:
            manifest = await asyncio.to_thread(coordinator.create)
            for raw_task in tasks:
                if not isinstance(raw_task, dict):
                    raise CoordinatorError("every task must be an object")
                task_id = raw_task.get("task_id")
                description = raw_task.get("description")
                dependencies = raw_task.get("dependencies")
                if not isinstance(task_id, str) or not isinstance(description, str):
                    raise CoordinatorError("every task requires task_id and description strings")
                if dependencies is None:
                    dependency_ids: tuple[str, ...] = ()
                elif isinstance(dependencies, list) and all(
                    isinstance(item, str) for item in dependencies
                ):
                    dependency_ids = tuple(dependencies)
                else:
                    raise CoordinatorError("dependencies must be a list of task IDs")
                manifest = await asyncio.to_thread(
                    coordinator.add_task,
                    manifest.orchestration_id,
                    task_id=task_id,
                    description=description,
                    dependencies=dependency_ids,
                )
            dispatched = await coordinator.dispatch_ready(manifest.orchestration_id)
            refreshed = coordinator.manifest(manifest.orchestration_id)
        except (CoordinatorError, RepositoryError, WorktreeError, OSError, ValueError) as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "message": "Worker orchestration was not dispatched; inspect the repository and retry.",
            }
        return {
            "ok": True,
            "orchestration_id": refreshed.orchestration_id,
            "parent_session_id": refreshed.parent_session_id,
            "base_commit": refreshed.base_commit,
            "dispatched": [task.task_id for task in dispatched],
            "waiting": [
                task.task_id
                for task in refreshed.tasks
                if task.task_id not in {item.task_id for item in dispatched}
            ],
            "tasks": [task.to_dict() for task in refreshed.tasks],
            "message": (
                "Worker agents were created in isolated worktrees. "
                "Use the orchestration controls to collect, review, integrate, and verify them."
            ),
        }

    return spawn_worker_agents
