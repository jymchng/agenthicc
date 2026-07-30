"""Regression tests for displaying a prior session transcript on resume."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.runtime.session_log import SessionEventLog, load_user_message_history
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.unit


def test_scroll_appender_replays_history_without_store_or_log_mutation() -> None:
    state = AppState.create()
    console = Console(record=True, force_terminal=False)
    appender = ScrollBufferAppender(state, console)
    observed: list[ConversationEvent] = []
    state.conversation.on_event(observed.append)

    history = [
        ConversationEvent("old-turn", "turn_start", {"agent_name": "assistant"}, 1.0),
        ConversationEvent("old-user", "user_message", {"text": "previous question"}, 2.0),
        ConversationEvent("old-text", "text", {"text": "previous answer"}, 3.0),
    ]
    appender.replay(history)
    appender._flush_batch()

    rendered = console.export_text()
    assert "previous question" in rendered
    assert "previous answer" in rendered
    assert all(event.rendered for event in history)
    assert observed == []


def test_resume_load_can_return_unrendered_events_without_changing_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("resume")
    log.append(ConversationEvent("event-1", "text", {"text": "old"}, 1.0))
    log.close()

    normal = SessionEventLog.load("resume")
    replay = SessionEventLog.load("resume", rendered=False)
    assert normal[0].rendered is True
    assert replay[0].rendered is False
    assert replay[0].payload == {"text": "old"}


def test_resumed_input_history_uses_persisted_user_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import agenthicc.tui.runtime.session_log as session_log
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime import CommandBus

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("resume")
    log.append(ConversationEvent("user-1", "user_message", {"text": "first request"}, 1.0))
    log.append(ConversationEvent("answer", "text", {"text": "answer"}, 2.0))
    log.append(ConversationEvent("user-2", "user_message", {"text": "second request"}, 3.0))
    log.close()

    session = UnifiedInputSession(
        AppState.create(), CommandBus(), history=load_user_message_history("resume")
    )
    asyncio.run(session._dispatch(Key.UP, ""))

    assert session._buf.text == "second request"


@pytest.mark.asyncio
async def test_resumed_tui_replays_transcript_before_accepting_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.tui.runtime import CommandBus
    from agenthicc.runners.tui_session import TUISession
    import agenthicc.tui.runtime.session_log as session_log

    history = [ConversationEvent("old", "text", {"text": "from yesterday"}, 1.0)]
    monkeypatch.setattr(
        session_log.SessionEventLog,
        "load",
        staticmethod(lambda _session_id, *, rendered=True: history),
    )

    class Workspace:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.events: list[ConversationEvent] = []

        def start(self) -> None:
            self.calls.append("start")

        async def replay_transcript(self, events: list[ConversationEvent]) -> None:
            self.calls.append("replay")
            self.events.extend(events)

        def stop(self) -> None:
            self.calls.append("stop")

    class Input:
        async def run(self) -> None:
            return

    async def processor_run() -> None:
        await __import__("asyncio").Event().wait()

    workspace = Workspace()
    ctx = SimpleNamespace(
        resumed=True,
        session_id="resumed-session",
        command_bus=CommandBus(),
        processor=SimpleNamespace(run=processor_run),
        app_state=AppState.create(),
    )
    session = TUISession.__new__(TUISession)
    session._ctx = ctx
    session._workspace = workspace
    session._input_session = Input()
    session._agent_task = None
    session._msg_queue = []
    session._wire_approval_overlay = lambda: None
    session._notify_incomplete_workflow = lambda: None
    session._maybe_resume_interrupted_turn = lambda: None

    await session.run()

    assert workspace.calls[:2] == ["start", "replay"]
    assert workspace.events == history
