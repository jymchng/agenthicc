"""Phase-scoped control tools for the parallel code-plan workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from agenthicc.worktrees.coordinator import CoordinatorError, ParallelCoordinator
from agenthicc.worktrees.model import TaskStatus


def _failure(message: str, fix: str) -> dict[str, object]:
    return {"ok": False, "error": message, "fix": fix, "message": f"{message} Fix: {fix}"}


def make_task_graph_tools(
    coordinator: ParallelCoordinator,
    orchestration_id: str,
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_control  # noqa: PLC0415

    @tool_control
    @_tool()
    async def add_parallel_task(
        task_id: str,
        description: str,
        dependencies: list[str],
    ) -> dict[str, object]:
        """Add one independently implementable task to the durable task graph."""

        if not isinstance(task_id, str) or not task_id.strip():
            return _failure("task_id must be non-empty", "Provide a stable short task id.")
        if not isinstance(description, str) or not description.strip():
            return _failure("description must be non-empty", "Describe the concrete worker task.")
        try:
            manifest = coordinator.add_task(
                orchestration_id,
                task_id=task_id,
                description=description,
                dependencies=tuple(dependencies) if isinstance(dependencies, list) else (),
            )
        except (CoordinatorError, KeyError, ValueError) as exc:
            return _failure(str(exc), "Correct the task graph input and try again.")
        return {
            "ok": True,
            "task_id": task_id,
            "task_count": len(manifest.tasks),
            "message": "Task added. Add the remaining tasks, then finalize the graph.",
        }

    @tool_control
    @_tool()
    async def finalize_task_graph(summary: str) -> dict[str, object]:
        """Finish decomposition after every worker task has been added."""

        if not isinstance(summary, str) or not summary.strip():
            return _failure("summary must be non-empty", "Summarize the task graph.")
        manifest = coordinator.manifest(orchestration_id)
        if not manifest.tasks:
            return _failure("task graph is empty", "Add at least one worker task first.")
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "task_count": len(manifest.tasks), "message": "Task graph finalized."}

    return [add_parallel_task, finalize_task_graph]


def make_dispatch_tools(
    coordinator: ParallelCoordinator,
    orchestration_id: str,
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_control  # noqa: PLC0415

    @tool_control
    @_tool()
    async def dispatch_parallel_workers(summary: str) -> dict[str, object]:
        """Create isolated branches and launch all currently ready workers."""

        if not isinstance(summary, str) or not summary.strip():
            return _failure("summary must be non-empty", "Explain what is being dispatched.")
        try:
            tasks = await coordinator.dispatch_ready(orchestration_id)
        except (CoordinatorError, RuntimeError, OSError, ValueError) as exc:
            return _failure(str(exc), "Resolve the repository or dependency issue and retry.")
        data["summary"] = summary.strip()
        event.set()
        return {
            "ok": True,
            "dispatched": [task.task_id for task in tasks],
            "message": "Workers dispatched. Stop this phase and let the runner monitor them.",
        }

    return [dispatch_parallel_workers]


def make_collect_tools(
    coordinator: ParallelCoordinator,
    orchestration_id: str,
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def collect_parallel_workers(summary: str) -> dict[str, object]:
        """Refresh completion evidence for all dispatched workers."""

        if not isinstance(summary, str) or not summary.strip():
            return _failure("summary must be non-empty", "Summarize the observed worker state.")
        manifest = coordinator.manifest(orchestration_id)
        results: list[dict[str, object]] = []
        for task in manifest.tasks:
            if task.status in {TaskStatus.RUNNING, TaskStatus.COMPLETED, TaskStatus.CONFLICT}:
                try:
                    result = coordinator.inspect_task(orchestration_id, task.task_id)
                except (CoordinatorError, OSError, RuntimeError) as exc:
                    return _failure(str(exc), f"Worker {task.task_id} is not ready; retry later.")
                results.append(result.to_dict())
        refreshed = coordinator.manifest(orchestration_id)
        pending = [
            task.task_id
            for task in refreshed.tasks
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.INTEGRATED}
        ]
        if pending:
            return {
                "ok": False,
                "pending": pending,
                "results": results,
                "message": "Some workers are not complete; wait and call collect_parallel_workers again.",
            }
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "results": results, "message": "All workers have completion evidence."}

    return [collect_parallel_workers]


def make_integrate_tools(
    coordinator: ParallelCoordinator,
    orchestration_id: str,
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def integrate_parallel_worker(task_id: str, summary: str) -> dict[str, object]:
        """Merge one completed worker branch into the coordinator branch."""

        if not isinstance(task_id, str) or not task_id.strip():
            return _failure("task_id must be non-empty", "Choose a completed task.")
        if not isinstance(summary, str) or not summary.strip():
            return _failure("summary must be non-empty", "Explain the integration decision.")
        try:
            result = coordinator.integrate_task(orchestration_id, task_id)
        except (CoordinatorError, OSError, RuntimeError) as exc:
            return _failure(str(exc), "Review the worker evidence and retry integration.")
        if not result.ok:
            return {
                "ok": False,
                "conflict": True,
                "conflict_paths": list(result.conflict_paths),
                "message": result.message,
            }
        data["summary"] = summary.strip()
        if all(
            item.status == TaskStatus.INTEGRATED
            for item in coordinator.manifest(orchestration_id).tasks
        ):
            event.set()
        return {"ok": True, "task_id": task_id, "head_commit": result.head_commit}

    return [integrate_parallel_worker]


def make_complete_tool(
    coordinator: ParallelCoordinator,
    orchestration_id: str,
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def mark_parallel_complete(summary: str) -> dict[str, object]:
        """Complete the workflow after every task is integrated and verified."""

        if not isinstance(summary, str) or not summary.strip():
            return _failure("summary must be non-empty", "Provide final verification evidence.")
        manifest = coordinator.manifest(orchestration_id)
        if any(task.status != TaskStatus.INTEGRATED for task in manifest.tasks):
            return _failure(
                "not every worker is integrated",
                "Integrate every completed task before marking the workflow complete.",
            )
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Parallel implementation completed."}

    return [mark_parallel_complete]
