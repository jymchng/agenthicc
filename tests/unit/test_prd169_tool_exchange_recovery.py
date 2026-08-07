"""PRD-169 unit coverage for shared tool-exchange recovery in agenthicc."""

from __future__ import annotations

import json

import pytest

from lauren_ai import ToolConversationIntegrityError, ToolResult
from lauren_ai._transport import Completion, TokenUsage, ToolCall

from agenthicc.memory.journal import ConversationJournal
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.runners.agent_turn import (
    _preserve_interrupted_memory,
    _require_tool_transaction_api,
)

pytestmark = pytest.mark.unit


def _completion(*calls: ToolCall) -> Completion:
    return Completion(
        id="assistant-1",
        model="mock",
        content="",
        tool_calls=list(calls),
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def test_journaled_interrupted_exchange_is_repaired_once_and_rehydrated(tmp_path) -> None:
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    memory = JournaledShortTermMemory(journal)
    memory.add_user("read both assets")
    first = ToolCall(tool_use_id="read-a", name="Read", input={"path": "a.png"})
    second = ToolCall(tool_use_id="read-b", name="Read", input={"path": "b.png"})
    memory.add_assistant(_completion(first, second))
    memory.begin_tool_exchange([first, second], run_id="run-1")

    assert _preserve_interrupted_memory(memory) is True
    assert _preserve_interrupted_memory(memory) is False
    assert memory.validate_tool_history().ok
    assert [block["tool_use_id"] for block in memory._messages[-1]["content"]] == [
        "read-a",
        "read-b",
    ]

    entries = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert any(entry["kind"] == "reset" for entry in entries)
    assert any(entry["kind"] == "tool_exchange_started" for entry in entries)
    assert any(entry["kind"] == "tool_exchange_result_recorded" for entry in entries)
    assert any(entry["kind"] == "tool_exchange_aborted" for entry in entries)
    lifecycle_entries = [entry for entry in entries if entry["kind"].startswith("tool_exchange_")]
    assert all("read-a" not in json.dumps(entry) for entry in lifecycle_entries)
    assert all("read-b" not in json.dumps(entry) for entry in lifecycle_entries)

    journal.close()
    resumed = JournaledShortTermMemory(ConversationJournal(tmp_path / "conversation.jsonl"))
    assert resumed.validate_tool_history().ok
    assert resumed._messages == memory._messages
    resumed.close()


def test_transaction_commit_orders_results_and_synthesizes_unresolved_call(tmp_path) -> None:
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    memory = JournaledShortTermMemory(journal)
    memory.add_user("inspect")
    first = ToolCall(tool_use_id="one", name="Read", input={"path": "one"})
    second = ToolCall(tool_use_id="two", name="Read", input={"path": "two"})
    memory.add_assistant(_completion(first, second))
    exchange = memory.begin_tool_exchange([first, second], run_id="run-1")

    committed = memory.commit_tool_exchange(
        exchange,
        [ToolResult.ok("one result", tool_use_id="one")],
    )

    assert committed.state == "committed"
    assert committed.call_ids == ("one", "two")
    assert [outcome.tool_use_id for outcome in committed.outcomes] == ["one", "two"]
    assert committed.outcomes[-1].synthetic is True
    assert memory.validate_tool_history().ok
    assert [block["tool_use_id"] for block in memory._messages[-1]["content"]] == ["one", "two"]
    journal.close()


def test_non_adjacent_result_is_moved_before_queued_user_message(tmp_path) -> None:
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    memory = JournaledShortTermMemory(journal)
    memory.add_user("inspect the asset")
    call = ToolCall(tool_use_id="asset-1", name="Read", input={"path": "asset.txt"})
    memory.add_assistant(_completion(call))
    # This is the invalid shape reported by the TUI: a continuation was
    # appended before the result from the interrupted tool task arrived.
    memory.add_user("continue from where you stopped")
    memory._messages.append(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "asset-1", "content": "done"}],
        }
    )

    report = memory.validate_tool_history()
    assert report.first_issue is not None
    assert report.first_issue.code == "non_adjacent_results"

    assert _preserve_interrupted_memory(memory) is True
    assert memory.validate_tool_history().ok
    assert memory._messages[2]["content"][0]["tool_use_id"] == "asset-1"
    assert memory._messages[3]["content"] == "continue from where you stopped"
    journal.close()


def test_journal_resume_repairs_non_adjacent_result_before_constructor_returns(tmp_path) -> None:
    path = tmp_path / "conversation.jsonl"
    journal = ConversationJournal(path)
    journal.append_message({"role": "user", "content": "inspect the asset"})
    journal.append_message(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "asset-1", "name": "Read", "input": {}}],
        }
    )
    journal.append_message({"role": "user", "content": "continue from where you stopped"})
    journal.append_message(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "asset-1", "content": "done"}],
        }
    )
    journal.close()

    resumed_journal = ConversationJournal(path)
    resumed = JournaledShortTermMemory(resumed_journal)
    assert resumed.validate_tool_history().ok
    assert resumed._messages[2]["content"][0]["tool_use_id"] == "asset-1"
    assert resumed._messages[3]["content"] == "continue from where you stopped"
    resumed_journal.close()


def test_non_adjacent_duplicate_result_fails_closed() -> None:
    from lauren_ai._memory import ShortTermMemory

    memory = ShortTermMemory()
    memory._messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "same"}]},
        {"role": "user", "content": "continue"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "same"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "same"}]},
    ]

    with pytest.raises(ToolConversationIntegrityError) as caught:
        _preserve_interrupted_memory(memory)
    assert caught.value.code == "non_adjacent_results"


def test_repaired_journal_rejects_late_commit_from_stale_exchange(tmp_path) -> None:
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    memory = JournaledShortTermMemory(journal)
    memory.add_user("inspect")
    call = ToolCall(tool_use_id="late-1", name="Read", input={})
    memory.add_assistant(_completion(call))
    exchange = memory.begin_tool_exchange([call], run_id="run-1")
    memory.add_user("continue")
    memory._messages.append(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "late-1", "content": "done"}],
        }
    )

    assert _preserve_interrupted_memory(memory) is True
    with pytest.raises(ToolConversationIntegrityError) as caught:
        memory.commit_tool_exchange(exchange, [ToolResult.ok("late", tool_use_id="late-1")])
    assert caught.value.code == "exchange_owner_mismatch"
    assert memory.validate_tool_history().ok
    journal.close()


def test_orphan_result_is_rejected_before_provider_io() -> None:
    from lauren_ai._memory import ShortTermMemory

    memory = ShortTermMemory()
    memory._messages = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "orphan"}]}
    ]

    report = memory.validate_tool_history()
    assert report.first_issue is not None
    assert report.first_issue.code == "orphan_result"
    with pytest.raises(ToolConversationIntegrityError) as caught:
        memory.ensure_valid()
    assert caught.value.code == "orphan_result"
    assert "tool_use_id" not in str(caught.value)


def test_old_memory_fails_closed_with_actionable_error() -> None:
    class LegacyMemory:
        pass

    with pytest.raises(RuntimeError, match="transaction-capable lauren-ai"):
        _require_tool_transaction_api(LegacyMemory())
