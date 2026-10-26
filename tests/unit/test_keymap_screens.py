"""Screens dispatch, hint and help from one keymap (AGX-13 acceptance)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.config import UISettings
from agenthicc.tui.actions import COMPOSER_ACTIONS
from agenthicc.tui.cbreak_reader import Key, key_name
from agenthicc.tui.conversation_store import AgentState, AppState
from agenthicc.tui.input.unified_session import InputMode, UnifiedInputSession
from agenthicc.tui.keymap import Action
from agenthicc.tui.keymap_service import KeymapService
from agenthicc.tui.runtime.commands import CommandBus
from agenthicc.tui.workspace import Workspace
from agenthicc.tui.workspace.components import FooterComponent
from tests.tui_frames import settle_frames

pytestmark = pytest.mark.unit

_KEYS: dict[str, tuple[Key, str]] = {
    "enter": (Key.ENTER, ""),
    "ctrl+j": (Key.CTRL_ENTER, ""),
    "/": (Key.CHAR, "/"),
    "@": (Key.AT, ""),
    "ctrl+v": (Key.CTRL_V, ""),
    "escape": (Key.ESC, ""),
    "ctrl+c": (Key.CTRL_C, ""),
    "ctrl+d": (Key.CTRL_D, ""),
    "backspace": (Key.BACKSPACE, ""),
    "ctrl+u": (Key.CTRL_U, ""),
    "left": (Key.LEFT, ""),
    "right": (Key.RIGHT, ""),
    "home": (Key.HOME, ""),
    "end": (Key.END, ""),
    "up": (Key.UP, ""),
    "down": (Key.DOWN, ""),
    "shift+tab": (Key.SHIFT_TAB, ""),
    "alt+enter": (Key.MODIFIED, "alt+enter"),
    "ctrl+g": (Key.CTRL_G, ""),
    "ctrl+o": (Key.CTRL_O, ""),
}


def _session(state: AppState, action: Action) -> UnifiedInputSession:
    if "subagents" in action.when:
        from agenthicc.subagents.pool import SubagentPoolState, WorkerState

        state.conversation.subagent_pool_state.set(
            SubagentPoolState("pool", 1, (WorkerState("explorer #1", "explorer", worker_id="w"),))
        )
    session = UnifiedInputSession(state, CommandBus())
    if "running" in action.when:
        session.set_mode(InputMode.STREAMING)
    if action.when & {"paste", "paste_at_cursor"}:
        session._buf.set(list("pasted"))
        session._paste.condensed = True
        session._paste.end = session._buf.cursor
    return session


@pytest.mark.parametrize("action", COMPOSER_ACTIONS, ids=lambda action: action.name)
def test_every_composer_action_is_dispatchable_hinted_and_in_help(action: Action) -> None:
    state = AppState.create()
    keymap = state.keymap()
    # Dispatchable: its key, under its conditions, resolves to it.
    key, ch = _KEYS[action.keys[0]]
    assert key_name(key, ch) == action.keys[0]
    session = _session(state, action)
    resolved = keymap.resolve("composer", key_name(key, ch), session.conditions())
    assert resolved is not None and resolved.name == action.name

    # In help.
    assert action.name in {entry.action for entry in keymap.help("composer")}

    # In the footer whenever it applies and fits.
    if action.hint_priority:
        if "running" in action.when:
            state.conversation.agent_state.set(AgentState.THINKING)
        if action.when & {"paste", "paste_at_cursor"}:
            state.input.paste_condensed.set(True)
        footer = " ".join(FooterComponent(state).markup_lines(400))
        assert action.label in footer


def test_deferred_keymap_keeps_first_frame_hints_then_installs_before_input(
    tmp_path: Path,
) -> None:
    state = AppState.create(defer_keymap=True)
    assert state.keymap() is None
    with pytest.raises(RuntimeError, match="not initialized"):
        state.require_keymap()

    footer = " ".join(FooterComponent(state).markup_lines(200))
    assert "Enter" in footer and "Ctrl+J" in footer and "commands" in footer

    service = KeymapService(state, tmp_path / "keybindings.toml")
    assert state.require_keymap() is service.current.keymap
    session = UnifiedInputSession(state, CommandBus())
    assert session._state.require_keymap() is service.current.keymap


async def test_typing_a_bound_key_runs_its_action() -> None:
    state = AppState.create()
    session = UnifiedInputSession(state, CommandBus())
    for ch in "ab":
        await session._dispatch(Key.CHAR, ch)
    await session._dispatch(Key.CTRL_ENTER, "")
    await session._dispatch(Key.CHAR, "c")
    assert "".join(state.input.buf()) == "ab\nc"
    await session._dispatch(Key.CTRL_U, "")
    assert state.input.buf() == []


async def test_rebinding_applies_without_restart(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    state = AppState.create()
    service = KeymapService(state, path)
    footer = FooterComponent(state)
    assert "Ctrl+J" in " ".join(footer.markup_lines(200))

    watch = asyncio.create_task(service.watch())
    try:
        await asyncio.sleep(0.05)
        path.write_text('[composer]\n"composer.newline" = "alt+enter"\n', encoding="utf-8")
        for _ in range(200):
            if state.keymap().action("composer.newline").keys == ("alt+enter",):
                break
            await asyncio.sleep(0.01)
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)

    assert "Key bindings reloaded" in str(state.conversation.notification())
    state.conversation.notification.set(None)
    hints = " ".join(footer.markup_lines(200))
    assert "Alt+Enter" in hints and "Ctrl+J" not in hints
    session = UnifiedInputSession(state, CommandBus())
    await session._dispatch(Key.CHAR, "x")
    await session._dispatch(Key.CTRL_ENTER, "")
    assert "".join(state.input.buf()) == "x"
    await session._dispatch(Key.MODIFIED, "alt+enter")
    assert "".join(state.input.buf()) == "x\n"


def test_an_invalid_edit_keeps_the_defaults_and_says_why(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    state = AppState.create()
    service = KeymapService(state, path)
    path.write_text('[composer]\n"composer.clear" = "enter"\n', encoding="utf-8")
    loaded = service.reload()
    assert loaded.errors
    assert state.keymap().action("composer.clear").keys == ("ctrl+u",)
    assert "Key bindings not applied" in str(state.conversation.notification())


# ── mouse reporting ──────────────────────────────────────────────────────────


class _Backend:
    def __init__(self) -> None:
        self.reporting: list[dict[str, bool | None]] = []

    def is_interactive(self) -> bool:
        return True

    @contextmanager
    def enter_raw_mode(self) -> Iterator[None]:
        yield

    def read_key(self) -> tuple[Key, str]:
        return (Key.CTRL_D, "")  # an empty prompt exits

    def restore(self) -> None:
        return None

    def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
        self.reporting.append({"focus": focus, "mouse": mouse})


@pytest.mark.parametrize(("mode", "reported"), [("on", True), ("auto", False), ("off", False)])
async def test_the_inline_tui_reports_the_mouse_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, mode: str, reported: bool
) -> None:
    backend = _Backend()
    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: backend)
    state = AppState.create()
    state.ui.set(UISettings(mouse=mode))
    await UnifiedInputSession(state, CommandBus()).run()
    assert (backend.reporting == [{"focus": None, "mouse": True}]) is reported
    assert reported or backend.reporting == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX raw mode")
def test_posix_reporting_is_turned_off_when_raw_mode_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from agenthicc.tui.terminal.posix_backend import PosixBackend

    master, slave = os.openpty()
    written = StringIO()
    monkeypatch.setattr(sys, "stdout", written)
    backend = PosixBackend()
    backend._fd = slave
    try:
        with backend.enter_raw_mode():
            backend.set_reporting(mouse=True)
            assert "\x1b[?1000h\x1b[?1006h" in written.getvalue()
        assert written.getvalue().endswith("\x1b[?1006l\x1b[?1000l") or (
            "\x1b[?1006l\x1b[?1000l" in written.getvalue()
        )
    finally:
        backend.close()
        os.close(master)
        os.close(slave)


# ── screen reader and reduced motion ─────────────────────────────────────────


def _workspace(ui: UISettings) -> tuple[AppState, Workspace, StringIO]:
    state = AppState.create()
    state.ui.set(ui)
    state.conversation.model_name.set("anthropic/claude-sonnet-5-5")
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=100, height=30)
    return state, Workspace(state, console), output


async def test_screen_reader_mode_draws_no_spinner_and_says_each_change_once() -> None:
    state, workspace, output = _workspace(UISettings(screen_reader=True))
    workspace.start()
    try:
        state.conversation.begin_turn("agent", "turn-1")
        await settle_frames(workspace)
        stats = workspace.frame_stats()
        assert stats is not None
        frames = stats.frames
        for _ in range(5):
            # What the session tick does for animation, without the clock.
            state.conversation.frame.set(state.conversation.frame() + 1)
            await asyncio.sleep(0)
        await settle_frames(workspace)
        assert stats.frames == frames  # no spinner frames
        state.conversation.agent_state.set(AgentState.COMPLETE)
        state.conversation.agent_state.set(AgentState.COMPLETE)
        await settle_frames(workspace)
        await asyncio.sleep(0.01)
    finally:
        workspace.stop()
    text = output.getvalue()
    assert text.count("Thinking.") == 1
    assert text.count("Done.") == 1
    assert "─" not in text
    assert not any(flower in text for flower in "✿❀❁❃✾❋✽❊")


def test_reduced_motion_keeps_the_layout_without_animation() -> None:
    state, workspace, _output = _workspace(UISettings(reduced_motion=True))
    state.conversation.begin_turn("agent", "turn-1")
    first = workspace.status.markup_lines(100)
    state.conversation.frame.set(state.conversation.frame() + 3)
    assert workspace.status.markup_lines(100) == first
    assert first[0].startswith("●")
    rows = ["".join(segment.text for segment in line) for line in workspace._build_lines()]
    assert any(set(row) == {"─"} for row in rows)  # borders stay
