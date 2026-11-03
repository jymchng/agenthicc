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

    async def _undelivered_message_count(self) -> int:
        return 0

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


# ── SUBAGENT-08 criterion 5 (SUBAGENT-10 criterion 25) ────────────────────────


class _WriteThenFinishTransport:
    """Each worker asks to write its own file once, then finishes."""

    async def complete(self, messages: list[object], **kwargs: object) -> object:
        from lauren_ai._transport import Completion, TokenUsage, ToolCall

        has_result = any(
            isinstance(message, dict)
            and isinstance(message.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in message["content"]
            )
            for message in messages
        )
        usage = TokenUsage(input_tokens=1, output_tokens=1)
        if has_result:
            return Completion(
                id="final",
                model="mock-model",
                content="done",
                tool_calls=[],
                stop_reason="end_turn",
                usage=usage,
            )
        task = next(
            str(message.get("content"))
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        )
        return Completion(
            id=f"write-{task}",
            model="mock-model",
            content="",
            tool_calls=[
                ToolCall(
                    tool_use_id=f"call-{task}",
                    name="write_note",
                    input={"path": f"{task}.md", "content": "x"},
                )
            ],
            stop_reason="tool_use",
            usage=usage,
        )


async def _wait_until(condition: object, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():  # type: ignore[operator]
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


async def test_stopping_a_worker_waiting_on_approval_withdraws_only_its_prompt() -> None:
    """Abort a worker blocked on an approval: its prompt goes, its sibling's stays answerable."""
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._tools import tool

    from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.tools.capabilities import tool_write
    from agenthicc.tui.conversation_store import AppState

    executed: list[str] = []

    @tool_write
    @tool()
    async def write_note(path: str, content: str) -> dict[str, object]:
        """Write a note."""
        executed.append(path)
        return {"ok": True, "path": path}

    app_state = AppState.create()  # Safe mode: every write needs approval
    approvals = ApprovalService(app_state)
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="writer",
            allowed_tools=frozenset({"write_note"}),
            max_turns=4,
            system_prompt="Write the note.",
        )
    )
    pool = SubagentPool(
        [SubagentTask("alpha", "writer", "alpha"), SubagentTask("beta", "writer", "beta")],
        parent_runner=AgentRunnerBase(transport=_WriteThenFinishTransport()),
        parent_model="mock-model",
        all_tools=[write_note],
        app_state=app_state,
        approval_svc=approvals,
        registry=registry,
        max_concurrent=2,
    )
    running = asyncio.create_task(pool.run())
    try:
        # Both workers are blocked: one on the visible prompt, one queued behind it.
        await _wait_until(
            lambda: all(
                (lifecycle := pool.lifecycle(worker)) is not None and lifecycle.phase == "waiting"
                for worker in ("alpha", "beta")
            )
        )
        await _wait_until(lambda: app_state.pending_approval() is not None)
        first = app_state.pending_approval()
        assert first is not None and first.requester is not None
        stopped = first.requester.worker_id
        sibling = "beta" if stopped == "alpha" else "alpha"

        pool.request_stop(stopped, mode="abort", source="user", reason="user_requested")

        # The stopped worker's prompt is withdrawn and the sibling's takes its place.
        await _wait_until(
            lambda: (pending := app_state.pending_approval()) is not None and pending is not first
        )
        replacement = app_state.pending_approval()
        assert replacement is not None and replacement.requester is not None
        assert replacement.requester.worker_id == sibling
        approvals.respond(allowed=True)

        result = await asyncio.wait_for(running, timeout=10)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    assert executed == [f"{sibling}.md"]
    stopped_lifecycle = pool.lifecycle(stopped)
    assert stopped_lifecycle is not None and stopped_lifecycle.phase == "cancelled"
    assert app_state.pending_approval() is None
    assert result.succeeded == 1 and result.total == 2


# ── SUBAGENT-08 criterion 8 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_refuses_live_and_succeeded_workers_without_changing_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``retry_worker`` refuses a live worker and a succeeded one, changing nothing."""
    _prepare_workers(monkeypatch, ("live", "done"))
    pool = _pool(
        [SubagentTask("live", "researcher", "a"), SubagentTask("done", "researcher", "b")],
        max_concurrent=2,
    )
    run = asyncio.create_task(pool.run())
    try:
        await asyncio.wait_for(_ControlledWorker.started["live"].wait(), timeout=1)
        await asyncio.wait_for(_ControlledWorker.started["done"].wait(), timeout=1)
        _ControlledWorker.release["done"].set()
        await _wait_until(
            lambda: (
                (lifecycle := pool.lifecycle("done")) is not None and lifecycle.phase == "completed"
            )
        )

        live_before = pool.lifecycle("live")
        done_before = pool.lifecycle("done")
        # A live worker in an active pool cannot be retried.
        assert await pool.retry_worker("live") is None
        assert pool.lifecycle("live") == live_before
        # Nor can a worker that already succeeded, even while its sibling runs.
        assert await pool.retry_worker("done") is None
        assert pool.lifecycle("done") == done_before
    finally:
        _ControlledWorker.release["live"].set()
        result = await asyncio.wait_for(run, timeout=5)

    assert result.succeeded == 2
    done_after = pool.lifecycle("done")
    assert done_after is not None
    # After the pool finished, the succeeded worker is still refused.
    assert await pool.retry_worker("done") is None
    assert pool.lifecycle("done") == done_after
    assert done_after.attempt == 1
