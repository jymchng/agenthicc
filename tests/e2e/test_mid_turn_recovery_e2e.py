"""End-to-end durable-session coverage for PRD-182.

The provider-step transport behavior is exercised with a fault-injected
lauren-ai runner. This scenario covers the session boundary around it: a
failed logical turn is closed without erasing its committed prefix, the
journal is reopened as a fresh process would reopen it, and a follow-up user
message is appended to that same conversation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.memory.journal import fold_resume_state
from agenthicc.runners.session_conversation import SessionConversation

pytestmark = pytest.mark.e2e


def test_failed_turn_survives_restart_and_follow_up_message(tmp_path: Path) -> None:
    journal_path = tmp_path / "conversation-journal.jsonl"
    conversation = SessionConversation.open(
        "mid-turn-session",
        max_tokens=32_000,
        journal_path=journal_path,
    )
    try:
        conversation.memory.add_user("inspect the repository")
        conversation.memory.add_assistant(
            {"role": "assistant", "content": "I inspected the repository layout."}
        )
        conversation.journal.turn_started(
            "turn-1",
            "continue the inspection",
            base_count=2,
            conversation_id=conversation.conversation_id,
        )
        conversation.journal.step_started(
            "turn-1",
            "turn-1:0",
            "turn-1:0:a",
            step_index=0,
            base_cursor=conversation.cursor,
        )
        conversation.memory.add_assistant(
            {"role": "assistant", "content": "The first provider step completed."}
        )
        conversation.journal.step_committed(
            "turn-1",
            "turn-1:0",
            step_index=0,
            message_count=len(conversation.messages),
        )
        conversation.journal.step_started(
            "turn-1",
            "turn-1:1",
            "turn-1:1:a",
            step_index=1,
            base_cursor=conversation.cursor,
        )
        conversation.journal.step_interrupted(
            "turn-1",
            "turn-1:1",
            "turn-1:1:a",
            error_kind="TransientTransportError",
            partial_chars=18,
            retryable=True,
        )
        conversation.memory.record_partial_fragment(
            "turn-1",
            "incomplete provider text",
            step_id="turn-1:1",
            attempt_id="turn-1:1:a",
        )
        conversation.journal.turn_failed(
            "turn-1",
            last_committed_step="turn-1:0",
            cursor=conversation.cursor,
            error_kind="TransientTransportError",
            retryable=True,
        )
    finally:
        conversation.close()

    reopened = SessionConversation.open(
        "mid-turn-session",
        max_tokens=32_000,
        journal_path=journal_path,
    )
    try:
        assert reopened.conversation_id == "mid-turn-session"
        assert [message["content"] for message in reopened.messages] == [
            "inspect the repository",
            "I inspected the repository layout.",
            "The first provider step completed.",
        ]
        assert fold_resume_state(journal_path) is None

        reopened.memory.add_user("what was completed?")
        assert reopened.messages[-1] == {
            "role": "user",
            "content": "what was completed?",
        }
        entries = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(entry["kind"] == "partial_fragment" for entry in entries)
        assert entries[-1]["kind"] == "append"
    finally:
        reopened.close()
