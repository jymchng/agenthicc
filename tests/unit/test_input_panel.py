"""Tests for InputPanel and TriggerMenu widgets (PRD-55 Phase 3).

Uses Textual's Pilot API for widget interaction wherever possible; falls back
to direct state inspection for headless-safe assertions.

All tests are tagged @pytest.mark.unit.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from textual.app import App, ComposeResult
from textual.events import Key as TextualKey, Paste as TextualPaste

from agenthicc.tui.messages import InputSubmitted, ModeCycled
from agenthicc.tui.trigger import TriggerRegistry
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.widgets.input_panel import InputPanel, _find_trigger_tail
from agenthicc.tui.widgets.mode_footer import ModeFooter
from agenthicc.tui.widgets.trigger_menu import TriggerMenu


# ── Minimal host app ──────────────────────────────────────────────────────────


class PanelApp(App):
    """Minimal Textual app wrapping InputPanel for tests."""

    CSS = """
    Screen { height: 24; }
    InputPanel { height: auto; }
    TriggerMenu { height: auto; }
    ModeFooter { height: 1; }
    """

    def __init__(self, trigger_registry: TriggerRegistry | None = None) -> None:
        super().__init__()
        self._trigger_registry = trigger_registry or TriggerRegistry()

    def compose(self) -> ComposeResult:
        yield InputPanel(registry=self._trigger_registry, id="panel")
        yield ModeFooter(id="mode-footer")

    def on_mount(self) -> None:
        # Focus the InputPanel so key events reach it.
        self.query_one("#panel", InputPanel).focus()

    def panel(self) -> InputPanel:
        return self.query_one("#panel", InputPanel)


# ── Helper ────────────────────────────────────────────────────────────────────


async def _type(pilot, text: str) -> None:
    """Type each character in *text* via pilot.press()."""
    for ch in text:
        await pilot.press(ch)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_typing_chars() -> None:
    """Typing characters inserts them into the buffer."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        panel = app.panel()
        await _type(pilot, "hello")
        assert panel._buf == list("hello")
        assert panel._cursor == 5


@pytest.mark.unit
async def test_enter_submits() -> None:
    """Pressing Enter emits InputSubmitted with the buffered text."""
    received: list[str] = []

    class TrackingApp(PanelApp):
        def on_input_submitted(self, msg: InputSubmitted) -> None:
            received.append(msg.value)

    app = TrackingApp()
    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "hello")
        await pilot.press("enter")
        assert received == ["hello"]
        # Buffer cleared after submit.
        assert app.panel()._buf == []


@pytest.mark.unit
async def test_ctrl_j_newline() -> None:
    """Ctrl+J inserts a newline into the buffer."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "line1")
        await pilot.press("ctrl+j")
        await _type(pilot, "line2")
        panel = app.panel()
        buf_text = "".join(panel._buf)
        assert "\n" in buf_text
        assert "line1" in buf_text
        assert "line2" in buf_text


@pytest.mark.unit
async def test_backspace_deletes() -> None:
    """Backspace removes the character before the cursor."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "abc")
        await pilot.press("backspace")
        panel = app.panel()
        assert panel._buf == list("ab")
        assert panel._cursor == 2


@pytest.mark.unit
async def test_ctrl_u_clears() -> None:
    """Ctrl+U clears the entire buffer."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "hello")
        await pilot.press("ctrl+u")
        panel = app.panel()
        assert panel._buf == []
        assert panel._cursor == 0


@pytest.mark.unit
async def test_paste_condense() -> None:
    """Pasting more than threshold lines condenses the paste."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        panel = app.panel()
        big_paste = "\n".join(f"line{i}" for i in range(10))
        # Invoke the paste handler directly — this mirrors what Textual does
        # when it receives a bracketed-paste event from the terminal.
        panel.on_paste(TextualPaste(big_paste))
        assert panel._paste_condensed is True
        assert "Pasted text" in panel._paste_label


@pytest.mark.unit
async def test_paste_expand() -> None:
    """Ctrl+V expands a condensed paste."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        panel = app.panel()
        big_paste = "\n".join(f"line{i}" for i in range(10))
        panel.on_paste(TextualPaste(big_paste))
        assert panel._paste_condensed is True

        await pilot.press("ctrl+v")
        assert panel._paste_condensed is False


@pytest.mark.unit
async def test_paste_backspace_deletes_all() -> None:
    """Backspace on a condensed paste deletes the entire pasted range."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        panel = app.panel()
        big_paste = "\n".join(f"line{i}" for i in range(10))
        panel.on_paste(TextualPaste(big_paste))
        assert panel._paste_condensed is True

        await pilot.press("backspace")
        assert panel._buf == []
        assert panel._paste_condensed is False


@pytest.mark.unit
async def test_history_navigation() -> None:
    """Up arrow after two submissions navigates to most-recent entry."""
    received: list[str] = []

    class TrackingApp(PanelApp):
        def on_input_submitted(self, msg: InputSubmitted) -> None:
            received.append(msg.value)

    app = TrackingApp()
    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "first")
        await pilot.press("enter")
        await _type(pilot, "second")
        await pilot.press("enter")
        assert received == ["first", "second"]

        # Press up — should restore "second"
        await pilot.press("up")
        panel = app.panel()
        assert "".join(panel._buf) == "second"


@pytest.mark.unit
async def test_trigger_at_activates() -> None:
    """Typing '@' at a valid position shows TriggerMenu."""
    registry = TriggerRegistry()
    registry.register(AtMentionTrigger())
    app = PanelApp(trigger_registry=registry)

    async with app.run_test(headless=True) as pilot:
        await pilot.press("@")
        panel = app.panel()
        menu = panel._trigger_menu()
        assert menu.display is True


@pytest.mark.unit
async def test_trigger_slash_activates() -> None:
    """Typing '/' on an empty buffer shows TriggerMenu (if handler registered)."""
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger

    registry = TriggerRegistry()
    registry.register(SlashCommandTrigger())
    app = PanelApp(trigger_registry=registry)

    async with app.run_test(headless=True) as pilot:
        await pilot.press("/")
        panel = app.panel()
        menu = panel._trigger_menu()
        assert menu.display is True


@pytest.mark.unit
async def test_home_end_keys() -> None:
    """Home moves cursor to start of current line; End moves to its end."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "hello")
        await pilot.press("ctrl+j")
        await _type(pilot, "world")
        panel = app.panel()
        # Cursor is at end of "world".
        full_text = "".join(panel._buf)
        assert full_text == "hello\nworld"

        # Home — should move cursor to start of "world" (after the newline).
        await pilot.press("home")
        assert panel._cursor == len("hello\n")

        # End — should move cursor to end of "world".
        await pilot.press("end")
        assert panel._cursor == len("hello\nworld")


@pytest.mark.unit
async def test_cursor_left_right() -> None:
    """Left/right arrow keys move the cursor."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "abc")
        panel = app.panel()
        assert panel._cursor == 3

        await pilot.press("left")
        assert panel._cursor == 2

        await pilot.press("left")
        assert panel._cursor == 1

        await pilot.press("right")
        assert panel._cursor == 2


@pytest.mark.unit
async def test_multiline_up_down() -> None:
    """Up key on second line of multiline input moves cursor to first line."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "line1")
        await pilot.press("ctrl+j")
        await _type(pilot, "line2")
        panel = app.panel()
        # cursor is at end of "line2" (position 11 in "line1\nline2")
        assert panel._cursor == len("line1\nline2")

        await pilot.press("up")
        # Should be on "line1", same column (5), so cursor = 5.
        assert panel._cursor == 5


# ── _find_trigger_tail unit tests (pure function) ─────────────────────────────


@pytest.mark.unit
def test_find_trigger_tail_at() -> None:
    """_find_trigger_tail finds '@' token at end of buf."""
    registry = TriggerRegistry()
    registry.register(AtMentionTrigger())

    buf = list("hello @src")
    result = _find_trigger_tail(buf, registry)
    assert result is not None
    tch, pre, frag = result
    assert tch == "@"
    assert frag == "src"
    assert pre == list("hello ")


@pytest.mark.unit
def test_find_trigger_tail_no_match() -> None:
    """_find_trigger_tail returns None when no trigger in buf."""
    registry = TriggerRegistry()
    registry.register(AtMentionTrigger())

    buf = list("hello world")
    result = _find_trigger_tail(buf, registry)
    assert result is None


@pytest.mark.unit
def test_find_trigger_tail_whitespace_stops_scan() -> None:
    """_find_trigger_tail stops at whitespace even if '@' is further left."""
    registry = TriggerRegistry()
    registry.register(AtMentionTrigger())

    # '@src foo' — the whitespace in 'foo' (well, space before foo) prevents re-entry
    buf = list("@src foo")
    result = _find_trigger_tail(buf, registry)
    # 'foo' has no trigger char; scan hits the space and returns None.
    assert result is None


# ── Added tests for skipped scenarios ─────────────────────────────────────────


@pytest.mark.unit
async def test_ctrl_c_first_press_shows_warning() -> None:
    """Ctrl+C once clears buffer and shows 'Press Ctrl+C again' notification."""
    notifications: list[str] = []

    class TrackingApp(PanelApp):
        pass

    app = TrackingApp()
    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "hello")
        panel = app.panel()
        assert panel._buf == list("hello")

        await pilot.press("ctrl+c")
        # Buffer should be cleared on first Ctrl+C.
        assert panel._buf == []
        assert panel._cursor == 0
        # Notification should be set in ModeFooter (now at app level, not inside InputPanel).
        footer = app.query_one(ModeFooter)
        assert footer.notification is not None
        assert "Ctrl+C" in footer.notification


@pytest.mark.unit
async def test_ctrl_c_second_press_exits() -> None:
    """Two Ctrl+C presses in succession should call app.exit()."""
    exit_called: list[bool] = []

    class ExitTrackingApp(PanelApp):
        def exit(self, *args, **kwargs) -> None:
            exit_called.append(True)
            # Don't actually exit in tests — just record it.

    app = ExitTrackingApp()
    async with app.run_test(headless=True) as pilot:
        await pilot.press("ctrl+c")  # first press — shows warning
        await pilot.press("ctrl+c")  # second press — should call exit
        assert exit_called, "app.exit() should have been called on second Ctrl+C"


@pytest.mark.unit
async def test_slash_trigger_after_newline() -> None:
    """'/' typed at the start of a new line (after newline) activates slash trigger."""
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger

    registry = TriggerRegistry()
    registry.register(SlashCommandTrigger())
    app = PanelApp(trigger_registry=registry)

    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "text")
        await pilot.press("ctrl+j")   # insert newline
        await pilot.press("/")        # '/' after a newline → triggers slash menu
        panel = app.panel()
        menu = panel._trigger_menu()
        assert menu.display is True, "TriggerMenu should be visible after '/' on a new line"


@pytest.mark.unit
async def test_at_trigger_after_space() -> None:
    """'@' typed immediately after a space activates @mention trigger."""
    registry = TriggerRegistry()
    registry.register(AtMentionTrigger())
    app = PanelApp(trigger_registry=registry)

    async with app.run_test(headless=True) as pilot:
        await _type(pilot, "word ")   # trailing space
        await pilot.press("@")        # '@' after space → activates
        panel = app.panel()
        menu = panel._trigger_menu()
        assert menu.display is True, "TriggerMenu should be visible after '@' following a space"


@pytest.mark.unit
async def test_paste_inline_label_in_render() -> None:
    """Pasting 10 lines condenses the paste; render() shows the label string."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        panel = app.panel()
        big_paste = "\n".join(f"line{i}" for i in range(10))
        panel.on_paste(TextualPaste(big_paste))
        assert panel._paste_condensed is True

        rendered = panel.render()
        # The paste label (e.g. "Pasted text #1 +10 lines") must appear in the render.
        assert panel._paste_label in rendered, (
            f"Paste label {panel._paste_label!r} not found in rendered output: {rendered!r}"
        )


@pytest.mark.unit
async def test_history_cycled_after_submit() -> None:
    """Submit 3 messages; up/up/up restores the first message."""
    received: list[str] = []

    class TrackingApp(PanelApp):
        def on_input_submitted(self, msg: InputSubmitted) -> None:
            received.append(msg.value)

    app = TrackingApp()
    async with app.run_test(headless=True) as pilot:
        for text in ("first", "second", "third"):
            await _type(pilot, text)
            await pilot.press("enter")
        assert received == ["first", "second", "third"]

        # Navigate back through history: up × 3 should reach "first".
        await pilot.press("up")  # → "third"
        await pilot.press("up")  # → "second"
        await pilot.press("up")  # → "first"

        panel = app.panel()
        assert "".join(panel._buf) == "first", (
            f"Expected 'first' but got {''.join(panel._buf)!r}"
        )


@pytest.mark.unit
async def test_multiline_cursor_up_down() -> None:
    """Cursor Up/Down tracks column correctly across multiline input."""
    app = PanelApp()
    async with app.run_test(headless=True) as pilot:
        # Line 1: "abc" (length 3), then newline, Line 2: "de" (length 2)
        await _type(pilot, "abc")
        await pilot.press("ctrl+j")
        await _type(pilot, "de")
        panel = app.panel()

        # Cursor is at end of "de" — position 6 in "abc\nde"
        assert panel._cursor == len("abc\nde"), f"Cursor should be at 6, got {panel._cursor}"

        # Press Up: should move to "abc" at column min(2, 3) = 2 (col 2 of "de")
        await pilot.press("up")
        assert panel._cursor == 2, f"After Up, cursor should be at col 2 of 'abc', got {panel._cursor}"

        # Press Down: should move back to "de" at column min(2, 2) = 2
        await pilot.press("down")
        assert panel._cursor == len("abc\nde"), (
            f"After Down, cursor should be at end of 'de', got {panel._cursor}"
        )
