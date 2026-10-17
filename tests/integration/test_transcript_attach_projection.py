"""Integration coverage for the bounded visual attach projection (PRD-244)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.memory.journal import fold_path
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.tui.conversation_store import ConversationEvent
from agenthicc.tui.runtime.session_log import (
    SessionEventLog,
    load_recent_transcript_page,
    user_message_history_from_events,
)

pytestmark = pytest.mark.integration


def test_attach_preview_is_bounded_without_trimming_provider_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    journal_path = tmp_path / "conversation-journal.jsonl"
    conversation = SessionConversation.open(
        "attach-projection", max_tokens=100_000, journal_path=journal_path
    )
    for index in range(40):
        conversation.memory.add_user(f"request {index}")
        conversation.memory.add_assistant({"role": "assistant", "content": f"response {index}"})
    provider_messages = conversation.messages

    log = SessionEventLog("attach-projection")
    for index in range(1_000):
        log.append(
            ConversationEvent(
                f"event-{index}",
                "user_message" if index % 2 == 0 else "text",
                {"text": f"event content {index}"},
                float(index),
            )
        )
    log.close()

    preview = load_recent_transcript_page(
        "attach-projection",
        last_turns=0,
        max_events=24,
        max_bytes=8 * 1024,
        max_event_bytes=1024,
    )
    history = user_message_history_from_events(preview.events)

    assert len(preview.entries) <= 24
    assert preview.retained_bytes <= 8 * 1024
    assert preview.scanned_bytes <= 8 * 1024 + 1024
    assert [event.event_id for event in preview.events] == [
        f"event-{index}" for index in range(976, 1_000)
    ]
    assert history == [f"event content {index}" for index in range(976, 1_000, 2)]
    assert fold_path(journal_path) == (provider_messages, None)
    conversation.close()

    resumed = SessionConversation.open(
        "attach-projection", max_tokens=100_000, journal_path=journal_path
    )
    assert resumed.messages == provider_messages
    assert len(resumed.messages) == 80
    resumed.close()


def test_partial_or_corrupt_log_tail_does_not_hide_older_valid_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("corrupt-tail")
    log.append(ConversationEvent("good", "text", {"text": "kept"}, 1.0))
    log.close()
    path = session_log.get_session_log_path("corrupt-tail")
    with path.open("ab") as handle:
        handle.write(b'{"event_id":"partial"')

    page = load_recent_transcript_page("corrupt-tail", max_events=10)
    assert [event.event_id for event in page.events] == ["good"]
    assert "Malformed" in page.notice


def test_history_page_offsets_ignore_new_events_appended_after_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("stable-pages")
    for index in range(6):
        log.append(ConversationEvent(f"old-{index}", "text", {"text": str(index)}, index))
    log.close()
    newest = load_recent_transcript_page("stable-pages", max_events=2)
    assert newest.older_cursor is not None

    live = SessionEventLog("stable-pages")
    live.append(ConversationEvent("live", "text", {"text": "new"}, 10.0))
    live.close()
    older = session_log.load_older_transcript_page(
        "stable-pages", newest.older_cursor, max_events=2
    )
    assert [event.event_id for event in older.events] == ["old-2", "old-3"]
    assert all(event.event_id != "live" for event in older.events)
