"""TUX-09 §2.3: the fullscreen layout (acceptance criterion 6)."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.commands import CommandContext, build_builtin_registry
from agenthicc.config import AgenthiccConfig, UISettings
from agenthicc.runners.tui_session import TUISession
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.runtime import CommandBus, ScrollPaneCommand
from agenthicc.tui.workspace.live_region import lines_text
from agenthicc.tui.workspace.workspace import Workspace
from tests.transcript_support import (
    FakeTerminal,
    scheduler_session,
    use_sessions_dir,
    write_session,
)
from tests.tui_frames import settle_frames

pytestmark = pytest.mark.unit

#: One frame's rows, built from a 10,000-event log: under one frame interval.
FRAME_BUDGET_S = 0.016
#: A page scrolled to events never rendered before (about 25 of them, rendered
#: once and then cached): well inside TUIR-04's 0.25 s loop-stall budget.
SCROLL_BUDGET_S = 0.1


@pytest.fixture(autouse=True)
def sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "sessions"
    use_sessions_dir(monkeypatch, root)
    yield root


def _rig(
    sessions: Path,
    events: list[tuple[str, dict[str, object]]],
    *,
    layout: str = "inline",
    width: int = 100,
    height: int = 30,
) -> tuple[AppState, Workspace, FakeTerminal, UnifiedInputSession]:
    write_session(sessions, "s", events)
    state = AppState.create()
    state.conversation.session_id.set("s")
    state.ui.set(replace(UISettings(), layout=layout))
    terminal = FakeTerminal(width, height)
    workspace = Workspace(state, terminal.console)
    bus = CommandBus()
    bus.register(ScrollPaneCommand, lambda command: workspace.scroll_pane(command.action))
    session = UnifiedInputSession(state, bus)
    return state, workspace, terminal, session


async def _ready(workspace: Workspace) -> None:
    layout = workspace._fullscreen
    assert layout is not None
    assert await asyncio.to_thread(layout.source.wait_ready, 20)
    workspace._redraw_input()
    await settle_frames(workspace)


def _rows(workspace: Workspace) -> list[str]:
    assert workspace._live is not None
    return [row.rstrip() for row in lines_text(workspace._live.lines)]


def test_the_default_layout_is_inline_and_screen_readers_force_it() -> None:
    assert UISettings().layout == "inline" and UISettings().fullscreen is False
    assert UISettings(layout="fullscreen").fullscreen is True
    assert UISettings(layout="fullscreen", screen_reader=True).fullscreen is False
    with pytest.raises(ValueError, match="ui.layout"):
        UISettings(layout="sideways")


async def test_fullscreen_shows_the_pane_over_the_docked_composer(sessions: Path) -> None:
    _state, workspace, terminal, _input = _rig(sessions, scheduler_session(8), layout="fullscreen")
    workspace.start()
    try:
        await _ready(workspace)
        assert terminal.console.is_alt_screen
        rows = _rows(workspace)
        assert len(rows) == 30
        assert any("Done with turn 8." in row for row in rows[:-4])
        # The composer frame and the status line are docked at the bottom.
        assert "❯" in "\n".join(rows[-6:])
        assert rows[-4].startswith("─") or rows[-3].startswith("─")
    finally:
        workspace.stop()
    assert not terminal.console.is_alt_screen


async def test_slash_fullscreen_switches_layout_without_losing_the_draft(sessions: Path) -> None:
    state, workspace, terminal, input_session = _rig(sessions, scheduler_session(6))
    workspace.start()
    reporting: list[bool] = []
    session = object.__new__(TUISession)
    session._ctx = SimpleNamespace(app_state=state, cfg=AgenthiccConfig(), session_id="s")  # type: ignore[assignment]
    session._workspace = workspace
    session._input_session = SimpleNamespace(sync_reporting=lambda: reporting.append(True))  # type: ignore[assignment]
    notices: list[tuple[str, str]] = []
    context = CommandContext(
        text="/fullscreen",
        args="",
        model="m",
        console=terminal.console,
        config=AgenthiccConfig(),
        session_id="s",
        toggle_fullscreen=session.toggle_fullscreen,
        transient_notify=lambda severity, text: notices.append((severity, text)),
    )
    command = build_builtin_registry().get("/fullscreen")
    assert command is not None and command.handler is not None
    try:
        input_session.set_text("a draft I am still typing")
        await settle_frames(workspace)
        assert not terminal.console.is_alt_screen

        command.handler(context)
        assert terminal.console.is_alt_screen and workspace.fullscreen
        assert state.conversation.fullscreen() is True
        assert "".join(state.input.buf()) == "a draft I am still typing"
        await _ready(workspace)
        assert any("a draft I am still typing" in row for row in _rows(workspace)[-6:])

        command.handler(context)
        assert not terminal.console.is_alt_screen and not workspace.fullscreen
        assert "".join(state.input.buf()) == "a draft I am still typing"
        assert state.conversation.fullscreen() is False
        assert notices == [("info", "Fullscreen layout on."), ("info", "Inline layout on.")]
        assert reporting == [True, True]  # the mouse reporting followed the layout
    finally:
        workspace.stop()


async def test_a_screen_reader_stays_inline(sessions: Path) -> None:
    state, workspace, terminal, _input = _rig(sessions, scheduler_session(2))
    config = AgenthiccConfig()
    config.ui = replace(config.ui, screen_reader=True)
    session = object.__new__(TUISession)
    session._ctx = SimpleNamespace(app_state=state, cfg=config, session_id="s")  # type: ignore[assignment]
    session._workspace = workspace
    session._input_session = SimpleNamespace(sync_reporting=lambda: None)  # type: ignore[assignment]
    workspace.start()
    try:
        assert session.toggle_fullscreen() is False
        assert not terminal.console.is_alt_screen
        assert state.conversation.transient_notice() is not None
    finally:
        workspace.stop()


def _play(state: AppState, events: list[tuple[str, dict[str, object]]]) -> None:
    """Append *events* through the store, which persists them to the session log."""
    from agenthicc.tui.runtime.session_log import SessionEventLog

    log = SessionEventLog("s")
    unsubscribe = state.conversation.on_event(log.append)
    try:
        for kind, payload in events:
            state.conversation.append_event(kind, payload)
    finally:
        unsubscribe()
        log.close()


async def test_exiting_prints_the_compact_transcript_to_the_normal_screen(
    sessions: Path,
) -> None:
    state, workspace, terminal, _input = _rig(sessions, [], layout="fullscreen")
    workspace.start()
    try:
        await _ready(workspace)
        _play(state, scheduler_session(6))
        # Printed to the console while the layout is on: part of the conversation,
        # shown in the pane at the place it was printed.
        terminal.console.print("a command's listing")
        _play(state, [("user_message", {"text": "one more question"})])
        workspace._redraw_input()
        await settle_frames(workspace)
        assert any("a command's listing" in row for row in _rows(workspace))
        assert any("one more question" in row for row in _rows(workspace))
    finally:
        workspace.stop()
    after_exit = terminal.raw.rsplit("\x1b[?1049l", 1)[1]
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", after_exit)
    # Everything the pane held is on the normal screen now, in order, compactly.
    for turn in range(1, 7):
        assert f"turn {turn}: look at" in plain
    assert plain.index("turn 6: look at") < plain.index("a command's listing")
    assert plain.index("a command's listing") < plain.index("one more question")
    assert "Worked for 4s" in plain
    # Compact: a failed command shows its last eight lines, not the stored thirty-two.
    assert "output line 059" in plain and "output line 030" not in plain
    assert not terminal.console.is_alt_screen


async def test_exiting_prints_only_what_the_normal_screen_has_not_seen(sessions: Path) -> None:
    """A session that began inline already has its earlier turns in the scrollback."""
    state, workspace, terminal, _input = _rig(sessions, scheduler_session(3))
    workspace.start()
    try:
        workspace.enter_fullscreen()
        _play(state, [("text", {"text": "written in fullscreen"})])
        mark = len(terminal.raw)
        workspace.leave_fullscreen()
        shown = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", terminal.raw[mark:])
        assert "written in fullscreen" in shown
        assert "turn 1: look at" not in shown
    finally:
        workspace.stop()


async def test_page_up_stops_following_and_end_follows_again(sessions: Path) -> None:
    state, workspace, terminal, input_session = _rig(
        sessions, scheduler_session(30), layout="fullscreen"
    )
    workspace.start()
    try:
        await _ready(workspace)
        assert state.conversation.pane_scrolled() is False
        newest = [row for row in _rows(workspace) if "Done with turn 30." in row]
        assert newest

        await input_session._dispatch(Key.PAGE_UP, "")
        await settle_frames(workspace)
        assert state.conversation.pane_scrolled() is True
        rows = _rows(workspace)
        assert not any("Done with turn 30." in row for row in rows)
        assert re.search(r"↓ End to follow", "\n".join(rows))

        # New output arrives while scrolled back: the pane stays put and counts it.
        _play(
            state,
            [("text", {"text": "arrived while scrolled"}), ("text", {"text": "and another"})],
        )
        workspace._redraw_input()
        await settle_frames(workspace)
        rows = _rows(workspace)
        assert not any("arrived while scrolled" in row for row in rows)
        assert any("↓ 2 new · End to follow" in row for row in rows)

        # End, not the composer's own End, resumes following.
        await input_session._dispatch(Key.END, "")
        await settle_frames(workspace)
        assert state.conversation.pane_scrolled() is False
        rows = _rows(workspace)
        assert any("and another" in row for row in rows)
        assert not any("End to follow" in row for row in rows)
    finally:
        workspace.stop()


async def test_end_keeps_its_composer_meaning_when_the_pane_follows(sessions: Path) -> None:
    state, workspace, _terminal, input_session = _rig(
        sessions, scheduler_session(5), layout="fullscreen"
    )
    workspace.start()
    try:
        await _ready(workspace)
        input_session.set_text("abc")
        await input_session._dispatch(Key.HOME, "")
        assert state.input.cursor() == 0
        await input_session._dispatch(Key.END, "")
        assert state.input.cursor() == 3  # the line end; the pane was not asked to scroll
        assert state.conversation.pane_scrolled() is False
    finally:
        workspace.stop()


async def test_the_wheel_and_shift_arrows_scroll_the_pane(sessions: Path) -> None:
    from agenthicc.tui.cbreak_reader import MouseEvent

    state, workspace, _terminal, input_session = _rig(
        sessions, scheduler_session(30), layout="fullscreen"
    )
    workspace.start()
    try:
        await _ready(workspace)
        await input_session._dispatch(Key.MOUSE, MouseEvent(64, 1, 1, pressed=True).encode())
        assert state.conversation.pane_scrolled() is True
        await input_session._dispatch(Key.MOUSE, MouseEvent(65, 1, 1, pressed=True).encode())
        await input_session._dispatch(Key.MOUSE, MouseEvent(65, 1, 1, pressed=True).encode())
        assert state.conversation.pane_scrolled() is False  # back at the end
        await input_session._dispatch(Key.MODIFIED, "shift+up")
        assert state.conversation.pane_scrolled() is True
        await input_session._dispatch(Key.MODIFIED, "shift+down")
        assert state.conversation.pane_scrolled() is False
    finally:
        workspace.stop()


async def test_a_10000_event_log_scrolls_inside_the_frame_budget(sessions: Path) -> None:
    events = scheduler_session(2_300)  # about 10,000 events
    assert len(events) >= 10_000, len(events)
    state, workspace, _terminal, _input = _rig(sessions, events, layout="fullscreen")
    workspace.start()
    try:
        await _ready(workspace)
        layout = workspace._fullscreen
        assert layout is not None and layout.source.count() >= 10_000
        frames: list[float] = []
        steps: list[float] = []
        actions: list[str] = ["page_up"] * 60 + ["page_down"] * 40 + ["line_up"] * 30 + ["follow"]
        for action in actions:
            started = time.perf_counter()
            workspace.scroll_pane(action)  # type: ignore[arg-type]
            scrolled = time.perf_counter()
            workspace._build_lines()
            frames.append(time.perf_counter() - scrolled)
            steps.append(time.perf_counter() - started)
        frames.sort()
        steps.sort()
        frame_p95 = frames[int(len(frames) * 0.95)]
        assert frame_p95 < FRAME_BUDGET_S, f"p95 frame {frame_p95 * 1000:.1f} ms"
        assert steps[-1] < SCROLL_BUDGET_S, f"slowest scroll {steps[-1] * 1000:.1f} ms"
        # Rendering is bounded by the window: far fewer items rendered than exist.
        assert len(layout.pane._bodies) < 800
    finally:
        workspace.stop()
