"""Integration coverage for session-scoped usage persistence and projections."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._memory import ShortTermMemory
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.memory.compactor import compact_memory
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.usage_ledger import UsageLedger, UsageQuality
from agenthicc.subagents.pool import SubagentTask, SubagentWorker
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.tui.runtime.session_export import export_session, inspect_session

pytestmark = pytest.mark.integration


class _Usage:
    def __init__(self, inp: int, out: int) -> None:
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_tokens = None
        self.cache_write_tokens = None


def test_one_session_conversation_and_ledger_survive_close_and_reopen(tmp_path: Path) -> None:
    journal_path = tmp_path / "conversation-journal.jsonl"
    conversation = SessionConversation.open(
        "session-1", max_tokens=8_000, journal_path=journal_path
    )
    ledger = UsageLedger.open(
        "session-1",
        journal=conversation.journal,
        conversation_id=conversation.conversation_id,
    )

    for index, (category, inp, out) in enumerate(
        (
            ("agent", 10, 2),
            ("workflow", 20, 4),
            ("subagent", 30, 6),
            ("compaction", 5, 1),
        )
    ):
        call = ledger.begin_call(
            run_id=f"run-{index}",
            model="mock-model",
            category=category,
            agent_name=category,
        )
        ledger.complete(call, _Usage(inp, out), cost_usd=0.01 * (index + 1))

    expected = ledger.snapshot()
    conversation.close()

    reopened_conversation = SessionConversation.open(
        "session-1", max_tokens=8_000, journal_path=journal_path
    )
    reopened = UsageLedger.open(
        "session-1",
        journal=reopened_conversation.journal,
        conversation_id=reopened_conversation.conversation_id,
    )
    actual = reopened.snapshot()
    assert actual.input_tokens == expected.input_tokens
    assert actual.output_tokens == expected.output_tokens
    assert actual.cost_usd == pytest.approx(expected.cost_usd)
    assert actual.usage_status == UsageQuality.COMPLETE
    assert {record.category for record in reopened.records()} == {
        "agent",
        "workflow",
        "subagent",
        "compaction",
    }
    assert {record.conversation_id for record in reopened.records()} == {"session-1"}
    reopened_conversation.close()


def test_inspect_and_export_use_canonical_usage_records(tmp_path: Path) -> None:
    session_id = "inspect-usage"
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    (session_dir / "metadata.json").write_text(json.dumps({"model": "mock"}), encoding="utf-8")
    (tmp_path / f"{session_id}.jsonl").write_text("", encoding="utf-8")
    (session_dir / "conversation.jsonl").write_text(
        json.dumps(
            {
                "kind": "tokens",
                "payload": {"input_tokens": 999, "output_tokens": 999, "cost_usd": 99},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    conversation = SessionConversation.open(
        session_id, max_tokens=8_000, journal_path=session_dir / "conversation-journal.jsonl"
    )
    ledger = UsageLedger.open(session_id, journal=conversation.journal)
    call = ledger.begin_call(run_id="run", model="mock")
    ledger.complete(call, _Usage(11, 4), cost_usd=0.11)
    conversation.close()

    inspected = inspect_session(session_id, sessions_dir=tmp_path)
    tokens = inspected["conversation"]["tokens"]  # type: ignore[index]
    assert tokens["input"] == 11
    assert tokens["output"] == 4
    assert tokens["cost_usd"] == pytest.approx(0.11)
    assert tokens["status"] == "complete"
    assert tokens["calls"] == 1

    destination = export_session(session_id, tmp_path / "export.json", sessions_dir=tmp_path)
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document["usage"]["input"] == 11
    assert document["usage"]["output"] == 4
    assert document["usage"]["calls"] == 1


def test_inspect_without_usage_reports_unavailable_instead_of_false_zero(tmp_path: Path) -> None:
    session_id = "no-usage"
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    (session_dir / "metadata.json").write_text("{}", encoding="utf-8")
    (session_dir / "conversation.jsonl").write_text("", encoding="utf-8")
    (tmp_path / f"{session_id}.jsonl").write_text("", encoding="utf-8")

    summary = inspect_session(session_id, sessions_dir=tmp_path)
    tokens = summary["conversation"]["tokens"]  # type: ignore[index]
    assert tokens["input"] == 0
    assert tokens["output"] == 0
    assert tokens["status"] == "unavailable"
    assert tokens["cost_status"] == "unavailable"


def test_restore_uses_canonical_records_before_legacy_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.memory.journal as journal_module
    import agenthicc.tui.runtime.session_log as log_module
    from agenthicc.tui.conversation_store import AppState, ConversationEvent
    from agenthicc.tui.runtime.session_log import SessionEventLog, restore_session

    monkeypatch.setattr(log_module, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(log_module, "_SESSION_INDEX", tmp_path / "index.json")
    monkeypatch.setattr(journal_module, "_SESSIONS_DIR", tmp_path)
    session_dir = tmp_path / "restore-usage"
    session_dir.mkdir()
    event_log = SessionEventLog("restore-usage")
    event_log.append(
        ConversationEvent(
            "legacy", "tokens", {"input_tokens": 999, "output_tokens": 999, "cost_usd": 9}
        )
    )
    event_log.close()

    conversation = SessionConversation.open(
        "restore-usage", max_tokens=8_000, journal_path=session_dir / "conversation-journal.jsonl"
    )
    ledger = UsageLedger.open("restore-usage", journal=conversation.journal)
    call = ledger.begin_call(run_id="run", model="mock")
    ledger.complete(call, _Usage(2, 1), cost_usd=0.02)
    conversation.close()

    state = AppState.create()
    import asyncio

    asyncio.run(restore_session("restore-usage", state))
    assert state.conversation.tokens_in() == 2
    assert state.conversation.tokens_out() == 1
    assert state.conversation.usage_status() == "complete"


@pytest.mark.asyncio
async def test_standard_compaction_and_subagent_paths_use_the_same_ledger() -> None:
    ledger = UsageLedger("session-runtime", conversation_id="session-runtime")

    class _CompactionTransport:
        async def complete(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(content="summary", usage=_Usage(6, 2))

    memory = ShortTermMemory(max_tokens=8_000)
    memory.add_user("retain this context")
    await compact_memory(
        memory,
        _CompactionTransport(),
        model="mock-model",
        usage_ledger=ledger,
        session_id="session-runtime",
        run_id="compaction-run",
    )

    mock = MockTransport()
    mock.queue_response(
        Completion(
            id="subagent-completion",
            model="mock-model",
            content="worker result",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=9, output_tokens=3),
        )
    )
    parent = AgentRunnerBase(transport=mock)
    worker = SubagentWorker(
        task=SubagentTask("task-1", "explorer", "inspect the project"),
        spec=DEFAULT_REGISTRY.get("explorer"),
        index=1,
        parent_runner=parent,
        parent_model="mock-model",
        all_tools=[],
        usage_ledger=ledger,
        conversation_id="session-runtime",
        parent_run_id="parent-run",
    )
    result = await worker.run()

    assert result.ok
    assert {record.category for record in ledger.records()} == {"compaction", "subagent"}
    assert ledger.snapshot().input_tokens == 15
    assert ledger.snapshot().output_tokens == 5
