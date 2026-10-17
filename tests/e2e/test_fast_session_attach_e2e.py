"""End-to-end local terminal flow for fast resume preview and history paging."""

from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.runtime.session_log import (
    SessionEventLog,
    load_recent_transcript_page,
)
from agenthicc.tui.workspace.overlays.transcript_history import TranscriptHistoryOverlay
from agenthicc.tui.workspace.workspace import Workspace

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_resume_preview_pages_old_history_and_keeps_live_events_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("attach-e2e")
    old_events = [
        ConversationEvent(f"old-{index}", "text", {"text": f"old answer {index}"}, index + 1.0)
        for index in range(8)
    ]
    for event in old_events:
        log.append(event)
    log.close()

    page = load_recent_transcript_page(
        "attach-e2e", last_turns=0, max_events=3, max_bytes=4096, max_event_bytes=1024
    )
    assert [event.event_id for event in page.events] == ["old-5", "old-6", "old-7"]

    state = AppState.create()
    console = Console(file=StringIO(), record=True, force_terminal=False, width=100, height=30)
    workspace = Workspace(state, console)
    workspace.start()
    live_log = SessionEventLog("attach-e2e")
    try:
        await workspace.replay_transcript(page.events)
        history_overlay = TranscriptHistoryOverlay("attach-e2e", page, workspace.overlays.hide)
        workspace.overlays.show(history_overlay)
        history_overlay.handle_key(Key.PAGE_UP, "")
        assert [entry.event.event_id for entry in history_overlay._pages[1].entries] == [
            "old-0",
            "old-1",
            "old-2",
            "old-3",
            "old-4",
        ]
        history_overlay.handle_key(Key.ESC, "")
        assert workspace.overlays.active is False

        # Historical browsing and rendering are projection-only. Replaying
        # did not call the durable log subscriber; the live event is appended
        # exactly once through the normal ConversationStore path.
        state.conversation.on_event(live_log.append)
        assert [event.event_id for event in SessionEventLog.load("attach-e2e")] == [
            event.event_id for event in old_events
        ]
        # The IDs seeded by replay suppress a duplicate event arriving through
        # the normal live store path.
        duplicate = state.conversation.append_event(
            "text", {"text": "must not be emitted again"}, event_id="old-7"
        )
        assert duplicate.event_id == "old-7"
        state.conversation.append_event("text", {"text": "live answer once"}, event_id="live-1")
        await asyncio.sleep(0)
        workspace.flush_scroll()

        persisted = SessionEventLog.load("attach-e2e")
        assert [event.event_id for event in persisted] == [
            *(event.event_id for event in old_events),
            "live-1",
        ]
        assert sum(event.event_id == "live-1" for event in persisted) == 1
        rendered = console.export_text()
        assert rendered.count("live answer once") == 1
    finally:
        workspace.stop()
        live_log.close()
