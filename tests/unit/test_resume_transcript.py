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


def test_resume_load_reads_only_the_newest_complete_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_log, "_TAIL_READ_BYTES", 64)
    log = SessionEventLog("large-resume")
    for index in range(40):
        log.append(
            ConversationEvent(
                f"turn-{index}",
                "turn_start",
                {"agent_name": "assistant"},
                float(index),
            )
        )
        log.append(
            ConversationEvent(
                f"text-{index}",
                "text",
                {"text": f"answer-{index}-" + ("x" * 100)},
                float(index) + 0.5,
            )
        )
    log.close()

    recent = SessionEventLog.load("large-resume", rendered=False, last_turns=3)

    assert [event.event_id for event in recent] == [
        "turn-37",
        "text-37",
        "turn-38",
        "text-38",
        "turn-39",
        "text-39",
    ]
    assert all(event.rendered is False for event in recent)


def test_session_log_kind_projection_does_not_load_unrelated_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    monkeypatch.setattr(session_log, "_SESSIONS_DIR", tmp_path / "sessions")
    log = SessionEventLog("usage-projection")
    log.append(ConversationEvent("text", "text", {"text": "not usage"}, 1.0))
    log.append(ConversationEvent("tokens", "tokens", {"input_tokens": 3, "output_tokens": 2}, 2.0))
    log.close()

    events = SessionEventLog.load("usage-projection", kinds={"tokens"})

    assert [event.event_id for event in events] == ["tokens"]


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
    load_calls: list[dict[str, object]] = []

    def load_history(_session_id: str, **kwargs: object) -> list[ConversationEvent]:
        load_calls.append(kwargs)
        return history

    monkeypatch.setattr(
        session_log.SessionEventLog,
        "load",
        staticmethod(load_history),
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
        cfg=SimpleNamespace(behaviour=SimpleNamespace(resume_transcript_turns=2)),
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
    assert load_calls == [{"rendered": False, "last_turns": 2}]


@pytest.mark.asyncio
async def test_workspace_replay_shows_loading_spinner_and_yields_between_chunks() -> None:
    from agenthicc.tui.workspace.workspace import Workspace

    state = AppState.create()
    workspace = Workspace(state, Console(record=True, force_terminal=False))
    observations: list[tuple[bool, int, bool, int]] = []

    class RecordingScroll:
        def replay(self, events, *, continue_group: bool = False) -> None:
            observations.append(
                (
                    state.conversation.transcript_loading(),
                    state.conversation.frame(),
                    continue_group,
                    len(events),
                )
            )

    workspace.scroll = RecordingScroll()  # type: ignore[assignment]
    events = [ConversationEvent(str(index), "text", {"text": "history"}) for index in range(130)]

    await workspace.replay_transcript(events)

    assert [entry[2] for entry in observations] == [False, True, True]
    assert all(entry[0] is True for entry in observations)
    assert [entry[3] for entry in observations] == [64, 64, 2]
    assert len({entry[1] for entry in observations}) == 1
    assert state.conversation.transcript_loading() is False
