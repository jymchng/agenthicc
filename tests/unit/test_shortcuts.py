"""TUX-10 §2.6 / AC 7: ``/shortcuts`` and ``?`` list every key in every context."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.commands import build_builtin_registry
from agenthicc.commands.command import CommandContext
from agenthicc.commands.dispatcher import CommandDispatcher
from agenthicc.config import AgenthiccConfig
from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.input.unified_session import InputMode, UnifiedInputSession
from agenthicc.tui.keymap import Keymap, load_keybindings
from agenthicc.tui.runtime.commands import CommandBus, OpenShortcutsCommand
from agenthicc.tui.shortcuts import GROUPS, USER_SOURCE, shortcut_groups
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.workspace.overlays.shortcuts import ShortcutsOverlay

pytestmark = pytest.mark.unit

#: An internal identifier (``library_tab``) or a raw negation (``not session``).
_IDENTIFIER = re.compile(r"\b[a-z]+_[a-z_]+\b")
_NEGATION = re.compile(r"\bnot [a-z_]+\b")


def _keymap(tmp_path: Path, bindings: str = "") -> Keymap:
    path = tmp_path / "keybindings.toml"
    if bindings:
        path.write_text(bindings, encoding="utf-8")
    loaded = load_keybindings(DEFAULT_ACTIONS, path)
    assert not loaded.errors, loaded.errors
    return loaded.keymap


def _page(overlay: ShortcutsOverlay, width: int = 100) -> str:
    """The whole page as text: the viewport is made tall enough to show every row."""
    overlay.set_viewport(width, 4_000)
    console = Console(width=width, file=io.StringIO(), force_terminal=False, color_system=None)
    console.print(overlay.render())
    return console.file.getvalue()  # type: ignore[attr-defined]


def _overlay(keymap: Keymap) -> ShortcutsOverlay:
    return ShortcutsOverlay(keymap, on_close=lambda: None)


# ── AC 7.1: every action, once per context ────────────────────────────────────


def test_every_action_in_the_default_actions_appears_exactly_once(tmp_path: Path) -> None:
    groups = shortcut_groups(_keymap(tmp_path))
    listed = [row.action for group in groups for row in group.rows]
    assert sorted(listed) == sorted(action.name for action in DEFAULT_ACTIONS)
    assert len(listed) == len(set(listed))


def test_an_action_is_listed_under_its_own_context_and_the_groups_are_the_five_screens(
    tmp_path: Path,
) -> None:
    keymap = _keymap(tmp_path)
    groups = shortcut_groups(keymap)
    assert [group.title for group in groups] == [
        "Composer",
        "Pickers",
        "Dialogs",
        "Transcript view",
        "Agents hub",
    ]
    for group in groups:
        for section in group.sections:
            for row in section.rows:
                assert keymap.action(row.action).context == section.context
    assert {context for _, contexts in GROUPS for context in contexts} >= {
        action.context for action in DEFAULT_ACTIONS
    }


def test_a_context_no_group_names_is_still_listed(tmp_path: Path) -> None:
    from dataclasses import replace

    extra = replace(DEFAULT_ACTIONS[0], name="overlay.example", context="overlay", keys=("f9",))
    keymap = Keymap.build((*DEFAULT_ACTIONS, extra))
    other = [group for group in shortcut_groups(keymap) if group.title == "Other"]
    assert [row.action for row in other[0].rows] == ["overlay.example"]


def test_the_page_lists_the_keys_the_user_sees(tmp_path: Path) -> None:
    text = _page(_overlay(_keymap(tmp_path)))
    for heading in ("Composer", "Pickers", "Dialogs", "Transcript view", "Agents hub"):
        assert heading in text
    assert "Keyboard shortcuts" in text
    assert "Ctrl+R" in text and "Send the prompt" in text


# ── AC 7.2: the user's own bindings ───────────────────────────────────────────


def test_a_rebinding_appears_with_its_new_key_and_says_so(tmp_path: Path) -> None:
    keymap = _keymap(tmp_path, '[composer]\n"composer.undo" = "ctrl+q"\n')
    rows = {row.action: row for group in shortcut_groups(keymap) for row in group.rows}
    assert rows["composer.undo"].keys == "Ctrl+Q"
    assert rows["composer.undo"].source == USER_SOURCE == "your keybindings"
    assert rows["composer.redo"].source == "default"
    text = " ".join(re.sub(r"[│╭╮╰╯─]", " ", _page(_overlay(keymap))).split())
    assert "Ctrl+Q" in text and "your keybindings" in text
    undo = next(line for line in _page(_overlay(keymap)).splitlines() if "Undo the last" in line)
    assert "Ctrl+Q" in undo and "Ctrl+Z" not in undo


def test_an_unbound_action_says_unbound(tmp_path: Path) -> None:
    keymap = _keymap(tmp_path, '[composer]\n"composer.undo" = "none"\n')
    rows = {row.action: row for group in shortcut_groups(keymap) for row in group.rows}
    assert rows["composer.undo"].keys == "unbound"


def test_the_page_without_a_bindings_file_names_no_user_binding(tmp_path: Path) -> None:
    assert "your keybindings" not in _page(_overlay(_keymap(tmp_path)))


# ── AC 7.3: words, never condition identifiers ────────────────────────────────


def test_no_row_shows_a_condition_identifier(tmp_path: Path) -> None:
    keymap = _keymap(tmp_path)
    for group in shortcut_groups(keymap):
        for row in group.rows:
            assert _IDENTIFIER.search(row.conditions) is None, row
            assert _NEGATION.search(row.conditions) is None, row
    text = _page(_overlay(keymap))
    for found in re.findall(r"\(([^)]*)\)", text):
        assert _IDENTIFIER.search(found) is None, found
    assert "while a turn runs" in text and "when the prompt is empty" in text


# ── the page itself ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("width", [50, 80, 140])
def test_long_descriptions_wrap_instead_of_being_cut(tmp_path: Path, width: int) -> None:
    text = _page(_overlay(_keymap(tmp_path)), width)
    squashed = " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())
    # The last words of the longest descriptions are still there.
    for tail in (
        "(Ctrl+X Ctrl+E, or Ctrl+X alone)",
        "Move to the line start; pressed again, to the start of the prompt",
    ):
        words = tail.split()
        assert " ".join(words[-3:]) in squashed, (width, tail)
    assert all(len(line) <= width for line in text.splitlines())


def test_the_pager_scrolls_and_esc_closes_it(tmp_path: Path) -> None:
    closed: list[bool] = []
    overlay = ShortcutsOverlay(_keymap(tmp_path), on_close=lambda: closed.append(True))
    overlay.set_viewport(80, 14)
    overlay.on_mount()
    first = overlay.render()
    assert overlay.handle_key(Key.PAGE_DOWN, "") is True
    assert overlay.dialog.scroll > 0 and first is not None
    assert closed == []
    overlay.handle_key(Key.ESC, "")
    assert closed == [True]
    closed.clear()
    overlay.handle_key(Key.CHAR, "q")
    assert closed == [True]


def test_ascii_glyphs_draw_nothing_above_ascii(tmp_path: Path) -> None:
    from agenthicc.tui.theme import ASCII_GLYPHS, BUILTIN_THEMES, current_theme, use_theme

    before = current_theme()
    try:
        use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
        text = _page(_overlay(_keymap(tmp_path)))
    finally:
        use_theme(before)
    # Keys shown as text ("Up", "Left") are the keymap's own; the chrome is ASCII.
    chrome = [
        line for line in text.splitlines() if "Keyboard shortcuts" in line or "scroll" in line
    ]
    assert chrome and all(line.isascii() for line in chrome)


# ── /shortcuts and ? ──────────────────────────────────────────────────────────


def test_shortcuts_is_a_builtin_command_the_picker_can_see() -> None:
    from pathlib import Path as _Path

    from agenthicc.tui.trigger import TriggerContext
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger

    registry = build_builtin_registry()
    command = registry.get("/shortcuts")
    assert command is not None and command.menu_factory is not None
    matches = SlashCommandTrigger(registry).get_matches("shortc", TriggerContext(cwd=_Path.cwd()))
    assert "/shortcuts" in [match.value for match in matches]


def test_the_command_opens_the_page_for_the_sessions_keymap(tmp_path: Path) -> None:
    state = AppState.create()
    state.keymap.set(_keymap(tmp_path, '[composer]\n"composer.undo" = "ctrl+q"\n'))
    shown: list[Overlay] = []
    context = CommandContext(
        text="/shortcuts",
        args="",
        model="test/model",
        console=Console(file=io.StringIO()),
        config=AgenthiccConfig(),
        set_pending_menu=shown.append,
        close_overlay=lambda: None,
        app_state=state,
    )
    assert CommandDispatcher(build_builtin_registry()).dispatch("/shortcuts", context)
    assert isinstance(shown[0], ShortcutsOverlay)
    assert "Ctrl+Q" in _page(shown[0])


def _session(
    keymap: Keymap | None = None, *, handled: bool = True
) -> tuple[UnifiedInputSession, list[str], AppState]:
    state = AppState.create()
    if keymap is not None:
        state.keymap.set(keymap)
    bus = CommandBus()
    opened: list[str] = []
    if handled:

        async def open_page(_command: OpenShortcutsCommand) -> None:
            opened.append("shortcuts")

        bus.register(OpenShortcutsCommand, open_page)
    return UnifiedInputSession(state, bus), opened, state


async def test_question_mark_on_an_empty_prompt_opens_the_page_and_types_nothing() -> None:
    session, opened, state = _session()
    await session._dispatch(Key.CHAR, "?")
    assert opened == ["shortcuts"]
    assert state.input.buf() == []


async def test_question_mark_in_a_prompt_is_a_character() -> None:
    session, opened, state = _session()
    for ch in "why":
        await session._dispatch(Key.CHAR, ch)
    await session._dispatch(Key.CHAR, "?")
    assert opened == []
    assert "".join(state.input.buf()) == "why?"


async def test_it_opens_over_a_running_turn_too() -> None:
    session, opened, _ = _session()
    session.set_mode(InputMode.STREAMING)
    await session._dispatch(Key.CHAR, "?")
    assert opened == ["shortcuts"]


async def test_where_nothing_can_show_the_page_the_key_is_a_character() -> None:
    """An answer composer inside a dialog: a question may start with ``?``."""
    session, opened, state = _session(handled=False)
    await session._dispatch(Key.CHAR, "?")
    assert opened == []
    assert "".join(state.input.buf()) == "?"


async def test_the_key_is_rebindable(tmp_path: Path) -> None:
    keymap = _keymap(tmp_path, '[composer]\n"composer.shortcuts" = "f2"\n')
    session, opened, state = _session(keymap)
    await session._dispatch(Key.CHAR, "?")
    assert opened == [] and "".join(state.input.buf()) == "?"
    state.input.update([], 0)
    session._buf.set([])
    await session._dispatch(Key.FUNCTION, "f2")
    assert opened == ["shortcuts"]


def test_the_hint_row_and_the_status_line_end_with_the_key() -> None:
    from rich.text import Text

    from agenthicc.tui.conversation_store import AgentState
    from agenthicc.tui.workspace.components import ComposerComponent, ComposerFrame, StatusLine

    state = AppState.create()
    state.conversation.agent_state.set(AgentState.THINKING)
    state.conversation.begin_turn("agent", "t1")
    frame = ComposerFrame(state, ComposerComponent(state))
    row = Text.from_markup(frame.hint_row(120) or "").plain
    assert row.rstrip().endswith("? shortcuts")
    # Once something is typed, ? is a character: the hint would be wrong.
    state.input.update(list("hi"), 2)
    assert "? shortcuts" not in Text.from_markup(frame.hint_row(120) or "").plain
    assert Text.from_markup(StatusLine(state).markup(120)).plain.rstrip().endswith("? shortcuts")
    # Narrow: the other hints give way, the shortcuts hint stays last.
    state.input.update([], 0)
    narrow = Text.from_markup(frame.hint_row(30) or "").plain
    assert narrow.rstrip().endswith("? shortcuts")


def test_an_unbound_key_leaves_the_status_line_pointing_at_help(tmp_path: Path) -> None:
    from rich.text import Text

    from agenthicc.tui.workspace.components import StatusLine

    state = AppState.create()
    state.keymap.set(_keymap(tmp_path, '[composer]\n"composer.shortcuts" = "none"\n'))
    assert Text.from_markup(StatusLine(state).markup(120)).plain.rstrip().endswith("/help")
