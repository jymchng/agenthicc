"""Regression coverage for the agenthicc-owned context compaction fallback."""

from __future__ import annotations

from pathlib import Path
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
from agenthicc.tools.cloakbrowser import BrowserPolicy, BrowserSessionManager
from agenthicc.config import CloakBrowserSettings
from tests.unit.test_cloakbrowser_tools import FakeBrowserClient
from agenthicc.config import ExecutionSettings


class _SmallExecutionConfig(ExecutionSettings):
    """Real execution settings with a 1,000-token window and no transport retries."""

    def __init__(self) -> None:
        super().__init__(
            auto_compact=True,
            max_output_tokens=100,
            transport_max_retries=0,
            transport_retry_base_delay_s=0.0,
            transport_retry_max_total_s=0.0,
        )

    def effective_context_window(self) -> int:
        return 1_000

    def effective_usable_budget(self) -> int:
        return 800


class _SummaryTransport:
    def __init__(self, summary: str = "preserved history summary") -> None:
        self.summary = summary
        self.calls: list[dict[str, object]] = []

    async def complete(self, messages, **kwargs):
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
        exec_cfg=_SmallExecutionConfig(),
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
async def test_token_counter_error_falls_back_to_conservative_estimate() -> None:
    class _FailingCounter(_SummaryTransport):
        async def count_tokens(self, messages, *, model, system=None):
            raise TimeoutError("counter unavailable")

    memory = ShortTermMemory(max_tokens=800)
    memory.add_user("h" * 2_800)
    transport = _FailingCounter()
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"

    compacted = await runner._auto_compact_if_needed(
        SimpleNamespace(_transport=transport),
        "continue",
        max_input_tokens=800,  # type: ignore[arg-type]
    )

    assert compacted is True
    assert transport.calls
    ctx.conv_store.append_event.assert_any_call(  # type: ignore[union-attr]
        "compaction_status",
        {"status": "token_count_unavailable", "error_type": "TimeoutError"},
    )


@pytest.mark.unit
async def test_exact_token_count_can_skip_conservative_compaction() -> None:
    class _UnderBudgetCounter(_SummaryTransport):
        async def count_tokens(self, messages, *, model, system=None):
            return 100

    memory = ShortTermMemory(max_tokens=800)
    memory.add_user("h" * 2_800)
    transport = _UnderBudgetCounter()
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"

    compacted = await runner._auto_compact_if_needed(
        SimpleNamespace(_transport=transport),
        "continue",
        max_input_tokens=800,  # type: ignore[arg-type]
    )

    assert compacted is False
    assert transport.calls == []


@pytest.mark.unit
async def test_empty_summary_uses_a_bounded_local_fallback() -> None:
    memory = ShortTermMemory(max_tokens=800)
    for _ in range(3):
        memory.add_user("h" * 700)
        memory.add_assistant({"role": "assistant", "content": "a" * 200})
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
    # The fallback drops complete oldest exchanges; it never converts an
    # arbitrary transcript excerpt into a synthetic tool-history message.
    assert len(memory._messages) < 6
    fallback_notices = [
        call.args[1]["text"]
        for call in ctx.conv_store.append_event.call_args_list  # type: ignore[union-attr]
        if call.args[0] == "system"
        and str(call.args[1].get("text", "")).startswith("⎋ Compaction fallback:")
    ]
    assert len(fallback_notices) == 1
    assert "retained recent complete exchanges" in fallback_notices[0]
    assert "summary unavailable: empty_final" in fallback_notices[0]


@pytest.mark.unit
async def test_empty_summary_plus_oversized_tool_output_is_reduced_with_causal_status() -> None:
    memory = ShortTermMemory(max_tokens=10_000)
    memory.add_user("Inspect the output and report the result.")
    memory._messages.append(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool-1", "name": "run_command", "input": {}}],
            "reasoning_content": "provider reasoning metadata",
        }
    )
    memory.add_tool_result(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "head " + "x" * 12_000 + " tail",
                }
            ],
        }
    )
    transport = _SummaryTransport(summary="")
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"

    compacted = await runner._auto_compact_if_needed(
        SimpleNamespace(_transport=transport),
        "continue",
        max_input_tokens=800,
        force=True,
        agent_instance=object(),
    )

    assert compacted is True
    assert len(transport.calls) == 2
    result_block = memory._messages[-1]["content"][0]
    assert result_block["tool_use_id"] == "tool-1"
    assert "characters omitted" in result_block["content"]
    assert memory._messages[1]["reasoning_content"] == "provider reasoning metadata"
    statuses = [
        call.args[1]
        for call in ctx.conv_store.append_event.call_args_list  # type: ignore[union-attr]
        if call.args[0] == "compaction_status"
        and call.args[1].get("status") == "compacted_local_fallback"
    ]
    assert len(statuses) == 1
    assert statuses[0]["summary_result"] == "empty_final"
    assert statuses[0]["summary_attempts"] == 2
    assert statuses[0]["fallback_result"] == "compacted_tool_output"


@pytest.mark.unit
async def test_generated_output_fallback_uses_exact_request_framing_and_bounded_recount() -> None:
    class _CountedTransport(_SummaryTransport):
        def __init__(self) -> None:
            super().__init__(summary="")
            self.counts = [1_000, 700]
            self.count_calls: list[dict[str, object]] = []

        async def count_tokens(self, messages, *, model, system=None, tools=None):
            self.count_calls.append(
                {"messages": messages, "model": model, "system": system, "tools": tools}
            )
            return self.counts.pop(0)

    memory = ShortTermMemory(max_tokens=10_000)
    memory.add_user("Inspect this output.")
    memory._messages.append(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool-2", "name": "run_command", "input": {}}],
        }
    )
    memory.add_tool_result(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-2", "content": "z" * 12_000}],
        }
    )
    transport = _CountedTransport()
    active_runner = SimpleNamespace(
        _transport=transport,
        _get_meta=lambda _agent: SimpleNamespace(system="stable system prompt"),
        _get_tool_schemas=lambda _meta: [{"name": "run_command", "parameters": {}}],
    )
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"

    compacted = await runner._auto_compact_if_needed(
        active_runner,
        "continue",
        max_input_tokens=800,
        force=True,
        agent_instance=object(),
    )

    assert compacted is True
    assert len(transport.count_calls) == 2
    assert all(call["system"] == "stable system prompt" for call in transport.count_calls)
    assert all(
        call["tools"] == [{"name": "run_command", "parameters": {}}]
        for call in transport.count_calls
    )


@pytest.mark.unit
async def test_still_over_budget_candidate_is_not_committed_or_sent() -> None:
    class _OverBudgetTransport(_SummaryTransport):
        def __init__(self) -> None:
            super().__init__(summary="")
            self.counts = [1_000, 950]
            self.count_calls = 0

        async def count_tokens(self, messages, *, model, system=None, tools=None):
            self.count_calls += 1
            return self.counts.pop(0)

    memory = ShortTermMemory(max_tokens=10_000)
    memory.add_user("Inspect this output.")
    memory._messages.append(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool-3", "name": "run_command", "input": {}}],
        }
    )
    memory.add_tool_result(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-3", "content": "z" * 12_000}],
        }
    )
    original_messages = memory._messages.copy()
    transport = _OverBudgetTransport()
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"

    from agenthicc.memory.compactor import CompactionRecoveryRequired

    with pytest.raises(CompactionRecoveryRequired) as raised:
        await runner._auto_compact_if_needed(
            SimpleNamespace(_transport=transport),
            "continue",
            max_input_tokens=800,
            force=True,
            agent_instance=object(),
        )

    assert raised.value.outcome.recovery_reason == "exact_count_over_budget"
    assert transport.count_calls == 2
    assert transport.calls  # summary request only; no model-turn request was sent
    assert memory._messages == original_messages


@pytest.mark.unit
async def test_irreducible_current_input_reports_summary_and_fallback_causes() -> None:
    user_input = "user-authored input " + "x" * 8_000
    memory = ShortTermMemory(max_tokens=10_000)
    memory.add_user(user_input)
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"

    from agenthicc.memory.compactor import CompactionRecoveryRequired

    with pytest.raises(CompactionRecoveryRequired) as raised:
        await runner._auto_compact_if_needed(
            SimpleNamespace(_transport=_SummaryTransport(summary="")),
            user_input,
            max_input_tokens=800,
            force=True,
        )

    assert raised.value.outcome.summary_result == "empty_final"
    assert raised.value.outcome.summary_attempts == 2
    assert raised.value.outcome.fallback_result == "irreducible_latest_exchange"
    assert raised.value.outcome.recovery_reason == "current_input_exceeds_budget"
    assert "summary=empty_final" in str(raised.value)
    assert "reason=current_input_exceeds_budget" in str(raised.value)
    assert "user-authored input" not in str(raised.value)
    assert memory._messages[0]["content"] == user_input


@pytest.mark.unit
async def test_failed_turn_browser_cleanup_does_not_terminally_close_session(
    tmp_path: Path,
) -> None:
    async def resolve(_host: str) -> list[str]:
        return ["93.184.216.34"]

    manager = BrowserSessionManager(
        CloakBrowserSettings(enabled=True, allowed_domains=["example.com"]),
        "session-1",
        tmp_path,
        client=FakeBrowserClient(),
        policy=BrowserPolicy(("example.com",), resolver=resolve),
    )
    await manager.open("https://example.com/")

    runner = object.__new__(AgentTurnRunner)
    runner._ctx = SimpleNamespace(browser_manager=manager)
    await runner._close_browser_after_failure()

    assert (await manager.status())["status"] == "ready"
    assert (await manager.open("https://example.com/"))["ok"] is True


@pytest.mark.unit
async def test_irreducible_context_overflow_is_recoverable_not_retried_unchanged() -> None:
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

    async def run_stream(*args, **kwargs):
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

    from agenthicc.memory.compactor import CompactionRecoveryRequired

    with pytest.raises(CompactionRecoveryRequired):
        await runner._stream(object(), "continue", active_runner)  # type: ignore[arg-type]

    assert active_runner.run_stream.await_count == 1
    # No second model request is made with the same irreducible history.
    # One empty-summary retry is bounded by the existing compaction helper;
    # the full model turn itself is not retried with unchanged context.
    assert len(transport.calls) == 2
    assert (
        sum(
            message.get("content") == "continue"
            for message in memory._messages
            if isinstance(message, dict)
        )
        == 1
    )


@pytest.mark.unit
async def test_failed_durable_compaction_keeps_direct_turn_discoverable(
    tmp_path, monkeypatch
) -> None:
    from agenthicc.memory.compactor import CompactionRecoveryRequired
    from agenthicc.memory.journal import ConversationJournal, fold_path, fold_resume_state
    from agenthicc.memory.journaled import JournaledShortTermMemory

    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    memory = JournaledShortTermMemory(journal, max_tokens=800)
    memory.add_user("earlier context " + "x" * 1_500)
    original_messages, original_summary = fold_path(journal.path)

    def fail_replace(journal_arg, *args, **kwargs):
        raise OSError("simulated durable commit failure")

    monkeypatch.setattr(ConversationJournal, "replace_state", fail_replace)
    overflow = Exception("maximum context length exceeded")
    overflow.status_code = 400  # type: ignore[attr-defined]
    transport = _SummaryTransport(summary="safe summary")

    async def failed_stream():
        raise overflow
        yield  # pragma: no cover

    async def run_stream(*args, **kwargs):
        memory.add_user(args[1])
        return failed_stream()

    active_runner = SimpleNamespace(
        _transport=transport,
        run_stream=AsyncMock(side_effect=run_stream),
    )
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"
    runner._intent_id = "turn-recoverable"

    with pytest.raises(CompactionRecoveryRequired):
        await runner._stream(object(), "continue", active_runner)  # type: ignore[arg-type]

    assert fold_path(journal.path) == (
        original_messages + [{"role": "user", "content": "continue"}],
        original_summary,
    )
    incomplete = fold_resume_state(journal.path)
    assert incomplete is not None
    assert incomplete.turn_id == "turn-recoverable"
    journal.close()
    assert any(
        call.args[0] is not None and call.args[1] == "continue"
        for call in active_runner.run_stream.await_args_list
    )
    ctx.conv_store.close_turn.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.unit
async def test_proactive_compaction_failure_journals_unsubmitted_direct_input(
    tmp_path: Path,
) -> None:
    from agenthicc.memory.compactor import CompactionOutcome, CompactionRecoveryRequired
    from agenthicc.memory.journal import ConversationJournal, fold_path, fold_resume_state
    from agenthicc.memory.journaled import JournaledShortTermMemory

    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    memory = JournaledShortTermMemory(journal, max_tokens=800)
    memory.add_user("prior conversation")
    original_messages, _ = fold_path(journal.path)
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._intent_id = "turn-proactive-compaction"
    runner._auto_compact_if_needed = AsyncMock(
        side_effect=CompactionRecoveryRequired(
            CompactionOutcome(
                "blocked_durability_error",
                1_000,
                1_000,
                durable=False,
                error_type="OSError",
            )
        )
    )  # type: ignore[method-assign]

    with pytest.raises(CompactionRecoveryRequired):
        await runner._stream(
            object(),
            "accepted direct input",
            SimpleNamespace(_transport=_SummaryTransport()),  # type: ignore[arg-type]
        )

    # Compaction ran before lauren-ai had appended the submitted user message.
    # The durable turn marker keeps that input available for an exact resume.
    assert fold_path(journal.path) == (original_messages, None)
    incomplete = fold_resume_state(journal.path)
    assert incomplete is not None
    assert incomplete.turn_id == "turn-proactive-compaction"
    assert incomplete.user_message == "accepted direct input"
    journal.close()


@pytest.mark.unit
async def test_recovery_marker_write_failure_does_not_mask_compaction_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.memory.compactor import CompactionOutcome, CompactionRecoveryRequired
    from agenthicc.memory.journal import ConversationJournal, fold_resume_state
    from agenthicc.memory.journaled import JournaledShortTermMemory

    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    memory = JournaledShortTermMemory(journal, max_tokens=800)
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._intent_id = "turn-marker-write-failure"
    runner._auto_compact_if_needed = AsyncMock(
        side_effect=CompactionRecoveryRequired(
            CompactionOutcome("blocked_durability_error", 900, 900, durable=False)
        )
    )  # type: ignore[method-assign]

    def fail_turn_started(*_args: object, **_kwargs: object) -> None:
        raise OSError("journal unavailable")

    monkeypatch.setattr(ConversationJournal, "turn_started", fail_turn_started)
    with pytest.raises(CompactionRecoveryRequired) as raised:
        await runner._stream(
            object(),
            "accepted input",
            SimpleNamespace(_transport=_SummaryTransport()),  # type: ignore[arg-type]
        )

    assert raised.value.failure_category == "compaction_persistence_failed"
    assert fold_resume_state(journal.path) is None
    journal.close()


@pytest.mark.unit
async def test_context_overflow_does_not_restore_before_committed_prefix() -> None:
    """A compatibility compaction retry keeps messages from earlier steps."""
    memory = ShortTermMemory(max_tokens=800)
    memory.add_user("earlier request")
    memory.add_assistant({"role": "assistant", "content": "committed step"})
    transport = _SummaryTransport(summary="unused")
    overflow = Exception("maximum context length exceeded")
    overflow.status_code = 400  # type: ignore[attr-defined]
    attempt = 0

    async def run_stream(*args, **kwargs):
        nonlocal attempt
        attempt += 1
        metadata = kwargs.get("metadata", {})
        if not metadata.get("_agenthicc_resume_existing_turn"):
            memory.add_user(args[1])
        if attempt == 1:

            async def failed_stream():
                raise overflow
                yield  # pragma: no cover

            return failed_stream()

        async def successful_stream():
            yield CompletionChunk(delta="continued", stop_reason="end_turn")

        return successful_stream()

    active_runner = SimpleNamespace(
        _transport=transport,
        run_stream=AsyncMock(side_effect=run_stream),
    )
    ctx = _make_context(memory)
    runner = AgentTurnRunner(ctx)
    runner._model_id = "test-model"
    runner._intent_id = "turn-1"
    runner._auto_compact_if_needed = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await runner._stream(object(), "continue", active_runner)  # type: ignore[arg-type]

    assert memory._messages[:2] == [
        {"role": "user", "content": "earlier request"},
        {"role": "assistant", "content": "committed step"},
    ]
    assert (
        sum(
            message.get("content") == "continue"
            for message in memory._messages
            if isinstance(message, dict)
        )
        == 1
    )
