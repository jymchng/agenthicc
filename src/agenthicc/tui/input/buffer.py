"""InputBuffer — typed buffer + cursor management.

A pure-Python value object with no I/O.  All mutation goes through named
methods so callers never manipulate buf/cursor directly.

Readline editing (TUX-04 §2.1) lives here too: word motion, the kill ring,
yank and an undo/redo stack of edit groups.  Consecutive single-character
inserts form one group; every other edit is a group of its own, and moving
the cursor ends the current group.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

#: Undo groups kept per buffer (TUX-04 §2.1).
UNDO_LIMIT = 200
#: Kills kept in the ring; Ctrl+Y yanks the newest.
KILL_RING_LIMIT = 16

#: Restores state that lives beside the text (a condensed paste's range).
Restore = Callable[[], None]
#: Captures that state when an undo group starts; returns its restore.
SnapshotHook = Callable[[], Restore]

_EditKind = Literal["insert", "edit"]


def is_word_char(ch: str) -> bool:
    """Letters, digits and ``_`` form words (Unicode-aware); all else separates.

    Path separators therefore stop word motion: Ctrl+W on ``src/agenthicc/tui``
    deletes ``tui``, as in a shell.
    """
    return ch == "_" or ch.isalnum()


@dataclass(frozen=True)
class _Snapshot:
    chars: tuple[str, ...]
    cursor: int
    restore: Restore | None


class InputBuffer:
    """Manages the text buffer and the insertion cursor.

    *buf* is a list of single characters (possibly including ``'\\n'`` for
    multi-line input).  *cursor* is the byte-index of the insertion point;
    it is always in ``[0, len(buf)]``.
    """

    def __init__(self, initial: list[str] | None = None) -> None:
        self._buf: list[str] = list(initial or [])
        self._cursor: int = len(self._buf)
        self._undo: list[_Snapshot] = []
        self._redo: list[_Snapshot] = []
        self._last_edit: _EditKind | None = None
        self._kills: list[str] = []
        #: Whether the last edit was a kill, so the next one appends to it.
        self._killing = False
        #: Called when an undo group starts (the session's paste state).
        self.snapshot_hook: SnapshotHook | None = None

    # ── read-only views ───────────────────────────────────────────────────────

    @property
    def buf(self) -> list[str]:
        return self._buf

    @property
    def cursor(self) -> int:
        return self._cursor

    @cursor.setter
    def cursor(self, v: int) -> None:
        self._cursor = max(0, min(len(self._buf), v))
        self._end_group()

    @property
    def text(self) -> str:
        return "".join(self._buf)

    @property
    def kill_ring(self) -> tuple[str, ...]:
        """Killed text, oldest first."""
        return tuple(self._kills)

    def __len__(self) -> int:
        return len(self._buf)

    # ── undo groups ───────────────────────────────────────────────────────────

    def _snapshot(self) -> _Snapshot:
        hook = self.snapshot_hook
        return _Snapshot(tuple(self._buf), self._cursor, hook() if hook is not None else None)

    def _begin(self, kind: _EditKind) -> None:
        """Start an undo group unless *kind* continues the current one."""
        if kind == "insert" and self._last_edit == "insert":
            return
        self._undo.append(self._snapshot())
        if len(self._undo) > UNDO_LIMIT:
            del self._undo[0]
        self._redo.clear()
        self._last_edit = kind

    def _end_group(self) -> None:
        self._last_edit = None
        self._killing = False

    def _restore(self, snapshot: _Snapshot) -> None:
        self._buf = list(snapshot.chars)
        self._cursor = snapshot.cursor
        if snapshot.restore is not None:
            snapshot.restore()
        self._end_group()

    def undo(self) -> bool:
        """Restore the state before the last edit group; ``False`` if none."""
        if not self._undo:
            return False
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        """Re-apply the last undone edit group; ``False`` if none."""
        if not self._redo:
            return False
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        return True

    def forget_undo(self) -> None:
        """Drop the undo and redo history (a submitted prompt starts afresh)."""
        self._undo.clear()
        self._redo.clear()
        self._end_group()

    # ── mutations ─────────────────────────────────────────────────────────────

    def insert(self, ch: str) -> None:
        """Insert *ch* at the current cursor and advance cursor by 1."""
        self._begin("insert")
        self._killing = False
        self._buf.insert(self._cursor, ch)
        self._cursor += 1

    def insert_many(self, chars: list[str]) -> tuple[int, int]:
        """Insert *chars* at cursor; return ``(start, end)``.

        One typed character continues a run of typing (one undo group);
        several (a paste, a yank) are a group of their own.
        """
        typed = len(chars) == 1
        self._begin("insert" if typed else "edit")
        self._killing = False
        start = self._cursor
        self._buf[start:start] = chars
        self._cursor = start + len(chars)
        self._last_edit = "insert" if typed else None
        return start, self._cursor

    def delete_before(self) -> None:
        """Backspace — delete the character immediately left of cursor."""
        if self._cursor > 0:
            self._begin("edit")
            del self._buf[self._cursor - 1]
            self._cursor -= 1
            self._end_group()

    def delete_after(self) -> bool:
        """Delete — remove the character under the cursor; ``False`` at the end."""
        if self._cursor >= len(self._buf):
            return False
        self._begin("edit")
        del self._buf[self._cursor]
        self._end_group()
        return True

    def delete_range(self, start: int, end: int) -> None:
        """Delete ``buf[start:end]`` and clamp cursor to new length."""
        if start >= end:
            return
        self._begin("edit")
        del self._buf[start:end]
        if self._cursor >= end:
            self._cursor -= end - start
        elif self._cursor > start:
            self._cursor = start
        self._cursor = min(self._cursor, len(self._buf))
        self._end_group()

    def replace_range(self, start: int, end: int, chars: list[str], cursor: int) -> None:
        """Replace ``buf[start:end]`` with *chars* and put the cursor at *cursor*."""
        self._begin("edit")
        self._buf[start:end] = chars
        self._cursor = max(0, min(len(self._buf), cursor))
        self._end_group()

    def set(self, chars: list[str], cursor: int | None = None) -> None:
        """Replace the entire buffer (undoable); cursor defaults to end."""
        if list(chars) != self._buf:
            self._begin("edit")
        self._buf = list(chars)
        self._cursor = len(self._buf) if cursor is None else max(0, min(len(self._buf), cursor))
        self._end_group()

    def clear(self) -> None:
        if self._buf:
            self._begin("edit")
        self._buf.clear()
        self._cursor = 0
        self._end_group()

    # ── kill ring ─────────────────────────────────────────────────────────────

    def _kill(self, start: int, end: int, *, backward: bool) -> tuple[int, int] | None:
        """Delete ``buf[start:end]`` into the kill ring; return the range."""
        if start >= end:
            return None
        killed = "".join(self._buf[start:end])
        if self._killing and self._kills:
            # Consecutive kills accumulate into one entry, as in readline.
            self._kills[-1] = killed + self._kills[-1] if backward else self._kills[-1] + killed
        else:
            self._kills.append(killed)
            if len(self._kills) > KILL_RING_LIMIT:
                del self._kills[0]
        self.delete_range(start, end)
        self._killing = True
        return start, end

    def kill_to_end(self) -> tuple[int, int] | None:
        """Ctrl+K — kill to the end of the line (the newline itself at its end)."""
        end = self._line_end(self._cursor)
        if end == self._cursor and end < len(self._buf):
            end += 1
        return self._kill(self._cursor, end, backward=False)

    def kill_to_start(self) -> tuple[int, int] | None:
        """Ctrl+U — kill to the line start; everything when already there."""
        start = self._line_start(self._cursor)
        if start == self._cursor:
            return self._kill(0, len(self._buf), backward=True)
        return self._kill(start, self._cursor, backward=True)

    def delete_word_before(self) -> tuple[int, int] | None:
        """Ctrl+W / Alt+Backspace — kill the word before the cursor."""
        return self._kill(self._word_start(self._cursor), self._cursor, backward=True)

    def delete_word_after(self) -> tuple[int, int] | None:
        """Alt+D — kill the word after the cursor."""
        return self._kill(self._cursor, self._word_end(self._cursor), backward=False)

    def yank(self) -> tuple[int, int] | None:
        """Ctrl+Y — insert the newest kill at the cursor; return its range."""
        if not self._kills:
            return None
        return self.insert_many(list(self._kills[-1]))

    # ── cursor navigation ─────────────────────────────────────────────────────

    def _line_start(self, index: int) -> int:
        return "".join(self._buf[:index]).rfind("\n") + 1

    def _line_end(self, index: int) -> int:
        rest = "".join(self._buf[index:]).find("\n")
        return len(self._buf) if rest == -1 else index + rest

    def _word_start(self, index: int) -> int:
        while index > 0 and not is_word_char(self._buf[index - 1]):
            index -= 1
        while index > 0 and is_word_char(self._buf[index - 1]):
            index -= 1
        return index

    def _word_end(self, index: int) -> int:
        size = len(self._buf)
        while index < size and not is_word_char(self._buf[index]):
            index += 1
        while index < size and is_word_char(self._buf[index]):
            index += 1
        return index

    def move_left(self) -> None:
        self.cursor = self._cursor - 1

    def move_right(self) -> None:
        self.cursor = self._cursor + 1

    def word_left(self) -> None:
        """Alt+B / Ctrl+← — move to the start of the previous word."""
        self.cursor = self._word_start(self._cursor)

    def word_right(self) -> None:
        """Alt+F / Ctrl+→ — move to the end of the next word."""
        self.cursor = self._word_end(self._cursor)

    def move_home(self) -> None:
        """Start of the logical line; already there, the start of the input."""
        start = self._line_start(self._cursor)
        self.cursor = 0 if start == self._cursor else start

    def move_end(self) -> None:
        """End of the logical line; already there, the end of the input."""
        end = self._line_end(self._cursor)
        self.cursor = len(self._buf) if end == self._cursor else end

    def move_up(self) -> bool:
        """Move to same column on the previous logical line.

        Returns ``True`` when movement happened, ``False`` when already on
        the first line (caller should fall through to history navigation).
        """
        text = self.text
        before = text[: self._cursor]
        all_lines = text.split("\n")
        lines_before = before.split("\n")
        curr_line = len(lines_before) - 1
        curr_col = len(lines_before[-1])
        if curr_line == 0:
            return False
        prev_len = len(all_lines[curr_line - 1])
        target_col = min(curr_col, prev_len)
        self.cursor = sum(len(all_lines[i]) + 1 for i in range(curr_line - 1)) + target_col
        return True

    def move_down(self) -> bool:
        """Move to same column on the next logical line.

        Returns ``True`` when movement happened, ``False`` when already on
        the last line (caller should fall through to history navigation).
        """
        text = self.text
        before = text[: self._cursor]
        all_lines = text.split("\n")
        lines_before = before.split("\n")
        curr_line = len(lines_before) - 1
        curr_col = len(lines_before[-1])
        if curr_line >= len(all_lines) - 1:
            return False
        next_len = len(all_lines[curr_line + 1])
        target_col = min(curr_col, next_len)
        self.cursor = sum(len(all_lines[i]) + 1 for i in range(curr_line + 1)) + target_col
        return True
