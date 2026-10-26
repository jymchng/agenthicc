"""Regression tests for displaying a prior session transcript on resume."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.supervised_tasks import SupervisedTasks
from rich.console import Console

from agenthicc.config import AgenthiccConfig
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


@pytest.mark.parametrize("load_fails", [False, True])
@pytest.mark.asyncio
async def test_resumed_tui_replays_transcript_before_accepting_input(
    monkeypatch: pytest.MonkeyPatch,
    load_fails: bool,
) -> None:
    from agenthicc.tui.runtime import CommandBus
    from agenthicc.runners.tui_session import TUISession
    from agenthicc.runners.startup import ReadinessState, StartupCoordinator
    import agenthicc.tui.runtime.session_log as session_log
    from agenthicc.tui.runtime.session_log import TranscriptEntry, TranscriptPage

    history = [
        ConversationEvent("old-user", "user_message", {"text": "from last session"}, 0.5),
        ConversationEvent("old", "text", {"text": "from yesterday"}, 1.0),
    ]
    load_calls: list[dict[str, object]] = []

    def load_history(_session_id: str, **kwargs: object) -> TranscriptPage:
        load_calls.append(kwargs)
        if load_fails:
            raise OSError("injected transcript read failure")
        return TranscriptPage(
            (
                TranscriptEntry(history[0], 0, 1),
                TranscriptEntry(history[1], 1, 2),
            ),
            None,
        )

    monkeypatch.setattr(
        session_log,
        "load_recent_transcript_page",
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
        history: list[str] = []
        on_ready = None

        def set_history(self, messages: list[str]) -> None:
            self.history = messages

        async def run(self) -> None:
            assert self.on_ready is not None
            self.on_ready(True)

    async def processor_run() -> None:
        await __import__("asyncio").Event().wait()

    workspace = Workspace()
    input_session = Input()
    startup = StartupCoordinator()
    startup.begin("time_to_interactive_input")
    config = AgenthiccConfig()
    config.behaviour.resume_transcript_turns = 2
    ctx = SimpleNamespace(
        resumed=True,
        session_id="resumed-session",
        startup=startup,
        command_bus=CommandBus(),
        processor=SimpleNamespace(run=processor_run),
        app_state=AppState.create(),
        cfg=config,
    )
    session = TUISession.__new__(TUISession)
    session._ctx = ctx
    session._workspace = workspace
    session._input_session = input_session
    session._agent_task = None
    session._msg_queue = []
    session._tasks = SupervisedTasks("test tui session")
    session._wire_approval_overlay = lambda: None
    # The post-frame agent runtime and account check are covered elsewhere.
    session._start_agent_runtime = lambda: None
    session._start_account_check = lambda: None
    session._runtime_task = None
    session._account_task = None
    session._ad_task = None
    session._agents_service = None
    input_session.on_ready = lambda interactive: startup.finish(
        "time_to_interactive_input",
        ReadinessState.READY if interactive else ReadinessState.DEGRADED,
    )

    await session.run()

    assert workspace.calls[:2] == ["start", "replay"]
    assert workspace.events == ([] if load_fails else history)
    assert load_calls == [
        {
            "last_turns": 2,
            "max_events": session_log.DEFAULT_TRANSCRIPT_PREVIEW_EVENTS,
            "max_bytes": session_log.DEFAULT_TRANSCRIPT_PREVIEW_BYTES,
            "max_event_bytes": session_log.DEFAULT_TRANSCRIPT_EVENT_BYTES,
        }
    ]
    assert session._input_session.history == ([] if load_fails else ["from last session"])
    assert startup.report("first_frame").state is ReadinessState.READY
    assert startup.report("transcript_preview_read").state is (
        ReadinessState.DEGRADED if load_fails else ReadinessState.READY
    )
    assert startup.report("input_history_projection").state is ReadinessState.READY
    assert startup.report("transcript_preview_render").state is ReadinessState.READY
    assert startup.report("time_to_interactive_input").state is ReadinessState.READY


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
