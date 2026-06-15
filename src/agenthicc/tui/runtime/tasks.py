"""Observable async tasks (PRD-61 §4)."""
from __future__ import annotations

import asyncio
from enum import Enum, auto
from typing import Coroutine

from agenthicc.reactive import Signal


class TaskState(Enum):
    PENDING   = auto()
    RUNNING   = auto()
    DONE      = auto()
    CANCELLED = auto()
    FAILED    = auto()


class TaskHandle:
    """Observable wrapper around an asyncio.Task."""

    def __init__(self, name: str, coro: Coroutine) -> None:
        self.name  = name
        self.state = Signal(TaskState.PENDING)
        self.error: Exception | None = None
        self._coro = coro
        self._task: asyncio.Task | None = None

    def start(self) -> "TaskHandle":
        async def _run() -> None:
            self.state.set(TaskState.RUNNING)
            try:
                await self._coro
                self.state.set(TaskState.DONE)
            except asyncio.CancelledError:
                self.state.set(TaskState.CANCELLED)
            except Exception as exc:
                self.error = exc
                self.state.set(TaskState.FAILED)

        self._task = asyncio.create_task(_run(), name=self.name)
        return self

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def is_running(self) -> bool:
        return self.state() == TaskState.RUNNING

    async def wait(self) -> None:
        if self._task:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


class TaskManager:
    """Registry of active tasks. Enables cancellation and monitoring."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskHandle] = {}

    def spawn(self, name: str, coro: Coroutine) -> TaskHandle:
        handle = TaskHandle(name, coro).start()
        self._tasks[name] = handle
        return handle

    def cancel(self, name: str) -> None:
        if handle := self._tasks.get(name):
            handle.cancel()

    def cancel_all(self) -> None:
        for handle in list(self._tasks.values()):
            handle.cancel()

    @property
    def active(self) -> list[TaskHandle]:
        return [h for h in self._tasks.values() if h.is_running]

    def get(self, name: str) -> TaskHandle | None:
        return self._tasks.get(name)
