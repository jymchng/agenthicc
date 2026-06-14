"""InputBuffer — typed buffer + cursor management.

A pure-Python value object with no I/O.  All mutation goes through named
methods so callers never manipulate buf/cursor directly.
"""
from __future__ import annotations


class InputBuffer:
    """Manages the text buffer and the insertion cursor.

    *buf* is a list of single characters (possibly including ``'\\n'`` for
    multi-line input).  *cursor* is the byte-index of the insertion point;
    it is always in ``[0, len(buf)]``.
    """

    def __init__(self, initial: list[str] | None = None) -> None:
        self._buf: list[str] = list(initial or [])
        self._cursor: int = len(self._buf)

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

    @property
    def text(self) -> str:
        return "".join(self._buf)

    def __len__(self) -> int:
        return len(self._buf)

    # ── mutations ─────────────────────────────────────────────────────────────

    def insert(self, ch: str) -> None:
        """Insert *ch* at the current cursor and advance cursor by 1."""
        self._buf.insert(self._cursor, ch)
        self._cursor += 1

    def insert_many(self, chars: list[str]) -> tuple[int, int]:
        """Insert *chars* at cursor; return ``(start, end)`` range."""
        start = self._cursor
        for ch in chars:
            self._buf.insert(self._cursor, ch)
            self._cursor += 1
        return start, self._cursor

    def delete_before(self) -> None:
        """Backspace — delete the character immediately left of cursor."""
        if self._cursor > 0:
            del self._buf[self._cursor - 1]
            self._cursor -= 1

    def delete_range(self, start: int, end: int) -> None:
        """Delete ``buf[start:end]`` and clamp cursor to new length."""
        del self._buf[start:end]
        self._cursor = min(self._cursor, len(self._buf))

    def set(self, chars: list[str], cursor: int | None = None) -> None:
        """Replace the entire buffer; cursor defaults to end."""
        self._buf = list(chars)
        self._cursor = len(self._buf) if cursor is None else max(0, min(len(self._buf), cursor))

    def clear(self) -> None:
        self._buf.clear()
        self._cursor = 0

    # ── cursor navigation ─────────────────────────────────────────────────────

    def move_left(self) -> None:
        self._cursor = max(0, self._cursor - 1)

    def move_right(self) -> None:
        self._cursor = min(len(self._buf), self._cursor + 1)

    def move_home(self) -> None:
        """Move to start of the current logical line."""
        text_before = "".join(self._buf[: self._cursor])
        last_nl = text_before.rfind("\n")
        self._cursor = last_nl + 1  # 0 when no '\n' (rfind returns -1)

    def move_end(self) -> None:
        """Move to end of the current logical line."""
        rest = "".join(self._buf[self._cursor :])
        next_nl = rest.find("\n")
        self._cursor = len(self._buf) if next_nl == -1 else self._cursor + next_nl

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
        self._cursor = (
            sum(len(all_lines[i]) + 1 for i in range(curr_line - 1)) + target_col
        )
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
        self._cursor = (
            sum(len(all_lines[i]) + 1 for i in range(curr_line + 1)) + target_col
        )
        return True
