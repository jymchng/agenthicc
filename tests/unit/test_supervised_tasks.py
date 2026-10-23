"""SupervisedTasks holds, reports and drains background tasks (PRD-250 D-02)."""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from agenthicc.supervised_tasks import SupervisedTasks

pytestmark = pytest.mark.unit


async def test_a_started_task_survives_garbage_collection_until_it_finishes() -> None:
    tasks = SupervisedTasks("test")
    release = asyncio.Event()
    finished: list[str] = []

    async def work() -> None:
        await release.wait()
        finished.append("done")

    tasks.start(work(), name="work")  # the caller keeps no reference
    gc.collect()
    assert tasks.pending == 1
    release.set()

    assert await tasks.wait(timeout=5)
    assert finished == ["done"]
    assert tasks.pending == 0


async def test_a_failure_is_logged_with_the_owner_and_task_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tasks = SupervisedTasks("indexer")

    async def explode() -> None:
        raise RuntimeError("index exploded")

    with caplog.at_level(logging.ERROR, logger="agenthicc.supervised_tasks"):
        tasks.start(explode(), name="index-doc-1")
        assert await tasks.wait(timeout=5)

    [record] = caplog.records
    assert record.getMessage() == "indexer: background task 'index-doc-1' failed"
    assert record.exc_info is not None and "index exploded" in str(record.exc_info[1])


async def test_a_cancelled_task_is_not_reported_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tasks = SupervisedTasks("test")
    task = tasks.start(asyncio.sleep(10), name="sleeper")
    task.cancel()

    assert await tasks.wait(timeout=5)
    assert not caplog.records


async def test_wait_includes_tasks_started_while_waiting() -> None:
    tasks = SupervisedTasks("test")
    order: list[str] = []

    async def second() -> None:
        order.append("second")

    async def first() -> None:
        await asyncio.sleep(0)
        tasks.start(second(), name="second")
        order.append("first")

    tasks.start(first(), name="first")

    assert await tasks.wait(timeout=5)
    assert order == ["first", "second"]


async def test_wait_reports_a_timeout() -> None:
    tasks = SupervisedTasks("test")
    tasks.start(asyncio.sleep(10), name="sleeper")

    assert await tasks.wait(timeout=0.01) is False
    await tasks.aclose(timeout=0)


async def test_aclose_lets_quick_tasks_finish_and_cancels_the_rest() -> None:
    tasks = SupervisedTasks("test")
    finished: list[str] = []

    async def quick() -> None:
        await asyncio.sleep(0)
        finished.append("quick")

    slow = tasks.start(asyncio.sleep(10), name="slow")
    tasks.start(quick(), name="quick")

    await tasks.aclose(timeout=0.05)

    assert finished == ["quick"]
    assert slow.cancelled()
    assert tasks.closed
    assert tasks.pending == 0


async def test_start_after_aclose_raises_and_closes_the_coroutine() -> None:
    tasks = SupervisedTasks("test")
    await tasks.aclose()

    async def never() -> None:
        raise AssertionError("must not run")

    coro = never()
    with pytest.raises(RuntimeError, match="test is shut down"):
        tasks.start(coro, name="late")
    assert coro.cr_frame is None  # closed, so no "never awaited" warning


async def test_a_task_can_close_its_own_supervisor_without_waiting_for_itself() -> None:
    tasks = SupervisedTasks("test")
    other = tasks.start(asyncio.sleep(10), name="other")

    async def closer() -> None:
        await tasks.aclose(timeout=0.05)

    closing = tasks.start(closer(), name="closer")

    await asyncio.wait_for(closing, timeout=5)
    assert other.cancelled()
