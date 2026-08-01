"""Regression coverage for the agenthicc-owned context compaction fallback."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from lauren_ai._memory import ShortTermMemory
from lauren_ai._transport import CompletionChunk

from agenthicc.runners.agent_turn import (
    AgentTurnRunner,
    _is_context_overflow_error,
)
from agenthicc.runners.agent_turn_context import AgentTurnContext


class _SmallExecutionConfig:
    auto_compact = True
    max_output_tokens = 100
    transport_max_retries = 0
    transport_retry_base_delay_s = 0.0
    transport_retry_max_total_s = 0.0

    @staticmethod
    def effective_context_window() -> int:
        return 1_000

    @staticmethod
    def effective_usable_budget() -> int:
        return 800


class _SummaryTransport:
    def __init__(self, summary: str = "preserved history summary") -> None:
        self.summary = summary
        self.calls: list[dict[str, object]] = []

    async def complete(self, messages, **kwargs):  # noqa: ANN001
        self.calls.append({"messages": messages, **kwargs})
        return SimpleNamespace(content=self.summary)


def _make_context(memory: ShortTermMemory) -> AgentTurnContext:
    return AgentTurnContext(
        text="continue",
        runner=SimpleNamespace(_transport=MagicMock(), _signals=None),  # type: ignore[arg-type]
        processor=MagicMock(),
        session_memory=memory,
        conversation_id="session-1",
        max_agent_turns=1,
        conv_store=MagicMock(),
        exec_cfg=_SmallExecutionConfig(),  # type: ignore[arg-type]
        skills={},
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="default",
        completed_turns=0,
        approval_svc=None,
        output_collector=[],
        system_prompt_suffix="",
    )


@pytest.mark.unit
def test_context_length_provider_error_is_detected_through_sdk_wrapper() -> None:
    inner = Exception(
        "This model's maximum context length is 1048576 tokens. "
        "However, you requested 1049457 tokens."
    )
    inner.status_code = 400  # type: ignore[attr-defined]
    outer = Exception("TransportError")
    outer.__cause__ = inner  # type: ignore[attr-defined]

    assert _is_context_overflow_error(outer) is True


@pytest.mark.unit
async def test_old_lauren_fallback_compacts_at_the_usable_budget() -> None:
    memory = ShortTermMemory(max_tokens=800)
    memory.add_user("h" * 2_800)  # ~700 tokens; threshold is 80% of 800
    transport = _SummaryTransport()
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"
    runner._intent_id = "turn-1"
    active_runner = SimpleNamespace(_transport=transport)

    compacted = await runner._auto_compact_if_needed(
        active_runner,
        "continue",
        max_input_tokens=800,  # type: ignore[arg-type]
    )

    assert compacted is True
    # The small synthetic budget deliberately exercises map-reduce: the
    # transcript is split into bounded chunks and then reduced.
    assert len(transport.calls) >= 1
    assert memory._messages[0]["content"].startswith("[COMPACT SUMMARY]")
    assert "preserved history summary" in memory._messages[0]["content"]


@pytest.mark.unit
async def test_empty_summary_uses_a_bounded_local_fallback() -> None:
    memory = ShortTermMemory(max_tokens=800)
    memory.add_user("h" * 2_800)
    transport = _SummaryTransport(summary="")
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"
    runner._intent_id = "turn-1"

    compacted = await runner._auto_compact_if_needed(
        SimpleNamespace(_transport=transport),
        "continue",
        max_input_tokens=800,  # type: ignore[arg-type]
    )

    assert compacted is True
    assert memory.token_estimate < 700
    assert "COMPACT FALLBACK" in memory._messages[0]["content"]
    ctx.conv_store.append_event.assert_any_call(  # type: ignore[union-attr]
        "system", {"text": "⎋ Compaction fallback: retained recent history"}
    )


@pytest.mark.unit
async def test_context_overflow_compacts_then_retries_without_duplicate_user_message() -> None:
    memory = ShortTermMemory(max_tokens=800)
    memory.add_user("h" * 1_800)  # ~450 tokens; below the proactive threshold
    # Reproduce the reported endpoint behaviour: the compaction completion
    # succeeds at the transport layer but contains no assistant text.
    transport = _SummaryTransport(summary="")
    overflow = Exception(
        "TransportError: This model's maximum context length is 1048576 tokens; "
        "you requested too many tokens"
    )
    overflow.status_code = 400  # type: ignore[attr-defined]

    async def successful_stream():
        yield CompletionChunk(delta="continued", stop_reason="end_turn")

    attempt = 0

    async def run_stream(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal attempt
        attempt += 1
        # Lauren's real run_stream seeds the user message before the provider
        # call.  Mirror that mutation so the rollback assertion is meaningful.
        memory.add_user(args[1])
        if attempt == 1:
            raise overflow
        return successful_stream()

    active_runner = SimpleNamespace(
        _transport=transport,
        run_stream=AsyncMock(side_effect=run_stream),
    )
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"
    runner._intent_id = "turn-1"

    await runner._stream(object(), "continue", active_runner)  # type: ignore[arg-type]

    assert active_runner.run_stream.await_count == 2
    assert len(transport.calls) == 1
    assert "COMPACT FALLBACK" in memory._messages[0]["content"]
    assert (
        sum(
            message.get("content") == "continue"
            for message in memory._messages
            if isinstance(message, dict)
        )
        == 1
    )
    assert any(
        call.args[0] is not None and call.args[1] == "continue"
        for call in active_runner.run_stream.await_args_list
    )
    ctx.conv_store.close_turn.assert_called_once()  # type: ignore[union-attr]
