"""TriggerPickerOverlay — @mention / /command picker in the Live region (PRD-62, PRD-69).

The picker has no text of its own (TUX-04 §2.2). It reads the composer's
:class:`InputBuffer`: the trigger is the text from the trigger character at
*anchor* to the cursor. It handles only navigation and choice (the ``picker``
keymap context: ↑/↓, Ctrl+P/N, PageUp/PageDown, Tab, Enter, Esc); every other
key edits the prompt through the composer, after which the composer calls
:meth:`TriggerPickerOverlay.refresh` and the picker follows the buffer — or
reports that the trigger is gone and closes.
"""

from __future__ import annotations

import shutil
from enum import Enum, auto
from typing import TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key, key_name
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerHandler, TriggerResult
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import RenderableType

    from agenthicc.tui.input.buffer import InputBuffer
    from agenthicc.tui.input.paste import PasteState
    from agenthicc.tui.keymap import Keymap

__all__ = ["PickerOutcome", "TriggerPickerOverlay"]


class PickerOutcome(Enum):
    """What the composer does after the picker handled a key."""

    STAY = auto()  # the picker stays open
    CLOSE = auto()  # close the picker, keep the prompt
    SUBMIT = auto()  # close the picker and send the prompt


class TriggerPickerOverlay(Overlay):
    """Shows a dropdown picker for @mention and /command triggers.

    Rendered inside the always-on Live block — no pause/restart required.
    PRD-69: uses handler.get_lines() for line-height-aware scroll so long
    descriptions can wrap without overflowing the visible window.
    """

    name = "trigger_picker"
    _MAX_LINES = 12  # terminal-line budget for the dropdown (not item count)

    def __init__(
        self,
        buffer: InputBuffer,
        anchor: int,
        handler: TriggerHandler,
        *,
        cwd: Path,
        keymap: Keymap,
        busy: bool = False,
        paste_state: PasteState | None = None,
    ) -> None:
        self._buffer = buffer
        self._anchor = anchor
        self._handler = handler
        self._char = handler.char
        self._keymap = keymap
        self._ctx = TriggerContext(cwd=cwd, busy=busy)
        self._paste_state = paste_state
        self._fragment: str | None = None
        self._matches: list[MatchItem] = []
        self._selected: int = 0  # index into self._matches
        self._scroll: int = 0  # index of first visible item
        self._hint: str | None = None
        self.refresh()

    # ── the trigger, derived from the buffer ─────────────────────────────────

    @property
    def handler(self) -> TriggerHandler:
        return self._handler

    @property
    def anchor(self) -> int:
        """Index of the trigger character in the composer's buffer."""
        return self._anchor

    @property
    def fragment(self) -> str:
        """The text typed after the trigger character, up to the cursor."""
        return self._fragment or ""

    @property
    def matches(self) -> list[MatchItem]:
        return self._matches

    @property
    def selected(self) -> int:
        return self._selected

    def refresh(self) -> bool:
        """Re-derive the fragment from the buffer; ``False`` when the trigger is gone.

        The trigger is gone when its character was deleted or replaced, when
        the cursor left the span after it, or when the span crosses a line.
        """
        buf = self._buffer.buf
        cursor = self._buffer.cursor
        anchor = self._anchor
        if anchor >= len(buf) or buf[anchor] != self._char or cursor <= anchor:
            return False
        span = buf[anchor + 1 : cursor]
        if "\n" in span or not self._handler.can_activate(buf[:anchor]):
            return False
        fragment = "".join(span)
        if fragment != self._fragment:
            self._fragment = fragment
            self._matches = self._handler.get_matches(fragment, self._ctx)
            self._selected = 0
            self._scroll = 0
            self._hint = self._handler.get_hint(self._matches[0] if self._matches else None)
        return True

    # ── keys ──────────────────────────────────────────────────────────────────

    def choose(self, key: Key, ch: str) -> PickerOutcome | None:
        """Handle a navigation or choice key; ``None`` for a key the prompt edits."""
        action = self._keymap.resolve("picker", key_name(key, ch))
        if action is None:
            return None
        match action.name:
            case "picker.up":
                self._move(-1)
            case "picker.down":
                self._move(1)
            case "picker.page_up":
                self._move(-max(1, self._page_size()))
            case "picker.page_down":
                self._move(max(1, self._page_size()))
            case "picker.close":
                return PickerOutcome.CLOSE
            case "picker.complete":
                self._apply(self._handler.on_select(self._item(), self.fragment, self._pre()))
                return PickerOutcome.CLOSE
            case "picker.choose":
                result = self._handler.on_enter(self._item(), self.fragment, self._pre())
                self._apply(result)
                if result.keep_open and self.refresh():
                    return PickerOutcome.STAY
                return PickerOutcome.SUBMIT if result.submit else PickerOutcome.CLOSE
            case _:
                return None
        return PickerOutcome.STAY

    def handle_key(self, key: Key, ch: str) -> bool:
        """Overlay contract: whether the picker claims *key*.

        The composer routes keys through :meth:`choose`, which also says
        whether to close the picker or send the prompt.
        """
        return self.choose(key, ch) is not None

    def _item(self) -> MatchItem | None:
        return self._matches[self._selected] if self._matches else None

    def _pre(self) -> list[str]:
        return list(self._buffer.buf[: self._anchor])

    def _apply(self, result: TriggerResult) -> None:
        """Replace the trigger span (anchor to cursor) with the handler's result."""
        cursor = self._buffer.cursor
        suffix = self._buffer.buf[cursor:]
        new_cursor = result.cursor if result.cursor is not None else len(result.buffer)
        self._buffer.replace_range(0, len(self._buffer), [*result.buffer, *suffix], new_cursor)
        # Text after the cursor moved by the change in the span's length.
        delta = len(result.buffer) - cursor
        if self._paste_state is not None and delta > 0:
            self._paste_state.note_inserted(cursor, cursor + delta)
        elif self._paste_state is not None and delta < 0:
            self._paste_state.note_deleted(cursor + delta, cursor)

    def _move(self, delta: int) -> None:
        if not self._matches:
            return
        if abs(delta) == 1:
            self._selected = (self._selected + delta) % len(self._matches)
        else:
            self._selected = max(0, min(len(self._matches) - 1, self._selected + delta))
        self._hint = self._handler.get_hint(self._matches[self._selected])

    def _page_size(self) -> int:
        """Matches that fit in one dropdown page (at least one)."""
        cols = shutil.get_terminal_size((80, 24)).columns
        width = max(cols - 4, 20)
        used = 0
        count = 0
        for item in self._matches[self._scroll :]:
            used += len(self._handler.get_lines(item, width))
            if used > self._MAX_LINES:
                break
            count += 1
        return max(1, count)

    # ── Overlay interface ──────────────────────────────────────────────────────

    def render(self) -> RenderableType:
        from rich.console import Group
        from rich.text import Text

        from agenthicc.tui.input.renderer import build_prompt

        prompt_buf = self._buffer.buf
        prompt_cursor = self._buffer.cursor
        if self._paste_state is not None:
            prompt_buf, prompt_cursor = self._paste_state._condensed_view(self._buffer)
        result_lines: list[RenderableType] = [build_prompt(prompt_buf, prompt_cursor)]

        if self._matches:
            cols = shutil.get_terminal_size((80, 24)).columns
            # 4-char prefix "  ▶ " / "    " is added by us below
            avail_w = max(cols - 4, 20)
            item_line_lists = [self._handler.get_lines(item, avail_w) for item in self._matches]

            # Clamp scroll so selected item is fully visible within _MAX_LINES.
            self._clamp_scroll(item_line_lists)

            # Render items from scroll position until line budget exhausted.
            lines_used = 0
            for idx in range(self._scroll, len(self._matches)):
                item_lines = item_line_lists[idx]
                if lines_used + len(item_lines) > self._MAX_LINES:
                    break
                selected = idx == self._selected
                indicator = "▶" if selected else " "
                style = "reverse" if selected else ""
                for li, line_text in enumerate(item_lines):
                    prefix = f"  {indicator} " if li == 0 else "    "
                    result_lines.append(Text(prefix + line_text, style=style))
                lines_used += len(item_lines)

        if self._hint:
            result_lines.append(Text(f"  {self._hint}", style="dim"))

        return Group(*result_lines)

    # ── scroll helpers ─────────────────────────────────────────────────────────

    def _clamp_scroll(self, item_line_lists: list[list[str]]) -> None:
        """Adjust self._scroll so the selected item is fully visible."""
        if not item_line_lists:
            return

        sel = self._selected

        # If selected item is above the window, scroll up.
        if sel < self._scroll:
            self._scroll = sel
            return

        # Count lines from _scroll to sel (inclusive) to check if sel is visible.
        lines_to_sel_end = sum(len(item_line_lists[i]) for i in range(self._scroll, sel + 1))
        if lines_to_sel_end <= self._MAX_LINES:
            return  # already fully visible

        # Selected item is below the window — advance scroll until it fits.
        while self._scroll <= sel:
            self._scroll += 1
            lines_to_sel_end = sum(len(item_line_lists[i]) for i in range(self._scroll, sel + 1))
            if lines_to_sel_end <= self._MAX_LINES:
                break
