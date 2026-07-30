"""End-to-end provider-stream coverage for PRD-157 accounting."""

from __future__ import annotations

import pytest
from lauren_ai._agents import agent
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._memory import ShortTermMemory
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.runners.usage_ledger import UsageQuality, UsageRunTracker

pytestmark = pytest.mark.e2e


@agent(model="mock-model", system="You are an E2E test agent.")
class _TestAgent: ...


async def _run_with_ledger(ledger, mock: MockTransport, run_id: str) -> UsageRunTracker:
    runner = AgentRunnerBase(transport=mock)
    tracker = UsageRunTracker(
        ledger,
        run_id=run_id,
        provider="mock",
        model="mock-model",
        agent_id="test-agent",
        agent_name="e2e",
    )
    tracker.ensure_call()
    stream = await runner.run_stream(
        _TestAgent(),
        "Respond once",
        conversation_id="session-e2e",
        run_id=run_id,
        memory=ShortTermMemory(max_tokens=8_000),
        event_sinks=[tracker.sink],
    )
    async for chunk in stream:
        if chunk.usage is not None:
            tracker.observe_chunk(chunk.usage)
    tracker.finalize("completed")
    return tracker


async def test_real_mock_provider_stream_is_counted_once() -> None:
    from agenthicc.runners.usage_ledger import UsageLedger

    ledger = UsageLedger("session-e2e", conversation_id="session-e2e")
    mock = MockTransport()
    mock.queue_response(
        Completion(
            id="completion-1",
            model="mock-model",
            content="done",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=14, output_tokens=5),
        )
    )

    await _run_with_ledger(ledger, mock, "direct-turn")

    snapshot = ledger.snapshot()
    assert snapshot.input_tokens == 14
    assert snapshot.output_tokens == 5
    assert snapshot.usage_status == UsageQuality.COMPLETE
    assert len(ledger.records()) == 1
    assert ledger.records()[0].run_id == "direct-turn"


async def test_provider_without_usage_is_explicitly_partial_not_zero() -> None:
    from agenthicc.runners.usage_ledger import UsageLedger

    ledger = UsageLedger("session-no-usage")
    mock = MockTransport()
    mock.queue_response(
        Completion(
            id="completion-unknown",
            model="mock-model",
            content="done",
            tool_calls=[],
            stop_reason="end_turn",
            usage=None,
        )
    )

    await _run_with_ledger(ledger, mock, "workflow-phase")

    snapshot = ledger.snapshot()
    assert snapshot.input_tokens == 0
    assert snapshot.output_tokens == 0
    assert snapshot.usage_status == UsageQuality.PARTIAL
    assert snapshot.unavailable_calls == 1
    assert ledger.records()[0].input_tokens is None
    assert ledger.records()[0].output_tokens is None
