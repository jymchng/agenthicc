"""Bounded tail/page reader and on-demand transcript overlay tests."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.commands.builtins import _cmd_history
from agenthicc.commands.command import CommandContext
from agenthicc.config import AgenthiccConfig
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.runtime.session_log import (
    SessionEventLog,
    TranscriptEntry,
    TranscriptPage,
    load_older_transcript_page,
    load_recent_transcript_page,
    load_transcript_entry,
    user_message_history_from_events,
)
from agenthicc.tui.workspace.overlays.transcript_history import TranscriptHistoryOverlay

pytestmark = pytest.mark.unit


def _append(log: SessionEventLog, event_id: str, kind: str, payload: dict[str, object]) -> None:
    log.append(ConversationEvent(event_id, kind, payload, float(len(event_id))))


def test_preview_pages_are_byte_event_bounded_chronological_and_zero_turns_stay_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("bounded")
    for index in range(30):
        _append(log, f"event-{index}", "text", {"text": f"message-{index}"})
    log.close()

    newest = load_recent_transcript_page(
        "bounded", last_turns=0, max_events=5, max_bytes=4096, max_event_bytes=1024
    )
    assert [entry.event.event_id for entry in newest.entries] == [
        "event-25",
        "event-26",
        "event-27",
        "event-28",
        "event-29",
    ]
    assert newest.retained_bytes <= 4096
    assert len(newest.entries) <= 5
    assert newest.older_cursor is not None

    older = load_older_transcript_page(
        "bounded", newest.older_cursor, max_events=5, max_bytes=4096, max_event_bytes=1024
    )
    assert [entry.event.event_id for entry in older.entries] == [
        "event-20",
        "event-21",
        "event-22",
        "event-23",
        "event-24",
    ]
    assert not {entry.event.event_id for entry in newest.entries} & {
        entry.event.event_id for entry in older.entries
    }


def test_page_cursor_remains_stable_when_live_records_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("append-stable")
    for index in range(10):
        _append(log, f"old-{index}", "text", {"text": str(index)})
    log.close()

    newest = load_recent_transcript_page("append-stable", max_events=3)
    assert newest.older_cursor is not None
    live = SessionEventLog("append-stable")
    _append(live, "new-live", "text", {"text": "arrived during browsing"})
    live.close()

    older = load_older_transcript_page("append-stable", newest.older_cursor, max_events=3)
    ids = [entry.event.event_id for entry in older.entries]
    assert ids == ["old-4", "old-5", "old-6"]
    assert "new-live" not in ids


def test_preview_turn_limit_keeps_latest_complete_turn_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("turns")
    for turn in range(5):
        _append(log, f"turn-{turn}", "turn_start", {"agent_name": "assistant"})
        _append(log, f"user-{turn}", "user_message", {"text": f"request-{turn}"})
        _append(log, f"answer-{turn}", "text", {"text": f"response-{turn}"})
    log.close()

    page = load_recent_transcript_page("turns", last_turns=2, max_events=30)
    assert [entry.event.event_id for entry in page.entries] == [
        "turn-3",
        "user-3",
        "answer-3",
        "turn-4",
        "user-4",
        "answer-4",
    ]
    assert user_message_history_from_events(page.events) == ["request-3", "request-4"]


def test_oversized_event_is_a_bounded_marker_and_can_be_inspected_on_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("oversized")
    _append(log, "before", "text", {"text": "before"})
    full_payload = "start " + "x" * 10_000 + " end"
    _append(log, "too-large", "text", {"text": full_payload})
    log.close()

    page = load_recent_transcript_page(
        "oversized", max_events=10, max_bytes=4096, max_event_bytes=1024
    )
    assert len(page.entries) == 1
    marker = page.entries[0]
    assert marker.oversized is True
    assert "omitted from preview" in str(marker.event.payload["text"])
    restored = load_transcript_entry(marker)
    assert restored is not None
    assert restored.event_id == "too-large"
    assert restored.payload["text"] == full_payload


def test_exceptionally_large_single_line_never_reads_an_unbounded_preview_and_can_reach_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_log, "_TRANSCRIPT_SCAN_CHUNK_BYTES", 128)
    log = SessionEventLog("huge-line")
    _append(log, "older-1", "text", {"text": "before huge event"})
    _append(log, "older-2", "text", {"text": "also before"})
    _append(log, "huge", "text", {"text": "z" * 20_000})
    log.close()

    page = load_recent_transcript_page(
        "huge-line", max_events=10, max_bytes=1024, max_event_bytes=256
    )
    assert page.scanned_bytes <= 1024 + 256
    assert page.entries[-1].partial_line is True
    cursor = page.older_cursor
    assert cursor is not None and cursor.skip_oversized_line

    # Bounded requests advance backwards through the one very large line. They
    # eventually reach prior complete records rather than hanging at a repeat
    # cursor or allocating the oversized payload during attach.
    for _ in range(200):
        older = load_older_transcript_page(
            "huge-line", cursor, max_events=10, max_bytes=1024, max_event_bytes=256
        )
        assert older.scanned_bytes <= 1024 + 256
        if older.entries:
            break
        assert older.older_cursor is not None
        assert older.older_cursor.offset < cursor.offset
        cursor = older.older_cursor
    else:
        pytest.fail("bounded history paging did not pass the oversized line")

    assert [entry.event.event_id for entry in older.entries] == ["older-1", "older-2"]


def test_corrupt_trailing_jsonl_is_skipped_and_history_overlay_pages_without_live_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("overlay")
    for index in range(7):
        _append(log, f"old-{index}", "text", {"text": f"event {index}"})
    log.close()
    path = session_log.get_session_log_path("overlay")
    with path.open("ab") as handle:
        handle.write(b"{malformed trailing record\n")

    page = load_recent_transcript_page("overlay", max_events=3)
    assert [entry.event.event_id for entry in page.entries] == ["old-4", "old-5", "old-6"]
    closed: list[bool] = []
    overlay = TranscriptHistoryOverlay("overlay", page, lambda: closed.append(True))
    overlay.set_viewport(100, 20)
    overlay.handle_key(Key.PAGE_UP, "")
    assert overlay._page_index == 1
    assert [entry.event.event_id for entry in overlay._pages[1].entries] == [
        "old-0",
        "old-1",
        "old-2",
        "old-3",
    ]
    overlay.handle_key(Key.ENTER, "")
    assert overlay._detail is not None
    overlay.handle_key(Key.ESC, "")
    assert overlay._detail is None
    overlay.handle_key(Key.ESC, "")
    assert closed == [True]


@pytest.mark.asyncio
async def test_history_input_ready_callback_marks_noninteractive_and_interactive_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime import CommandBus

    state = AppState.create()
    result: list[bool] = []

    class Backend:
        interactive = False

        def __init__(self) -> None:
            self.reporting: list[dict[str, bool | None]] = []

        def is_interactive(self) -> bool:
            return self.interactive

        def enter_raw_mode(self):
            return nullcontext()

        def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
            self.reporting.append({"focus": focus, "mouse": mouse})

        def read_key(self):
            return Key.CTRL_D, ""

    backend = Backend()
    monkeypatch.setattr(
        "agenthicc.tui.terminal.backend.get_backend",
        lambda: backend,
    )
    session = UnifiedInputSession(state, CommandBus(), on_ready=result.append)
    await session.run()
    backend.interactive = True
    await session.run()
    assert result == [False, True]
    assert backend.reporting == [{"focus": True, "mouse": False}]


def test_transcript_history_detail_serializes_event_without_hidden_or_private_fields() -> None:
    event = ConversationEvent(
        "event",
        "text",
        {
            "text": "visible",
            "reasoning_content": "hidden",
            "_private": "private",
            "nested": {"analysis": "nested secret", "public": "nested visible"},
        },
    )
    page = TranscriptPage((TranscriptEntry(event, 0, 1),), None)
    overlay = TranscriptHistoryOverlay("session", page, lambda: None)
    overlay.handle_key(Key.ENTER, "")
    assert overlay._detail is not None
    detail = "\n".join(overlay._detail)
    assert "visible" in detail
    assert "hidden" not in detail
    assert "private" not in detail
    assert "nested secret" not in detail
    assert "nested visible" in detail


def test_oversized_event_detail_is_hard_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("hard-detail-limit")
    _append(log, "giant", "text", {"text": "x" * (256 * 1024)})
    log.close()

    page = load_recent_transcript_page(
        "hard-detail-limit", max_events=2, max_bytes=1024, max_event_bytes=256
    )
    assert len(page.entries) == 1
    overlay = TranscriptHistoryOverlay("hard-detail-limit", page, lambda: None)
    overlay.handle_key(Key.ENTER, "")
    assert overlay._detail is not None
    detail = "\n".join(overlay._detail)
    assert "inspection limit" in detail
    assert len(detail) < 1_000


def test_history_command_opens_bounded_overlay_for_the_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("history-command")
    for index in range(200):
        _append(log, f"event-{index}", "text", {"text": f"text {index}"})
    log.close()

    overlays: list[object] = []
    context = CommandContext(
        text="/history",
        args="",
        model="offline-test",
        console=Console(record=True, force_terminal=False),
        config=AgenthiccConfig(),
        session_id="history-command",
        set_pending_menu=overlays.append,
    )
    assert _cmd_history(context)
    assert len(overlays) == 1
    assert isinstance(overlays[0], TranscriptHistoryOverlay)
    assert (
        len(overlays[0]._pages[0].entries) <= context.config.behaviour.resume_transcript_max_events
    )
