"""A one-line text editor for the hub's search, dispatch and prompt lines."""

from __future__ import annotations

import enum

from agenthicc.tui.cbreak_reader import Key

__all__ = ["EditResult", "LineEditor"]


class EditResult(enum.Enum):
    CHANGED = "changed"
    MOVED = "moved"
    SUBMIT = "submit"
    CANCEL = "cancel"
    IGNORED = "ignored"


class LineEditor:
    """Text and a cursor; Enter submits, Esc cancels.

    Typing keys are the editor's own, not key-map actions: while an editor has
    the focus every printable key is text. With ``multiline`` Ctrl+J inserts a
    line break (the dispatch line, AGX-05 §5.3); otherwise a paste's line
    breaks become spaces.
    """

    def __init__(self, text: str = "", *, multiline: bool = False, limit: int = 4_000) -> None:
        self._chars = list(text[:limit])
        self.cursor = len(self._chars)
        self.multiline = multiline
        self.limit = limit

    @property
    def text(self) -> str:
        return "".join(self._chars)

    def set(self, text: str) -> None:
        self._chars = list(text[: self.limit])
        self.cursor = len(self._chars)

    def _insert(self, text: str) -> EditResult:
        if not self.multiline:
            text = " ".join(text.splitlines())
        room = self.limit - len(self._chars)
        if room <= 0 or not text:
            return EditResult.IGNORED
        text = text[:room]
        self._chars[self.cursor : self.cursor] = list(text)
        self.cursor += len(text)
        return EditResult.CHANGED

    def handle(self, key: Key, ch: str) -> EditResult:
        """Apply one key; returns what it did."""
        if key is Key.ENTER:
            return EditResult.SUBMIT
        if key is Key.ESC:
            return EditResult.CANCEL
        if key is Key.CHAR and ch:
            return self._insert(ch)
        if key is Key.AT:
            return self._insert("@")
        if key is Key.PASTE:
            return self._insert(ch)
        if key is Key.CTRL_ENTER and self.multiline:
            return self._insert("\n")
        if key is Key.BACKSPACE:
            if self.cursor == 0:
                return EditResult.IGNORED
            del self._chars[self.cursor - 1]
            self.cursor -= 1
            return EditResult.CHANGED
        if key is Key.DELETE:
            if self.cursor >= len(self._chars):
                return EditResult.IGNORED
            del self._chars[self.cursor]
            return EditResult.CHANGED
        if key is Key.CTRL_U:
            if not self._chars:
                return EditResult.IGNORED
            self._chars.clear()
            self.cursor = 0
            return EditResult.CHANGED
        if key is Key.LEFT:
            self.cursor = max(0, self.cursor - 1)
            return EditResult.MOVED
        if key is Key.RIGHT:
            self.cursor = min(len(self._chars), self.cursor + 1)
            return EditResult.MOVED
        if key is Key.HOME:
            self.cursor = 0
            return EditResult.MOVED
        if key is Key.END:
            self.cursor = len(self._chars)
            return EditResult.MOVED
        return EditResult.IGNORED

    def view(self, width: int) -> tuple[str, int]:
        """The visible slice of the text for *width* cells and the cursor column.

        Line breaks show as ``↵``; long text scrolls so the cursor stays in view.
        """
        shown = self.text.replace("\n", "↵")
        width = max(1, width)
        start = max(0, self.cursor - width + 1)
        return shown[start : start + width], self.cursor - start
