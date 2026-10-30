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
    assert pool_started.payload["tasks"] == [
        {"task_id": "one", "type": "explorer", "description": "find files"},
        {"task_id": "fail", "type": "tester", "description": "run tests"},
        {"task_id": "unknown", "type": "not-real", "description": "unknown"},
    ]
    assert pool_started.payload["max_concurrent"] == 2

    starts = {
        event.payload["task_id"]: event.payload
        for event in emitted
        if event.event_type == "SubagentStarted"
    }
    assert starts["one"]["type"] == "explorer"
    assert starts["one"]["task"] == "find files"
    assert starts["fail"]["label"] == "tester #1"

    completions = {
        event.payload["task_id"]: event.payload
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
