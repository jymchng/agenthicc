"""TUX-04: composer ergonomics — the acceptance criteria of §4.

1. the §2.1 editing table, on the main composer and (8.) every
   ``EmbeddedComposer``;
2. the F10 reproduction (keys reach the prompt while a picker is open);
3. one Enter runs a command;
4. Ctrl+R history search;
5. Ctrl+X Ctrl+E external editor;
6. a visible, editable queue (the interjection's delivery is an integration
   test, ``tests/integration/test_composer_interjection.py``);
7. Shift+Enter and the kitty keyboard mode.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agenthicc.tui.cbreak_reader import Key, decode_csi, kitty_keyboard_supported
from agenthicc.tui.conversation_store import AgentState, AppState
from agenthicc.tui.input import unified_session as unified
from agenthicc.tui.input.capabilities import _EXIT
from agenthicc.tui.input.embedded import ComposerContext, EmbeddedComposer
from agenthicc.tui.input.history_search import HistorySearchOverlay
from agenthicc.tui.input.unified_session import InputMode, TerminalHooks, UnifiedInputSession
from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand
from agenthicc.tui.trigger import TriggerManager
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.tui.workspace.overlay import OverlayHost
from agenthicc.tui.workspace.queue_rows import queue_rows

pytestmark = pytest.mark.unit

Press = tuple[Key, str]


def _chars(text: str) -> list[Press]:
    return [(Key.AT, "") if ch == "@" else (Key.CHAR, ch) for ch in text]


_PASTE = "line 1\nline 2\nline 3\nline 4\nline 5"

#: §2.1, one row per binding: (start text, cursor, keys, text, cursor).
_TABLE: list[tuple[str, str, int, list[Press], str, int]] = [
    ("ctrl+a line start", "ab\ncd", 4, [(Key.CTRL_A, "")], "ab\ncd", 3),
    ("ctrl+a again: input start", "ab\ncd", 3, [(Key.CTRL_A, "")], "ab\ncd", 0),
    ("ctrl+e line end", "ab\ncd", 0, [(Key.CTRL_E, "")], "ab\ncd", 2),
    ("ctrl+e again: input end", "ab\ncd", 2, [(Key.CTRL_E, "")], "ab\ncd", 5),
    ("alt+b", "foo bar", 7, [(Key.ALT, "b")], "foo bar", 4),
    ("ctrl+left", "foo bar", 7, [(Key.MODIFIED, "ctrl+left")], "foo bar", 4),
    ("alt+f", "foo bar", 0, [(Key.ALT, "f")], "foo bar", 3),
    ("ctrl+right", "foo bar", 0, [(Key.MODIFIED, "ctrl+right")], "foo bar", 3),
    ("ctrl+w stops at /", "src/agenthicc/tui", 17, [(Key.CTRL_W, "")], "src/agenthicc/", 14),
    (
        "alt+backspace",
        "src/agenthicc/tui",
        17,
        [(Key.MODIFIED, "alt+backspace")],
        "src/agenthicc/",
        14,
    ),
    ("alt+d", "foo bar", 3, [(Key.ALT, "d")], "foo", 3),
    ("ctrl+k", "foo bar", 3, [(Key.CTRL_K, "")], "foo", 3),
    ("ctrl+k ctrl+y round trip", "foo bar", 3, [(Key.CTRL_K, ""), (Key.CTRL_Y, "")], "foo bar", 7),
    ("ctrl+w ctrl+y", "foo bar", 7, [(Key.CTRL_W, ""), (Key.CTRL_Y, "")], "foo bar", 7),
    ("ctrl+u to line start", "ab\ncd", 5, [(Key.CTRL_U, "")], "ab\n", 3),
    ("ctrl+u at the start clears", "ab\ncd", 3, [(Key.CTRL_U, "")], "", 0),
    ("delete", "abc", 1, [(Key.DELETE, "")], "ac", 1),
    ("ctrl+d on a non-empty prompt", "abc", 1, [(Key.CTRL_D, "")], "ac", 1),
    ("ctrl+z: typing is one group", "", 0, [*_chars("abc"), (Key.CTRL_Z, "")], "", 0),
    (
        "ctrl+z: a move ends the group",
        "",
        0,
        [*_chars("ab"), (Key.LEFT, ""), *_chars("c"), (Key.CTRL_Z, "")],
        "ab",
        1,
    ),
    (
        "ctrl+shift+z",
        "",
        0,
        [*_chars("abc"), (Key.CTRL_Z, ""), (Key.MODIFIED, "ctrl+shift+z")],
        "abc",
        3,
    ),
    ("alt+z", "", 0, [*_chars("abc"), (Key.CTRL_Z, ""), (Key.ALT, "z")], "abc", 3),
    ("undo after a paste", "draft", 5, [(Key.PASTE, _PASTE), (Key.CTRL_Z, "")], "draft", 5),
    ("shift+enter", "ab", 2, [(Key.SHIFT_ENTER, "")], "ab\n", 3),
    ("alt+enter", "ab", 2, [(Key.MODIFIED, "alt+enter")], "ab\n", 3),
    ("ctrl+enter", "ab", 2, [(Key.MODIFIED, "ctrl+enter")], "ab\n", 3),
    ("ctrl+j", "ab", 2, [(Key.CTRL_ENTER, "")], "ab\n", 3),
]


def _session(tmp_path: Path, **kwargs: object) -> tuple[UnifiedInputSession, AppState, list[str]]:
    state = AppState.create()
    bus = CommandBus()
    sent: list[str] = []
    bus.register(SendMessageCommand, lambda command: sent.append(command.text))
    triggers = TriggerManager()
    triggers.register(AtMentionTrigger())
    session = UnifiedInputSession(
        state,
        bus,
        trigger_registry=triggers,
        overlay_host=OverlayHost(state),
        cwd=tmp_path,
        **kwargs,  # type: ignore[arg-type]
    )
    return session, state, sent


async def _press(session: UnifiedInputSession, keys: list[Press]) -> None:
    for key, ch in keys:
        await session._dispatch(key, ch)


# ── 1. the editing table ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "cursor", "keys", "text", "final"),
    [row[1:] for row in _TABLE],
    ids=[row[0] for row in _TABLE],
)
async def test_the_editing_table(
    tmp_path: Path, start: str, cursor: int, keys: list[Press], text: str, final: int
) -> None:
    session, state, sent = _session(tmp_path)
    session.set_text(start)
    session._buf.cursor = cursor
    await _press(session, keys)

    assert session._buf.text == text
    assert session._buf.cursor == final
    assert "".join(state.input.buf()) == text
    assert sent == []
    assert not session._paste.condensed


async def test_ctrl_d_on_an_empty_prompt_still_exits(tmp_path: Path) -> None:
    session, _state, _ = _session(tmp_path)
    assert await session._dispatch(Key.CTRL_D, "") is _EXIT


async def test_ctrl_l_redraws_the_live_region(tmp_path: Path) -> None:
    repaints: list[bool] = []
    session, _state, _ = _session(
        tmp_path, terminal_hooks=TerminalHooks(repaint=lambda: repaints.append(True))
    )
    await session._dispatch(Key.CTRL_L, "")
    assert repaints == [True]


async def test_kills_accumulate_and_the_undo_stack_is_bounded(tmp_path: Path) -> None:
    from agenthicc.tui.input.buffer import UNDO_LIMIT, InputBuffer

    session, _state, _ = _session(tmp_path)
    session.set_text("one two three")
    await _press(session, [(Key.CTRL_W, ""), (Key.CTRL_W, "")])
    assert session._buf.kill_ring[-1] == "two three"

    buffer = InputBuffer()
    for index in range(UNDO_LIMIT + 50):
        buffer.insert_many(list(f"{index},"))  # a paste: one group each
    undone = 0
    while buffer.undo():
        undone += 1
    assert undone == UNDO_LIMIT


async def test_word_boundaries_are_unicode_aware(tmp_path: Path) -> None:
    session, _state, _ = _session(tmp_path)
    session.set_text("grüße_ä/日本語")
    await _press(session, [(Key.CTRL_W, "")])
    assert session._buf.text == "grüße_ä/"
    await _press(session, [(Key.CTRL_W, "")])
    assert session._buf.text == ""


# ── 8. embedded composers inherit everything ─────────────────────────────────


@pytest.mark.parametrize(
    ("start", "cursor", "keys", "text", "final"),
    [row[1:] for row in _TABLE],
    ids=[row[0] for row in _TABLE],
)
async def test_embedded_composers_pass_the_same_table(
    tmp_path: Path, start: str, cursor: int, keys: list[Press], text: str, final: int
) -> None:
    submitted: list[str] = []

    async def on_submit(value: str) -> bool:
        submitted.append(value)
        return True

    composer = EmbeddedComposer(ComposerContext(cwd=tmp_path), on_submit=on_submit)
    composer.set_text(start)
    composer.editor._buf.cursor = cursor
    for key, ch in keys:
        composer.feed(key, ch)
    await composer.settle()

    assert composer.text == text
    assert composer.editor._buf.cursor == final
    assert submitted == []


# ── 2. the F10 reproduction ──────────────────────────────────────────────────


def _project(tmp_path: Path) -> Path:
    for name in ("workspace", "widgets", "input"):
        (tmp_path / "src" / "agenthicc" / "tui" / name).mkdir(parents=True)
    (tmp_path / "src" / "README.md").write_text("x", encoding="utf-8")
    return tmp_path


async def test_ctrl_u_with_a_picker_open_clears_the_prompt_and_closes_it(
    tmp_path: Path,
) -> None:
    session, state, sent = _session(_project(tmp_path))
    await _press(session, _chars("see @src/agenthicc/tui/w"))
    assert session.picker is not None

    await session._dispatch(Key.CTRL_U, "")
    assert state.input.buf() == []
    assert session.picker is None
    assert session._overlay is not None and not session._overlay.active
    # Typing /help now is a fresh prompt, not a fragment (the F10 bug).
    await _press(session, _chars("hello"))
    await session._dispatch(Key.ENTER, "")
    assert sent == ["hello"]


async def test_ctrl_w_with_a_picker_open_keeps_the_picker_in_step(tmp_path: Path) -> None:
    session, state, _ = _session(_project(tmp_path))
    await _press(session, _chars("see @src/ag"))
    assert session.picker is not None and session.picker.fragment == "src/ag"

    await session._dispatch(Key.CTRL_W, "")
    assert "".join(state.input.buf()) == "see @src/"
    picker = session.picker
    assert picker is not None and picker.fragment == "src/"
    assert {item.value for item in picker.matches} == {"src/agenthicc/", "src/README.md"}


async def test_editing_keys_with_a_picker_open(tmp_path: Path) -> None:
    session, state, _ = _session(_project(tmp_path))
    await _press(session, _chars("@src/agenthicc"))
    await _press(session, [(Key.LEFT, ""), (Key.LEFT, "")])
    assert session.picker is not None and session.picker.fragment == "src/agenthi"
    await session._dispatch(Key.CTRL_E, "")
    assert session.picker is not None and session.picker.fragment == "src/agenthicc"
    await session._dispatch(Key.PASTE, "/tui")
    assert session.picker is not None and session.picker.fragment == "src/agenthicc/tui"
    await session._dispatch(Key.CTRL_Z, "")
    assert session.picker is not None and session.picker.fragment == "src/agenthicc"
    await session._dispatch(Key.CTRL_A, "")
    assert session.picker is None
    assert "".join(state.input.buf()) == "@src/agenthicc"


# ── 3. one Enter ─────────────────────────────────────────────────────────────


def _command_session(tmp_path: Path) -> tuple[UnifiedInputSession, list[str]]:
    from agenthicc.commands import build_builtin_registry
    from agenthicc.workflows.plugin import WorkflowPlugin
    from agenthicc.workflows.registry import WorkflowRegistry

    class Docs(WorkflowPlugin):
        name = "docs"
        description = "Build documentation"

    workflows = WorkflowRegistry()
    workflows.register(Docs)
    session, _state, sent = _session(tmp_path)
    assert session._registry is not None
    session._registry.register(SlashCommandTrigger(build_builtin_registry(), workflows))
    return session, sent


async def test_one_enter_runs_a_command_without_a_required_argument(tmp_path: Path) -> None:
    session, sent = _command_session(tmp_path)
    await _press(session, _chars("/help"))
    picker = session.picker
    assert picker is not None
    assert picker.matches[picker.selected].value == "/help"  # visible and highlighted

    await session._dispatch(Key.ENTER, "")
    assert sent == ["/help"]
    assert session.picker is None
    assert session._buf.text == ""


async def test_enter_on_a_command_that_needs_an_argument_lists_its_arguments(
    tmp_path: Path,
) -> None:
    session, sent = _command_session(tmp_path)
    await _press(session, _chars("/workflo"))
    await session._dispatch(Key.ENTER, "")

    assert sent == []
    assert session._buf.text == "/workflow "
    picker = session.picker
    assert picker is not None
    assert "/workflow docs" in [item.value for item in picker.matches]
    await _press(session, _chars("d"))
    await session._dispatch(Key.ENTER, "")
    assert sent == ["/workflow docs"]


async def test_tab_only_completes(tmp_path: Path) -> None:
    session, sent = _command_session(tmp_path)
    await _press(session, _chars("/help"))
    await session._dispatch(Key.TAB, "")
    assert sent == []
    assert session._buf.text == "/help"
    assert session.picker is None


async def test_one_enter_opens_help_through_the_session(tmp_path: Path) -> None:
    """Picker visibility and execution end to end: /help, Enter, the help overlay."""
    from agenthicc.commands import build_builtin_registry
    from agenthicc.tui.workspace.overlays.help import HelpOverlay

    from tests.unit.test_tui_session_coverage import _make_session

    tui, ctx, workspace, _input = _make_session()
    help_command = build_builtin_registry().get("/help")
    assert help_command is not None
    ctx.cmd_registry.register(help_command)
    ctx.command_bus.register(SendMessageCommand, tui.handle_send)
    triggers = TriggerManager()
    triggers.register(SlashCommandTrigger(ctx.cmd_registry))
    composer = UnifiedInputSession(
        ctx.app_state,
        ctx.command_bus,
        trigger_registry=triggers,
        overlay_host=workspace.overlays,
        cwd=tmp_path,
    )
    await _press(composer, _chars("/help"))
    assert composer.picker is workspace.overlays.widget

    await composer._dispatch(Key.ENTER, "")
    assert isinstance(workspace.overlays.widget, HelpOverlay)


# ── 4. Ctrl+R ────────────────────────────────────────────────────────────────


async def test_ctrl_r_finds_an_entry_and_enter_puts_it_in_the_prompt_unsent(
    tmp_path: Path,
) -> None:
    session, state, sent = _session(tmp_path, history=["git status", "git rebase -i main"])
    await session._dispatch(Key.CTRL_R, "")
    search = session._overlay.widget if session._overlay is not None else None
    assert isinstance(search, HistorySearchOverlay)
    await _press(session, _chars("reb"))
    assert search.match == "git rebase -i main"

    await session._dispatch(Key.ENTER, "")
    assert session._buf.text == "git rebase -i main"
    assert "".join(state.input.buf()) == "git rebase -i main"
    assert sent == []
    assert session._overlay is not None and not session._overlay.active


async def test_ctrl_r_again_is_older_ctrl_s_newer_and_case_is_ignored(tmp_path: Path) -> None:
    session, _state, _ = _session(
        tmp_path, history=["Git one", "unrelated", "git two\nsecond line", "GIT three"]
    )
    await session._dispatch(Key.CTRL_R, "")
    await _press(session, _chars("git"))
    search = session._overlay.widget if session._overlay is not None else None
    assert isinstance(search, HistorySearchOverlay)
    assert search.match == "GIT three"
    await session._dispatch(Key.CTRL_R, "")
    assert search.match == "git two\nsecond line"
    await session._dispatch(Key.CTRL_R, "")
    assert search.match == "Git one"
    await session._dispatch(Key.CTRL_S, "")
    assert search.match == "git two\nsecond line"
    # A multi-line entry matches on any of its lines.
    await _press(session, [(Key.BACKSPACE, "")] * 3 + _chars("second"))
    assert search.match == "git two\nsecond line"
    await session._dispatch(Key.RIGHT, "")
    assert session._buf.text == "git two\nsecond line"


async def test_esc_restores_the_draft_exactly(tmp_path: Path) -> None:
    session, state, _ = _session(tmp_path, history=["git status"])
    session.set_text("my draft\nline two")
    session._buf.cursor = 3
    await session._dispatch(Key.CTRL_R, "")
    await _press(session, _chars("git"))
    await session._dispatch(Key.ESC, "")

    assert session._buf.text == "my draft\nline two"
    assert session._buf.cursor == 3
    assert "".join(state.input.buf()) == "my draft\nline two"
    assert session._overlay is not None and not session._overlay.active


def test_the_search_row_shows_the_count_and_hints() -> None:
    from rich.console import Console

    from agenthicc.tui.keymap import Keymap
    from agenthicc.tui.actions import DEFAULT_ACTIONS

    search = HistorySearchOverlay(
        ["git status", "git rebase -i main"], Keymap.build(DEFAULT_ACTIONS), [], 0
    )
    search.choose(Key.CHAR, "g")
    console = Console(file=io.StringIO(), width=120, record=True)
    console.print(search.render())
    text = console.export_text()
    assert "(search) g" in text
    assert "1 of 2" in text and "Ctrl+R older" in text and "Esc cancel" in text
    assert "git rebase -i main" in text


# ── 5. external editor ───────────────────────────────────────────────────────


class _FakeTerminal:
    """A backend whose terminal mode the test can compare before and after."""

    def __init__(self, keys: list[Press], state: AppState) -> None:
        self.mode = "cooked"
        self.keys = list(keys)
        self.state = state
        self.prompts: list[str] = []

    def is_interactive(self) -> bool:
        return True

    @contextmanager
    def enter_raw_mode(self) -> Iterator[None]:
        previous, self.mode = self.mode, "raw"
        try:
            yield
        finally:
            self.mode = previous

    def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
        return None

    def read_key(self) -> Press:
        if not self.keys:
            self.prompts.append("".join(self.state.input.buf()))
            return (Key.CTRL_C, "")
        return self.keys.pop(0)

    def restore(self) -> None:
        return None

    def close(self) -> None:
        return None


def _editor(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", f"sh -c '{script}' --")


async def test_ctrl_x_ctrl_e_replaces_the_prompt_with_the_edited_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _editor(monkeypatch, 'printf edited > "$1"')
    state = AppState.create()
    terminal = _FakeTerminal([(Key.CTRL_X, ""), (Key.CTRL_E, "")], state)
    modes: list[str] = []
    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: terminal)
    session = UnifiedInputSession(
        state,
        CommandBus(),
        cwd=tmp_path,
        terminal_hooks=TerminalHooks(
            suspend=lambda: modes.append(terminal.mode), runtime_dir=tmp_path / "runtime"
        ),
    )
    session.set_text("draft")
    before = terminal.mode

    await session.run()

    assert terminal.prompts[0] == "edited"
    assert modes == ["cooked"]  # the editor ran with the terminal handed back
    assert terminal.mode == before
    assert list((tmp_path / "runtime").iterdir()) == []  # the file is deleted


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="GNU stat")
async def test_the_editor_file_is_private_and_holds_the_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = tmp_path / "seen"
    _editor(monkeypatch, f'stat -c %a "$1" > {seen}; cat "$1" >> {seen}')
    done = asyncio.Event()

    async def handoff(operation: Callable[[], object]) -> None:
        await operation()  # type: ignore[misc]
        done.set()

    session, _state, _ = _session(
        tmp_path, terminal_hooks=TerminalHooks(runtime_dir=tmp_path / "run", handoff=handoff)
    )
    session.set_text("my draft")
    session.open_external_editor()
    await asyncio.wait_for(done.wait(), 5)
    assert seen.read_text(encoding="utf-8") == "600\nmy draft"
    assert session._buf.text == "my draft"
    assert list((tmp_path / "run").iterdir()) == []


async def test_a_failing_editor_keeps_the_draft_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _editor(monkeypatch, 'printf lost > "$1"; exit 3')
    done = asyncio.Event()

    async def handoff(operation: Callable[[], object]) -> None:
        await operation()  # type: ignore[misc]
        done.set()

    session, state, _ = _session(
        tmp_path, terminal_hooks=TerminalHooks(runtime_dir=tmp_path, handoff=handoff)
    )
    session.set_text("keep me")
    session.open_external_editor()
    await asyncio.wait_for(done.wait(), 5)
    assert session._buf.text == "keep me"
    assert "status 3" in (state.conversation.notification() or "")


async def test_ctrl_x_alone_opens_the_editor_after_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _editor(monkeypatch, 'printf alone > "$1"')
    monkeypatch.setattr(unified, "CHORD_TIMEOUT_S", 0.01)
    done = asyncio.Event()

    async def handoff(operation: Callable[[], object]) -> None:
        await operation()  # type: ignore[misc]
        done.set()

    session, _state, _ = _session(
        tmp_path, terminal_hooks=TerminalHooks(runtime_dir=tmp_path, handoff=handoff)
    )
    await session._dispatch(Key.CTRL_X, "")
    await asyncio.wait_for(done.wait(), 5)
    assert session._buf.text == "alone"


async def test_another_key_after_ctrl_x_cancels_the_chord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(unified, "CHORD_TIMEOUT_S", 0.01)
    opened: list[bool] = []

    async def handoff(operation: Callable[[], object]) -> None:
        opened.append(True)

    session, _state, _ = _session(tmp_path, terminal_hooks=TerminalHooks(handoff=handoff))
    await session._dispatch(Key.CTRL_X, "")
    await session._dispatch(Key.CHAR, "a")
    await asyncio.sleep(0.05)
    assert opened == []
    assert session._buf.text == "a"


# ── 6. the queue ─────────────────────────────────────────────────────────────


def _rows(state: AppState) -> list[str]:
    from rich.text import Text

    return [Text.from_markup(row).plain for row in queue_rows(state, 80)]


async def test_two_messages_typed_during_a_turn_render_as_two_rows() -> None:
    from tests.unit.test_tui_session_coverage import _make_session

    tui, ctx, _workspace, _input = _make_session()
    ctx.command_bus.register(SendMessageCommand, tui.handle_send)
    active = asyncio.create_task(asyncio.sleep(30))
    tui._agent_task = active
    composer = UnifiedInputSession(ctx.app_state, ctx.command_bus)
    composer.set_mode(InputMode.STREAMING)
    try:
        await _press(composer, [*_chars("also update the changelog"), (Key.ENTER, "")])
        await _press(composer, [*_chars("/compact"), (Key.ENTER, "")])

        ctx.app_state.conversation.agent_state.set(AgentState.THINKING)
        rows = _rows(ctx.app_state)
        assert len(rows) == 2  # one row per message; the last carries "↑ edit queue"
        assert "1  also update the changelog" in rows[0]
        assert "2  /compact" in rows[1]
        assert "edit queue" in rows[1]
        # While the runtime loads (no turn yet) ↑ recalls history: no queue hint.
        ctx.app_state.conversation.agent_state.set(AgentState.IDLE)
        assert "edit queue" not in _rows(ctx.app_state)[1]
        # No "⌛ Queued #N" notification any more.
        assert "Queued" not in (ctx.app_state.conversation.notification() or "")
    finally:
        active.cancel()


def test_at_most_three_rows_then_more() -> None:
    state = AppState.create()
    for index in range(5):
        state.conversation.queue.append(f"message {index}")
    state.input.update(list("typing"), 6)
    rows = _rows(state)
    assert len(rows) == 4
    assert rows[-1].strip() == "+2 more"


async def test_deleting_the_first_leaves_only_the_second_to_be_sent() -> None:
    from tests.unit.test_tui_session_coverage import _make_session

    tui, ctx, _workspace, _input = _make_session()
    ctx.command_bus.register(SendMessageCommand, tui.handle_send)
    started: list[str] = []

    async def agent_task_body(text: str, **_kwargs: object) -> None:
        started.append(text)

    tui.agent_task_body = agent_task_body  # type: ignore[method-assign]
    active = asyncio.create_task(asyncio.sleep(30))
    tui._agent_task = active
    composer = UnifiedInputSession(ctx.app_state, ctx.command_bus)
    composer.set_mode(InputMode.STREAMING)
    await _press(composer, [*_chars("first"), (Key.ENTER, "")])
    await _press(composer, [*_chars("second"), (Key.ENTER, "")])

    queue = ctx.app_state.conversation.queue
    await composer._dispatch(Key.UP, "")  # into the queue: the newest row
    await composer._dispatch(Key.UP, "")  # the first message
    assert queue.focus() == queue.items()[0].id
    rows = _rows(ctx.app_state)
    assert "1  first" in rows[0] and "remove" in rows[-1]  # the focus hint row
    await composer._dispatch(Key.DELETE, "")
    assert queue.texts() == ["second"]
    await composer._dispatch(Key.ESC, "")
    assert queue.focus() is None

    active.cancel()
    await asyncio.gather(active, return_exceptions=True)
    tui._agent_task = None
    tui.advance()
    await asyncio.sleep(0)
    assert started == ["second"]


async def test_enter_in_the_queue_moves_the_message_back_into_the_prompt() -> None:
    state = AppState.create()
    composer = UnifiedInputSession(state, CommandBus())
    composer.set_mode(InputMode.STREAMING)
    state.conversation.queue.append("fix the typo")
    state.conversation.queue.append("then run the tests")

    await composer._dispatch(Key.UP, "")
    assert state.conversation.queue.focus() == state.conversation.queue.items()[-1].id
    await composer._dispatch(Key.ENTER, "")
    assert composer._buf.text == "then run the tests"
    assert state.conversation.queue.texts() == ["fix the typo"]
    assert state.conversation.queue.focus() is None
    # ↑ on a non-empty prompt does not enter the queue.
    await composer._dispatch(Key.UP, "")
    assert state.conversation.queue.focus() is None


async def test_ctrl_enter_while_busy_queues_an_interjection() -> None:
    from tests.unit.test_tui_session_coverage import _make_session

    tui, ctx, _workspace, _input = _make_session()
    ctx.command_bus.register(SendMessageCommand, tui.handle_send)
    active = asyncio.create_task(asyncio.sleep(30))
    tui._agent_task = active
    composer = UnifiedInputSession(ctx.app_state, ctx.command_bus)
    composer.set_mode(InputMode.STREAMING)
    try:
        await _press(composer, [*_chars("after"), (Key.ENTER, "")])
        await _press(composer, [*_chars("now"), (Key.MODIFIED, "ctrl+enter")])
        await _press(composer, [*_chars("also now"), (Key.MODIFIED, "alt+enter")])
        items = ctx.app_state.conversation.queue.items()
        assert [(item.text, item.interject) for item in items] == [
            ("after", False),
            ("now", True),
            ("also now", True),
        ]
        assert "at next tool" in _rows(ctx.app_state)[1]
        assert tui._next_queued_message() == "now"
    finally:
        active.cancel()


# ── 7. Shift+Enter and the kitty keyboard mode ───────────────────────────────


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        (b"13;2u", (Key.SHIFT_ENTER, "")),
        (b"13;5u", (Key.MODIFIED, "ctrl+enter")),
        (b"13;3u", (Key.MODIFIED, "alt+enter")),
        (b"13u", (Key.ENTER, "")),
        (b"27u", (Key.ESC, "")),
        (b"9;2u", (Key.SHIFT_TAB, "")),
        (b"97;5u", (Key.CTRL_A, "")),
        (b"119;5u", (Key.CTRL_W, "")),
        (b"98;3u", (Key.ALT, "b")),
        (b"127;3u", (Key.MODIFIED, "alt+backspace")),
        (b"122;6u", (Key.MODIFIED, "ctrl+shift+z")),
        (b"64u", (Key.AT, "")),
        (b"99;69u", (Key.CTRL_C, "")),  # Caps Lock does not change the key
    ],
)
def test_csi_u_keys_decode_to_their_legacy_keys(sequence: bytes, expected: Press) -> None:
    assert decode_csi(sequence) == expected


def test_the_kitty_mode_is_only_used_where_supported() -> None:
    assert kitty_keyboard_supported({"TERM": "xterm-kitty"})
    assert kitty_keyboard_supported({"TERM_PROGRAM": "WezTerm"})
    assert not kitty_keyboard_supported({"TERM": "xterm-256color"})
    assert not kitty_keyboard_supported({"TERM": "xterm-kitty", "TMUX": "/tmp/tmux"})


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal")
def test_the_posix_backend_pushes_and_restores_the_kitty_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios

    from agenthicc.tui.cbreak_reader import KITTY_KEYBOARD_POP, KITTY_KEYBOARD_PUSH
    from agenthicc.tui.terminal.posix_backend import PosixBackend

    controller, tty = os.openpty()
    try:
        monkeypatch.setenv("TERM", "xterm-kitty")
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("STY", raising=False)
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        backend = PosixBackend()
        backend._fd = tty
        before = termios.tcgetattr(tty)
        with backend.enter_raw_mode():
            assert not termios.tcgetattr(tty)[3] & termios.ICANON
            assert out.getvalue().count(KITTY_KEYBOARD_PUSH) == 1
            assert KITTY_KEYBOARD_POP not in out.getvalue()
        written = out.getvalue()
        assert written.index(KITTY_KEYBOARD_PUSH) < written.index(KITTY_KEYBOARD_POP)
        assert termios.tcgetattr(tty) == before
        backend.close()
    finally:
        os.close(controller)
        os.close(tty)
