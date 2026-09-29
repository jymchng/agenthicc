"""Behavior coverage for the durable parallel-code-plan controls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agenthicc.workflows.parallel_code_plan.phase_tools import (
    make_collect_tools,
    make_complete_tool,
    make_dispatch_tools,
    make_integrate_tools,
    make_task_graph_tools,
)
from agenthicc.worktrees.manager import IntegrationResult
from agenthicc.worktrees.model import ParallelTask, TaskStatus, WorkerResult


class _Coordinator:
    def __init__(self) -> None:
        self.tasks: list[ParallelTask] = []
        self.raise_on: set[str] = set()
        self.integration = IntegrationResult(True, "/repo", "main", "head")

    def add_task(self, _orchestration_id: str, **kwargs: object) -> SimpleNamespace:
        if "add" in self.raise_on:
            raise ValueError("bad graph")
        task = ParallelTask(
            task_id=str(kwargs["task_id"]),
            description=str(kwargs["description"]),
            dependencies=tuple(kwargs.get("dependencies", ())),
        )
        self.tasks.append(task)
        return SimpleNamespace(tasks=tuple(self.tasks))

    def manifest(self, _orchestration_id: str) -> SimpleNamespace:
        return SimpleNamespace(tasks=tuple(self.tasks))

    async def dispatch_ready(self, _orchestration_id: str) -> list[ParallelTask]:
        if "dispatch" in self.raise_on:
            from agenthicc.worktrees.coordinator import CoordinatorError

            raise CoordinatorError("dispatch failed")
        self.tasks = [task.evolve(status=TaskStatus.RUNNING) for task in self.tasks]
        return list(self.tasks)

    def inspect_task(self, _orchestration_id: str, task_id: str) -> WorkerResult:
        if "collect" in self.raise_on:
            raise RuntimeError("collect failed")
        result = WorkerResult(
            task_id=task_id,
            worker_session_id="worker",
            worktree_id="worktree",
            base_commit="base",
            head_commit="head",
            clean=True,
            status="complete",
            summary="tests pass",
        )
        self.tasks = [
            task.evolve(status=TaskStatus.COMPLETED, result=result)
            if task.task_id == task_id
            else task
            for task in self.tasks
        ]
        return result

    def integrate_task(self, _orchestration_id: str, _task_id: str) -> IntegrationResult:
        if "integrate_error" in self.raise_on:
            raise RuntimeError("integration failed")
        return self.integration


async def _call(tool: object, *args: object, **kwargs: object) -> dict[str, object]:
    return await tool(*args, **kwargs)  # type: ignore[operator]


@pytest.mark.asyncio
async def test_task_graph_dispatch_collect_integrate_and_complete_controls() -> None:
    coordinator = _Coordinator()
    event = asyncio.Event()
    data: dict[str, object] = {}

    add, finalize = make_task_graph_tools(coordinator, "orch", event, data)
    assert (await _call(add, "", "task", []))["ok"] is False
    assert (await _call(add, "task", "", []))["ok"] is False
    coordinator.raise_on.add("add")
    assert (await _call(add, "task", "description", []))["ok"] is False
    coordinator.raise_on.clear()
    assert (await _call(add, "task", "description", []))["ok"] is True
    assert (await _call(finalize, ""))["ok"] is False
    assert (await _call(finalize, "graph summary"))["ok"] is True
    assert data["summary"] == "graph summary"
    assert event.is_set()

    event = asyncio.Event()
    data = {}
    dispatch = make_dispatch_tools(coordinator, "orch", event, data)[0]
    assert (await _call(dispatch, ""))["ok"] is False
    coordinator.raise_on.add("dispatch")
    assert (await _call(dispatch, "dispatch summary"))["ok"] is False
    coordinator.raise_on.clear()
    assert (await _call(dispatch, "dispatch summary"))["ok"] is True

    event = asyncio.Event()
    data = {}
    collect = make_collect_tools(coordinator, "orch", event, data)[0]
    assert (await _call(collect, ""))["ok"] is False
    coordinator.raise_on.add("collect")
    assert (await _call(collect, "collect summary"))["ok"] is False
    coordinator.raise_on.clear()
    assert (await _call(collect, "collect summary"))["ok"] is True
    assert event.is_set()

    event = asyncio.Event()
    data = {}
    integrate = make_integrate_tools(coordinator, "orch", event, data)[0]
    assert (await _call(integrate, "", "summary"))["ok"] is False
    assert (await _call(integrate, "task", ""))["ok"] is False
    coordinator.raise_on.add("integrate_error")
    assert (await _call(integrate, "task", "summary"))["ok"] is False
    coordinator.raise_on.clear()
    coordinator.integration = IntegrationResult(
        False, "/repo", "main", conflict_paths=("src/app.py",), message="conflict"
    )
    conflict = await _call(integrate, "task", "summary")
    assert conflict["conflict"] is True
    coordinator.integration = IntegrationResult(True, "/repo", "main", "head")
    success = await _call(integrate, "task", "summary")
    assert success["ok"] is True

    complete = make_complete_tool(coordinator, "orch", event, data)[0]
    assert (await _call(complete, ""))["ok"] is False
    coordinator.tasks = [task.evolve(status=TaskStatus.INTEGRATED) for task in coordinator.tasks]
    assert (await _call(complete, "verified"))["ok"] is True
