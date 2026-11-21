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
from agenthicc.tui.workspace.picker import Picker, picker_height, rows_from_matches

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

    Rendered inside the always-on Live block — nothing pauses or restarts.
    TUX-05: the matches are drawn by :class:`agenthicc.tui.workspace.picker.Picker`
    (groups, match highlight, keys, wrapped details, a position footer).
    """

    name = "trigger_picker"

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
        # The rows are built once per query, not once per redraw: an ``@``
        # listing can hold every file of a project.
        self._picker: Picker[MatchItem] = Picker()
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
            self._picker.set_rows(
                rows_from_matches(self._matches),
                query=self._char + fragment if self._char == "/" else fragment,
            )
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

    def _page_size(self) -> int:
        """Matches the last render showed (at least one)."""
        return max(1, self._picker.visible)

    # ── Overlay interface ──────────────────────────────────────────────────────

    def render(self) -> RenderableType:
        """The prompt line, then the matches drawn by :class:`Picker` (TUX-05)."""
        from rich.console import Group

        from agenthicc.tui.input.renderer import build_prompt
        from agenthicc.tui.theme import glyphs

        prompt_buf = self._buffer.buf
        prompt_cursor = self._buffer.cursor
        if self._paste_state is not None:
            prompt_buf, prompt_cursor = self._paste_state._condensed_view(self._buffer)
        result_lines: list[RenderableType] = [build_prompt(prompt_buf, prompt_cursor)]

        if self._matches:
            size = shutil.get_terminal_size((80, 24))
            g = glyphs()
            picker = self._picker
            picker.footer_keys = f"{g.up}{g.down} tab enter"
            picker.selected = self._selected
            picker.scroll = self._scroll
            result_lines.extend(picker.render(size.columns, picker_height(size.lines)))
            self._scroll = picker.scroll

        return Group(*result_lines)
