"""InputState — pure state machine for the input bar."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import ClassVar

from .terminal import Key

__all__ = ["InputResult", "InputResultKind", "InputState"]


class InputResultKind(Enum):
    CONTINUE = auto()
    SUBMIT = auto()
    EXIT = auto()
    SPECIAL = auto()


@dataclass
class InputResult:
    kind: InputResultKind
    text: str = ""
    key: Key | None = None

    @classmethod
    def continue_(cls) -> InputResult:
        return cls(kind=InputResultKind.CONTINUE)

    @classmethod
    def submit(cls, text: str) -> InputResult:
        return cls(kind=InputResultKind.SUBMIT, text=text)

    @classmethod
    def exit_(cls) -> InputResult:
        return cls(kind=InputResultKind.EXIT)


class InputState:
    """Pure state machine for the text input bar."""

    def __init__(self, history: list[str] | None = None) -> None:
        self._buf: list[str] = []
        self._cursor: int = 0
        self._history: list[str] = list(history or [])
        self._hist_idx: int = -1       # -1 = not navigating
        self._saved_buf: list[str] = []
        self._kill_ring: str = ""
        self.ctrl_c_count: int = 0

    @property
    def history(self) -> list[str]:
        return list(self._history)

    @property
    def text(self) -> str:
        return "".join(self._buf)

    @property
    def cursor(self) -> int:
        return self._cursor

    def _set_text(self, text: str) -> None:
        self._buf = list(text)
        self._cursor = len(self._buf)

    def handle(self, key: Key, char: str = "") -> InputResult:
        if key == Key.CHAR:
            self._buf.insert(self._cursor, char)
            self._cursor += 1
            self.ctrl_c_count = 0
            return InputResult.continue_()

        if key == Key.ENTER:
            text = self.text
            # Trailing backslash continues the line (inserts newline)
            if text.endswith("\\"):
                self._buf[-1] = "\n"
                self.ctrl_c_count = 0
                return InputResult.continue_()
            if text:
                self._history.append(text)
            self._hist_idx = -1
            self._buf.clear()
            self._cursor = 0
            self.ctrl_c_count = 0
            return InputResult.submit(text)

        if key == Key.BACKSPACE:
            if self._cursor > 0:
                del self._buf[self._cursor - 1]
                self._cursor -= 1
            self.ctrl_c_count = 0
            return InputResult.continue_()

        if key == Key.DELETE:
            if self._cursor < len(self._buf):
                del self._buf[self._cursor]
            self.ctrl_c_count = 0
            return InputResult.continue_()

        if key == Key.LEFT:
            if self._cursor > 0:
                self._cursor -= 1
            return InputResult.continue_()

        if key == Key.RIGHT:
            if self._cursor < len(self._buf):
                self._cursor += 1
            return InputResult.continue_()

        if key == Key.HOME or key == Key.CTRL_A:
            self._cursor = 0
            return InputResult.continue_()

        if key == Key.END or key == Key.CTRL_E:
            self._cursor = len(self._buf)
            return InputResult.continue_()

        if key == Key.CTRL_U:
            self._kill_ring = "".join(self._buf[: self._cursor])
            del self._buf[: self._cursor]
            self._cursor = 0
            return InputResult.continue_()

        if key == Key.CTRL_K:
            self._kill_ring = "".join(self._buf[self._cursor :])
            del self._buf[self._cursor :]
            return InputResult.continue_()

        if key == Key.CTRL_W:
            # delete word before cursor
            pos = self._cursor
            while pos > 0 and self._buf[pos - 1] == " ":
                pos -= 1
            while pos > 0 and self._buf[pos - 1] != " ":
                pos -= 1
            self._kill_ring = "".join(self._buf[pos : self._cursor])
            del self._buf[pos : self._cursor]
            self._cursor = pos
            return InputResult.continue_()

        if key == Key.CTRL_Y:
            for ch in self._kill_ring:
                self._buf.insert(self._cursor, ch)
                self._cursor += 1
            return InputResult.continue_()

        if key == Key.CTRL_C:
            if len(self._buf) > 0:
                # First Ctrl+C: clear buffer
                self._buf.clear()
                self._cursor = 0
                self.ctrl_c_count = 1
                return InputResult.continue_()
            self.ctrl_c_count += 1
            if self.ctrl_c_count >= 2:
                return InputResult.exit_()
            return InputResult.continue_()

        if key == Key.CTRL_D:
            if len(self._buf) == 0:
                return InputResult.exit_()
            # Non-empty: submit current text (same as Enter)
            text = self.text
            if text:
                self._history.append(text)
            self._hist_idx = -1
            self._buf.clear()
            self._cursor = 0
            self.ctrl_c_count = 0
            return InputResult.submit(text)

        if key == Key.UP:
            if not self._history:
                return InputResult.continue_()
            if self._hist_idx == -1:
                self._saved_buf = list(self._buf)
                self._hist_idx = len(self._history) - 1
            elif self._hist_idx > 0:
                self._hist_idx -= 1
            self._set_text(self._history[self._hist_idx])
            return InputResult.continue_()

        if key == Key.DOWN:
            if self._hist_idx == -1:
                return InputResult.continue_()
            self._hist_idx += 1
            if self._hist_idx >= len(self._history):
                self._hist_idx = -1
                self._buf = list(self._saved_buf)
                self._cursor = len(self._buf)
            else:
                self._set_text(self._history[self._hist_idx])
            return InputResult.continue_()

        if key in (Key.NEWLINE, Key.ALT_ENTER, Key.SHIFT_ENTER):
            self._buf.insert(self._cursor, "\n")
            self._cursor += 1
            self.ctrl_c_count = 0
            return InputResult.continue_()

        # Any unhandled key resets ctrl_c count
        self.ctrl_c_count = 0
        return InputResult.continue_()

    def push_history(self, text: str) -> None:
        if text:
            self._history.append(text)

    def reset(self) -> None:
        self._buf.clear()
        self._cursor = 0
        self._hist_idx = -1

    @classmethod
    def exit(cls) -> InputResult:
        return cls(kind=InputResultKind.EXIT)
