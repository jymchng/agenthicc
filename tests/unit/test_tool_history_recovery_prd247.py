"""Regression coverage for non-fatal session opening after history repair fails."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.memory.journal import ConversationJournal
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.memory.tool_history import ToolHistoryRecoveryRequired

pytestmark = pytest.mark.unit


def _call_hash(call_id: str) -> str:
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:12]


def _write_unrepairable_journal(path: Path) -> bytes:
    records = [
        {"seq": 0, "kind": "append", "message": {"role": "user", "content": "run phase"}},
        {
            "seq": 1,
            "kind": "append",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {}}],
            },
        },
        {"seq": 2, "kind": "append", "message": {"role": "user", "content": "continue"}},
        {
            "seq": 3,
            "kind": "append",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "SENSITIVE_RESULT_SENTINEL_A",
                    }
                ],
            },
        },
        {
            "seq": 4,
            "kind": "append",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "SENSITIVE_RESULT_SENTINEL_B",
                    }
                ],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path.read_bytes()


@pytest.mark.asyncio
async def test_session_conversation_opens_read_only_when_repair_candidate_is_invalid(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversation.jsonl"
    original_journal = _write_unrepairable_journal(path)

    conversation = SessionConversation.open(
        "session-prd247",
        max_tokens=10_000,
        journal_path=path,
    )
    try:
        state = conversation.tool_history_recovery
        assert state.status in {"repair_required", "irrecoverable"}
        assert state.provider_ready is False
        assert "non_adjacent_results" in state.issue_codes
        assert conversation.memory._messages[1]["content"][0]["id"] == "call-1"
        assert path.read_bytes() == original_journal

        with pytest.raises(ToolHistoryRecoveryRequired) as caught:
            conversation.memory.ensure_provider_ready()
        assert "non_adjacent_results" in caught.value.state.issue_codes
        assert "SENSITIVE_RESULT_SENTINEL" not in str(caught.value)
        assert path.read_bytes() == original_journal

        # Provider execution is guarded before the runner can construct or
        # invoke a transport with this invalid session history.
        from agenthicc.runners.agent_turn import AgentTurnRunner

        runner = object.__new__(AgentTurnRunner)
        runner._ctx = SimpleNamespace(session_memory=conversation.memory, conv_store=None)
        with pytest.raises(ToolHistoryRecoveryRequired):
            await runner._stream(object(), "must not be sent", object())
        assert path.read_bytes() == original_journal
    finally:
        conversation.close()


def test_failed_repair_does_not_append_candidate_or_rollback_records(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    original_journal = _write_unrepairable_journal(path)
    journal = ConversationJournal(path)
    try:
        memory = JournaledShortTermMemory(journal)
        assert memory.tool_history_recovery.provider_ready is False
        assert path.read_bytes() == original_journal
        state = memory.retry_tool_history_recovery()
        assert state.status in {"repair_required", "irrecoverable"}
        assert path.read_bytes() == original_journal
    finally:
        journal.close()


def test_missing_interrupted_result_is_repaired_before_following_user_message(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversation.jsonl"
    records = [
        {"seq": 0, "kind": "append", "message": {"role": "user", "content": "inspect"}},
        {
            "seq": 1,
            "kind": "append",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call-missing", "name": "Read", "input": {}}
                ],
            },
        },
        {
            "seq": 2,
            "kind": "tool_exchange_started",
            "exchange_id": "exchange-missing",
            "call_count": 1,
            "call_ids": [_call_hash("call-missing")],
        },
        {
            "seq": 3,
            "kind": "append",
            "message": {"role": "user", "content": "continue after interruption"},
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    conversation = SessionConversation.open(
        "session-prd247-missing-result",
        max_tokens=10_000,
        journal_path=path,
    )
    try:
        assert conversation.tool_history_recovery.status == "repaired"
        assert conversation.memory.validate_tool_history().ok
        assert conversation.messages[3]["content"] == "continue after interruption"
        synthetic = conversation.messages[2]["content"][0]
        assert synthetic["tool_use_id"] == "call-missing"
        assert synthetic["is_error"] is True
        assert "interrupted" in synthetic["content"]
    finally:
        conversation.close()


def test_non_adjacent_result_and_missing_sibling_are_repaired_without_lifecycle_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unproven-interruption.jsonl"
    records = [
        {"seq": 0, "kind": "append", "message": {"role": "user", "content": "inspect"}},
        {
            "seq": 1,
            "kind": "append",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "late-call", "name": "Read", "input": {}}],
            },
        },
        {"seq": 2, "kind": "append", "message": {"role": "user", "content": "continue"}},
        {
            "seq": 3,
            "kind": "append",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "late-call", "content": "late"}],
            },
        },
        {
            "seq": 4,
            "kind": "append",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "unproven", "name": "Read", "input": {}}],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    original_journal = path.read_bytes()
    original_journal = path.read_bytes()
    conversation = SessionConversation.open(
        "session-prd247-unproven-missing",
        max_tokens=10_000,
        journal_path=path,
    )
    conversation_open = True
    try:
        # The source has a tool result stranded after a user continuation and
        # a second persisted tool call whose result was lost in the crash
        # window before tool_exchange_started. Recovery reorders the exact
        # existing result and adds an explicit interruption error for the
        # second call, so /workflow resume can safely use the transcript.
        assert conversation.tool_history_recovery.provider_ready is True
        assert conversation.memory.validate_tool_history().ok
        assert conversation.messages[2]["content"][0]["tool_use_id"] == "late-call"
        assert conversation.messages[3]["content"] == "continue"
        synthetic = conversation.messages[5]["content"][0]
        assert synthetic["tool_use_id"] == "unproven"
        assert synthetic["is_error"] is True
        assert "interrupted" in synthetic["content"]
        assert path.read_bytes() != original_journal

        # The repaired projection is durable and a subsequent resume does not
        # synthesize a second result or alter the message sequence.
        messages = conversation.messages
        conversation.close()
        conversation_open = False
        resumed = SessionConversation.open(
            "session-prd247-unproven-missing",
            max_tokens=10_000,
            journal_path=path,
        )
        try:
            assert resumed.tool_history_recovery.provider_ready is True
            assert resumed.messages == messages
        finally:
            resumed.close()
    finally:
        if conversation_open:
            conversation.close()


def test_non_adjacent_code_with_only_an_unrelated_later_result_is_repaired(
    tmp_path: Path,
) -> None:
    """An unrelated later result must not strand the earlier unanswered call."""
    path = tmp_path / "unrelated-later-result.jsonl"
    records = [
        {"seq": 0, "kind": "append", "message": {"role": "user", "content": "inspect"}},
        {
            "seq": 1,
            "kind": "append",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-a", "name": "Read", "input": {}}],
            },
        },
        {
            "seq": 2,
            "kind": "append",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-b", "name": "Read", "input": {}}],
            },
        },
        {
            "seq": 3,
            "kind": "append",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-b", "content": "result B"}
                ],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    conversation = SessionConversation.open(
        "session-prd247-unrelated-result",
        max_tokens=10_000,
        journal_path=path,
    )
    try:
        assert conversation.tool_history_recovery.status == "repaired"
        assert conversation.memory.validate_tool_history().ok
        synthetic = conversation.messages[2]["content"][0]
        assert synthetic["tool_use_id"] == "call-a"
        assert synthetic["is_error"] is True
        assert "interrupted" in synthetic["content"]
        assert conversation.messages[3]["content"][0]["id"] == "call-b"
        assert conversation.messages[4]["content"][0]["tool_use_id"] == "call-b"
    finally:
        conversation.close()


def test_single_tool_result_append_is_durable_and_does_not_need_synthetic_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "single-result.jsonl"
    journal = ConversationJournal(path)
    memory = JournaledShortTermMemory(journal)
    memory.add_user("run one tool")
    memory.add_assistant(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "single-call", "name": "Read", "input": {}}],
        }
    )
    memory.add_tool_result(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "single-call",
                    "content": "complete",
                }
            ],
        }
    )
    expected = list(memory._messages)
    journal.close()

    conversation = SessionConversation.open(
        "session-prd247-single-result",
        max_tokens=10_000,
        journal_path=path,
    )
    try:
        assert conversation.tool_history_recovery.provider_ready is True
        assert conversation.messages == expected
    finally:
        conversation.close()


def test_non_adjacent_batch_with_missing_sibling_repairs_as_one_valid_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversation.jsonl"
    messages = [
        {"seq": 0, "kind": "append", "message": {"role": "user", "content": "inspect both"}},
        {
            "seq": 1,
            "kind": "append",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call-a", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "call-b", "name": "Read", "input": {}},
                ],
            },
        },
        {
            "seq": 2,
            "kind": "tool_exchange_started",
            "exchange_id": "exchange-batch",
            "call_count": 2,
            "call_ids": [_call_hash("call-a"), _call_hash("call-b")],
        },
        {"seq": 3, "kind": "append", "message": {"role": "user", "content": "continue"}},
        {
            "seq": 4,
            "kind": "append",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-a", "content": "result A"}
                ],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in messages), encoding="utf-8")

    conversation = SessionConversation.open(
        "session-prd247-mixed-results",
        max_tokens=10_000,
        journal_path=path,
    )
    try:
        assert conversation.tool_history_recovery.status == "repaired"
        assert conversation.memory.validate_tool_history().ok
        adjacent = conversation.messages[2]["content"]
        assert [block["tool_use_id"] for block in adjacent] == ["call-a", "call-b"]
        assert adjacent[0]["content"] == "result A"
        assert adjacent[1]["is_error"] is True
        assert conversation.messages[3]["content"] == "continue"
    finally:
        conversation.close()


def test_corrupt_journal_tail_is_reported_and_never_provider_ready(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    raw = (
        json.dumps({"seq": 0, "kind": "append", "message": {"role": "user", "content": "kept"}})
        + "\n{partial-json\n"
        + json.dumps({"seq": 1, "kind": "append", "message": {"role": "user", "content": "tail"}})
        + "\n"
    ).encode()
    path.write_bytes(raw)

    conversation = SessionConversation.open(
        "session-prd247-corrupt-journal",
        max_tokens=10_000,
        journal_path=path,
    )
    try:
        state = conversation.tool_history_recovery
        assert state.status == "journal_corrupt"
        assert state.provider_ready is False
        assert state.issue_codes == ("corrupt_trailing_records:2",)
        assert conversation.messages == [{"role": "user", "content": "kept"}]
        with pytest.raises(ToolHistoryRecoveryRequired):
            conversation.memory.ensure_provider_ready()
        assert conversation.retry_tool_history_recovery().status == "journal_corrupt"
        assert path.read_bytes() == raw
    finally:
        conversation.close()


def test_history_repair_command_reports_blocked_state_without_content(tmp_path: Path) -> None:
    from io import StringIO

    from agenthicc.commands.builtins import _cmd_history
    from agenthicc.commands.command import CommandContext
    from agenthicc.config import AgenthiccConfig
    from agenthicc.memory.tool_history import ToolHistoryRecoveryState
    from rich.console import Console

    output = StringIO()
    context = CommandContext(
        text="/history repair",
        args="repair",
        model="test-model",
        console=Console(file=output, force_terminal=False),
        config=AgenthiccConfig(),
        repair_session_history=lambda: ToolHistoryRecoveryState(
            "irrecoverable", ("duplicate_result_id",), True
        ),
    )

    assert _cmd_history(context) is True
    rendered = output.getvalue()
    assert "duplicate_result_id" in rendered
    assert "No journal contents were discarded" in rendered
    assert "SENSITIVE_RESULT_SENTINEL" not in rendered


def test_history_repair_command_recovers_non_adjacent_call_with_unrelated_later_result(
    tmp_path: Path,
) -> None:
    from io import StringIO

    from agenthicc.commands.builtins import _cmd_history
    from agenthicc.commands.command import CommandContext
    from agenthicc.config import AgenthiccConfig
    from agenthicc.memory.tool_history import ToolHistoryRecoveryState
    from rich.console import Console

    conversation = SessionConversation.open(
        "session-prd247-history-repair-command",
        max_tokens=10_000,
        journal_path=tmp_path / "history-repair-command.jsonl",
    )
    conversation.memory.restore(
        [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "first", "name": "Read", "input": {}}],
            },
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "second", "name": "Read", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "second", "content": "ok"}],
            },
        ]
    )
    conversation.memory._tool_history_recovery = ToolHistoryRecoveryState(
        "irrecoverable", ("non_adjacent_results",), True
    )
    output = StringIO()
    context = CommandContext(
        text="/history repair",
        args="repair",
        model="test-model",
        console=Console(file=output, force_terminal=False),
        config=AgenthiccConfig(),
        repair_session_history=conversation.retry_tool_history_recovery,
    )
    try:
        assert _cmd_history(context) is True
        assert conversation.tool_history_recovery.provider_ready is True
        assert conversation.memory.validate_tool_history().ok
        assert "Conversation history was repaired and validated" in output.getvalue()
    finally:
        conversation.close()
