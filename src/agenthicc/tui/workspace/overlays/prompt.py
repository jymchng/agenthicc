"""PromptOverlay — reusable base for overlays that need a text input buffer (PRD-86).

Inherits Overlay and adds an embedded InputBuffer with standard single-line
editing key dispatch.  Subclasses call _handle_prompt_key(key, ch) and read
_prompt_text when the user submits.
"""
from __future__ import annotations

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.workspace.overlay import Overlay


class PromptOverlay(Overlay):
    """Overlay base with an embedded single-line text input buffer.

    Subclasses:
    - Call super().__init__() in their own __init__.
    - Call _handle_prompt_key(key, ch) and check the bool return value to
      decide whether the key was consumed by the buffer or should be handled
      by the subclass's own state machine.
    - Read _prompt_text to get the current buffer content.
    - The buffer is cleared automatically on on_mount().
    """

    def __init__(self) -> None:
        self._buf = InputBuffer()

    def on_mount(self) -> None:
        self._buf.clear()

    @property
    def _prompt_text(self) -> str:
        """Current text in the prompt buffer."""
        return self._buf.text

    def _render_prompt_line(self) -> str:
        """Return a Rich markup string for the prompt row: ❯ text▌."""
        from agenthicc.tui.input.renderer import PROMPT_CHAR, CURSOR_CHAR  # noqa: PLC0415
        from rich.markup import escape as _e                                # noqa: PLC0415
        buf    = self._buf.buf
        cursor = self._buf.cursor
        before = _e("".join(buf[:cursor]))
        after  = _e("".join(buf[cursor:]))
        return (
            f"[bold yellow]{PROMPT_CHAR}[/bold yellow] "
            f"{before}[bold]{CURSOR_CHAR}[/bold]{after}"
        )

    def _handle_prompt_key(self, key: Key, ch: str) -> bool:
        """Delegate one keystroke to the buffer.  Returns True if consumed."""
        match key:
            case Key.CHAR if ch and ch != "\n":
                self._buf.insert(ch)
                return True
            case Key.BACKSPACE:
                self._buf.delete_before()
                return True
            case Key.LEFT:
                self._buf.move_left()
                return True
            case Key.RIGHT:
                self._buf.move_right()
                return True
            case Key.HOME:
                self._buf.move_home()
                return True
            case Key.END:
                self._buf.move_end()
                return True
        return False
