"""Unit tests for conversation compactor (PRD-119)."""

from __future__ import annotations

import pytest

from lauren_ai._memory import ShortTermMemory
from agenthicc.memory.compactor import compact_memory, _format_transcript


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
        mem = self._make_mem()
        transport = _MockTransport("done")
        await compact_memory(mem, transport, model="m", conv_store=conv)
        # compact_memory appends system events — since there's no active turn,
        # they go into the current turn if one exists; we just verify no crash.
        assert not conv.compaction_active()

    async def test_works_without_conv_store(self) -> None:
        mem = self._make_mem()
        transport = _MockTransport()
        result = await compact_memory(mem, transport, model="m", conv_store=None)
        assert isinstance(result, int)
        assert len(mem._messages) == 2


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
