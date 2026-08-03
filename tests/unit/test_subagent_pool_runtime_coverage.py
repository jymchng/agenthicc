"""Coverage for the production SubagentPool orchestration path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from lauren_ai._transport import Completion, TokenUsage

from agenthicc.subagents.pool import AggregatedResult, SubagentResult, SubagentTask, SubagentPool
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
    emitted: list[object] = []

    async def emit(event: object) -> None:
        emitted.append(event)

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
        processor=SimpleNamespace(emit=emit),
        conv_store=app.conversation,
        registry=DEFAULT_REGISTRY,
    )
    result = await pool.run()
    assert result.total == 3
    assert result.succeeded == 1
    assert result.failed == 2
    assert emitted
    assert app.conversation.subagent_pool_state() is None


@pytest.mark.asyncio
async def test_run_pool_convenience_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    import agenthicc.subagents.pool as pool_module

    async def fake_run(self: object) -> AggregatedResult:
        return AggregatedResult("pool", 0, 0, 0, "")

    monkeypatch.setattr(pool_module.SubagentPool, "run", fake_run)
    result = await pool_module.run_pool(
        [], SimpleNamespace(_transport=None), "model", [], registry=DEFAULT_REGISTRY
    )
    assert result.pool_id == "pool"
