"""Coverage for the production SubagentPool orchestration path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from lauren_ai._transport import Completion, TokenUsage

from agenthicc.kernel import Event
from agenthicc.subagents.pool import AggregatedResult, SubagentPool, SubagentResult, SubagentTask
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.tui.conversation_store import AppState

pytestmark = pytest.mark.unit


class _NullUsageTransport:
    """OpenAI-compatible endpoint that omits usage values."""

    async def complete(self, messages: list[object], **kwargs: object) -> Completion:
        return Completion(
            id="completion",
            model="model",
            content="done",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=None, output_tokens=None),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_pool_normalizes_nullable_provider_usage_for_large_batch() -> None:
    """Ten workers must not fail on ``int + None`` usage arithmetic."""
    from agenthicc.subagents.pool import SubagentPool, SubagentTask

    pool = SubagentPool(
        [SubagentTask(f"task-{i}", "researcher", "research") for i in range(10)],
        parent_runner=SimpleNamespace(_transport=_NullUsageTransport()),
        parent_model="model",
        all_tools=[],
        max_concurrent=4,
        registry=DEFAULT_REGISTRY,
    )

    result = await pool.run()

    assert result.total == 10
    assert result.succeeded == 10
    assert result.failed == 0


@pytest.mark.asyncio
async def test_real_pool_run_emits_state_and_normalizes_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenthicc.subagents.pool as pool_module

    class FakeWorker:
        def __init__(self, task: SubagentTask, spec: object, index: int, **kwargs: object) -> None:
            self._task = task
            self.label = f"{task.agent_type} #{index}"

        async def run(self) -> SubagentResult:
            return SubagentResult(
                self._task.task_id,
                self._task.agent_type,
                self.label,
                self._task.task_id != "fail",
                "result" if self._task.task_id != "fail" else "",
                "bad" if self._task.task_id == "fail" else None,
            )

    monkeypatch.setattr(pool_module, "SubagentWorker", FakeWorker)
    emitted: list[Event] = []
    drained = 0

    async def emit(event: Event) -> None:
        emitted.append(event)

    async def drain() -> None:
        nonlocal drained
        drained += 1

    app = AppState.create()
    tasks = [
        SubagentTask("one", "explorer", "find files"),
        SubagentTask("fail", "tester", "run tests"),
        SubagentTask("unknown", "not-real", "unknown"),
    ]
    pool = SubagentPool(
        tasks,
        parent_runner=SimpleNamespace(_transport=None),
        parent_model="model",
        all_tools=[],
        max_concurrent=2,
        processor=SimpleNamespace(emit=emit, drain=drain),
        conv_store=app.conversation,
        registry=DEFAULT_REGISTRY,
    )
    result = await pool.run()
    assert result.total == 3
    assert result.succeeded == 1
    assert result.failed == 2
    assert drained == 1

    assert [event.event_type for event in emitted].count("SubagentPoolStarted") == 1
    assert [event.event_type for event in emitted].count("SubagentStarted") == 3
    assert [event.event_type for event in emitted].count("SubagentCompleted") == 1
    assert [event.event_type for event in emitted].count("SubagentFailed") == 2
    assert [event.event_type for event in emitted].count("SubagentPoolCompleted") == 1

    pool_started = next(event for event in emitted if event.event_type == "SubagentPoolStarted")
    assert [
        {key: worker[key] for key in ("worker_id", "type", "task")}
        for worker in pool_started.payload["workers"]
    ] == [
        {"worker_id": "one", "type": "explorer", "task": "find files"},
        {"worker_id": "fail", "type": "tester", "task": "run tests"},
        {"worker_id": "unknown", "type": "not-real", "task": "unknown"},
    ]
    assert pool_started.payload["max_concurrent"] == 2

    starts = {
        event.payload["worker_id"]: event.payload
        for event in emitted
        if event.event_type == "SubagentStarted"
    }
    assert starts["one"]["type"] == "explorer"
    assert starts["one"]["task"] == "find files"
    assert starts["fail"]["label"] == "tester #1"

    completions = {
        event.payload["worker_id"]: event.payload
        for event in emitted
        if event.event_type in {"SubagentCompleted", "SubagentFailed"}
    }
    assert completions["one"]["text"] == "result"
    assert completions["one"]["duration_ms"] >= 0
    assert completions["fail"]["error"] == "bad"
    assert completions["unknown"]["error"] == "unknown subagent type: 'not-real'"

    pool_completed = next(event for event in emitted if event.event_type == "SubagentPoolCompleted")
    assert pool_completed.payload["pool_id"] == pool_started.payload["pool_id"]
    assert pool_completed.payload["total"] == 3
    assert pool_completed.payload["succeeded"] == 1
    assert pool_completed.payload["failed"] == 2
    assert pool_completed.payload["text"] == result.text
    assert pool_completed.payload["budget_exceeded_reason"] == ""
    # The finished pool stays published for the panel, with nothing running.
    state = app.conversation.subagent_pool_state()
    assert state is not None and state.finished_at is not None and not state.active
    assert [worker.status for worker in state.workers] == ["done", "failed", "failed"]


@pytest.mark.asyncio
async def test_run_pool_convenience_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    import agenthicc.subagents.pool as pool_module

    captured: dict[str, object] = {}

    def fake_init(self: object, *args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    async def fake_run(self: object) -> AggregatedResult:
        return AggregatedResult("pool", 0, 0, 0, "")

    monkeypatch.setattr(pool_module.SubagentPool, "__init__", fake_init)
    monkeypatch.setattr(pool_module.SubagentPool, "run", fake_run)
    from agenthicc.config import ExecutionSettings

    settings = ExecutionSettings()
    result = await pool_module.run_pool(
        [],
        SimpleNamespace(_transport=None),
        "model",
        [],
        registry=DEFAULT_REGISTRY,
        provider="anthropic",
        default_model="worker-model",
        default_effort="high",
        default_context=12_000,
        call_model="call-model",
        call_effort="low",
        execution_settings=settings,
        allowed_models=frozenset({"worker-model"}),
    )
    assert result.pool_id == "pool"
    assert captured["provider"] == "anthropic"
    assert captured["default_model"] == "worker-model"
    assert captured["default_effort"] == "high"
    assert captured["default_context"] == 12_000
    assert captured["call_model"] == "call-model"
    assert captured["call_effort"] == "low"
    assert captured["execution_settings"] is settings
    assert captured["allowed_models"] == frozenset({"worker-model"})


# ── SUBAGENT-09 criterion 2: no sink invents a state ────────────────────────


def test_the_lifecycle_table_maps_every_kind_once() -> None:
    from agenthicc.subagents.events import LIFECYCLE_TABLE, SubagentEventKind

    assert [row.kind for row in LIFECYCLE_TABLE] == list(SubagentEventKind)
    scroll = [row.scroll_kind for row in LIFECYCLE_TABLE if row.scroll_kind is not None]
    kernel = [row.kernel_type for row in LIFECYCLE_TABLE if row.kernel_type is not None]
    assert len(scroll) == len(set(scroll)) and len(kernel) == len(set(kernel))


@pytest.mark.asyncio
async def test_every_kind_a_real_pool_emits_is_in_the_lifecycle_table(tmp_path) -> None:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._tools import tool
    from lauren_ai._transport._mock import MockTransport

    from agenthicc.subagents.events import LIFECYCLE_TABLE, TRANSCRIPT_MARKER_KINDS
    from agenthicc.subagents.transcripts import list_pools, read_transcript
    from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec

    @tool()
    async def lookup(topic: str) -> str:
        """Look a topic up."""
        return f"fact:{topic}"

    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="researcher",
            allowed_tools=frozenset({"lookup"}),
            max_turns=3,
            system_prompt="Use lookup.",
            max_turn_time_s=10.0,
        )
    )
    transport = MockTransport()
    transport.queue_tool_use("lookup", {"topic": "a"}, tool_use_id="l-1", content="Looking.")
    transport.queue_response(
        Completion(
            id="f",
            model="m",
            content="Answer.",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
    )
    kernel: list[Event] = []

    async def emit(event: Event) -> None:
        kernel.append(event)

    async def drain() -> None:
        return None

    app = AppState.create()
    scroll: list[object] = []
    app.conversation.on_event(scroll.append)
    pool = SubagentPool(
        [SubagentTask("w", "researcher", "find a"), SubagentTask("x", "missing-type", "b")],
        parent_runner=AgentRunnerBase(transport=transport),
        parent_model="mock-model",
        all_tools=[lookup],
        max_concurrent=1,
        processor=SimpleNamespace(emit=emit, drain=drain),
        conv_store=app.conversation,
        registry=registry,
        transcript_root=tmp_path / "subagents",
    )
    await pool.run()

    kernel_types = {row.kernel_type for row in LIFECYCLE_TABLE}
    scroll_kinds = {row.scroll_kind for row in LIFECYCLE_TABLE}
    transcript_kinds = {kind for row in LIFECYCLE_TABLE for kind in row.transcript_kinds}
    statuses = {status for row in LIFECYCLE_TABLE for status in row.panel_statuses}
    emitted_scroll = {
        event.kind  # type: ignore[attr-defined]
        for event in scroll
        if event.kind.startswith("subagent_")  # type: ignore[attr-defined]
        and event.kind != "subagent_pool_result"  # type: ignore[attr-defined]
    }
    (record,) = list_pools(tmp_path / "subagents")
    emitted_transcript = {
        event.kind
        for worker in record.workers
        for event in read_transcript(record.transcript_path(worker.worker_id))
    }

    assert {event.event_type for event in kernel} <= kernel_types
    assert emitted_scroll <= scroll_kinds
    assert emitted_transcript <= transcript_kinds | TRANSCRIPT_MARKER_KINDS
    assert {worker.status for worker in pool.state.workers} <= statuses
    # The run exercised the main rows of each sink.
    assert {"SubagentPoolStarted", "SubagentStarted", "SubagentCompleted", "SubagentFailed"} <= {
        event.event_type for event in kernel
    }
    assert {"subagent_worker_done", "subagent_worker_failed", "subagent_pool_done"} <= (
        emitted_scroll
    )
    assert {"prompt", "tool_started", "tool_finished", "finished"} <= emitted_transcript


# ── SUBAGENT-11 criterion 3: backgrounding changes delivery, not the pool ───


async def _run_pool_both_ways(tmp_path, *, background: bool) -> tuple[str, list[str], list[str]]:
    import asyncio
    import json

    from lauren_ai._agents._runner import AgentRunnerBase

    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.runners.agent_turn_context import bind_background_task_owner
    from agenthicc.subagents.pool import active_pool
    from agenthicc.subagents.tool import make_spawn_subagents_tool
    from agenthicc.supervised_tasks import SupervisedTasks

    release = asyncio.Event()

    class _Transport:
        async def complete(self, messages: list[object], **kwargs: object) -> object:
            await release.wait()
            first = messages[0] if messages else {}
            task = first.get("content", "") if isinstance(first, dict) else ""
            return Completion(
                id="f",
                model="m",
                content=f"answer for {task}",
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )

    journal = ConversationJournal(tmp_path / f"journal-{background}.jsonl")
    app = AppState.create()
    scroll: list[str] = []
    app.conversation.on_event(lambda event: scroll.append(event.kind))
    supervisor = SupervisedTasks("parity")
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=_Transport()),
        "mock-model",
        [],
        registry=DEFAULT_REGISTRY,
        conversation_id="conv",
        parent_run_id="turn-1",
        conversation_journal=journal,
        conv_store=app.conversation,
    )
    tasks = [{"type": "explorer", "task": "alpha"}, {"type": "explorer", "task": "beta"}]
    try:
        with bind_background_task_owner(supervisor, interactive=True):
            call = asyncio.create_task(spawn(tasks=tasks, max_concurrent=2, timeout_s=30))  # type: ignore[operator]
            while not any(kind == "subagent_pool_started" for kind in scroll):
                await asyncio.sleep(0.01)
            if background:
                pool = next(
                    pool for pool_id in list(pool_module_active()) if (pool := active_pool(pool_id))
                )
                assert pool.request_background()
            else:
                release.set()
            receipt = await call
        release.set()
        if background:
            assert receipt["status"] == "backgrounded"
            assert await supervisor.wait(timeout=5)
            tools = {
                getattr(t, "__name__", ""): t
                for t in spawn.__agenthicc_subagent_communication_tools__  # type: ignore[attr-defined]
            }
            text = str((await tools["collect_subagent_results"](receipt["pool_id"]))["results"])
        else:
            text = str(receipt["results"])
    finally:
        release.set()
        await supervisor.aclose(timeout=0.1)
        journal.close()
    kinds = [
        str(json.loads(line).get("kind"))
        for line in (tmp_path / f"journal-{background}.jsonl").read_text().splitlines()
        if str(json.loads(line).get("kind", "")).startswith("subagent_")
    ]
    return text, kinds, [kind for kind in scroll if kind.startswith("subagent_")]


def pool_module_active() -> list[str]:
    import agenthicc.subagents.pool as pool_module

    return list(pool_module._ACTIVE_POOLS)


@pytest.mark.asyncio
async def test_a_backgrounded_pool_produces_the_same_aggregate_events_and_records(
    tmp_path,
) -> None:
    fg_text, fg_journal, fg_scroll = await _run_pool_both_ways(tmp_path, background=False)
    bg_text, bg_journal, bg_scroll = await _run_pool_both_ways(tmp_path, background=True)
    assert bg_text == fg_text and "answer for alpha" in fg_text
    # Both paths keep the records and kinds they had before SUBAGENT-11. The
    # background path adds its receipt and, having already returned to the
    # parent, writes no resume cache (subagent_pool_result) for the turn.
    assert fg_journal == [
        "subagent_pool_started",
        "subagent_worker_result",
        "subagent_worker_result",
        "subagent_pool_finished",
        "subagent_pool_result",
    ]
    assert bg_journal == [
        "subagent_pool_started",
        "subagent_pool_backgrounded",
        "subagent_worker_result",
        "subagent_worker_result",
        "subagent_pool_finished",
    ]
    pool_kinds = [
        "subagent_pool_started",
        "subagent_worker_done",
        "subagent_worker_done",
        "subagent_pool_done",
    ]
    assert fg_scroll == [*pool_kinds, "subagent_pool_result"]
    assert bg_scroll == pool_kinds
