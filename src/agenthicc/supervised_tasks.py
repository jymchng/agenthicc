"""SupervisedTasks — owned background tasks with observable failures.

``asyncio`` keeps only weak references to tasks. A task that nobody holds can
be garbage-collected before it finishes, and an exception that nobody
retrieves is reported, if at all, as an unattributed warning when the task is
destroyed. Every component that starts work in the background therefore owns
a :class:`SupervisedTasks`: it holds each task until the task finishes, logs a
failure with the owner and task names, and lets the owner wait for or cancel
the remaining work when it shuts down.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import TypeVar

__all__ = ["SupervisedTasks"]

log = logging.getLogger(__name__)

_T = TypeVar("_T")


class SupervisedTasks:
    """The detached tasks one component started, held until they finish."""

    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._tasks: set[asyncio.Task[object]] = set()
        self._closed = False

    @property
    def owner(self) -> str:
        """The component name used in failure logs."""
        return self._owner

    @property
    def pending(self) -> int:
        """Number of started tasks that have not finished."""
        return len(self._tasks)

    @property
    def closed(self) -> bool:
        """True once :meth:`aclose` has been called."""
        return self._closed

    def start(self, coro: Coroutine[object, object, _T], *, name: str) -> asyncio.Task[_T]:
        """Run *coro* as a task owned by this supervisor.

        Raises ``RuntimeError`` after :meth:`aclose`; the coroutine is closed
        so it is not reported as never awaited.
        """
        if self._closed:
            coro.close()
            raise RuntimeError(f"{self._owner} is shut down; cannot start {name!r}")
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "%s: background task %r failed",
                self._owner,
                task.get_name(),
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _others(self) -> set[asyncio.Task[object]]:
        """Pending tasks other than the caller's own, which cannot wait for itself."""
        current = asyncio.current_task()
        return {task for task in self._tasks if task is not current}

    async def wait(self, timeout: float | None = None) -> bool:
        """Wait for every task, including ones started while waiting.

        Returns ``True`` when none remain, ``False`` if *timeout* expired.
        Task failures are logged by the supervisor, never raised here.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while others := self._others():
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False
            await asyncio.wait(others, timeout=remaining)
        return True

    async def aclose(self, *, timeout: float = 5.0) -> None:
        """Stop accepting tasks, give running ones *timeout* seconds, cancel the rest."""
        self._closed = True
        if await self.wait(timeout):
            return
        remaining = self._others()
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
