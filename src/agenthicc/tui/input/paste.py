"""PasteState — manages bracketed paste condensation.

Large pastes are "condensed" to a single label line so they don't flood
the input bar.  Ctrl+V expands back to the full content.  Backspace deletes
the whole hidden range when the cursor is immediately after its placeholder;
elsewhere it deletes one character at a time while keeping the paste condensed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agenthicc.tui.input.buffer import InputBuffer

_CONDENSE_LINES = 3  # condense if paste has more logical lines than this


@dataclass
class PasteState:
    condensed: bool = False
    label: str = ""
    start: int = 0
    end: int = 0
    count: int = field(default=0, repr=False)
    _label_uses_lines: bool = field(default=False, repr=False)

    def _range(self, buf: InputBuffer) -> tuple[int, int]:
        """Return the hidden range clamped to the current buffer length."""
        start = max(0, min(self.start, len(buf)))
        end = max(start, min(self.end, len(buf)))
        return start, end

    def apply(self, buf: InputBuffer, text: str, cols: int) -> None:
        """Insert *text* at the current cursor; condense if large."""
        start, end = buf.insert_many(list(text))
        self.start = start
        self.end = end
        n_lines = text.count("\n") + 1
        should_condense = n_lines > _CONDENSE_LINES or len(text) > max(cols - 4, 40)
        if should_condense:
            self.count += 1
            self._label_uses_lines = n_lines > 1
            suffix = f"+{n_lines} lines" if n_lines > 1 else f"{len(text)} chars"
            self.label = f"[Pasted text #{self.count} {suffix}]"
            self.condensed = True

    def insert(self, buf: InputBuffer, text: str) -> None:
        """Insert user text while keeping a condensed range correctly located.

        ``InputBuffer`` stores the complete payload, whereas the composer
        renders the payload as one label.  Inserting before that payload must
        move both range boundaries; inserting after it must leave them alone.
        A cursor inside the hidden range can only be an artefact of navigation
        against the unprojected buffer, so it is normalized to the range's end
        before insertion rather than silently hiding the new character.
        """
        chars = list(text)
        if not chars:
            return

        cursor = buf.cursor
        if self.condensed:
            start, end = self._range(buf)
            if start < cursor < end:
                cursor = end
                buf.cursor = cursor

        buf.insert_many(chars)
        if not self.condensed:
            return

        amount = len(chars)
        if cursor <= self.start:
            self.start += amount
            self.end += amount
        elif cursor < self.end:
            self.end += amount

    def expand(self) -> None:
        """Ctrl+V — show full paste content."""
        self.condensed = False

    def delete_condensed(self, buf: InputBuffer) -> None:
        """Delete the hidden paste block while it is still condensed."""
        if not self.condensed:
            return
        start, end, cursor = self.start, self.end, buf.cursor
        buf.delete_range(start, end)
        # Preserve the cursor's logical position around the removed range. In
        # particular, a cursor exactly at the placeholder boundary belongs at
        # ``start`` after the paste disappears, while a typed suffix remains
        # to its right.
        if cursor <= start:
            new_cursor = cursor
        elif cursor >= end:
            new_cursor = start + cursor - end
        else:
            new_cursor = start
        buf.cursor = new_cursor
        self.condensed = False
        self.label = ""
        self.start = self.end = min(start, len(buf))

    def _condensed_view(self, buf: InputBuffer) -> tuple[list[str], int]:
        """Return the composer view with the paste replaced by its label.

        The input buffer keeps the complete pasted payload so submission and
        explicit expansion remain lossless.  The reactive composer instead
        receives this compact projection, which also preserves text typed
        around the hidden paste (for example, a character appended to it).
        """
        if not self.condensed:
            return list(buf.buf), buf.cursor

        start, end = self._range(buf)
        label = list(self.label)
        display = buf.buf[:start] + label + buf.buf[end:]

        if buf.cursor <= start:
            cursor = buf.cursor
        elif buf.cursor <= end:
            cursor = start + len(label)
        else:
            cursor = buf.cursor - (end - start) + len(label)
        return display, cursor

    def _buffer_cursor_from_display(
        self,
        buf: InputBuffer,
        display_cursor: int,
        *,
        prefer_end: bool = False,
    ) -> int:
        """Map a projected composer cursor back to the lossless buffer.

        Positions inside the label are not editable positions.  A left/home
        movement maps them to the range start; a right/end movement maps them
        to the range end.  The label boundary itself is the range end so that
        Backspace can continue to recognize the whole-paste deletion point.
        """
        start, end = self._range(buf)
        label_end = start + len(self.label)
        display_cursor = max(0, min(display_cursor, start + len(self.label) + len(buf) - end))
        if display_cursor <= start:
            return display_cursor
        if display_cursor < label_end:
            return end if prefer_end else start
        if display_cursor == label_end:
            return end
        return display_cursor - len(self.label) + (end - start)

    def move_left(self, buf: InputBuffer) -> None:
        """Move left in the projected composer, keeping the paste atomic."""
        if not self.condensed:
            buf.move_left()
            return
        start, end = self._range(buf)
        if buf.cursor > end:
            buf.move_left()
        elif buf.cursor > start:
            buf.cursor = start
        elif buf.cursor > 0:
            buf.move_left()

    def move_right(self, buf: InputBuffer) -> None:
        """Move right in the projected composer, keeping the paste atomic."""
        if not self.condensed:
            buf.move_right()
            return
        start, end = self._range(buf)
        if buf.cursor == start:
            buf.cursor = end
        elif buf.cursor < end:
            buf.cursor = end
        else:
            buf.move_right()

    def move_home(self, buf: InputBuffer) -> None:
        """Move to the start of the currently displayed composer line."""
        if not self.condensed:
            buf.move_home()
            return
        display, display_cursor = self._condensed_view(buf)
        last_nl = "".join(display[:display_cursor]).rfind("\n")
        target = last_nl + 1
        buf.cursor = self._buffer_cursor_from_display(buf, target)

    def move_end(self, buf: InputBuffer) -> None:
        """Move to the end of the currently displayed composer line."""
        if not self.condensed:
            buf.move_end()
            return
        display, display_cursor = self._condensed_view(buf)
        next_nl = "".join(display[display_cursor:]).find("\n")
        target = len(display) if next_nl == -1 else display_cursor + next_nl
        buf.cursor = self._buffer_cursor_from_display(buf, target, prefer_end=True)

    def move_up(self, buf: InputBuffer) -> bool:
        """Move up in the projected composer; return whether it moved."""
        if not self.condensed:
            return buf.move_up()
        return self._move_vertical(buf, direction=-1)

    def move_down(self, buf: InputBuffer) -> bool:
        """Move down in the projected composer; return whether it moved."""
        if not self.condensed:
            return buf.move_down()
        return self._move_vertical(buf, direction=1)

    def _move_vertical(self, buf: InputBuffer, *, direction: int) -> bool:
        display, display_cursor = self._condensed_view(buf)
        text = "".join(display)
        before = text[:display_cursor]
        lines = text.split("\n")
        lines_before = before.split("\n")
        current_line = len(lines_before) - 1
        current_col = len(lines_before[-1])
        target_line = current_line + direction
        if target_line < 0 or target_line >= len(lines):
            return False
        target_col = min(current_col, len(lines[target_line]))
        target = sum(len(lines[i]) + 1 for i in range(target_line)) + target_col
        buf.cursor = self._buffer_cursor_from_display(buf, target, prefer_end=direction > 0)
        return True

    def backspace(self, buf: InputBuffer) -> None:
        """Delete one character without expanding the condensed paste.

        Characters typed after the paste are deleted first. Once the cursor
        reaches the hidden range, backspace removes one hidden character and
        refreshes the label; this keeps backspace's normal editing semantics
        without flooding the composer with the full paste.
        """
        cursor = buf.cursor
        if cursor <= self.start:
            if cursor > 0:
                buf.delete_before()
                self.start -= 1
                self.end -= 1
            return

        if cursor > self.end:
            buf.delete_before()
            return

        # The cursor is inside or immediately after the hidden range.
        buf.delete_before()
        self.end -= 1
        remaining = "".join(buf.buf[self.start : self.end])
        if not remaining:
            self.condensed = False
            self.label = ""
            self.start = self.end = self.start
            return

        if self._label_uses_lines:
            line_count = remaining.count("\n") + 1
            suffix = f"+{line_count} lines"
        else:
            suffix = f"{len(remaining)} chars"
        self.label = f"[Pasted text #{self.count} {suffix}]"
