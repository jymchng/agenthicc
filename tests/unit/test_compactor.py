"""Unit tests for conversation compactor (PRD-119)."""

from __future__ import annotations

import asyncio

import pytest
from types import SimpleNamespace

from lauren_ai._memory import ShortTermMemory
from agenthicc.memory.compactor import (
    compact_memory,
    compact_memory_with_result,
    local_compaction_fallback,
    _format_transcript,
)


# ── auto_compact default ───────────────────────────────────────────────────────


class TestAutoCompactConfig:
    def test_default_auto_compact_is_true(self) -> None:
        from agenthicc.config import ExecutionSettings

        cfg = ExecutionSettings()
        assert cfg.auto_compact is True


# ── compact_memory ─────────────────────────────────────────────────────────────


class _MockTransport:
    """Minimal transport stub that returns a fixed summary string."""

    def __init__(self, summary: str = "Compact summary.") -> None:
        self._summary = summary
        self.calls: list[dict] = []

    async def complete(self, messages, *, model, system, max_tokens, temperature, stream):
        self.calls.append({"messages": messages, "model": model})

        class _Completion:
            content = self._summary

        return _Completion()


class _RetryingTransport:
    """Return an empty final answer once, then a usable summary."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        messages,
        *,
        model,
        system,
        max_tokens,
        temperature,
        stream,
        max_completion_tokens=None,
        request_options=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "max_completion_tokens": max_completion_tokens,
                "request_options": request_options,
            }
        )
        return SimpleNamespace(
            content="" if len(self.calls) == 1 else "Recovered conversation summary."
        )


class _RawContentTransport:
    """Expose final text only through an explicitly retained raw response."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, model, system, max_tokens, temperature, stream):
        self.calls += 1
        return SimpleNamespace(
            content="",
            raw_response=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Raw summary text."))]
            ),
        )


class _StreamingRetryTransport:
    """Return final text only in the streamed retry response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        messages,
        *,
        model,
        system,
        max_tokens,
        temperature,
        stream,
        max_completion_tokens=None,
        request_options=None,
    ):
        self.calls.append(
            {
                "stream": stream,
                "max_completion_tokens": max_completion_tokens,
            }
        )
        if len(self.calls) == 1:
            return SimpleNamespace(content="")

        async def chunks():
            yield SimpleNamespace(thinking_delta="hidden reasoning")
            yield SimpleNamespace(delta="Recovered ")
            yield SimpleNamespace(delta="from stream.")
            yield SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=4))

        return chunks()


@pytest.mark.unit
class TestCompactMemory:
    def _make_mem(self) -> ShortTermMemory:
        mem = ShortTermMemory(max_tokens=32_000)
        mem.add_user("Please list all files")
        mem._messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "list_directory", "input": {}}
                ],
            }
        )
        mem._messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "x" * 10_000}],
            }
        )
        return mem

    async def test_replaces_messages_with_two(self) -> None:
        mem = self._make_mem()
        transport = _MockTransport("Summary of work done.")
        await compact_memory(mem, transport, model="test-model")
        assert len(mem._messages) == 2

    async def test_first_message_is_user_with_summary(self) -> None:
        mem = self._make_mem()
        transport = _MockTransport("Summary of work done.")
        await compact_memory(mem, transport, model="test-model")
        first = mem._messages[0]
        assert first["role"] == "user"
        assert "[COMPACT SUMMARY]" in first["content"]
        assert "Summary of work done." in first["content"]

    async def test_second_message_is_assistant_ack(self) -> None:
        mem = self._make_mem()
        transport = _MockTransport()
        await compact_memory(mem, transport, model="test-model")
        second = mem._messages[1]
        assert second["role"] == "assistant"

    async def test_returns_new_token_estimate(self) -> None:
        mem = self._make_mem()
        before = mem.token_estimate
        transport = _MockTransport("Short summary.")
        result = await compact_memory(mem, transport, model="test-model")
        assert result == mem.token_estimate
        assert result < before

    async def test_transport_called_once_with_correct_model(self) -> None:
        mem = self._make_mem()
        transport = _MockTransport()
        await compact_memory(mem, transport, model="my-model")
        assert len(transport.calls) == 1
        assert transport.calls[0]["model"] == "my-model"

    async def test_compaction_active_cleared_on_success(self) -> None:
        from agenthicc.tui.conversation_store import ConversationStore

        conv = ConversationStore()
        mem = self._make_mem()
        transport = _MockTransport()
        await compact_memory(mem, transport, model="m", conv_store=conv)
        assert not conv.compaction_active()

    async def test_compaction_active_cleared_on_failure(self) -> None:
        from agenthicc.tui.conversation_store import ConversationStore

        class _FailTransport:
            async def complete(self, *a, **kw):  # noqa: ANN001
                raise RuntimeError("network error")

        conv = ConversationStore()
        mem = self._make_mem()
        before_messages = list(mem._messages)
        await compact_memory(mem, _FailTransport(), model="m", conv_store=conv)
        # Signal cleared even on failure
        assert not conv.compaction_active()
        # Memory unchanged on failure
        assert mem._messages == before_messages

    async def test_conv_store_events_appended(self) -> None:
        from agenthicc.tui.conversation_store import ConversationStore

        conv = ConversationStore()
        events = []
        conv.on_event(events.append)
        mem = self._make_mem()
        transport = _MockTransport("done")
        await compact_memory(mem, transport, model="m", conv_store=conv)
        statuses = [event.payload for event in events if event.kind == "compaction_status"]
        assert statuses[0]["status"] == "started"
        assert isinstance(statuses[0]["before_tokens"], int)
        assert statuses[-1]["status"] == "compacted_summary"
        assert "transcript" not in str(statuses).lower()
        assert not conv.compaction_active()

    async def test_works_without_conv_store(self) -> None:
        mem = self._make_mem()
        transport = _MockTransport()
        result = await compact_memory(mem, transport, model="m", conv_store=None)
        assert isinstance(result, int)
        assert len(mem._messages) == 2

    async def test_empty_model_summary_is_reported_as_recoverable(self) -> None:
        from agenthicc.tui.conversation_store import ConversationStore

        conv = ConversationStore()
        events = []
        conv.on_event(events.append)
        mem = self._make_mem()
        await compact_memory(mem, _MockTransport(""), model="m", conv_store=conv)

        assert any(
            event.kind == "system"
            and str(event.payload.get("text", "")).startswith(
                "⎋ Compaction summary unavailable (empty_final; 2 attempts)"
            )
            for event in events
        )
        assert not any(event.payload.get("text") == "⎋ Nothing to compact" for event in events)

    async def test_summary_outcome_classifies_empty_final_and_attempt_count(self) -> None:
        mem = self._make_mem()
        before = mem.snapshot()
        transport = _MockTransport("")

        outcome = await compact_memory_with_result(mem, transport, model="m")

        assert outcome.status == "skipped_summary_unavailable"
        assert outcome.summary_result == "empty_final"
        assert outcome.summary_attempts == 2
        assert outcome.summary_error_type == ""
        assert mem.snapshot() == before
        assert len(transport.calls) == 2

    async def test_reasoning_only_summary_is_classified_without_exposing_reasoning(self) -> None:
        class _ReasoningOnlyTransport:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
                self.calls += 1
                return SimpleNamespace(
                    content=[{"type": "thinking", "thinking": "private reasoning payload"}]
                )

        from agenthicc.tui.conversation_store import ConversationStore

        conv = ConversationStore()
        events = []
        conv.on_event(events.append)
        mem = self._make_mem()
        before = mem.snapshot()
        transport = _ReasoningOnlyTransport()

        outcome = await compact_memory_with_result(mem, transport, model="m", conv_store=conv)

        assert outcome.summary_result == "reasoning_only"
        assert outcome.summary_attempts == 2
        assert transport.calls == 2
        assert mem.snapshot() == before
        assert "private reasoning payload" not in str(events)

    def test_local_fallback_excerpts_tool_output_without_changing_protocol_or_user_text(
        self,
    ) -> None:
        memory = ShortTermMemory(max_tokens=10_000)
        user_request = "Read the build log and report the failing target."
        memory.add_user(user_request)
        memory._messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "run_command", "input": {}}
                ],
                "reasoning_content": "provider-required reasoning metadata",
            }
        )
        memory.add_tool_result(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "BEGIN " + "x" * 10_000 + " END",
                        "is_error": False,
                    }
                ],
            }
        )

        outcome = local_compaction_fallback(memory, max_input_tokens=800)

        assert outcome.status == "compacted_local_fallback"
        assert outcome.fallback_result == "compacted_tool_output"
        assert outcome.durable is True
        assert memory._messages[0] == {"role": "user", "content": user_request}
        assistant = memory._messages[1]
        result = memory._messages[2]["content"][0]
        assert assistant["content"][0]["id"] == "tool-1"
        assert assistant["reasoning_content"] == "provider-required reasoning metadata"
        assert result["tool_use_id"] == "tool-1"
        assert result["is_error"] is False
        assert "characters omitted" in result["content"]
        assert result["content"].startswith("BEGIN ")
        assert result["content"].endswith(" END")
        assert memory.validate_tool_history().ok

    def test_irreducible_user_message_is_unchanged_and_classified(self) -> None:
        memory = ShortTermMemory(max_tokens=10_000)
        memory.add_user("user-authored " + "x" * 10_000)
        before = memory.snapshot()

        outcome = local_compaction_fallback(memory, max_input_tokens=100)

        assert outcome.status == "blocked_irreducible_context"
        assert outcome.fallback_result == "irreducible_latest_exchange"
        assert memory.snapshot() == before

    async def test_empty_final_response_is_retried_before_reporting_failure(self) -> None:
        from agenthicc.tui.conversation_store import ConversationStore

        conv = ConversationStore()
        events = []
        conv.on_event(events.append)
        mem = self._make_mem()
        transport = _RetryingTransport()

        await compact_memory(
            mem,
            transport,
            model="m",
            conv_store=conv,
            max_completion_tokens=128,
            request_options=SimpleNamespace(option="preserved"),
        )

        assert len(transport.calls) == 2
        assert transport.calls[0]["max_completion_tokens"] == 1_024
        assert transport.calls[1]["max_completion_tokens"] == 2_048
        assert transport.calls[0]["request_options"].option == "preserved"
        assert mem._messages[0]["content"].endswith("Recovered conversation summary.")
        assert any(event.payload.get("text", "").startswith("⎋ Compacted →") for event in events)
        assert not any("empty summary" in event.payload.get("text", "") for event in events)

    async def test_raw_provider_content_is_used_without_using_reasoning_blocks(self) -> None:
        mem = self._make_mem()
        transport = _RawContentTransport()

        await compact_memory(mem, transport, model="m")

        assert transport.calls == 1
        assert "Raw summary text." in mem._messages[0]["content"]

    async def test_empty_response_retries_as_stream_and_accumulates_final_deltas(self) -> None:
        mem = self._make_mem()
        transport = _StreamingRetryTransport()

        await compact_memory(mem, transport, model="m")

        assert [call["stream"] for call in transport.calls] == [False, True]
        assert transport.calls[1]["max_completion_tokens"] == 2_048
        assert "Recovered from stream." in mem._messages[0]["content"]
        assert "hidden reasoning" not in mem._messages[0]["content"]

    async def test_summary_cancellation_propagates_without_mutating_memory(self) -> None:
        class _CancelledTransport:
            async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise asyncio.CancelledError

        mem = self._make_mem()
        before = mem.snapshot()

        with pytest.raises(asyncio.CancelledError):
            await compact_memory_with_result(mem, _CancelledTransport(), model="m")

        assert mem.snapshot() == before

    async def test_journal_reset_failure_returns_typed_blocked_outcome(
        self, tmp_path, monkeypatch
    ) -> None:
        from agenthicc.memory.journal import ConversationJournal, fold_path
        from agenthicc.memory.journaled import JournaledShortTermMemory

        journal = ConversationJournal(tmp_path / "journal.jsonl")
        mem = JournaledShortTermMemory(journal)
        mem.add_user("important user request " + "history " * 500)
        before = mem.snapshot()

        def fail_replace(journal_arg, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
            raise OSError("disk is unavailable")

        monkeypatch.setattr(ConversationJournal, "replace_state", fail_replace)
        outcome = await compact_memory_with_result(mem, _MockTransport(), model="m")

        assert outcome.status == "blocked_durability_error"
        assert not outcome.applied
        assert mem.snapshot() == before
        assert fold_path(journal.path) == (before["messages"], before["summary"])
        journal.close()

    def test_local_fallback_drops_whole_oldest_exchanges(self) -> None:
        mem = ShortTermMemory(max_tokens=8_000)
        for index in range(5):
            mem.add_user(f"request {index}: " + "u" * 800)
            mem.add_assistant({"role": "assistant", "content": f"answer {index}: " + "a" * 800})
        original_count = len(mem._messages)

        outcome = local_compaction_fallback(mem, max_input_tokens=1_000)

        assert outcome.status == "compacted_local_fallback"
        assert len(mem._messages) < original_count
        assert mem._messages[0]["role"] == "user"
        assert mem._messages[-1]["role"] == "assistant"
        assert all("request" in str(msg["content"]) for msg in mem._messages[::2])


# ── _format_transcript ─────────────────────────────────────────────────────────


class TestFormatTranscript:
    def test_string_content(self) -> None:
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = _format_transcript(messages)
        assert "USER: Hello" in result
        assert "ASSISTANT: Hi there" in result

    def test_skips_system_messages(self) -> None:
        messages = [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "Do something"},
        ]
        result = _format_transcript(messages)
        assert "system" not in result.lower()
        assert "USER: Do something" in result

    def test_tool_use_block(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "git_status", "id": "t1", "input": {}},
                ],
            }
        ]
        result = _format_transcript(messages)
        assert "tool_call:git_status" in result

    def test_tool_result_block_truncated(self) -> None:
        big = "x" * 1000
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": big},
                ],
            }
        ]
        result = _format_transcript(messages)
        assert "tool_result:" in result
        assert "…" in result  # truncated at 500 chars

    def test_tool_result_short_not_truncated(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "short"},
                ],
            }
        ]
        result = _format_transcript(messages)
        assert "…" not in result
        assert "short" in result

    def test_empty_messages_returns_empty(self) -> None:
        assert _format_transcript([]) == ""


# ── config TOML parsing ───────────────────────────────────────────────────────


class TestConfigParsing:
    def test_default_values(self) -> None:
        from agenthicc.config import _dict_to_config

        cfg = _dict_to_config({})
        assert cfg.execution.auto_compact is True

    def test_override_auto_compact(self) -> None:
        from agenthicc.config import _dict_to_config

        cfg = _dict_to_config({"execution": {"auto_compact": False}})
        assert cfg.execution.auto_compact is False
