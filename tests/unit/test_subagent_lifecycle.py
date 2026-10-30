"""Worker lifecycle stop, drain, and cancellation behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import agenthicc.subagents.pool as pool_module
from agenthicc.subagents.pool import (
    SubagentPool,
    SubagentResult,
    SubagentTask,
    active_pool,
    retryable_pool,
)
from agenthicc.subagents.transcripts import read_transcript
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.tui.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


class _ControlledWorker:
    started: ClassVar[dict[str, asyncio.Event]] = {}
    release: ClassVar[dict[str, asyncio.Event]] = {}

    def __init__(self, task: SubagentTask, spec: object, index: int, **kwargs: object) -> None:
        self._task = task
        self.label = f"{task.agent_type} #{index}"

    async def run(self) -> SubagentResult:
        task_id = self._task.task_id
        self.started[task_id].set()
        try:
            await self.release[task_id].wait()
        except asyncio.CancelledError:
            return SubagentResult(
                task_id,
                self._task.agent_type,
                self.label,
                False,
                "provider turn completed before cancellation",
                error="cancelled",
                partial=True,
                stop_reason="cancelled",
            )
        return SubagentResult(
            task_id,
            self._task.agent_type,
            self.label,
            True,
            f"completed {task_id}",
        )


def _prepare_workers(monkeypatch: pytest.MonkeyPatch, task_ids: tuple[str, ...]) -> None:
    _ControlledWorker.started = {task_id: asyncio.Event() for task_id in task_ids}
    _ControlledWorker.release = {task_id: asyncio.Event() for task_id in task_ids}
    monkeypatch.setattr(pool_module, "SubagentWorker", _ControlledWorker)


def _pool(
    tasks: list[SubagentTask],
    *,
    max_concurrent: int,
    stop_grace_s: float = 5.0,
    transcript_root: Path | None = None,
    conv_store: ConversationStore | None = None,
) -> SubagentPool:
    return SubagentPool(
        tasks,
        parent_runner=SimpleNamespace(_transport=None),
        parent_model="model",
        all_tools=[],
        max_concurrent=max_concurrent,
        registry=DEFAULT_REGISTRY,
        stop_grace_s=stop_grace_s,
        transcript_root=transcript_root,
        conv_store=conv_store,
    )


@pytest.mark.asyncio
async def test_abort_salvages_partial_output_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workers(monkeypatch, ("worker-1",))
    pool = _pool(
        [SubagentTask("worker-1", "researcher", "research")], max_concurrent=1, stop_grace_s=0
    )
    run = asyncio.create_task(pool.run())
    await asyncio.wait_for(_ControlledWorker.started["worker-1"].wait(), timeout=1)

    first = pool.request_stop(
        "worker-1", mode="abort", source="user", reason="cancelled_by_operator"
    )
    second = pool.request_stop("worker-1", mode="abort", source="user", reason="duplicate")
    result = await run

    assert first is not None
    assert second == first
    assert result.failed == 1
    assert "provider turn completed before cancellation" in result.text
    lifecycle = pool.lifecycle("worker-1")
    assert lifecycle is not None
    assert lifecycle.phase == "cancelled"
    assert lifecycle.stop_request is not None and lifecycle.stop_request.phase == "cancelled"
    assert lifecycle.partial_result is not None
    assert lifecycle.partial_result.stop_reason == "cancelled_by_operator"
    assert lifecycle.partial_result.partial is True


@pytest.mark.asyncio
async def test_drain_finishes_running_worker_and_does_not_start_queued_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workers(monkeypatch, ("running", "queued"))
    tasks = [
        SubagentTask("running", "researcher", "first"),
        SubagentTask("queued", "researcher", "second"),
    ]
    pool = _pool(tasks, max_concurrent=1)
    run = asyncio.create_task(pool.run())
    await asyncio.wait_for(_ControlledWorker.started["running"].wait(), timeout=1)

    pool.request_pool_stop(mode="drain", source="budget", reason="budget_exceeded")
    _ControlledWorker.release["running"].set()
    result = await run

    assert result.total == 1
    assert result.succeeded == 1
    assert result.failed == 0
    assert result.not_started_workers == ("researcher #2",)
    assert "Not started" in result.text and "researcher #2" in result.text
    assert _ControlledWorker.started["queued"].is_set() is False
    running = pool.lifecycle("running")
    queued = pool.lifecycle("queued")
    assert running is not None and running.phase == "completed"
    assert running.stop_request is not None and running.stop_request.phase == "completed"
    assert queued is not None and queued.phase == "drained"
    assert queued.stop_request is not None and queued.stop_request.phase == "drained"
    assert queued.partial_result is None
    assert pool._results["queued"].not_started is True


@pytest.mark.asyncio
async def test_parent_cancellation_requests_abort_and_waits_for_worker_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workers(monkeypatch, ("worker-1",))
    pool = _pool(
        [SubagentTask("worker-1", "researcher", "research")], max_concurrent=1, stop_grace_s=0
    )
    run = asyncio.create_task(pool.run())
    await asyncio.wait_for(_ControlledWorker.started["worker-1"].wait(), timeout=1)

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    lifecycle = pool.lifecycle("worker-1")
    assert lifecycle is not None and lifecycle.phase == "cancelled"
    assert lifecycle.stop_request is not None
    assert lifecycle.stop_request.source == "parent"
    assert lifecycle.partial_result is not None
    assert lifecycle.partial_result.partial is True


@pytest.mark.asyncio
async def test_retry_reruns_only_failed_worker_and_records_attempt_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _RetryWorker:
        calls: ClassVar[dict[str, int]] = {}

        def __init__(self, task: SubagentTask, spec: object, index: int, **kwargs: object) -> None:
            self._task = task
            self.label = f"{task.agent_type} #{index}"

        async def run(self) -> SubagentResult:
            task_id = self._task.task_id
            attempt = self.calls.get(task_id, 0) + 1
            self.calls[task_id] = attempt
            if task_id == "retryable" and attempt == 1:
                return SubagentResult(
                    task_id=task_id,
                    agent_type=self._task.agent_type,
                    label=self.label,
                    ok=False,
                    text="first attempt output",
                    error="temporary failure",
                )
            return SubagentResult(
                task_id=task_id,
                agent_type=self._task.agent_type,
                label=self.label,
                ok=True,
                text=f"completed {task_id}",
            )

    _RetryWorker.calls = {}
    monkeypatch.setattr(pool_module, "SubagentWorker", _RetryWorker)
    conversation_store = ConversationStore()
    pool = _pool(
        [
            SubagentTask("retryable", "researcher", "retry this task"),
            SubagentTask("sibling", "researcher", "leave this task alone"),
        ],
        max_concurrent=2,
        transcript_root=tmp_path,
        conv_store=conversation_store,
    )

    initial = await pool.run()
    assert initial.failed == 1
    assert retryable_pool(pool.pool_id) is pool
    assert _RetryWorker.calls == {"retryable": 1, "sibling": 1}

    retried = await pool.retry_worker("retryable")

    assert retried is not None and retried.ok is True
    assert retryable_pool(pool.pool_id) is None
    assert _RetryWorker.calls == {"retryable": 2, "sibling": 1}
    lifecycle = pool.lifecycle("retryable")
    assert lifecycle is not None
    assert lifecycle.attempt == 2
    assert lifecycle.phase == "completed"
    assert len(lifecycle.previous_attempts) == 1
    assert lifecycle.previous_attempts[0].error == "temporary failure"
    assert pool.state.worker("retryable").status == "done"
    events = read_transcript(tmp_path / pool.pool_id / "retryable.jsonl")
    assert any(event.kind == "attempt_started" and event.attempt == 2 for event in events)
    assert any(event.kind == "finished" and event.attempt == 2 and event.ok for event in events)


@pytest.mark.asyncio
async def test_parent_cancellation_during_retry_stops_and_salvages_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RetryWorker:
        calls: ClassVar[int] = 0
        retry_started: ClassVar[asyncio.Event] = asyncio.Event()
        release: ClassVar[asyncio.Event] = asyncio.Event()

        def __init__(self, task: SubagentTask, spec: object, index: int, **kwargs: object) -> None:
            self._task = task
            self.label = f"{task.agent_type} #{index}"

        async def run(self) -> SubagentResult:
            type(self).calls += 1
            if type(self).calls == 1:
                return SubagentResult(
                    self._task.task_id,
                    self._task.agent_type,
                    self.label,
                    False,
                    "first attempt",
                    error="retry me",
                )
            type(self).retry_started.set()
            try:
                await type(self).release.wait()
            except asyncio.CancelledError:
                return SubagentResult(
                    self._task.task_id,
                    self._task.agent_type,
                    self.label,
                    False,
                    "completed turn before retry cancellation",
                    error="cancelled",
                    partial=True,
                )
            raise AssertionError("retry test worker should be cancelled")

    _RetryWorker.calls = 0
    _RetryWorker.retry_started = asyncio.Event()
    _RetryWorker.release = asyncio.Event()
    monkeypatch.setattr(pool_module, "SubagentWorker", _RetryWorker)
    pool = _pool([SubagentTask("worker", "researcher", "task")], max_concurrent=1)
    await pool.run()

    retry = asyncio.create_task(pool.retry_worker("worker"))
    await asyncio.wait_for(_RetryWorker.retry_started.wait(), timeout=1)
    retry.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retry

    lifecycle = pool.lifecycle("worker")
    assert lifecycle is not None
    assert lifecycle.attempt == 2
    assert lifecycle.phase == "cancelled"
    assert lifecycle.partial_result is not None
    assert lifecycle.partial_result.text == "completed turn before retry cancellation"
    assert active_pool(pool.pool_id) is None
