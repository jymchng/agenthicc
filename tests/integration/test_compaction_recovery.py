"""Durable compaction/restart contract tests for PRD-242."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from agenthicc.memory.compactor import compact_memory_with_result
from agenthicc.memory.compactor import CompactionRecoveryRequired
from agenthicc.memory.journal import ConversationJournal, fold_path, fold_resume_state
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.runners.agent_turn import AgentTurnRunner
from agenthicc.runners.agent_turn_context import AgentTurnContext
from agenthicc.runners.session_conversation import SessionConversation

pytestmark = pytest.mark.integration


class _SummaryTransport:
    async def complete(self, _messages, **_kwargs):  # noqa: ANN001
        return SimpleNamespace(content="A concise summary of decisions and unfinished work.")


class _CompactBeforeSendConfig:
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


def _seed_conversation(conversation: SessionConversation) -> None:
    for index in range(6):
        conversation.memory.add_user(f"Request {index}: " + "question " * 100)
        conversation.memory.add_assistant(
            {"role": "assistant", "content": f"Response {index}: " + "answer " * 100}
        )


@pytest.mark.asyncio
async def test_compacted_projection_rehydrates_identically_after_restart(tmp_path: Path) -> None:
    journal_path = tmp_path / "conversation-journal.jsonl"
    conversation = SessionConversation.open(
        "compact-success", max_tokens=8_000, journal_path=journal_path
    )
    _seed_conversation(conversation)

    outcome = await compact_memory_with_result(
        conversation.memory,
        _SummaryTransport(),
        model="offline-test",
        max_input_tokens=1_000,
    )

    assert outcome.status == "compacted_summary"
    expected_messages = list(conversation.memory._messages)
    assert fold_path(journal_path) == (expected_messages, None)
    conversation.close()

    resumed = SessionConversation.open(
        "compact-success", max_tokens=8_000, journal_path=journal_path
    )
    assert resumed.messages == expected_messages
    assert resumed.memory.summary is None
    resumed.close()


@pytest.mark.asyncio
async def test_failed_durable_compaction_keeps_original_projection_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path = tmp_path / "conversation-journal.jsonl"
    conversation = SessionConversation.open(
        "compact-failure", max_tokens=8_000, journal_path=journal_path
    )
    _seed_conversation(conversation)
    original_messages = list(conversation.memory._messages)

    def fail_replace(_journal, _messages, _summary) -> None:
        raise OSError("injected journal commit failure")

    monkeypatch.setattr(ConversationJournal, "replace_state", fail_replace)
    outcome = await compact_memory_with_result(
        conversation.memory,
        _SummaryTransport(),
        model="offline-test",
        max_input_tokens=1_000,
    )

    assert outcome.status == "blocked_durability_error"
    assert conversation.memory._messages == original_messages
    assert fold_path(journal_path) == (original_messages, None)
    conversation.close()

    resumed = SessionConversation.open(
        "compact-failure", max_tokens=8_000, journal_path=journal_path
    )
    assert resumed.messages == original_messages
    resumed.close()


@pytest.mark.asyncio
async def test_proactive_compaction_commit_failure_preserves_direct_input_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path = tmp_path / "conversation-journal.jsonl"
    journal = ConversationJournal(journal_path)
    memory = JournaledShortTermMemory(journal, max_tokens=800)
    memory.add_user("Earlier context " + "history " * 350)
    original_messages = list(memory._messages)

    def fail_replace(_journal, _messages, _summary) -> None:
        raise OSError("injected durable replacement failure")

    monkeypatch.setattr(ConversationJournal, "replace_state", fail_replace)
    context = AgentTurnContext(
        text="accepted direct request",
        runner=SimpleNamespace(_transport=None, _signals=None),  # type: ignore[arg-type]
        processor=SimpleNamespace(),
        session_memory=memory,
        conversation_id="direct-compaction-recovery",
        max_agent_turns=1,
        conv_store=None,
        exec_cfg=_CompactBeforeSendConfig(),  # type: ignore[arg-type]
        skills={},
        mention_cache=None,
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="default",
        completed_turns=0,
        approval_svc=None,
        output_collector=[],
        system_prompt_suffix="",
    )
    runner = AgentTurnRunner(context)
    runner._model_id = "offline-test"
    runner._intent_id = "direct-turn-1"

    with pytest.raises(CompactionRecoveryRequired):
        await runner._stream(
            object(),
            "accepted direct request",
            SimpleNamespace(_transport=_SummaryTransport()),  # type: ignore[arg-type]
        )

    assert memory._messages == original_messages
    incomplete = fold_resume_state(journal_path)
    assert incomplete is not None
    assert incomplete.turn_id == "direct-turn-1"
    assert incomplete.user_message == "accepted direct request"
    journal.close()

    # Restarting reconstructs the original projection and leaves the accepted
    # input discoverable as the same incomplete logical turn.
    reopened_journal = ConversationJournal(journal_path)
    reopened_memory = JournaledShortTermMemory(reopened_journal, max_tokens=800)
    assert reopened_memory._messages == original_messages
    resumed_incomplete = fold_resume_state(journal_path)
    assert resumed_incomplete is not None
    assert resumed_incomplete.turn_id == "direct-turn-1"
    assert resumed_incomplete.user_message == "accepted direct request"
    reopened_journal.close()
