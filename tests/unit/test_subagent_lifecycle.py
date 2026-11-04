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


@pytest.mark.asyncio
async def test_a_retried_worker_keeps_its_pool_scope_model_and_policy() -> None:
    """A retry is built by the same constructor as the first attempt.

    The retry constructor used to drop the pool id, model routing, depth, event
    sinks and the type's own policy, so a retried worker's usage fell outside
    its pool's accounting and it could run on a different model.
    """
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._transport import Completion, TokenUsage

    from agenthicc.runners.usage_ledger import UsageLedger
    from agenthicc.subagents.budget import worker_usage
    from agenthicc.subagents.policy import SubagentExecutionPolicy
    from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec
    from agenthicc.tui.conversation_store import AppState

    class _FailFirstTransport:
        calls: list[str] = []

        async def complete(self, messages: list[object], **kwargs: object) -> object:
            self.calls.append(str(kwargs.get("model", "")))
            if len(self.calls) == 1:
                raise RuntimeError("transient provider failure")
            return Completion(
                id="final",
                model="mock-model",
                content="second attempt",
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=40, output_tokens=2),
            )

    transport = _FailFirstTransport()
    transport.calls = []
    ledger = UsageLedger("session")
    read_only = SubagentExecutionPolicy(
        mode_name="Plan",
        badge="",
        color="",
        description="",
        system_prompt_suffix="",
        blocked_capabilities=frozenset({"write"}),
        approval_required=frozenset(),
        visible_tool_names=frozenset(),
    )
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="researcher",
            allowed_tools=frozenset(),
            max_turns=2,
            system_prompt="Research.",
            model="worker-model",
            policy=read_only,
        )
    )
    pool = SubagentPool(
        [SubagentTask("task-0", "researcher", "research")],
        parent_runner=AgentRunnerBase(transport=transport),
        parent_model="parent-model",
        all_tools=[],
        app_state=AppState.create(),
        registry=registry,
        usage_ledger=ledger,
        parent_run_id="turn-1",
        retry_config=None,
    )

    first = await pool.run()
    assert first.failed == 1
    retried = await pool.retry_worker("task-0")

    assert retried is not None and retried.ok is True
    worker = pool._workers["task-0"]
    assert worker._pool_id == pool.pool_id
    assert worker._model_id == "worker-model"
    assert worker._policy is not None and "write" in worker._policy.blocked_capabilities
    assert set(transport.calls) == {"worker-model"}
    assert worker_usage(ledger, pool.pool_id, "task-0").total_tokens == 42


# ── SUBAGENT-11 goal 7: the parent model's stop, retry and list ───────────────


def _control_tools(spawn: object) -> dict[str, object]:
    tools = spawn.__agenthicc_subagent_communication_tools__
    return {getattr(item, "__name__", ""): item for item in tools}


def _registry_for(tool_names: frozenset[str]) -> object:
    from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec

    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="researcher",
            allowed_tools=tool_names,
            max_turns=4,
            system_prompt="Research.",
        )
    )
    return registry


@pytest.mark.asyncio
async def test_the_parent_model_stops_one_worker_of_a_running_pool(tmp_path: Path) -> None:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._tools import tool
    from lauren_ai._transport import Completion, TokenUsage, ToolCall

    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.runners.agent_turn_context import bind_background_task_owner
    from agenthicc.subagents.tool import make_spawn_subagents_tool
    from agenthicc.supervised_tasks import SupervisedTasks

    release = asyncio.Event()

    @tool()
    async def wait_for_release(topic: str) -> str:
        """Wait until the test releases the worker."""
        await release.wait()
        return topic

    class _Transport:
        async def complete(self, messages: list[object], **kwargs: object) -> object:
            done = any(
                isinstance(m, dict)
                and isinstance(m.get("content"), list)
                and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]
                )
                for m in messages
            )
            usage = TokenUsage(input_tokens=1, output_tokens=1)
            if done:
                return Completion(
                    id="f",
                    model="m",
                    content="done",
                    tool_calls=[],
                    stop_reason="end_turn",
                    usage=usage,
                )
            return Completion(
                id="t",
                model="m",
                content="",
                stop_reason="tool_use",
                usage=usage,
                tool_calls=[
                    ToolCall(tool_use_id="w", name="wait_for_release", input={"topic": "x"})
                ],
            )

    journal = ConversationJournal(tmp_path / "journal.jsonl")
    supervisor = SupervisedTasks("stop test")
    store = ConversationStore()
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=_Transport()),
        "mock-model",
        [wait_for_release],
        registry=_registry_for(frozenset({"wait_for_release"})),  # type: ignore[arg-type]
        conversation_id="conv",
        parent_run_id="turn-1",
        conversation_journal=journal,
        conv_store=store,
    )
    tools = _control_tools(spawn)
    try:
        with bind_background_task_owner(supervisor):
            receipt = await spawn(  # type: ignore[operator]
                tasks=[
                    {"type": "researcher", "task": "first"},
                    {"type": "researcher", "task": "second"},
                ],
                max_concurrent=2,
                timeout_s=30,
                background=True,
            )
        pool_id = receipt["pool_id"]
        await asyncio.sleep(0.05)

        refused = await tools["retry_subagent"](pool_id, "task-0")  # type: ignore[operator]
        assert refused["ok"] is False and "still running" in refused["error"]

        first = await tools["stop_subagents"](pool_id, "task-0")  # type: ignore[operator]
        again = await tools["stop_subagents"](pool_id, "task-0")  # type: ignore[operator]
        assert first["ok"] is True and first["workers"][0]["mode"] == "abort"
        assert again["workers"][0]["request_id"] == first["workers"][0]["request_id"]

        listed = await tools["list_subagent_pools"]()  # type: ignore[operator]
        row = next(item for item in listed["pools"] if item["pool_id"] == pool_id)
        assert row["status"] == "running" and row["background"] is True

        release.set()
        assert await supervisor.wait(timeout=5)
        from agenthicc.subagents.communication import registry_for_session

        handle = registry_for_session("conv", store).get(pool_id)
        assert handle is not None and handle.pool is not None
        pool = handle.pool
        stopped = pool.lifecycle("task-0")
        sibling = pool.lifecycle("task-1")
        assert stopped.phase == "cancelled" and stopped.stop_request.source == "parent_model"
        assert sibling.phase == "completed"
        late = await tools["stop_subagents"](pool_id, "task-1")  # type: ignore[operator]
        assert late["ok"] is False and "not running" in late["error"]
    finally:
        release.set()
        await supervisor.aclose(timeout=0.1)
        journal.close()


@pytest.mark.asyncio
async def test_an_older_pool_stays_retryable_after_a_newer_one_finishes(tmp_path: Path) -> None:
    """Criterion 15: retryability no longer depends on which pool the panel shows."""
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._transport import Completion, TokenUsage

    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.subagents.tool import make_spawn_subagents_tool

    class _Transport:
        """Fails every call for task "a" until the provider recovers."""

        def __init__(self) -> None:
            self.calls = 0
            self.provider_down = True

        async def complete(self, messages: list[object], **kwargs: object) -> object:
            self.calls += 1
            last = messages[-1]
            if self.provider_down and isinstance(last, dict) and last.get("content") == "a":
                raise RuntimeError("provider failure")
            return Completion(
                id=f"c{self.calls}",
                model="m",
                content=f"answer {self.calls}",
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )

    transport = _Transport()
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    store = ConversationStore()
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        [],
        registry=_registry_for(frozenset()),  # type: ignore[arg-type]
        conversation_id="conv",
        parent_run_id="turn-1",
        conversation_journal=journal,
        conv_store=store,
    )
    tools = _control_tools(spawn)
    try:
        first = await spawn(tasks=[{"type": "researcher", "task": "a"}], timeout_s=30)  # type: ignore[operator]
        second = await spawn(tasks=[{"type": "researcher", "task": "b"}], timeout_s=30)  # type: ignore[operator]
        assert first["failed"] == 1 and second["ok"] is True

        transport.provider_down = False
        retried = await tools["retry_subagent"](first["pool_id"], "task-0")  # type: ignore[operator]
        assert retried["ok"] is True and retried["attempt"] == 2
        refusal = await tools["retry_subagent"](second["pool_id"], "task-0")  # type: ignore[operator]
        assert refusal["ok"] is False and "succeeded" in refusal["error"]
        listed = await tools["list_subagent_pools"]()  # type: ignore[operator]
        assert {first["pool_id"], second["pool_id"]} <= {row["pool_id"] for row in listed["pools"]}
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_a_pool_that_fails_at_once_reports_failure_not_a_clarification(
    tmp_path: Path,
) -> None:
    """Closing the broker wakes the question waiter; that is not a question."""
    from lauren_ai._agents._runner import AgentRunnerBase

    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.subagents.tool import make_spawn_subagents_tool

    class _DownTransport:
        async def complete(self, messages: list[object], **kwargs: object) -> object:
            raise RuntimeError("provider failure")

    journal = ConversationJournal(tmp_path / "journal.jsonl")
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=_DownTransport()),
        "mock-model",
        [],
        registry=_registry_for(frozenset()),  # type: ignore[arg-type]
        conversation_id="conv",
        parent_run_id="turn-1",
        conversation_journal=journal,
        conv_store=ConversationStore(),
    )
    try:
        for _ in range(3):
            result = await spawn(tasks=[{"type": "researcher", "task": "a"}], timeout_s=30)  # type: ignore[operator]
            assert result["status"] != "awaiting_clarification"
            assert result["failed"] == 1 and result["ok"] is False
    finally:
        journal.close()
