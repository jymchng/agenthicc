"""Durable compaction/restart contract tests for PRD-242."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from pathlib import Path

import pytest

from agenthicc.memory.compactor import compact_memory_with_result, local_compaction_fallback
from agenthicc.memory.compactor import CompactionRecoveryRequired
from agenthicc.memory.journal import ConversationJournal, fold_path, fold_resume_state
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.runners.agent_turn import AgentTurnRunner
from agenthicc.runners.agent_turn_context import AgentTurnContext
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.config import ExecutionSettings

pytestmark = pytest.mark.integration


class _SummaryTransport:
    async def complete(self, _messages, **_kwargs):
        return SimpleNamespace(content="A concise summary of decisions and unfinished work.")


class _CompactBeforeSendConfig(ExecutionSettings):
    """Real execution settings with a fixed small window and no transport retries."""

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


async def test_inflight_compaction_cannot_replace_a_newer_tool_history_projection(
    tmp_path: Path,
) -> None:
    """Compaction output is committed only against the history it summarized."""
    journal_path = tmp_path / "concurrent-compaction.jsonl"
    journal = ConversationJournal(journal_path)
    memory = JournaledShortTermMemory(journal, max_tokens=20_000)
    memory.add_user("Prior context " + "background " * 5_000)

    summary_started = asyncio.Event()
    release_summary = asyncio.Event()

    class _BlockingSummaryTransport:
        async def complete(self, _messages, **_kwargs):
            summary_started.set()
            await release_summary.wait()
            return SimpleNamespace(content="Summary of the pre-turn conversation.")

    compaction = asyncio.create_task(
        compact_memory_with_result(
            memory,
            _BlockingSummaryTransport(),
            model="offline-test",
            max_input_tokens=20_000,
        )
    )
    await summary_started.wait()

    # Model the durable sequence produced when a new provider/tool turn writes
    # while an independently-started compaction is awaiting its summarizer.
    memory.add_assistant(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "late-a", "name": "Read", "input": {}}],
        }
    )
    memory.add_user("Continue while preserving the pending tool work")
    memory.add_user(
        [
            {
                "type": "tool_result",
                "tool_use_id": "late-a",
                "content": "result A",
                "is_error": False,
            }
        ]
    )
    memory.add_assistant(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "late-b", "name": "Read", "input": {}}],
        }
    )
    expected_current_messages = copy.deepcopy(memory._messages)
    issue_codes = {issue.code for issue in memory.validate_tool_history().issues}
    assert "non_adjacent_results" in issue_codes
    assert "missing_results" in issue_codes

    release_summary.set()
    outcome = await compaction

    assert outcome.status == "skipped_concurrent_update"
    assert memory._messages == expected_current_messages
    assert fold_path(journal_path) == (expected_current_messages, None)
    journal.close()


@pytest.mark.asyncio
async def test_compaction_refuses_invalid_source_history_without_mutating_journal(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "invalid-source-compaction.jsonl"
    journal = ConversationJournal(journal_path)
    memory = JournaledShortTermMemory(journal, max_tokens=20_000)
    memory.add_user("Inspect the repository")
    memory.add_assistant(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-late", "name": "Read", "input": {}}],
        }
    )
    memory.add_user("Continue after interruption")
    memory.add_tool_result(
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call-late", "content": "late result"}
            ],
        }
    )
    memory.add_assistant(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-missing", "name": "Read", "input": {}}],
        }
    )
    original_messages = copy.deepcopy(memory._messages)
    original_journal = journal_path.read_bytes()

    class _MustNotSummarize:
        async def complete(self, *_args, **_kwargs):
            raise AssertionError("invalid source history must be rejected before summarization")

    summary_outcome = await compact_memory_with_result(
        memory,
        _MustNotSummarize(),
        model="offline-test",
        max_input_tokens=1_000,
    )
    local_outcome = local_compaction_fallback(memory, max_input_tokens=1_000)

    assert summary_outcome.status == "blocked_irreducible_context"
    assert summary_outcome.recovery_reason == "invalid_tool_history"
    assert local_outcome.status == "blocked_irreducible_context"
    assert local_outcome.recovery_reason == "invalid_tool_history"
    assert memory._messages == original_messages
    assert journal_path.read_bytes() == original_journal
    assert fold_path(journal_path) == (original_messages, None)
    journal.close()


def test_compaction_commit_boundary_rejects_invalid_candidate_without_journaling(
    tmp_path: Path,
) -> None:
    from agenthicc.memory.compactor import _InvalidCompactionCandidate, _replace_state

    journal_path = tmp_path / "invalid-candidate-compaction.jsonl"
    journal = ConversationJournal(journal_path)
    memory = JournaledShortTermMemory(journal, max_tokens=10_000)
    memory.add_user("Preserve this history")
    original_messages = copy.deepcopy(memory._messages)
    original_journal = journal_path.read_bytes()

    with pytest.raises(_InvalidCompactionCandidate, match="missing_results"):
        _replace_state(
            memory,
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "unanswered", "name": "Read", "input": {}}
                    ],
                }
            ],
            None,
        )

    assert memory._messages == original_messages
    assert journal_path.read_bytes() == original_journal
    assert fold_path(journal_path) == (original_messages, None)
    journal.close()


@pytest.mark.asyncio
async def test_tool_output_fallback_commits_protocol_valid_projection_for_restart(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "tool-output-compaction.jsonl"
    journal = ConversationJournal(journal_path)
    memory = JournaledShortTermMemory(journal, max_tokens=10_000)
    memory.add_user("Inspect the command output and report the error.")
    memory._messages.append(
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool-durable", "name": "run_command", "input": {}}
            ],
            "reasoning_content": "provider-required metadata",
        }
    )
    memory.add_tool_result(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-durable",
                    "content": "first line\n" + "log line\n" * 8_000 + "last line",
                    "is_error": False,
                }
            ],
        }
    )

    outcome = local_compaction_fallback(memory, max_input_tokens=800)

    assert outcome.status == "compacted_local_fallback"
    assert outcome.fallback_result == "compacted_tool_output"
    assert memory.validate_tool_history().ok
    expected_messages = list(memory._messages)
    assert fold_path(journal_path) == (expected_messages, None)
    assert expected_messages[0]["content"] == "Inspect the command output and report the error."
    assert expected_messages[1]["reasoning_content"] == "provider-required metadata"
    assert expected_messages[2]["content"][0]["tool_use_id"] == "tool-durable"
    assert "characters omitted" in expected_messages[2]["content"][0]["content"]
    journal.close()

    reopened_journal = ConversationJournal(journal_path)
    reopened = JournaledShortTermMemory(reopened_journal, max_tokens=10_000)
    assert reopened._messages == expected_messages
    assert reopened.validate_tool_history().ok
    reopened_journal.close()


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
