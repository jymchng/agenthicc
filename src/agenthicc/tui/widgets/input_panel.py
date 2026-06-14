"""InputPanel widget — unified input bar for the Textual TUI (PRD-55 Phase 3).

Replaces the CBREAK-mode state machine in mention_input.py with a Textual
Widget that owns:
  - Full buffer + cursor state (mirrors mention_input.py line-by-line)
  - Paste condensing (threshold: 3 lines or > cols-4 chars)
  - History navigation
  - Trigger activation (@mention, /slash-command)
  - TriggerMenu child for dropdown display
  - ModeFooter child for mode / notification display

Messages posted:
  InputSubmitted — when the user confirms input (Enter)
  ModeCycled     — when the user presses Shift+Tab
"""
from __future__ import annotations

import shutil
from pathlib import Path

from textual.app import ComposeResult
from textual.events import Key, Paste
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from agenthicc.tui.input_area import PROMPT_CHAR, CURSOR_CHAR
from agenthicc.tui.messages import InputSubmitted, ModeCycled
from agenthicc.tui.trigger import TriggerContext, TriggerHandler, TriggerRegistry
from agenthicc.tui.widgets.trigger_menu import TriggerMenu

__all__ = ["InputPanel"]

# ── Module-level helper ───────────────────────────────────────────────────────

def _find_trigger_tail(
    buf: list[str], registry: TriggerRegistry
) -> "tuple[str, list[str], str] | None":
    """Return (trigger_char, pre_buf, fragment) when buf ends with a trigger token.

    Scans backward from the end of *buf* for a registered trigger character
    with no whitespace between it and the end.  When found, checks that the
    handler would activate at the position of *pre_buf* (i.e. can_activate
    passes).

    Returns None if no activatable trigger tail is found.

    Mirrors the same function from mention_input.py.
    """
    for i in range(len(buf) - 1, -1, -1):
        ch = buf[i]
        if ch.isspace():
            return None  # whitespace terminates the scan
        if ch in registry.chars:
            pre_buf = buf[:i]
            fragment = "".join(buf[i + 1 :])
            handler = registry.get(ch)
            if handler is not None and handler.can_activate(pre_buf):
                return (ch, pre_buf, fragment)
    return None


# ── InputPanel ────────────────────────────────────────────────────────────────


class InputPanel(Widget):
    """Unified input widget: buffer management + trigger menu + mode footer.

    Layout (compose):
        - TriggerMenu (hidden by default, shown on trigger activation)
        - ModeFooter  (always visible 1-row footer)

    The panel owns the full editing state machine previously implemented in
    mention_input.py's ``read_line_with_mention`` function.
    """

    can_focus = True

    # ── Reactive state ─────────────────────────────────────────────────────────

    _paste_condensed: reactive[bool] = reactive(False, layout=True)

    # ── Constants ──────────────────────────────────────────────────────────────

    _PASTE_CONDENSE_THRESHOLD: int = 3

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        registry: TriggerRegistry | None = None,
        cwd: Path | None = None,
        history: list[str] | None = None,
        mode_manager: object | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)

        # Buffer state
        self._buf: list[str] = []
        self._cursor: int = 0

        # History state
        self._history: list[str] = list(history) if history else []
        self._hist_idx: int = len(self._history)
        self._saved_buf: list[str] = []

        # Mode manager (optional — for Shift+Tab cycling)
        self._mode_manager = mode_manager

        # Paste condensing state
        self._paste_label: str = ""
        self._paste_range: tuple[int, int] = (0, 0)
        self._paste_count: int = 0

        # Ctrl+C double-press state
        self._ctrl_c_count: int = 0

        # Trigger registry
        self._registry = registry or TriggerRegistry()
        self._cwd = cwd or Path.cwd()

        # Active trigger state
        self._active_handler: TriggerHandler | None = None
        self._trigger_fragment: str = ""

    def on_mount(self) -> None:
        self._update_display()
        self.focus()

    def compose(self) -> ComposeResult:
        # The Static is the visible prompt area.  In Textual, compose() children
        # are laid out inside the widget's box; their combined height drives
        # `height: auto` sizing.  Using a Static ensures render() output is
        # actually visible (render() alone is only a canvas background when
        # compose() is defined).
        yield Static("", id="prompt-static", markup=True)
        yield TriggerMenu(id="trigger-menu")

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def buf(self) -> list[str]:
        """Current buffer contents (read-only copy)."""
        return list(self._buf)

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def history(self) -> list[str]:
        return list(self._history)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _trigger_menu(self) -> TriggerMenu:
        return self.query_one("#trigger-menu", TriggerMenu)

    def _trigger_ctx(self) -> TriggerContext:
        return TriggerContext(cwd=self._cwd, history=self._history)

    def _cols(self) -> int:
        return shutil.get_terminal_size((80, 24)).columns

    def _update_display(self) -> None:
        """Push current render() output into the visible Static child."""
        try:
            self.query_one("#prompt-static", Static).update(self.render())
        except Exception:
            pass

    def _submit(self) -> None:
        """Submit current buffer: emit InputSubmitted, clear state, append history."""
        result = "".join(self._buf)
        if result:
            self._history.append(result)
        self._hist_idx = len(self._history)
        self._buf = []
        self._cursor = 0
        self._paste_condensed = False
        self._paste_label = ""
        self._paste_range = (0, 0)
        self._ctrl_c_count = 0
        self._update_display()
        self.post_message(InputSubmitted(result))

    def _display_buf(self) -> list[str]:
        """Return the buffer to render (label when condensed, else full buf)."""
        if self._paste_condensed:
            return list(self._paste_label)
        return self._buf

    # ── Rendering ──────────────────────────────────────────────────────────────

    def render(self) -> str:
        """Return a Rich markup string for the input area.

        First line: '[bold green]❯[/bold green] {content}[bold]▌[/bold]'
        Additional lines: '  {content}'
        """
        _INDENT = "  "
        display = self._display_buf()
        cursor_pos = len(display) if self._paste_condensed else self._cursor

        # Split display into visual lines.
        raw_lines: list[list[str]] = []
        current: list[str] = []
        for ch in display:
            if ch == "\n":
                raw_lines.append(current)
                current = []
            else:
                current.append(ch)
        raw_lines.append(current)

        # Find cursor line and column.
        cumulative = 0
        cursor_line = len(raw_lines) - 1
        cursor_col = len(raw_lines[-1])
        for i, ln in enumerate(raw_lines):
            if cumulative + len(ln) >= cursor_pos:
                cursor_line = i
                cursor_col = cursor_pos - cumulative
                break
            cumulative += len(ln) + 1  # +1 for '\n'

        # Build Rich markup lines.
        parts: list[str] = []
        for i, ln in enumerate(raw_lines):
            text = "".join(ln)
            if i == cursor_line:
                col = cursor_col
                content = (
                    _escape_markup(text[:col])
                    + f"[bold]{CURSOR_CHAR}[/bold]"
                    + _escape_markup(text[col:])
                )
            else:
                content = _escape_markup(text)

            if i == 0:
                parts.append(
                    f"[bold green]{PROMPT_CHAR}[/bold green] {content}"
                )
            else:
                parts.append(f"{_INDENT}{content}")

        return "\n".join(parts)

    # ── Key handling ──────────────────────────────────────────────────────────

    def on_key(self, event: Key) -> None:  # noqa: PLR0912, PLR0915
        """Handle all key events for buffer management, history, and triggers."""
        key = event.key
        char = event.character

        menu = self._trigger_menu()

        # ── Delegate to TriggerMenu when visible ─────────────────────────────
        if menu.display:
            # TriggerMenu handles Esc, Enter, Up, Down internally.
            # We only need to handle character typing (fragment update) here.
            if key == "escape":
                # Let TriggerMenu handle it (it'll post TriggerCancelled).
                return
            if key in ("enter", "up", "down"):
                # Let TriggerMenu handle it.
                return
            if key == "backspace":
                # Backspace on fragment: peel one char, update menu.
                if self._trigger_fragment:
                    self._trigger_fragment = self._trigger_fragment[:-1]
                    menu.update_fragment(self._trigger_fragment)
                else:
                    # Backspace past trigger char: cancel.
                    if self._active_handler is not None:
                        self._buf = self._active_handler.on_cancel(
                            self._trigger_fragment, self._buf
                        )
                        # Remove the trigger char itself.
                        if self._buf:
                            self._buf.pop()
                    self._active_handler = None
                    self._trigger_fragment = ""
                    menu.hide()
                    self._cursor = len(self._buf)
                    self._update_display()
                event.stop()
                return
            if char and char.isprintable():
                # Append to fragment.
                self._trigger_fragment += char
                menu.update_fragment(self._trigger_fragment)
                event.stop()
                return
            return

        # ── Normal editing ────────────────────────────────────────────────────

        if key == "ctrl+c":
            self._ctrl_c_count += 1
            if self._ctrl_c_count == 1:
                # First press: clear buf, show warning.
                self._buf = []
                self._cursor = 0
                self._paste_condensed = False
                try:
                    from agenthicc.tui.widgets.mode_footer import ModeFooter as _MF  # noqa: PLC0415
                    footer = self.app.query_one(_MF)
                    footer.set_notification("Press Ctrl+C again to exit.")
                except Exception:  # noqa: BLE001
                    pass
            else:
                # Second press: exit app.
                try:
                    self.app.exit()
                except Exception:  # noqa: BLE001
                    pass
            event.stop()
            self._update_display()
            return

        # Any key other than ctrl+c resets the double-press counter.
        self._ctrl_c_count = 0

        if key == "enter":
            event.stop()
            self._submit()
            return

        if key == "ctrl+j":
            # Insert newline at cursor for multi-line input.
            self._buf.insert(self._cursor, "\n")
            self._cursor += 1
            event.stop()
            self._update_display()
            return

        if key == "ctrl+u":
            self._buf.clear()
            self._cursor = 0
            self._paste_condensed = False
            event.stop()
            self._update_display()
            return

        if key == "ctrl+v":
            if self._paste_condensed:
                self._paste_condensed = False
                self._cursor = len(self._buf)
            event.stop()
            self._update_display()
            return

        if key == "backspace":
            if self._paste_condensed:
                # Delete the entire paste range.
                start, end = self._paste_range
                del self._buf[start:end]
                self._cursor = start
                self._paste_condensed = False
                event.stop()
                self._update_display()
                return

            # Re-enter trigger mode if buf ends with a trigger token.
            _tail = (
                _find_trigger_tail(self._buf, self._registry)
                if self._cursor == len(self._buf)
                else None
            )
            if _tail is not None:
                _tch, _tpre, _tfrag = _tail
                handler = self._registry.get(_tch)
                if handler is not None:
                    self._active_handler = handler
                    self._buf = _tpre
                    self._trigger_fragment = _tfrag
                    ctx = self._trigger_ctx()
                    menu.activate(handler, _tfrag, ctx)
                    event.stop()
                    self._update_display()
                    return

            if self._cursor > 0:
                del self._buf[self._cursor - 1]
                self._cursor -= 1
            event.stop()
            self._update_display()
            return

        if key == "left":
            self._cursor = max(0, self._cursor - 1)
            event.stop()
            self._update_display()
            return

        if key == "right":
            self._cursor = min(len(self._buf), self._cursor + 1)
            event.stop()
            self._update_display()
            return

        if key == "home":
            # Move to start of current line.
            text_before = "".join(self._buf[: self._cursor])
            last_nl = text_before.rfind("\n")
            self._cursor = last_nl + 1  # 0 when no '\n' found (rfind returns -1)
            event.stop()
            self._update_display()
            return

        if key == "end":
            # Move to end of current line.
            rest = "".join(self._buf[self._cursor :])
            next_nl = rest.find("\n")
            self._cursor = (
                len(self._buf) if next_nl == -1 else self._cursor + next_nl
            )
            event.stop()
            self._update_display()
            return

        if key == "up":
            # Within multiline: go to previous line.  At first line: history back.
            _text = "".join(self._buf)
            _before = _text[: self._cursor]
            _all_lines = _text.split("\n") if _text else [""]
            _lines_before = _before.split("\n")
            _curr_line = len(_lines_before) - 1
            _curr_col = len(_lines_before[-1])
            if _curr_line > 0:
                _prev_len = len(_all_lines[_curr_line - 1])
                _target_col = min(_curr_col, _prev_len)
                self._cursor = (
                    sum(len(_all_lines[i]) + 1 for i in range(_curr_line - 1))
                    + _target_col
                )
            else:
                if self._hist_idx == len(self._history):
                    self._saved_buf = list(self._buf)
                if self._hist_idx > 0:
                    self._hist_idx -= 1
                    self._buf = list(self._history[self._hist_idx])
                    self._cursor = len(self._buf)
            event.stop()
            self._update_display()
            return

        if key == "down":
            # Within multiline: go to next line.  At last line: history forward.
            _text = "".join(self._buf)
            _before = _text[: self._cursor]
            _all_lines = _text.split("\n") if _text else [""]
            _lines_before = _before.split("\n")
            _curr_line = len(_lines_before) - 1
            _curr_col = len(_lines_before[-1])
            if _curr_line < len(_all_lines) - 1:
                _next_len = len(_all_lines[_curr_line + 1])
                _target_col = min(_curr_col, _next_len)
                self._cursor = (
                    sum(len(_all_lines[i]) + 1 for i in range(_curr_line + 1))
                    + _target_col
                )
            else:
                if self._hist_idx < len(self._history) - 1:
                    self._hist_idx += 1
                    self._buf = list(self._history[self._hist_idx])
                    self._cursor = len(self._buf)
                elif self._hist_idx == len(self._history) - 1:
                    self._hist_idx = len(self._history)
                    self._buf = list(self._saved_buf)
                    self._cursor = len(self._buf)
            event.stop()
            self._update_display()
            return

        if key == "shift+tab":
            # Cycle mode via mode_manager when available, else use stub values.
            if self._mode_manager is not None:
                try:
                    self._mode_manager.cycle()
                    active = self._mode_manager.active
                    self.post_message(ModeCycled(new_name=active.name, new_badge=active.badge))
                except Exception:
                    self.post_message(ModeCycled(new_name="Auto", new_badge="⏵⏵"))
            else:
                self.post_message(ModeCycled(new_name="Auto", new_badge="⏵⏵"))
            event.stop()
            return

        # ── Trigger character detection ────────────────────────────────────────
        if char and char in self._registry.chars:
            trigger_ch = char
            _tail = (
                _find_trigger_tail(self._buf, self._registry)
                if self._cursor == len(self._buf)
                else None
            )
            if _tail is not None:
                _tch, _tpre, _tfrag = _tail
                handler = self._registry.get(_tch)
                if handler is not None:
                    self._active_handler = handler
                    self._buf = _tpre
                    self._trigger_fragment = _tfrag + trigger_ch
                    ctx = self._trigger_ctx()
                    menu.activate(handler, self._trigger_fragment, ctx)
                    event.stop()
                    self._update_display()
                    return

            handler = self._registry.get(trigger_ch)
            buf_pre = self._buf[: self._cursor]
            if handler is not None and handler.can_activate(buf_pre):
                self._active_handler = handler
                self._trigger_fragment = ""
                ctx = self._trigger_ctx()
                menu.activate(handler, "", ctx)
                event.stop()
                self._update_display()
                return

            # Trigger char not activatable — treat as normal char.
            if char:
                self._buf.insert(self._cursor, char)
                self._cursor += 1
                event.stop()
                self._update_display()
            return

        # ── Printable character ────────────────────────────────────────────────
        if char and char.isprintable():
            # Exit condensed mode on any printable key.
            if self._paste_condensed:
                self._paste_condensed = False
                self._cursor = len(self._buf)

            # Trigger-tail re-entry only when cursor is at end.
            _tail = (
                None
                if char.isspace() or self._cursor < len(self._buf)
                else _find_trigger_tail(self._buf, self._registry)
            )
            if _tail is not None:
                _tch, _tpre, _tfrag = _tail
                handler = self._registry.get(_tch)
                if handler is not None:
                    self._active_handler = handler
                    self._buf = _tpre
                    self._trigger_fragment = _tfrag + char
                    ctx = self._trigger_ctx()
                    menu.activate(handler, self._trigger_fragment, ctx)
                    event.stop()
                    self._update_display()
                    return

            self._buf.insert(self._cursor, char)
            self._cursor += 1
            event.stop()
            self._update_display()

    # ── Paste handling ────────────────────────────────────────────────────────

    def on_paste(self, event: Paste) -> None:
        """Handle bracketed paste events from Textual."""
        paste_text = event.text
        if not paste_text:
            return

        paste_chars = list(paste_text)
        _paste_start = self._cursor
        self._buf[self._cursor : self._cursor] = paste_chars
        self._cursor += len(paste_chars)
        self._paste_range = (_paste_start, self._cursor)
        # Reset history navigation.
        self._hist_idx = len(self._history)

        # Decide whether to condense.
        cols = self._cols()
        n_lines = paste_text.count("\n") + 1
        _should_condense = (
            n_lines > self._PASTE_CONDENSE_THRESHOLD
            or len(paste_text) > max(cols - 4, 40)
        )
        if _should_condense:
            self._paste_count += 1
            suffix = (
                f"+{n_lines} lines" if n_lines > 1 else f"{len(paste_text)} chars"
            )
            self._paste_label = f"Pasted text #{self._paste_count} {suffix}"
            self._paste_condensed = True
        self._update_display()

    # ── TriggerMenu message handlers ──────────────────────────────────────────

    def on_trigger_menu_trigger_selected(
        self, event: TriggerMenu.TriggerSelected
    ) -> None:
        """User selected a match item from TriggerMenu."""
        event.stop()
        if self._active_handler is not None:
            self._buf = self._active_handler.on_select(
                event.item, self._trigger_fragment, self._buf
            )
        self._active_handler = None
        self._trigger_fragment = ""
        self._cursor = len(self._buf)
        self._trigger_menu().hide()
        self._update_display()

    def on_trigger_menu_trigger_cancelled(
        self, event: TriggerMenu.TriggerCancelled
    ) -> None:
        """User cancelled the TriggerMenu (Esc)."""
        event.stop()
        if self._active_handler is not None:
            self._buf = self._active_handler.on_cancel(
                self._trigger_fragment, self._buf
            )
        self._active_handler = None
        self._trigger_fragment = ""
        self._cursor = len(self._buf)
        self._trigger_menu().hide()
        self._update_display()


# ── Markup escaping helper ────────────────────────────────────────────────────


def _escape_markup(text: str) -> str:
    """Escape Rich markup special characters in *text*."""
    return text.replace("[", r"\[").replace("]", r"\]")
