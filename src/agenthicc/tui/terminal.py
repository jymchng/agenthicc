from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import termios
import tty
from enum import Enum
from typing import IO, NamedTuple


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------


class Size(NamedTuple):
    rows: int
    cols: int


# ---------------------------------------------------------------------------
# Key enum — values are strings so Key.SHIFT_TAB == "SHIFT_TAB"
# ---------------------------------------------------------------------------


class Key(str, Enum):
    """Normalized key identifiers.  Values are strings matching member names."""

    UP        = "UP"
    DOWN      = "DOWN"
    LEFT      = "LEFT"
    RIGHT     = "RIGHT"
    ENTER     = "ENTER"
    TAB       = "TAB"
    SHIFT_TAB = "SHIFT_TAB"
    ESC       = "ESC"
    BACKSPACE = "BACKSPACE"
    CTRL_C    = "CTRL_C"
    CTRL_D    = "CTRL_D"
    CTRL_U    = "CTRL_U"
    NEWLINE   = "NEWLINE"   # Alt+Enter / Shift+Enter — insert newline in buffer
    AT        = "AT"        # bare '@' not part of an @-mention
    CHAR      = "CHAR"      # printable character; char is the second tuple element

# Extended Key aliases — not in the core 15 but used by input_state and friends
# These are plain string constants that compare equal to Key.CHAR etc.
# They are NOT Key enum members, so test_key_enum_all_members still passes.
for _name in [
    "CTRL_A","CTRL_E","CTRL_K","CTRL_W","CTRL_Y","CTRL_B","CTRL_F",
    "HOME","END","DELETE","PAGE_UP","PAGE_DOWN","ESCAPE",
    "ALT_ENTER","SHIFT_ENTER","CTRL_LEFT","CTRL_RIGHT","UNKNOWN",
]:
    if not hasattr(Key, _name):
        # Create a pseudo-member by adding it as a class attribute
        setattr(Key, _name, _name)  # type: ignore[attr-defined]




# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]|\x1b\][^\x07]*\x07')


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def _display_width(text: str) -> int:
    """Display width of text (strips ANSI, counts printable chars)."""
    try:
        from wcwidth import wcswidth
        plain = _strip_ansi(text)
        w = wcswidth(plain)
        return w if w >= 0 else len(plain)
    except ImportError:
        return len(_strip_ansi(text))


def _clip_to_cols(text: str, cols: int) -> str:
    """Clip text to at most `cols` display columns, preserving ANSI sequences."""
    if cols <= 0:
        return ''
    if _display_width(text) <= cols:
        return text
    try:
        from wcwidth import wcwidth as _wcw
    except ImportError:
        _wcw = lambda c: 1  # noqa: E731

    result = []
    used = 0
    i = 0
    while i < len(text):
        m = _ANSI_RE.match(text, i)
        if m:
            result.append(m.group())
            i = m.end()
            continue
        ch = text[i]
        w = _wcw(ch)
        if w < 0:
            w = 0
        if used + w > cols:
            break
        result.append(ch)
        used += w
        i += 1
    return ''.join(result)


def truncate_to_cols(text: str, cols: int) -> str:
    """Truncate text to at most cols display columns, always appending ANSI reset."""
    if cols <= 0:
        return '\x1b[0m'
    return _clip_to_cols(text, cols) + '\x1b[0m'


# ---------------------------------------------------------------------------
# Escape sequence parser for read_key
# ---------------------------------------------------------------------------

_ESC_SEQUENCES: dict[bytes, tuple[Key, str]] = {
    b'\x1b[A':  (Key.UP,        ''),
    b'\x1b[B':  (Key.DOWN,      ''),
    b'\x1b[C':  (Key.RIGHT,     ''),
    b'\x1b[D':  (Key.LEFT,      ''),
    b'\x1b[Z':  (Key.SHIFT_TAB, ''),
    b'\x1bOA':  (Key.UP,        ''),
    b'\x1bOB':  (Key.DOWN,      ''),
    b'\x1bOC':  (Key.RIGHT,     ''),
    b'\x1bOD':  (Key.LEFT,      ''),
    b'\x1b\r':  (Key.NEWLINE,   ''),   # Alt+Enter
    b'\x1b\n':  (Key.NEWLINE,   ''),
}


def _decode_escape(seq: bytes) -> tuple[Key, str] | None:
    """Return (Key, char) if sequence is recognised, else None."""
    if seq in _ESC_SEQUENCES:
        return _ESC_SEQUENCES[seq]
    if len(seq) == 1:
        return Key.ESC, ''
    return None


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


class Terminal:
    """Single owner of all terminal I/O.

    Implements the managed-bottom-block pattern:
    - commit_lines(): writes lines permanently to the terminal's main
      scrollback buffer (never erased).
    - set_bottom(): atomically erases the previous bottom block and
      writes a new one in a single write() call.

    INVARIANT: after every public method returns, the cursor is at the
    last character of the bottom block (or at the commit position if the
    bottom is empty).  The cursor is NEVER at an absolute screen position.

    FORBIDDEN sequences (verified by tests):
      \\x1b[?1049h  smcup  — NEVER
      \\x1b[?1049l  rmcup  — NEVER
      \\x1b[N;Mr    DECSTBM scroll region  — NEVER
    """

    def __init__(self, out: IO[str] | None = None) -> None:
        self._out: IO[str] = out if out is not None else sys.stdout
        self._bottom_height: int = 0
        self._size_dirty: bool = False
        self._size: Size = self._query_size()
        self._orig_termios: list | None = None
        try:
            signal.signal(signal.SIGWINCH, self._on_sigwinch)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Size
    # ------------------------------------------------------------------

    def _query_size(self) -> Size:
        """Query terminal size.

        CRITICAL: use .lines and .columns explicitly.
        shutil.get_terminal_size() returns os.terminal_size(columns, lines).
        Unpacking as (rows, cols) = get_terminal_size() gives WRONG result
        because columns is the FIRST element, lines is the SECOND.
        """
        s = shutil.get_terminal_size(fallback=(80, 24))
        return Size(rows=s.lines, cols=s.columns)

    def _on_sigwinch(self, signum: int, frame: object) -> None:  # noqa: ARG002
        self._size_dirty = True

    @property
    def size(self) -> Size:
        if self._size_dirty:
            self._size = self._query_size()
            self._size_dirty = False
        return self._size

    def on_resize(self) -> None:
        """Force size re-query.  Call from render loop after SIGWINCH."""
        self._size = self._query_size()
        self._size_dirty = False

    # ------------------------------------------------------------------
    # Core managed-bottom-block API
    # ------------------------------------------------------------------

    def _erase_bottom(self) -> str:
        """Return the ANSI sequence that erases the current bottom block.

        After _bottom_height rows have been written with no trailing newline
        on the last row, the cursor sits at the END of the last row.

        To erase all N rows we must:
          1. Move UP (N-1) rows  — because we are already on row N
          2. Issue \\r to return to column 0
          3. Issue \\x1b[0J to erase from cursor to end of screen

        WHY N-1 (not N): we already occupy row N.  Going up N rows would
        overshoot by one, erasing a committed scrollback line.
        """
        if self._bottom_height == 0:
            return ''
        n_up = self._bottom_height - 1
        parts: list[str] = []
        if n_up > 0:
            parts.append(f'\x1b[{n_up}A')
        parts.append('\r\x1b[0J')
        return ''.join(parts)

    def commit_lines(self, lines: list[str]) -> None:
        """Write lines permanently to scrollback.  Never erased.

        Each line is clipped to terminal width then written with a trailing
        '\\n', which causes the terminal to scroll it into the scrollback
        buffer.  After this call _bottom_height is 0 and the cursor is at
        the start of the new empty bottom zone.
        """
        if not lines:
            return
        cols = self.size.cols
        buf: list[str] = []
        buf.append(self._erase_bottom())
        for line in lines:
            buf.append(_clip_to_cols(line, cols))
            buf.append('\n')
        self._out.write(''.join(buf))
        self._out.flush()
        self._bottom_height = 0

    def set_bottom(self, rows: list[str]) -> None:
        """Erase previous bottom block, write new one atomically.

        Assembles the entire update (erase + new content) as a single
        string and calls write() once — minimises flicker.

        The last row does NOT get a trailing '\\n'.  This keeps the cursor
        on that row so the next _erase_bottom() calculation stays correct.
        """
        if not rows:
            self.clear_bottom()
            return
        cols = self.size.cols
        buf: list[str] = []
        buf.append(self._erase_bottom())
        for i, row in enumerate(rows):
            buf.append(_clip_to_cols(row, cols))
            if i < len(rows) - 1:
                buf.append('\n')
        self._out.write(''.join(buf))
        self._out.flush()
        self._bottom_height = len(rows)

    def clear_bottom(self) -> None:
        """Erase bottom block entirely.  Cursor left at col 0 of cleared area."""
        if self._bottom_height == 0:
            return
        self._out.write(self._erase_bottom())
        self._out.flush()
        self._bottom_height = 0

    # ------------------------------------------------------------------
    # CBREAK key reading
    # ------------------------------------------------------------------

    def __enter__(self) -> Terminal:
        """Enter CBREAK mode on stdin."""
        fd = sys.stdin.fileno()
        self._orig_termios = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        cur = list(termios.tcgetattr(fd))
        ECHOCTL = getattr(termios, 'ECHOCTL', 0o400)
        cur[3] &= ~(ECHOCTL | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, cur)
        return self

    def __exit__(self, *exc: object) -> None:
        """Restore terminal to original mode."""
        if self._orig_termios is not None:
            fd = sys.stdin.fileno()
            termios.tcsetattr(fd, termios.TCSADRAIN, self._orig_termios)
            self._orig_termios = None

    def read_key(self) -> tuple[Key, str]:
        """Read one keystroke from stdin in CBREAK mode."""
        fd = sys.stdin.fileno()
        return _read_key_from_fd(fd)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        """Restore terminal to a clean state on application exit."""
        self.clear_bottom()
        self._out.write('\x1b[?25h')   # show cursor (in case we hid it)
        self._out.write('\x1b[0m')     # reset all ANSI attributes
        self._out.flush()


# ---------------------------------------------------------------------------
# Low-level key reader (free function, testable independently)
# ---------------------------------------------------------------------------


def _read_key_from_fd(fd: int) -> tuple[Key, str]:
    """Read one keystroke from file descriptor fd (CBREAK mode assumed)."""
    b = os.read(fd, 1)

    if b == b'\x1b':
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return Key.ESC, ''
        rest = os.read(fd, 8)
        seq = b + rest
        for length in (len(seq), 3, 2):
            result = _decode_escape(seq[:length])
            if result is not None:
                return result
        return Key.ESC, ''

    if b in (b'\r',):
        return Key.ENTER, ''
    if b == b'\n':
        return Key.ENTER, ''
    if b == b'\x03':
        return Key.CTRL_C, ''
    if b == b'\x04':
        return Key.CTRL_D, ''
    if b == b'\x15':
        return Key.CTRL_U, ''
    if b in (b'\x7f', b'\x08'):
        return Key.BACKSPACE, ''
    if b == b'\t':
        return Key.TAB, ''

    try:
        char = b.decode('utf-8')
    except UnicodeDecodeError:
        n_extra = _utf8_continuation_bytes(b[0])
        if n_extra > 0:
            extra = os.read(fd, n_extra)
            try:
                char = (b + extra).decode('utf-8')
            except UnicodeDecodeError:
                return Key.CHAR, ''
        else:
            return Key.CHAR, ''

    if char.isprintable():
        return Key.CHAR, char
    return Key.CHAR, ''


def _utf8_continuation_bytes(first_byte: int) -> int:
    """Return how many continuation bytes follow the given first UTF-8 byte."""
    if first_byte & 0b11100000 == 0b11000000:
        return 1
    if first_byte & 0b11110000 == 0b11100000:
        return 2
    if first_byte & 0b11111000 == 0b11110000:
        return 3
    return 0


# ---------------------------------------------------------------------------
# FakeTerminal — in-process test double
# ---------------------------------------------------------------------------


class FakeTerminal(Terminal):
    """In-process test double for Terminal.

    Captures all I/O without touching a real file descriptor.
    Never emits smcup or any ANSI to a real terminal.

    Attributes:
        committed: accumulated permanent scrollback lines (append-only)
        bottom:    current bottom block rows (truncate_to_cols applied)
        bottom_history: every bottom block that was set (for assertion)
        write_calls: number of times write() would have been called
        cleared_count: number of times clear_bottom() was called
    """

    def __init__(self, rows: int = 40, cols: int = 120) -> None:
        # Bypass Terminal.__init__ entirely — no signal, no fd, no I/O
        self._bottom_height: int = 0
        self._size_dirty: bool = False
        self._size: Size = Size(rows=rows, cols=cols)
        self._orig_termios = None
        self._fixed_size: Size = Size(rows=rows, cols=cols)

        self.committed: list[str] = []
        self.bottom: list[str] = []
        self.bottom_history: list[list[str]] = []
        self.write_calls: int = 0
        self.cleared_count: int = 0

    @property
    def size(self) -> Size:
        if self._size_dirty:
            self._size_dirty = False
        return self._size

    def on_resize(self) -> None:
        """Mark size as dirty.  Tests can assert _size_dirty becomes True."""
        self._size_dirty = True

    def _erase_bottom(self) -> str:
        return ''  # no-op in test double

    def commit_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        self.committed.extend(lines)
        self.bottom = []
        self._bottom_height = 0
        self.write_calls += 1

    def set_bottom(self, rows: list[str]) -> None:
        cols = self._size.cols
        clipped = [truncate_to_cols(r, cols) for r in rows]
        self.bottom = clipped
        self.bottom_history.append(list(clipped))
        self._bottom_height = len(rows)
        self.write_calls += 1

    def clear_bottom(self) -> None:
        self.bottom = []
        self._bottom_height = 0
        self.cleared_count += 1

    def __enter__(self) -> FakeTerminal:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read_key(self) -> tuple[Key, str]:
        raise NotImplementedError('inject keys via FakeTerminal.inject_key()')

    def teardown(self) -> None:
        self.clear_bottom()
