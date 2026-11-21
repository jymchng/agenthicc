"""``/background``: what the user sees while it waits and once it has happened.

While a tool call runs the hand-off waits for it to end, and a blinking blue
``● Backgrounding after tool call ends…`` says so. Once the session is handed over
the turn it cancelled must not read as a failure: the transcript ends with
``● Successfully backgrounded``, not ``✗ cancelled`` (and not ``Interrupted``).
"""

from __future__ import annotations

import asyncio
import time
from io import StringIO

import pytest
from rich.console import Console
from rich.text import Text

from agenthicc.config import UISettings
from agenthicc.tui.conversation_store import (
    AppState,
    ConversationEvent,
    ConversationStore,
    RunningTool,
)
from agenthicc.tui.theme import ASCII_GLYPHS, BUILTIN_THEMES, glyphs, palette, use_theme
from agenthicc.tui.workspace.announcer import Announcer
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.workspace.running_rows import BLINK_FRAMES, handoff_row, running_rows

pytestmark = pytest.mark.unit


def _plain(markup: str) -> str:
    return Text.from_markup(markup).plain


def _state(*, pending: bool = True, ui: UISettings | None = None) -> AppState:
    state = AppState.create()
    if ui is not None:
        state.ui.set(ui)
    state.conversation.handoff_pending.set(pending)
    return state


# ── the live row ─────────────────────────────────────────────────────────────


def test_the_row_says_the_handoff_waits_for_the_tool_call() -> None:
    assert _plain(handoff_row(_state())) == "● Backgrounding after tool call ends…"


def test_the_dot_blinks_between_info_blue_and_a_still_dim_dot() -> None:
    state = _state()
    state.conversation.frame.set(0)
    on = handoff_row(state)
    state.conversation.frame.set(BLINK_FRAMES)
    off = handoff_row(state)
    assert on != off and _plain(on) == _plain(off)
    assert palette().info in on and palette().info not in off
    state.conversation.frame.set(2 * BLINK_FRAMES)
    assert handoff_row(state) == on  # and it comes back on


@pytest.mark.parametrize("ui", [UISettings(reduced_motion=True), UISettings(screen_reader=True)])
def test_reduced_motion_and_screen_readers_keep_the_dot_on(ui: UISettings) -> None:
    state = _state(ui=ui)
    rows = set()
    for frame in range(0, 4 * BLINK_FRAMES, 3):
        state.conversation.frame.set(frame)
        rows.add(handoff_row(state))
    assert len(rows) == 1


def test_ascii_glyphs_draw_a_star_and_three_dots() -> None:
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    assert _plain(handoff_row(_state())) == "* Backgrounding after tool call ends..."


def test_the_row_sits_under_the_running_tool_calls_it_is_waiting_for() -> None:
    state = _state()
    state.conversation.start_running_tool(RunningTool("t1", "$", "make test", time.time() - 3))
    rows = [_plain(row) for row in running_rows(state, 80)]
    assert rows[0] == "" and rows[-1] == ""
    assert rows[1].startswith("● $ make test")
    assert rows[2] == "● Backgrounding after tool call ends…"


def test_nothing_is_drawn_when_no_handoff_waits() -> None:
    assert running_rows(_state(pending=False), 80) == []


def test_the_frame_clock_runs_while_the_handoff_waits_so_the_dot_can_blink() -> None:
    conv = ConversationStore()
    conv.tick()
    assert conv.frame() == 0  # idle: no frames
    conv.handoff_pending.set(True)
    conv.tick()
    assert conv.frame() == 1


def test_a_screen_reader_hears_it_once() -> None:
    state = AppState.create()
    console = Console(file=StringIO(), width=80, color_system=None)
    appender = ScrollBufferAppender(state, console)
    said: list[str] = []
    appender.announce = said.append  # type: ignore[method-assign]
    announcer = Announcer(state, appender)
    announcer.start()
    try:
        said.clear()
        state.conversation.handoff_pending.set(True)
        state.conversation.handoff_pending.set(False)
    finally:
        announcer.stop()
    assert said == ["Backgrounding after the tool call ends."]


# ── the transcript row ───────────────────────────────────────────────────────


def _appender(state: AppState | None = None) -> tuple[ScrollBufferAppender, Console]:
    console = Console(file=StringIO(), record=True, width=80, color_system=None, highlight=False)
    return ScrollBufferAppender(state or AppState.create(), console), console


def _feed(appender: ScrollBufferAppender, kind: str, payload: dict[str, object]) -> None:
    appender._render_one(
        ConversationEvent(
            event_id=f"{kind}-1", kind=kind, payload=payload, timestamp=1_767_243_843.0
        )
    )


def test_a_handed_off_session_is_confirmed_with_its_id_and_status() -> None:
    appender, console = _appender()
    _feed(appender, "background_handoff", {"session_id": "6b9f7614-8ca1-4d2e", "status": "running"})
    assert console.export_text().splitlines()[0] == (
        f"{glyphs().bullet} Successfully backgrounded {glyphs().sep} 6b9f7614-8ca (running)"
    )


def test_the_confirmation_dot_is_the_success_colour() -> None:
    appender, console = _appender()
    _feed(appender, "background_handoff", {"session_id": "abc", "status": "starting"})
    rich_console = Console(file=StringIO(), width=80, color_system="truecolor", record=True)
    capture = ScrollBufferAppender(AppState.create(), rich_console)
    _feed(capture, "background_handoff", {"session_id": "abc", "status": "starting"})
    red, green, blue = (int(palette().success.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    assert f"38;2;{red};{green};{blue}" in rich_console.export_text(styles=True)


def test_a_screen_reader_gets_a_word_not_a_bullet() -> None:
    state = AppState.create()
    state.ui.set(UISettings(screen_reader=True))
    appender, console = _appender(state)
    _feed(appender, "background_handoff", {"session_id": "abc", "status": "running"})
    assert console.export_text().splitlines()[0].startswith(" Done: Successfully backgrounded")


def test_one_blank_line_follows_the_confirmation() -> None:
    appender, console = _appender()
    _feed(appender, "background_handoff", {"session_id": "abc", "status": "running"})
    _feed(appender, "system", {"text": "next"})
    rows = console.export_text().splitlines()
    assert rows[1] == "" and rows[2] != ""


# ── the order of what the cancelled turn writes ──────────────────────────────


async def _hold_turn(session: object) -> asyncio.Task[None]:
    """Start the session's real turn body around a model call that never returns."""
    started = asyncio.Event()

    async def run_turn(*_args: object, **_kwargs: object) -> None:
        session._ctx.app_state.conversation.begin_turn("agent", "turn-1")  # type: ignore[attr-defined]
        started.set()
        await asyncio.Event().wait()

    session.run_turn = run_turn  # type: ignore[attr-defined]
    task = asyncio.create_task(session.agent_task_body("work", turn_id="turn-1"))  # type: ignore[attr-defined]
    session._agent_task = task  # type: ignore[attr-defined]
    await asyncio.wait_for(started.wait(), 5)
    return task


def _kinds(events: list[ConversationEvent]) -> list[str]:
    return [event.kind for event in events]


async def test_a_handed_off_turn_ends_without_a_cancelled_error_and_says_so_last() -> None:
    from tests.unit.test_tui_session_coverage import _make_session

    session, ctx, _workspace, _input = _make_session()
    conv = ctx.app_state.conversation
    events: list[ConversationEvent] = []
    conv.on_event(events.append)
    task = await _hold_turn(session)

    # What integration._handoff does once the worker has started: record success, cancel.
    session._background_handoff_state = "started"
    session.announce_backgrounded("6b9f7614-8ca1-4d2e", "running")
    assert "background_handoff" not in _kinds(events)  # the turn is still unwinding
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    kinds = _kinds(events)
    assert "error" not in kinds  # no "✗ cancelled"
    ending = [e for e in events if e.kind == "turn_complete"]
    assert len(ending) == 1 and "interrupted" not in ending[0].payload  # not "Interrupted"
    assert kinds[-1] == "background_handoff"  # after the turn's own closing rows
    assert events[-1].payload == {"session_id": "6b9f7614-8ca1-4d2e", "status": "running"}


async def test_a_turn_the_user_stops_still_reads_as_interrupted() -> None:
    from tests.unit.test_tui_session_coverage import _make_session

    session, ctx, _workspace, _input = _make_session()
    events: list[ConversationEvent] = []
    ctx.app_state.conversation.on_event(events.append)
    task = await _hold_turn(session)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    ending = [e for e in events if e.kind == "turn_complete"]
    assert len(ending) == 1 and ending[0].payload.get("interrupted") is True
    assert "background_handoff" not in _kinds(events)


async def test_a_hand_off_that_did_not_start_is_not_confirmed() -> None:
    from tests.unit.test_tui_session_coverage import _make_session

    session, ctx, _workspace, _input = _make_session()
    events: list[ConversationEvent] = []
    ctx.app_state.conversation.on_event(events.append)
    task = await _hold_turn(session)
    session._background_handoff_state = "idle"  # the launch failed and ownership came back
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert "background_handoff" not in _kinds(events)


async def test_an_idle_session_is_confirmed_at_once() -> None:
    from tests.unit.test_tui_session_coverage import _make_session

    session, ctx, _workspace, _input = _make_session()
    events: list[ConversationEvent] = []
    ctx.app_state.conversation.on_event(events.append)
    session.announce_backgrounded("abc", "running")
    assert _kinds(events) == ["background_handoff"]
    session.announce_backgrounded("abc", "running")
    assert _kinds(events) == ["background_handoff", "background_handoff"]  # only the caller repeats


class _Handle:
    """The parts of a workflow run handle that a failure touches."""

    lifecycle = "running"
    current_phase = "implement"
    run_id = "run-1"
    workflow_name = "goal_flow"
    failure_kind = ""

    def __init__(self, checkpoint: object | None = None) -> None:
        self.failures: list[tuple[str, str, bool]] = []
        self._checkpoint = checkpoint

    def is_pause_requested(self) -> bool:
        return False

    def finalize_failure(self, error: str, *, kind: str, recoverable: bool) -> object | None:
        self.failures.append((error, kind, recoverable))
        return self._checkpoint


@pytest.mark.parametrize("announce", [True, False])
@pytest.mark.parametrize("saved", [False, True])
async def test_a_workflow_run_is_recorded_the_same_either_way(announce: bool, saved: bool) -> None:
    """Only what is shown differs: the checkpoint the worker resumes from does not."""
    from types import SimpleNamespace

    from tests.unit.test_tui_session_coverage import _make_session

    session, ctx, _workspace, _input = _make_session()
    conv = ctx.app_state.conversation
    events: list[ConversationEvent] = []
    conv.on_event(events.append)
    handle = _Handle(SimpleNamespace(revision=3) if saved else None)
    session._workflow_handle = handle  # type: ignore[assignment]
    session._release_workflow_claim = lambda _handle: None  # type: ignore[method-assign]
    session._workflow_pause_notice = lambda _handle: "Workflow paused; send a message"  # type: ignore[method-assign]
    conv.begin_turn("agent", "turn-1")

    session._fail_workflow_run("cancelled", kind="user_cancelled", announce=announce)

    assert handle.failures == [("cancelled", "user_cancelled", True)]  # recorded either way
    errors = [e for e in events if e.kind == "error"]
    assert (len(errors) == 1) is announce
    if announce:
        assert errors[0].payload["message"] == "cancelled"
    shown = "paused" in (conv.notification() or "")
    assert shown is (announce and saved)  # no "workflow paused" after a hand-off
