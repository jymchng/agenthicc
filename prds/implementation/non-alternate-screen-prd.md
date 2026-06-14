# Non-Alternate-Screen Terminal Rendering — Implementation PRD

**Status**: Implementation-ready  
**Audience**: Autonomous coding agents — zero additional clarification will be provided  
**Hard constraint**: `\x1b[?1049h` (smcup) and `\x1b[?1049l` (rmcup) MUST NEVER appear in any output, ever.

---

## 0. Absolute Prohibitions

These apply to every line of code written under this PRD. Violation is a blocker.

1. NEVER emit `\x1b[?1049h` — alternate screen enter (smcup)
2. NEVER emit `\x1b[?1049l` — alternate screen exit (rmcup)
3. NEVER emit `\x1b[?1047h` or `\x1b[?47h` — older alternate screen variants
4. NEVER emit `\x1b[{top};{bottom}r` — DECSTBM scroll region
5. NEVER call `Textual App.run()` without `inline=True`
6. NEVER call `shutil.get_terminal_size()` and unpack as `(cols, rows)` — the tuple is `(columns, lines)`, which means columns comes first; unpacking as `(rows, cols)` silently swaps them. Always use `.lines` and `.columns` attributes explicitly.
7. NEVER write directly to `sys.stdout` from any module other than `Terminal` — it is the sole stdout owner.

---

## 1. Terminal Class — Complete Implementation

**File**: `src/agenthicc/tui/terminal.py`

### 1.1 Full source

```python
from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import termios
import tty
from dataclasses import dataclass
from enum import Enum, auto
from typing import IO, NamedTuple


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------

class Size(NamedTuple):
    rows: int
    cols: int


# ---------------------------------------------------------------------------
# Key enum
# ---------------------------------------------------------------------------

class Key(Enum):
    UP        = auto()
    DOWN      = auto()
    LEFT      = auto()
    RIGHT     = auto()
    ENTER     = auto()
    TAB       = auto()
    SHIFT_TAB = auto()
    ESC       = auto()
    BACKSPACE = auto()
    CTRL_C    = auto()
    CTRL_D    = auto()
    CTRL_U    = auto()
    CTRL_A    = auto()
    CTRL_E    = auto()
    CTRL_K    = auto()
    CTRL_W    = auto()
    CTRL_Y    = auto()
    CTRL_B    = auto()
    NEWLINE   = auto()   # Alt+Enter / Shift+Enter — insert newline in buffer
    AT        = auto()   # bare '@' not part of email
    CHAR      = auto()
    UNKNOWN   = auto()


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]|\x1b\][^\x07]*\x07')


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def _display_width(text: str) -> int:
    """Display width of text (strips ANSI, counts printable chars).

    For production accuracy use wcwidth.wcswidth; this is a safe fallback.
    """
    try:
        from wcwidth import wcswidth
        plain = _strip_ansi(text)
        w = wcswidth(plain)
        return w if w >= 0 else len(plain)
    except ImportError:
        return len(_strip_ansi(text))


def _clip_to_cols(text: str, cols: int) -> str:
    """Clip text to at most `cols` display columns, preserving ANSI sequences.

    Strategy: strip ANSI to measure, then do a character-by-character walk
    accumulating display width until we hit `cols`.
    """
    if cols <= 0:
        return ''
    plain = _strip_ansi(text)
    if _display_width(text) <= cols:
        return text
    # Walk original string, track ANSI spans (zero-width), count printable cols
    try:
        from wcwidth import wcwidth as _wcw
    except ImportError:
        _wcw = lambda c: 1  # noqa: E731

    result = []
    used = 0
    i = 0
    while i < len(text):
        # Check for escape sequence at position i
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


def _decode_escape(seq: bytes) -> tuple[Key, str]:
    if seq in _ESC_SEQUENCES:
        return _ESC_SEQUENCES[seq]
    if len(seq) == 1:
        return Key.ESC, ''
    return Key.UNKNOWN, ''


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
        self._bottom_height: int = 0   # number of rows in current bottom block
        self._size_dirty: bool = False
        self._size: Size = self._query_size()
        self._orig_termios: list | None = None
        # Register SIGWINCH — safe even if not a TTY (signal is ignored)
        try:
            signal.signal(signal.SIGWINCH, self._on_sigwinch)
        except (OSError, ValueError):
            pass  # not a real terminal or in a thread

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
        """Force size re-query. Call from render loop after SIGWINCH."""
        self._size = self._query_size()
        self._size_dirty = False

    # ------------------------------------------------------------------
    # Core managed-bottom-block API
    # ------------------------------------------------------------------

    def _erase_bottom(self) -> str:
        """Return the ANSI sequence that erases the current bottom block.

        After `_bottom_height` rows have been written with no trailing newline
        on the last row, the cursor sits at the END of the last row.

        To erase all N rows we must:
          1. Move UP (N-1) rows  — because we are already on row N
          2. Issue \\r to return to column 0
          3. Issue \\x1b[0J to erase from cursor to end of screen

        WHY N-1 (not N): we already occupy row N.  Going up N rows would
        overshoot by one, erasing a committed scrollback line.

        Example: bottom_height=3, cursor at end of row 3.
          \\x1b[2A   moves cursor to row 1 of the block
          \\r         col 0
          \\x1b[0J   erase rows 1, 2, 3 (everything from cursor down)
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
        """Write lines permanently to scrollback. Never erased.

        Each line is clipped to terminal width then written with a trailing
        '\\n', which causes the terminal to scroll it into the scrollback
        buffer.  After this call _bottom_height is 0 and the cursor is at
        the start of the new empty bottom zone.
        """
        if not lines:
            return
        cols = self.size.cols
        buf: list[str] = []
        # Erase the current bottom block first
        buf.append(self._erase_bottom())
        # Write each line + newline  →  they scroll into scrollback permanently
        for line in lines:
            buf.append(_clip_to_cols(line, cols))
            buf.append('\n')
        self._out.write(''.join(buf))
        self._out.flush()
        self._bottom_height = 0

    def set_bottom(self, rows: list[str]) -> None:
        """Erase previous bottom block, write new one atomically.

        Assembles the entire update (erase + new content) as a single
        string and calls write() once — minimises flicker, especially
        important over SSH where each write() is a round trip.

        The last row does NOT get a trailing '\\n'.  This keeps the cursor
        on that row so the next _erase_bottom() calculation stays correct.
        Adding a trailing '\\n' here would cause an off-by-one: the cursor
        would be one row BELOW the block, so the next erase would go up
        only (N-1) rows and miss the first row of the previous block.
        """
        if not rows:
            self.clear_bottom()
            return
        cols = self.size.cols
        buf: list[str] = []
        # Erase old block
        buf.append(self._erase_bottom())
        # Write new block — rows separated by '\\n', NO trailing '\\n'
        for i, row in enumerate(rows):
            buf.append(_clip_to_cols(row, cols))
            if i < len(rows) - 1:
                buf.append('\n')
        self._out.write(''.join(buf))
        self._out.flush()
        self._bottom_height = len(rows)

    def clear_bottom(self) -> None:
        """Erase bottom block entirely. Cursor left at col 0 of cleared area."""
        if self._bottom_height == 0:
            return
        self._out.write(self._erase_bottom())
        self._out.flush()
        self._bottom_height = 0

    # ------------------------------------------------------------------
    # CBREAK key reading
    # ------------------------------------------------------------------

    def __enter__(self) -> 'Terminal':
        """Enter CBREAK mode on stdin."""
        fd = sys.stdin.fileno()
        self._orig_termios = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        # Post-setcbreak: also clear ECHOCTL and ISIG so that Ctrl+C
        # is delivered as a raw byte (0x03) rather than raising SIGINT,
        # letting the application handle it gracefully.
        cur = list(termios.tcgetattr(fd))
        # cur[3] is the c_lflag field
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
        """Read one keystroke from stdin in CBREAK mode.

        Returns (Key, char) where char is the Unicode character for CHAR
        keys and '' for all other keys.

        Must be called only while in CBREAK mode (i.e. inside a `with terminal:` block).
        """
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
        # Attempt to read the rest of an escape sequence within 50ms
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return Key.ESC, ''
        rest = os.read(fd, 8)
        seq = b + rest
        # Trim to first recognisable sequence
        for length in (len(seq), 3, 2):
            result = _decode_escape(seq[:length])
            if result[0] != Key.UNKNOWN:
                return result
        return Key.UNKNOWN, ''

    if b in (b'\r', ):
        return Key.ENTER, ''
    if b == b'\n':
        return Key.ENTER, ''
    if b == b'\x03':
        return Key.CTRL_C, ''
    if b == b'\x04':
        return Key.CTRL_D, ''
    if b == b'\x15':
        return Key.CTRL_U, ''
    if b == b'\x01':
        return Key.CTRL_A, ''
    if b == b'\x05':
        return Key.CTRL_E, ''
    if b == b'\x0b':
        return Key.CTRL_K, ''
    if b == b'\x17':
        return Key.CTRL_W, ''
    if b == b'\x19':
        return Key.CTRL_Y, ''
    if b == b'\x02':
        return Key.CTRL_B, ''
    if b in (b'\x7f', b'\x08'):
        return Key.BACKSPACE, ''
    if b == b'\t':
        return Key.TAB, ''

    # Multi-byte UTF-8 character
    try:
        char = b.decode('utf-8')
    except UnicodeDecodeError:
        # Read remaining bytes of multi-byte sequence
        n_extra = _utf8_continuation_bytes(b[0])
        if n_extra > 0:
            extra = os.read(fd, n_extra)
            try:
                char = (b + extra).decode('utf-8')
            except UnicodeDecodeError:
                return Key.UNKNOWN, ''
        else:
            return Key.UNKNOWN, ''

    if char.isprintable():
        return Key.CHAR, char
    return Key.UNKNOWN, ''


def _utf8_continuation_bytes(first_byte: int) -> int:
    """Return how many continuation bytes follow the given first UTF-8 byte."""
    if first_byte & 0b11100000 == 0b11000000:
        return 1
    if first_byte & 0b11110000 == 0b11100000:
        return 2
    if first_byte & 0b11111000 == 0b11110000:
        return 3
    return 0
```

---

## 2. FakeTerminal — Complete Test Double

**File**: `src/agenthicc/tui/terminal.py` (same file, appended after `Terminal`)

```python
class FakeTerminal(Terminal):
    """In-process test double for Terminal.

    Captures all I/O without touching a real file descriptor.
    Never emits smcup or any ANSI to a real terminal.

    Attributes:
        committed: accumulated permanent scrollback lines (append-only)
        bottom:    current bottom block rows
        bottom_history: every bottom block that was set (for assertion)
        write_calls: number of times _out.write() would have been called
    """

    def __init__(self, rows: int = 40, cols: int = 120) -> None:
        # Bypass Terminal.__init__ entirely — no signal, no fd, no I/O
        self._bottom_height: int = 0
        self._size_dirty: bool = False
        self._size: Size = Size(rows=rows, cols=cols)
        self._orig_termios = None
        self._fixed_size: Size = Size(rows=rows, cols=cols)

        # Test-observable state
        self.committed: list[str] = []
        self.bottom: list[str] = []
        self.bottom_history: list[list[str]] = []
        self.write_calls: int = 0
        self.cleared_count: int = 0

    @property
    def size(self) -> Size:
        return self._size

    def on_resize(self) -> None:
        pass  # size stays fixed in tests unless mutated directly

    def _erase_bottom(self) -> str:
        return ''  # no-op in test double

    def commit_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        self.committed.extend(lines)
        self._bottom_height = 0
        self.write_calls += 1

    def set_bottom(self, rows: list[str]) -> None:
        self.bottom = list(rows)
        self.bottom_history.append(list(rows))
        self._bottom_height = len(rows)
        self.write_calls += 1

    def clear_bottom(self) -> None:
        self.bottom = []
        self._bottom_height = 0
        self.cleared_count += 1

    def __enter__(self) -> 'FakeTerminal':
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read_key(self) -> tuple[Key, str]:
        raise NotImplementedError('inject keys via FakeTerminal.inject_key()')

    def teardown(self) -> None:
        self.clear_bottom()
```

### 2.1 FakeTerminal usage patterns

```python
# Assert committed scrollback content
term = FakeTerminal()
term.commit_lines(['hello', 'world'])
assert term.committed == ['hello', 'world']

# Assert bottom block content
term.set_bottom(['status line', '──────', '❯ input', 'footer'])
assert term.bottom[0] == 'status line'
assert len(term.bottom) == 4

# Assert write batching (each public call = 1 write)
assert term.write_calls == 2   # one commit + one set_bottom

# Assert no duplicate commits
count = len(term.committed)
term.commit_lines([])   # empty — no-op
assert len(term.committed) == count

# Simulate resize
term._size = Size(rows=24, cols=60)
```

---

## 3. FrameComposer — Complete Specification

**File**: `src/agenthicc/tui/frame_composer.py`

### 3.1 Data structures

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transcript import TranscriptModel


@dataclass
class StatusState:
    active: bool = False
    partial_text: str = ''
    spinner_frame: int = 0
    intent_started_at: float = 0.0
    session_id: str = 'unknown'
    model_name: str = 'claude-sonnet-4-6'
    completed_agents: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    session_cost_usd: float = 0.0
    mode_name: str = 'Auto'


@dataclass
class DropdownItem:
    label: str
    value: str
    icon: str = ''


@dataclass
class DropdownState:
    items: list[DropdownItem] = field(default_factory=list)
    selected_idx: int = 0
    trigger: str = ''    # '@' or '/'

    @property
    def active(self) -> bool:
        return bool(self.items)

    def render_rows(self, cols: int) -> list[str]:
        rows = []
        for i, item in enumerate(self.items[:8]):
            prefix = '\x1b[7m' if i == self.selected_idx else ''
            suffix = '\x1b[0m' if i == self.selected_idx else ''
            icon = f'{item.icon} ' if item.icon else ''
            row = f'  {prefix}{icon}{item.label}{suffix}'
            rows.append(_clip_to_cols(row, cols))
        return rows


@dataclass
class InputState:
    buffer: str = ''
    cursor: int = 0          # byte offset
    history: list[str] = field(default_factory=list)
    history_idx: int = -1
    mode_name: str = 'Auto'
    dropdown: DropdownState = field(default_factory=DropdownState)

    def render_lines(self, cols: int) -> list[str]:
        prompt = '\x1b[1;32m❯\x1b[0m '
        prompt_display_width = 2    # '❯ ' = 2 display columns
        raw_lines = self.buffer.split('\n') if self.buffer else ['']
        result = []
        for i, line in enumerate(raw_lines):
            if i == 0:
                prefix = prompt
                avail = cols - prompt_display_width
            else:
                prefix = '  '
                avail = cols - 2
            result.append(prefix + _clip_to_cols(line, avail))
        return result


@dataclass
class Frame:
    committed: list[str]   # all finalized transcript lines accumulated so far
    bottom: list[str]      # current bottom block rows
```

### 3.2 FrameComposer class

```python
SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
_THINKING_TEXT = 'Thinking…'
MAX_STREAMING_ROWS = 8


def _thinking_wave(frame_num: int) -> str:
    """Bold char sweeps L→R then R→L through 'Thinking…'."""
    text = _THINKING_TEXT
    length = len(text)
    cycle = 2 * (length - 1)
    pos = frame_num % cycle
    if pos >= length:
        pos = cycle - pos
    result = []
    for i, ch in enumerate(text):
        if i == pos:
            result.append(f'\x1b[1m{ch}\x1b[22m')
        else:
            result.append(ch)
    return ''.join(result)


def _render_status_active(status: StatusState, cols: int, frame_num: int) -> str:
    spinner = SPINNER_FRAMES[frame_num % len(SPINNER_FRAMES)]
    thinking = _thinking_wave(frame_num)
    elapsed = time.monotonic() - status.intent_started_at if status.intent_started_at else 0.0
    tok_in = f'\x1b[36m↑ {status.input_tokens:,}\x1b[0m'
    tok_out = f'\x1b[32m↓ {status.output_tokens:,}\x1b[0m'
    line = (
        f' \x1b[36m{spinner}\x1b[0m {thinking}'
        f'  \x1b[2m{elapsed:.1f}s\x1b[0m'
        f'  \x1b[2m│\x1b[0m  {tok_in}  {tok_out}'
    )
    return _clip_to_cols(line, cols)


def _render_status_idle(status: StatusState, cols: int) -> str:
    parts = [
        f'\x1b[2m{status.session_id[:8]}\x1b[0m',
        f'\x1b[2m{status.model_name}\x1b[0m',
        f'\x1b[2m{status.completed_agents} turns\x1b[0m',
        f'\x1b[2m${status.session_cost_usd:.3f}\x1b[0m',
        f'\x1b[36m↑ {status.input_tokens:,}\x1b[0m',
        f'\x1b[32m↓ {status.output_tokens:,}\x1b[0m',
    ]
    line = '  ' + '  \x1b[2m│\x1b[0m  '.join(parts)
    return _clip_to_cols(line, cols)


def _render_mode_footer(mode: str, active: bool, cols: int) -> str:
    if active:
        hints = 'Ctrl+C:cancel  Ctrl+B:background'
    else:
        hints = 'Enter:send  Shift+Tab:mode  /:commands  @:files'
    line = f'  \x1b[2m{hints}\x1b[0m'
    return _clip_to_cols(line, cols)


class FrameComposer:
    """Pure render function: (transcript, status, input_state, cols, frame_num) → Frame.

    Caches committed output so compose() is O(new turns) not O(all turns).
    """

    def __init__(self) -> None:
        self._committed_cache: list[str] = []
        self._committed_turns_count: int = 0   # number of finalized turns rendered

    def compose(
        self,
        transcript: 'TranscriptModel | None',
        status: StatusState | None,
        input_state: InputState | None,
        cols: int = 80,
        frame_num: int = 0,
    ) -> Frame:
        status = status or StatusState()

        # -- Committed lines (append-only cache) --------------------------
        if transcript is not None:
            finalized = [t for t in transcript.turns if t.finalized]
            new_turns = finalized[self._committed_turns_count:]
            if new_turns:
                from .transcript import _render_turn
                for turn in new_turns:
                    self._committed_cache.extend(_render_turn(turn, cols))
                self._committed_turns_count = len(finalized)

        committed = list(self._committed_cache)

        # -- Bottom block -------------------------------------------------
        bottom = self._compose_bottom(status, input_state, cols, frame_num)

        return Frame(committed=committed, bottom=bottom)

    def _compose_bottom(
        self,
        status: StatusState,
        input_state: InputState | None,
        cols: int,
        frame_num: int,
    ) -> list[str]:
        rows: list[str] = []

        # Zone 1: streaming partial text (only during active agent turn)
        if status.active and status.partial_text:
            wrapped = _wrap_plain(status.partial_text, cols - 2)
            streaming_rows = [f'  \x1b[2m{line}\x1b[0m' for line in wrapped]
            rows.extend(streaming_rows[-MAX_STREAMING_ROWS:])

        # Zone 2: status line
        if status.active:
            rows.append(_render_status_active(status, cols, frame_num))
        else:
            rows.append(_render_status_idle(status, cols))

        # Zone 3: divider
        rows.append(f'\x1b[2m{"─" * cols}\x1b[0m')

        # Zone 4: dropdown (above input, if active)
        if input_state is not None and input_state.dropdown.active:
            rows.extend(input_state.dropdown.render_rows(cols))

        # Zone 5: input rows
        if input_state is not None:
            rows.extend(input_state.render_lines(cols))
        else:
            rows.append('\x1b[1;32m❯\x1b[0m ')

        # Zone 6: mode footer
        mode = input_state.mode_name if input_state is not None else status.mode_name
        rows.append(_render_mode_footer(mode, status.active, cols))

        return rows

    def reset(self) -> None:
        """Clear the committed cache. Call when starting a new session."""
        self._committed_cache = []
        self._committed_turns_count = 0


def _wrap_plain(text: str, width: int) -> list[str]:
    """Word-wrap plain text to width columns. Returns list of lines."""
    if width <= 0:
        return [text]
    lines = []
    for paragraph in text.splitlines() or ['']:
        if not paragraph:
            lines.append('')
            continue
        while len(paragraph) > width:
            # Try to break at last space before width
            cut = paragraph.rfind(' ', 0, width)
            if cut <= 0:
                cut = width
            lines.append(paragraph[:cut])
            paragraph = paragraph[cut:].lstrip(' ')
        lines.append(paragraph)
    return lines
```

---

## 4. RenderLoop — Complete Specification

**File**: `src/agenthicc/tui/render_loop.py`

```python
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .terminal import Terminal
    from .frame_composer import FrameComposer, Frame, StatusState, InputState
    from .transcript import TranscriptModel


class RenderLoop:
    """Connects TranscriptModel + StatusState → Terminal via FrameComposer.

    Design rules:
    - Terminal.commit_lines() and Terminal.set_bottom() are called ONLY here.
    - Committed lines are sent to the terminal exactly once (tracked by
      _committed_count).  Old turns are never re-rendered.
    - Bottom block is redrawn every force_commit(); tick() is debounced.
    - RenderLoop is single-threaded (asyncio event loop); no locks needed.

    WHY commit_lines is NOT called for transcript Rich markup:
      transcript.render() returns strings that may contain Rich markup tags.
      Terminal.commit_lines() writes raw bytes — markup would show as literal
      text (e.g. '[bold]' on screen).  The app.py layer handles Rich rendering
      by calling terminal.commit_lines() with pre-rendered ANSI strings.
      _committed_count tracks how many lines have been flushed so FakeTerminal
      tests can assert the count without needing a Rich console.
    """

    MIN_INTERVAL: float = 0.05   # 50 ms debounce

    def __init__(
        self,
        terminal: 'Terminal',
        composer: 'FrameComposer',
    ) -> None:
        self.terminal = terminal
        self.composer = composer
        self._committed_count: int = 0
        self._frame_num: int = 0
        self._last_render: float = 0.0
        self._last_bottom: list[str] = []

    def render(
        self,
        transcript: 'TranscriptModel | None',
        status: 'StatusState | None',
        input_state: 'InputState | None',
    ) -> None:
        """Unconditional render. Always produces a frame and writes it."""
        cols = self.terminal.size.cols
        frame = self.composer.compose(
            transcript, status, input_state,
            cols=cols,
            frame_num=self._frame_num,
        )
        self._flush_frame(frame, cols)
        self._last_render = time.monotonic()
        self._frame_num += 1

    def tick(
        self,
        transcript: 'TranscriptModel | None',
        status: 'StatusState | None',
        input_state: 'InputState | None',
    ) -> None:
        """Debounced render. Skip if called again within MIN_INTERVAL."""
        now = time.monotonic()
        if now - self._last_render < self.MIN_INTERVAL:
            return
        self.render(transcript, status, input_state)

    def force_commit(
        self,
        transcript: 'TranscriptModel | None',
        status: 'StatusState | None',
        input_state: 'InputState | None',
    ) -> None:
        """Force immediate render bypassing debounce. Use at turn end."""
        self.render(transcript, status, input_state)

    def reset(self) -> None:
        """Reset state. Call when starting a new session or after resume."""
        self._committed_count = 0
        self._frame_num = 0
        self._last_render = 0.0
        self._last_bottom = []
        self.composer.reset()

    def _flush_frame(self, frame: 'Frame', cols: int) -> None:
        # New committed lines since last render
        new_lines = frame.committed[self._committed_count:]
        if new_lines:
            self.terminal.commit_lines(new_lines)
            self._committed_count = len(frame.committed)

        # Always refresh bottom block
        clipped = [_clip_to_cols(r, cols) for r in frame.bottom]
        self.terminal.set_bottom(clipped)
        self._last_bottom = clipped


# Import at bottom to avoid circular import issues in type checking
from .terminal import _clip_to_cols  # noqa: E402
```

---

## 5. CBREAK Input — Complete Implementation

### 5.1 Termios setup (exact sequence)

The `__enter__` / `__exit__` methods on `Terminal` (specified in Section 1) implement CBREAK. The exact sequence:

```python
# Step 1: call tty.setcbreak(fd)
#   This sets the terminal to CBREAK mode: characters are available
#   immediately (ICANON cleared), but signal generation (ISIG) is still on.

# Step 2: read the post-setcbreak termios and modify further:
cur = list(termios.tcgetattr(fd))
# cur[3] = c_lflag
ECHOCTL = getattr(termios, 'ECHOCTL', 0o400)
cur[3] &= ~(ECHOCTL | termios.ISIG)
termios.tcsetattr(fd, termios.TCSANOW, cur)
# Result: ECHOCTL cleared  → Ctrl characters are not echoed as ^C etc.
#         ISIG cleared     → Ctrl+C does not send SIGINT; it's a raw byte (0x03)
#                            so the application handles it cleanly without
#                            the event loop being interrupted by a signal.
```

### 5.2 Multi-byte UTF-8 in read_key

The `_read_key_from_fd` function (Section 1.1) handles multi-byte UTF-8:

```
Byte value range → continuation bytes needed:
  0x00–0x7F  → 0 (ASCII)
  0xC0–0xDF  → 1
  0xE0–0xEF  → 2
  0xF0–0xF7  → 3
```

Implementation: `_utf8_continuation_bytes(first_byte)` (defined in Section 1.1).

### 5.3 Key enum reference

| Key | Raw bytes | Notes |
|-----|-----------|-------|
| UP | `\x1b[A` or `\x1bOA` | Both VT100 variants |
| DOWN | `\x1b[B` or `\x1bOB` | |
| LEFT | `\x1b[D` or `\x1bOD` | |
| RIGHT | `\x1b[C` or `\x1bOC` | |
| ENTER | `\r` | CR from terminal |
| TAB | `\t` (0x09) | |
| SHIFT_TAB | `\x1b[Z` | Standard, works in xterm/tmux |
| ESC | `\x1b` alone (50ms timeout) | |
| BACKSPACE | `\x7f` or `\x08` | Terminal-dependent |
| CTRL_C | `\x03` | With ISIG cleared |
| CTRL_D | `\x04` | EOF |
| CTRL_U | `\x15` | Kill to start of line |
| CTRL_A | `\x01` | Move to start |
| CTRL_E | `\x05` | Move to end |
| CTRL_K | `\x0b` | Kill to end of line |
| CTRL_W | `\x17` | Kill word backward |
| CTRL_Y | `\x19` | Yank |
| CTRL_B | `\x02` | Background request |
| NEWLINE | `\x1b\r` or `\x1b\n` | Alt+Enter |
| CHAR | Any printable UTF-8 | Returns char in second element |

---

## 6. Resize Handling — SIGWINCH

### 6.1 Handler

```python
def _on_sigwinch(self, signum: int, frame: object) -> None:
    self._size_dirty = True
    # Do NOT do I/O in a signal handler.
    # The next call to self.size or self.on_resize() will re-query.
```

### 6.2 Render loop integration

```python
# In the main render/event loop:
if terminal.size.cols != last_cols or terminal.size.rows != last_rows:
    # Terminal was resized since last frame
    terminal.on_resize()
    # Force a full redraw at new dimensions
    render_loop.force_commit(transcript, status, input_state)
    last_cols = terminal.size.cols
    last_rows = terminal.size.rows
```

### 6.3 Bottom block reflow on resize

When the terminal narrows:
- `set_bottom()` clips each row via `_clip_to_cols(row, cols)` automatically
- The number of input rows may increase (multi-line wrap)
- `_bottom_height` is updated to the new row count

When the terminal widens:
- Rows remain short (they were already clipped to the old width)
- New width is used on the next `set_bottom()` call
- No special action needed

### 6.4 No-flicker strategy on resize

The SIGWINCH handler does NOT perform any I/O. The next render loop tick re-queries the size and calls `set_bottom()` with the new dimensions. The maximum visible glitch is one tick interval (50ms). For immediate response, the render loop can check `terminal._size_dirty` and bypass the debounce.

---

## 7. SSH/tmux Compatibility

### 7.1 Detection

```python
import os


def in_tmux() -> bool:
    return 'TMUX' in os.environ


def in_screen() -> bool:
    return os.environ.get('TERM', '').startswith('screen') or 'STY' in os.environ


def in_multiplexer() -> bool:
    return in_tmux() or in_screen()


def in_ssh() -> bool:
    return 'SSH_CONNECTION' in os.environ or 'SSH_CLIENT' in os.environ


def color_depth() -> int:
    """Return color depth in bits: 0=none, 8=basic ANSI, 256=256color, 24=truecolor."""
    if os.environ.get('NO_COLOR'):
        return 0
    ct = os.environ.get('COLORTERM', '')
    if ct in ('truecolor', '24bit'):
        return 24
    term = os.environ.get('TERM', '')
    if '256color' in term or ct == '256color':
        return 256
    if term == 'dumb' or not term:
        return 0
    return 8


def supports_unicode() -> bool:
    import locale
    enc = locale.getpreferredencoding(False).lower()
    return 'utf' in enc or 'utf8' in enc or 'utf-8' in enc
```

### 7.2 tmux-specific behaviours

| Issue | Behaviour |
|-------|-----------|
| Terminal width | tmux reports the pane width correctly via `TIOCGWINSZ`. `shutil.get_terminal_size()` reads this correctly. |
| Scrollback | Committed lines flow into tmux scrollback naturally. User scrolls with `Ctrl-b [`. |
| SIGWINCH | Fires correctly when pane is resized. Our handler works. |
| Color | `tmux-256color` supports 256 colors. Truecolor requires `set -as terminal-features ",xterm-256color:RGB"` in `.tmux.conf`. |
| Cursor style | `OSC 12` cursor color sequences may not pass through. Do not use them. |
| Double-width chars | `─` (U+2500), `●` (U+25CF), braille frames — all single-width, safe in tmux. |

### 7.3 SSH-specific behaviours

| Issue | Mitigation |
|-------|------------|
| High latency | Single `write()` per frame (already done). Debounce at 50ms (7fps sufficient). |
| RTT > 200ms | Optionally increase `MIN_INTERVAL` to 150ms. |
| TERM=xterm | 8-color degraded mode. Use ASCII spinner fallback (`| / - \`). |
| Mosh | No special handling needed; mosh does predictive local echo transparently. |

### 7.4 Width detection in multiplexers

Always use `shutil.get_terminal_size()` or `os.get_terminal_size(sys.stdout.fileno())`. Both correctly read the pty dimensions from the kernel. Do NOT rely on `$COLUMNS` environment variable — it is not always updated on resize inside tmux.

### 7.5 Detecting smcup from dependencies

```python
def install_alternate_screen_guard() -> None:
    """In debug mode, raise if any dependency emits smcup."""
    if not os.environ.get('AGENTHICC_DEBUG_NO_ALTSCREEN'):
        return
    import sys
    original_write = sys.stdout.write

    def guarded_write(s: str) -> int:
        if '\x1b[?1049h' in s:
            import traceback
            raise RuntimeError(
                'FORBIDDEN: alternate screen enter (smcup) detected!\n'
                + ''.join(traceback.format_stack())
            )
        if '\x1b[?1049l' in s:
            raise RuntimeError('FORBIDDEN: alternate screen exit (rmcup) detected!')
        return original_write(s)

    sys.stdout.write = guarded_write  # type: ignore[method-assign]
```

---

## 8. Test Specification

### 8.1 Unit tests — `tests/unit/test_terminal.py` (50+ tests)

All tests use `FakeTerminal` unless explicitly testing `Terminal` output bytes.

```python
"""
test_terminal.py — 55 unit tests for Terminal, FakeTerminal, FrameComposer, RenderLoop.

Markers: @pytest.mark.unit on every test.
asyncio_mode = "auto" — no @pytest.mark.asyncio needed.
"""
import io
import re
import pytest
from agenthicc.tui.terminal import (
    Terminal, FakeTerminal, Key, Size,
    _clip_to_cols, _strip_ansi, _display_width,
    _read_key_from_fd, _utf8_continuation_bytes,
)
from agenthicc.tui.frame_composer import (
    FrameComposer, Frame, StatusState, InputState, DropdownState, DropdownItem,
    _wrap_plain, _thinking_wave, SPINNER_FRAMES,
)
from agenthicc.tui.render_loop import RenderLoop


# ---------------------------------------------------------------------------
# Terminal — erase sequence correctness
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_erase_bottom_zero_height_returns_empty():
    """_erase_bottom with height=0 must return empty string — no ANSI emitted."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    term._bottom_height = 0
    assert term._erase_bottom() == ''


@pytest.mark.unit
def test_erase_bottom_height_1_no_cursor_up():
    """Height=1: cursor is already on row 1 of block. No \\x1b[A needed."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    term._bottom_height = 1
    seq = term._erase_bottom()
    assert '\x1b[' not in seq or 'A' not in seq  # no cursor-up
    assert '\r\x1b[0J' in seq


@pytest.mark.unit
def test_erase_bottom_height_3_goes_up_2():
    """Height=3: cursor is on row 3. Must go up 2 (not 3) to reach row 1."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    term._bottom_height = 3
    seq = term._erase_bottom()
    # Must contain \x1b[2A (up 2, NOT up 3)
    assert '\x1b[2A' in seq, f'Expected \\x1b[2A in {seq!r}'
    assert '\x1b[3A' not in seq, 'Off-by-one: went up 3 instead of 2'
    assert '\r\x1b[0J' in seq


@pytest.mark.unit
def test_erase_bottom_n_minus_1_invariant():
    """For any height N, _erase_bottom must go up exactly N-1 rows."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    for n in range(1, 20):
        term._bottom_height = n
        seq = term._erase_bottom()
        if n == 1:
            # No cursor-up at all
            assert f'\x1b[{n}A' not in seq
        else:
            assert f'\x1b[{n - 1}A' in seq, f'height={n}: expected up {n-1}'
            assert f'\x1b[{n}A' not in seq, f'height={n}: must NOT go up {n}'


@pytest.mark.unit
def test_set_bottom_does_not_add_trailing_newline():
    """set_bottom must NOT write \\n after the last row.

    A trailing newline shifts the cursor to the next row, breaking the
    n-1 calculation on the next _erase_bottom call.
    """
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.set_bottom(['line1', 'line2', 'line3'])
    output = buf.getvalue()
    # The output must not end with \\n
    assert not output.endswith('\n'), 'set_bottom wrote trailing newline — off-by-one bug'


@pytest.mark.unit
def test_set_bottom_updates_bottom_height():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.set_bottom(['a', 'b', 'c'])
    assert term._bottom_height == 3
    term.set_bottom(['x'])
    assert term._bottom_height == 1


@pytest.mark.unit
def test_set_bottom_empty_list_clears():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term._bottom_height = 5
    term.set_bottom([])
    assert term._bottom_height == 0


@pytest.mark.unit
def test_commit_lines_resets_bottom_height():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term._bottom_height = 4
    term.commit_lines(['hello'])
    assert term._bottom_height == 0


@pytest.mark.unit
def test_commit_lines_writes_newline_per_line():
    """Each committed line must end with \\n so it scrolls into scrollback."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.commit_lines(['line_a', 'line_b'])
    output = buf.getvalue()
    assert 'line_a\n' in output
    assert 'line_b\n' in output


@pytest.mark.unit
def test_commit_lines_empty_is_noop():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.commit_lines([])
    assert buf.getvalue() == ''


@pytest.mark.unit
def test_clear_bottom_resets_height():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term._bottom_height = 3
    term.clear_bottom()
    assert term._bottom_height == 0


@pytest.mark.unit
def test_clear_bottom_noop_when_already_zero():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term._bottom_height = 0
    term.clear_bottom()
    assert buf.getvalue() == ''  # nothing written


# ---------------------------------------------------------------------------
# Terminal size — the swap bug
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_size_uses_lines_not_columns(monkeypatch):
    """CRITICAL: size.rows must use .lines, size.cols must use .columns.

    os.terminal_size returns (columns, lines) — columns FIRST.
    Unpacking as (rows, cols) = get_terminal_size() would swap them.
    This test catches that bug.
    """
    import os
    fake_size = os.terminal_size((200, 50))  # columns=200, lines=50
    monkeypatch.setattr('shutil.get_terminal_size', lambda fallback=(80, 24): fake_size)
    buf = io.StringIO()
    term = Terminal(out=buf)
    s = term.size
    assert s.rows == 50,  f'rows should be .lines=50 but got {s.rows}'
    assert s.cols == 200, f'cols should be .columns=200 but got {s.cols}'


@pytest.mark.unit
def test_size_dirty_flag_triggers_requeries(monkeypatch):
    import os
    call_count = [0]
    def fake_gts(fallback=(80, 24)):
        call_count[0] += 1
        return os.terminal_size((80, 24))
    monkeypatch.setattr('shutil.get_terminal_size', fake_gts)
    buf = io.StringIO()
    term = Terminal(out=buf)
    initial = call_count[0]
    _ = term.size    # no re-query, _size_dirty=False
    assert call_count[0] == initial
    term._size_dirty = True
    _ = term.size    # must re-query
    assert call_count[0] == initial + 1


# ---------------------------------------------------------------------------
# No smcup / rmcup
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_no_smcup_emitted_by_terminal():
    """Terminal must NEVER emit \\x1b[?1049h (smcup)."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.set_bottom(['line1', 'line2'])
    term.commit_lines(['permanent line'])
    term.set_bottom(['new bottom'])
    term.clear_bottom()
    term.teardown()
    output = buf.getvalue()
    assert '\x1b[?1049h' not in output, 'FATAL: smcup detected in Terminal output'
    assert '\x1b[?1049l' not in output, 'FATAL: rmcup detected in Terminal output'


@pytest.mark.unit
def test_no_decstbm_emitted():
    """Terminal must NEVER emit DECSTBM scroll region (\\x1b[N;Mr)."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.set_bottom(['a', 'b'])
    term.commit_lines(['c'])
    output = buf.getvalue()
    assert not re.search(r'\x1b\[\d+;\d+r', output), 'DECSTBM scroll region detected'


@pytest.mark.unit
def test_teardown_does_not_emit_smcup():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.teardown()
    assert '\x1b[?1049h' not in buf.getvalue()
    assert '\x1b[?1049l' not in buf.getvalue()


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_clip_to_cols_passthrough_when_short():
    assert _clip_to_cols('hello', 80) == 'hello'


@pytest.mark.unit
def test_clip_to_cols_truncates_plain():
    result = _clip_to_cols('abcdef', 3)
    assert _strip_ansi(result) == 'abc'


@pytest.mark.unit
def test_clip_to_cols_preserves_ansi():
    colored = '\x1b[32mhello\x1b[0m'
    result = _clip_to_cols(colored, 80)
    assert '\x1b[32m' in result
    assert '\x1b[0m' in result


@pytest.mark.unit
def test_clip_to_cols_ansi_not_counted():
    """ANSI escape sequences must not count toward the column limit."""
    # 5 visible chars + ANSI wrapping
    colored = '\x1b[32mhello\x1b[0m'
    result = _clip_to_cols(colored, 5)
    plain = _strip_ansi(result)
    assert len(plain) <= 5


@pytest.mark.unit
def test_clip_to_cols_zero_returns_empty():
    assert _clip_to_cols('hello', 0) == ''


@pytest.mark.unit
def test_strip_ansi_removes_colors():
    assert _strip_ansi('\x1b[32mgreen\x1b[0m') == 'green'


@pytest.mark.unit
def test_strip_ansi_removes_cursor_moves():
    assert _strip_ansi('\x1b[2Ahello') == 'hello'


@pytest.mark.unit
def test_utf8_continuation_bytes():
    assert _utf8_continuation_bytes(ord('a')) == 0      # ASCII
    assert _utf8_continuation_bytes(0xC3) == 1           # 2-byte UTF-8
    assert _utf8_continuation_bytes(0xE2) == 2           # 3-byte UTF-8
    assert _utf8_continuation_bytes(0xF0) == 3           # 4-byte UTF-8


# ---------------------------------------------------------------------------
# FakeTerminal
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_fake_terminal_commit_accumulates():
    term = FakeTerminal()
    term.commit_lines(['a', 'b'])
    term.commit_lines(['c'])
    assert term.committed == ['a', 'b', 'c']


@pytest.mark.unit
def test_fake_terminal_set_bottom_tracks_history():
    term = FakeTerminal()
    term.set_bottom(['x'])
    term.set_bottom(['y', 'z'])
    assert len(term.bottom_history) == 2
    assert term.bottom_history[0] == ['x']
    assert term.bottom_history[1] == ['y', 'z']


@pytest.mark.unit
def test_fake_terminal_clear_resets_bottom():
    term = FakeTerminal()
    term.set_bottom(['a', 'b'])
    term.clear_bottom()
    assert term.bottom == []
    assert term._bottom_height == 0


@pytest.mark.unit
def test_fake_terminal_no_ansi_output():
    """FakeTerminal must not write any bytes to stdout."""
    import sys
    original = sys.stdout.write
    written = []
    sys.stdout.write = lambda s: written.append(s)  # type: ignore[method-assign]
    try:
        term = FakeTerminal()
        term.set_bottom(['test'])
        term.commit_lines(['line'])
        term.clear_bottom()
    finally:
        sys.stdout.write = original  # type: ignore[method-assign]
    assert written == [], f'FakeTerminal wrote to stdout: {written}'


@pytest.mark.unit
def test_fake_terminal_write_calls_counted():
    term = FakeTerminal()
    assert term.write_calls == 0
    term.commit_lines(['a'])
    assert term.write_calls == 1
    term.set_bottom(['b'])
    assert term.write_calls == 2
    term.commit_lines([])    # no-op
    assert term.write_calls == 2  # unchanged


# ---------------------------------------------------------------------------
# FrameComposer
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_frame_composer_empty_gives_bottom_only():
    fc = FrameComposer()
    frame = fc.compose(None, StatusState(), None, cols=80, frame_num=0)
    assert frame.committed == []
    # Bottom must have at least status + divider + input + footer = 4 rows
    assert len(frame.bottom) >= 4


@pytest.mark.unit
def test_frame_composer_bottom_has_divider():
    fc = FrameComposer()
    frame = fc.compose(None, StatusState(), None, cols=80)
    divider_rows = [r for r in frame.bottom if '─' in _strip_ansi(r)]
    assert divider_rows, 'No divider row (─) found in bottom block'


@pytest.mark.unit
def test_frame_composer_bottom_has_input_prompt():
    fc = FrameComposer()
    frame = fc.compose(None, StatusState(), InputState(), cols=80)
    prompt_rows = [r for r in frame.bottom if '❯' in r or '>' in _strip_ansi(r)]
    assert prompt_rows, 'No input prompt (❯) found in bottom block'


@pytest.mark.unit
def test_frame_composer_streaming_zone_shown_when_active():
    fc = FrameComposer()
    status = StatusState(active=True, partial_text='streaming content here')
    frame = fc.compose(None, status, None, cols=80)
    all_text = ' '.join(_strip_ansi(r) for r in frame.bottom)
    assert 'streaming content' in all_text


@pytest.mark.unit
def test_frame_composer_streaming_zone_absent_when_idle():
    fc = FrameComposer()
    status = StatusState(active=False, partial_text='')
    frame = fc.compose(None, status, None, cols=80)
    all_text = ' '.join(_strip_ansi(r) for r in frame.bottom)
    # No leftover streaming text
    assert 'streaming content' not in all_text


@pytest.mark.unit
def test_frame_composer_committed_cache():
    """compose() must not re-render already-committed turns."""
    from agenthicc.tui.transcript import TranscriptModel
    fc = FrameComposer()
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'first turn content')
    model.turns[-1].finalized = True

    frame1 = fc.compose(model, StatusState(), None, cols=80)
    count1 = len(frame1.committed)
    assert count1 > 0

    frame2 = fc.compose(model, StatusState(), None, cols=80)
    # Same turns — committed cache should not grow
    assert len(frame2.committed) == count1


@pytest.mark.unit
def test_frame_composer_new_turn_appends():
    from agenthicc.tui.transcript import TranscriptModel
    fc = FrameComposer()
    model = TranscriptModel()

    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'turn one')
    model.turns[-1].finalized = True
    frame1 = fc.compose(model, StatusState(), None, cols=80)

    model.append_turn('a2', 'assistant')
    model.append_line('a2', 'turn two')
    model.turns[-1].finalized = True
    frame2 = fc.compose(model, StatusState(), None, cols=80)

    assert len(frame2.committed) > len(frame1.committed)
    all_committed = ' '.join(frame2.committed)
    assert 'turn one' in all_committed
    assert 'turn two' in all_committed


@pytest.mark.unit
def test_frame_composer_in_progress_turn_not_in_committed():
    from agenthicc.tui.transcript import TranscriptModel
    fc = FrameComposer()
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'in progress content')
    # finalized = False

    frame = fc.compose(model, StatusState(active=True), None, cols=80)
    assert frame.committed == [], 'In-progress turn should not be committed'


@pytest.mark.unit
def test_frame_composer_rows_clipped_to_cols():
    fc = FrameComposer()
    frame = fc.compose(None, StatusState(), None, cols=40)
    for row in frame.bottom:
        plain = _strip_ansi(row)
        assert len(plain) <= 40, f'Row wider than 40 cols: {plain!r}'


@pytest.mark.unit
def test_frame_composer_dropdown_rows_included():
    fc = FrameComposer()
    dd = DropdownState(
        items=[DropdownItem('file.py', 'file.py'), DropdownItem('utils.py', 'utils.py')],
        trigger='@',
    )
    inp = InputState(dropdown=dd)
    frame = fc.compose(None, StatusState(), inp, cols=80)
    all_text = ' '.join(_strip_ansi(r) for r in frame.bottom)
    assert 'file.py' in all_text
    assert 'utils.py' in all_text


@pytest.mark.unit
def test_thinking_wave_cycles():
    text = _thinking_wave(0)
    assert '\x1b[1m' in text   # bold sequence present
    # Different frames produce different bold positions
    assert _thinking_wave(0) != _thinking_wave(1)


@pytest.mark.unit
def test_wrap_plain_wraps_at_word():
    lines = _wrap_plain('hello world foo bar', 10)
    for line in lines:
        assert len(line) <= 10, f'Line too long: {line!r}'


@pytest.mark.unit
def test_wrap_plain_empty_string():
    lines = _wrap_plain('', 80)
    assert lines == ['']


# ---------------------------------------------------------------------------
# RenderLoop
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_render_loop_commits_new_lines():
    from agenthicc.tui.transcript import TranscriptModel
    term = FakeTerminal()
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'hello world')
    model.turns[-1].finalized = True

    rl.force_commit(model, StatusState(), None)
    assert any('hello world' in line for line in term.committed)


@pytest.mark.unit
def test_render_loop_does_not_repeat_committed():
    from agenthicc.tui.transcript import TranscriptModel
    term = FakeTerminal()
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'line once')
    model.turns[-1].finalized = True

    rl.force_commit(model, StatusState(), None)
    count = len(term.committed)
    rl.force_commit(model, StatusState(), None)
    assert len(term.committed) == count, 'Lines were committed twice'


@pytest.mark.unit
def test_render_loop_tick_debounces():
    import time
    term = FakeTerminal()
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    rl._last_render = time.monotonic()  # just rendered

    before = term.write_calls
    rl.tick(None, StatusState(), None)   # within debounce window
    assert term.write_calls == before    # no render


@pytest.mark.unit
def test_render_loop_force_commit_bypasses_debounce():
    import time
    term = FakeTerminal()
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    rl._last_render = time.monotonic()  # just rendered

    before = term.write_calls
    rl.force_commit(None, StatusState(), None)  # force — ignores debounce
    assert term.write_calls > before


@pytest.mark.unit
def test_render_loop_sets_bottom_each_frame():
    term = FakeTerminal()
    fc = FrameComposer()
    rl = RenderLoop(term, fc)

    rl.force_commit(None, StatusState(), None)
    assert len(term.bottom) >= 4   # minimum bottom block height


@pytest.mark.unit
def test_render_loop_reset_clears_state():
    from agenthicc.tui.transcript import TranscriptModel
    term = FakeTerminal()
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'content')
    model.turns[-1].finalized = True
    rl.force_commit(model, StatusState(), None)
    assert rl._committed_count > 0

    rl.reset()
    assert rl._committed_count == 0
    assert rl._frame_num == 0
```

---

### 8.2 Integration tests — `tests/integration/test_tui_rendering.py` (20+ tests)

```python
"""
Integration tests using pyte virtual terminal emulator.
pyte interprets ANSI sequences and gives us a screen buffer to assert against.

Install: uv add --dev pyte
"""
import io
import re
import pytest
import pyte
from agenthicc.tui.terminal import Terminal, _strip_ansi
from agenthicc.tui.frame_composer import FrameComposer, StatusState, InputState
from agenthicc.tui.render_loop import RenderLoop

COLS, ROWS = 80, 24
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]')


def _run_through_pyte(raw: str, cols: int = COLS, rows: int = ROWS) -> pyte.Screen:
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(raw)
    return screen


def _screen_text(screen: pyte.Screen) -> str:
    lines = []
    for row in range(screen.lines):
        line = ''.join(screen.buffer[row][col].data for col in range(screen.columns))
        lines.append(line.rstrip())
    return '\n'.join(lines)


# ------------------------------------------------------------------
# smcup / rmcup guards — THE most important tests
# ------------------------------------------------------------------

@pytest.mark.integration
def test_no_smcup_in_any_terminal_output():
    """\\x1b[?1049h MUST NOT appear in any Terminal output under any conditions."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    fc = FrameComposer()
    rl = RenderLoop(term, fc)

    from agenthicc.tui.transcript import TranscriptModel
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'content')
    model.turns[-1].finalized = True
    rl.force_commit(model, StatusState(), InputState())
    rl.force_commit(model, StatusState(active=True, partial_text='streaming'), InputState())
    rl.force_commit(model, StatusState(), InputState())
    term.teardown()

    output = buf.getvalue()
    assert '\x1b[?1049h' not in output, 'FATAL: smcup (\\x1b[?1049h) detected!'
    assert '\x1b[?1049l' not in output, 'FATAL: rmcup (\\x1b[?1049l) detected!'


@pytest.mark.integration
def test_no_decstbm_scroll_region():
    buf = io.StringIO()
    term = Terminal(out=buf)
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    rl.force_commit(None, StatusState(), InputState())
    term.teardown()
    assert not re.search(r'\x1b\[\d+;\d+r', buf.getvalue()), 'DECSTBM scroll region found'


@pytest.mark.integration
def test_no_alt_screen_variant_1047():
    buf = io.StringIO()
    term = Terminal(out=buf)
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    rl.force_commit(None, StatusState(), None)
    assert '\x1b[?1047h' not in buf.getvalue()
    assert '\x1b[?47h' not in buf.getvalue()


# ------------------------------------------------------------------
# Pyte screen buffer tests
# ------------------------------------------------------------------

@pytest.mark.integration
def test_committed_content_visible_in_pyte_buffer():
    buf = io.StringIO()
    term = Terminal(out=buf)
    fc = FrameComposer()
    rl = RenderLoop(term, fc)

    from agenthicc.tui.transcript import TranscriptModel
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'UNIQUE_MARKER_XYZ')
    model.turns[-1].finalized = True
    rl.force_commit(model, StatusState(), None)

    screen = _run_through_pyte(buf.getvalue())
    text = _screen_text(screen)
    assert 'UNIQUE_MARKER_XYZ' in text, f'Committed content not in pyte buffer.\n{text}'


@pytest.mark.integration
def test_bottom_block_present_in_pyte_buffer():
    buf = io.StringIO()
    term = Terminal(out=buf)
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    rl.force_commit(None, StatusState(), InputState())

    screen = _run_through_pyte(buf.getvalue())
    text = _screen_text(screen)
    # Divider should appear
    assert '─' in text or '-' in text, 'No divider found in pyte buffer'


@pytest.mark.integration
def test_input_prompt_in_bottom_rows():
    buf = io.StringIO()
    term = Terminal(out=buf)
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    rl.force_commit(None, StatusState(), InputState())

    screen = _run_through_pyte(buf.getvalue())
    # Input prompt must be in the last 6 rows
    bottom_rows = range(ROWS - 6, ROWS)
    bottom_text = '\n'.join(
        ''.join(screen.buffer[r][c].data for c in range(COLS)).rstrip()
        for r in bottom_rows
    )
    assert '❯' in bottom_text or '>' in bottom_text, (
        f'Input prompt not in bottom rows.\nBottom: {bottom_text!r}'
    )


@pytest.mark.integration
def test_bottom_block_replaced_not_accumulated():
    """Each set_bottom call must replace the previous block, not accumulate."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.set_bottom(['first_bottom_row'])
    term.set_bottom(['second_bottom_row'])
    term.set_bottom(['third_bottom_row'])

    screen = _run_through_pyte(buf.getvalue())
    text = _screen_text(screen)
    # Only the latest bottom block row should be visible
    assert 'third_bottom_row' in text
    # Previous rows should have been erased
    visible_count = text.count('_bottom_row')
    assert visible_count == 1, f'Multiple bottom blocks visible: {visible_count}'


@pytest.mark.integration
def test_commit_lines_permanent_in_scrollback():
    """Committed lines must survive subsequent set_bottom calls."""
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.commit_lines(['permanent_line_alpha'])
    term.set_bottom(['bottom_row'])
    term.set_bottom(['different_bottom'])

    screen = _run_through_pyte(buf.getvalue())
    text = _screen_text(screen)
    assert 'permanent_line_alpha' in text, 'Committed line was erased by set_bottom'


@pytest.mark.integration
def test_multiple_commit_accumulate():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.commit_lines(['line_1'])
    term.commit_lines(['line_2'])
    term.commit_lines(['line_3'])
    term.set_bottom(['status'])

    screen = _run_through_pyte(buf.getvalue())
    text = _screen_text(screen)
    assert 'line_1' in text
    assert 'line_2' in text
    assert 'line_3' in text


@pytest.mark.integration
def test_bottom_height_correct_after_set_bottom():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.set_bottom(['a', 'b', 'c', 'd', 'e'])
    assert term._bottom_height == 5


@pytest.mark.integration
def test_clear_bottom_zeros_height():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.set_bottom(['a', 'b', 'c'])
    term.clear_bottom()
    assert term._bottom_height == 0


@pytest.mark.integration
def test_resize_bottom_reflows():
    buf = io.StringIO()
    term = Terminal(out=buf)
    long_row = 'x' * 120
    term.set_bottom([long_row])
    # Simulate resize to 80 cols
    term._size = Size(rows=24, cols=80)
    term.set_bottom([long_row])   # must clip to 80
    screen = _run_through_pyte(buf.getvalue(), cols=80)
    text = _screen_text(screen)
    # Each visible line should be ≤ 80 chars
    for line in text.splitlines():
        assert len(line) <= 80, f'Line wider than 80 after resize: {line!r}'


@pytest.mark.integration
def test_streaming_text_in_bottom_not_committed():
    from agenthicc.tui.transcript import TranscriptModel
    term = FakeTerminal()
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    model = TranscriptModel()

    # Active turn with streaming text
    model.append_turn('a1', 'assistant')
    status = StatusState(active=True, partial_text='streaming in progress')
    rl.force_commit(model, status, None)

    assert term.committed == [], 'Streaming text must not be in committed scrollback'
    bottom_text = ' '.join(_strip_ansi(r) for r in term.bottom)
    assert 'streaming' in bottom_text, 'Streaming text must be in bottom block'


@pytest.mark.integration
def test_force_commit_at_turn_end_moves_to_scrollback():
    from agenthicc.tui.transcript import TranscriptModel
    term = FakeTerminal()
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    model = TranscriptModel()

    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'final turn content')
    model.turns[-1].finalized = True
    status = StatusState(active=False, partial_text='')
    rl.force_commit(model, status, None)

    assert any('final turn content' in l for l in term.committed)


@pytest.mark.integration
def test_single_write_per_set_bottom():
    """set_bottom must produce exactly one write() call per invocation."""
    term = FakeTerminal()
    before = term.write_calls
    term.set_bottom(['a', 'b', 'c'])
    assert term.write_calls == before + 1, 'set_bottom must batch into single write'


@pytest.mark.integration
def test_render_loop_single_write_per_frame():
    """Each force_commit call must call write() at most 2 times:
    once for commit_lines (if any new lines) and once for set_bottom."""
    term = FakeTerminal()
    fc = FrameComposer()
    rl = RenderLoop(term, fc)

    before = term.write_calls
    rl.force_commit(None, StatusState(), InputState())
    after = term.write_calls
    # At most: 1 commit_lines + 1 set_bottom = 2
    assert after - before <= 2, f'Too many write calls: {after - before}'


@pytest.mark.integration
def test_teardown_leaves_clean_terminal():
    buf = io.StringIO()
    term = Terminal(out=buf)
    term.set_bottom(['live content'])
    term.teardown()
    # After teardown, show cursor must be issued
    assert '\x1b[?25h' in buf.getvalue()
    # Bottom height should be 0
    assert term._bottom_height == 0
```

---

### 8.3 E2E tests — `tests/e2e/test_tui_e2e.py` (15+ tests)

```python
"""
End-to-end tests for the full TUI rendering pipeline.
Uses FakeTerminal for isolation; asserts on logical content.
"""
import pytest
from agenthicc.tui.terminal import FakeTerminal, _strip_ansi
from agenthicc.tui.frame_composer import FrameComposer, StatusState, InputState
from agenthicc.tui.render_loop import RenderLoop
from agenthicc.tui.transcript import TranscriptModel


def _make_stack() -> tuple[FakeTerminal, FrameComposer, RenderLoop]:
    term = FakeTerminal(rows=40, cols=120)
    fc = FrameComposer()
    rl = RenderLoop(term, fc)
    return term, fc, rl


@pytest.mark.e2e
def test_multi_turn_scrollback_accumulation():
    """3 complete turns must all be in committed scrollback."""
    term, fc, rl = _make_stack()
    model = TranscriptModel()
    status = StatusState()

    for i in range(3):
        model.append_turn(f'a{i}', 'assistant')
        model.append_line(f'a{i}', f'turn_{i}_content')
        model.turns[-1].finalized = True
        rl.force_commit(model, status, None)

    committed = ' '.join(term.committed)
    for i in range(3):
        assert f'turn_{i}_content' in committed, f'turn_{i} missing from scrollback'


@pytest.mark.e2e
def test_streaming_lifecycle():
    """Streaming text: in bottom during stream, committed after finalize."""
    term, fc, rl = _make_stack()
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')

    # During streaming: content in bottom only
    status = StatusState(active=True, partial_text='stream chunk accumulating')
    rl.force_commit(model, status, None)
    assert term.committed == []
    bottom_text = ' '.join(_strip_ansi(r) for r in term.bottom)
    assert 'stream chunk' in bottom_text

    # After finalize: committed to scrollback
    model.append_line('a1', 'stream chunk accumulating')
    model.turns[-1].finalized = True
    status = StatusState(active=False, partial_text='')
    rl.force_commit(model, status, None)
    assert any('stream chunk' in l for l in term.committed)


@pytest.mark.e2e
def test_smcup_never_in_output():
    """The absolute hard constraint: smcup must NEVER appear."""
    import io
    buf = io.StringIO()
    from agenthicc.tui.terminal import Terminal
    term_real = Terminal(out=buf)
    fc = FrameComposer()
    rl = RenderLoop(term_real, fc)
    model = TranscriptModel()
    status = StatusState()

    for i in range(5):
        model.append_turn(f'a{i}', 'assistant')
        model.append_line(f'a{i}', f'content {i}')
        model.turns[-1].finalized = True
        rl.force_commit(model, status, InputState())

    term_real.teardown()
    output = buf.getvalue()
    assert '\x1b[?1049h' not in output, 'FATAL: smcup in e2e output'
    assert '\x1b[?1049l' not in output, 'FATAL: rmcup in e2e output'


@pytest.mark.e2e
def test_input_state_reflected_in_bottom():
    term, fc, rl = _make_stack()
    inp = InputState(buffer='user typed this')
    rl.force_commit(None, StatusState(), inp)
    bottom_text = ' '.join(_strip_ansi(r) for r in term.bottom)
    assert 'user typed this' in bottom_text


@pytest.mark.e2e
def test_mode_name_in_footer():
    term, fc, rl = _make_stack()
    inp = InputState(mode_name='Review')
    rl.force_commit(None, StatusState(), inp)
    bottom_text = ' '.join(_strip_ansi(r) for r in term.bottom)
    # Mode footer should be present
    assert 'Enter' in bottom_text or 'send' in bottom_text


@pytest.mark.e2e
def test_long_session_200_turns():
    """200 turns must not OOM and committed must grow monotonically."""
    term, fc, rl = _make_stack()
    model = TranscriptModel()
    status = StatusState()
    prev_count = 0

    for i in range(200):
        model.append_turn(f'a{i}', 'assistant')
        model.append_line(f'a{i}', f'turn {i}')
        model.turns[-1].finalized = True
        rl.force_commit(model, status, None)
        assert len(term.committed) >= prev_count, 'Committed count must be monotonic'
        prev_count = len(term.committed)

    assert len(term.committed) > 0


@pytest.mark.e2e
def test_resize_does_not_corrupt_scrollback():
    """Simulated resize mid-session must not corrupt committed content."""
    term, fc, rl = _make_stack()
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'before resize')
    model.turns[-1].finalized = True
    rl.force_commit(model, StatusState(), None)

    # Simulate resize
    term._size = Size(rows=24, cols=60)
    rl.force_commit(model, StatusState(), None)

    assert any('before resize' in l for l in term.committed)


@pytest.mark.e2e
def test_tick_debounce_limits_write_rate():
    """tick() calls within 50ms must not all result in writes."""
    term, fc, rl = _make_stack()

    # Simulate rapid ticks (all within debounce window)
    rl._last_render = __import__('time').monotonic()
    before = term.write_calls
    for _ in range(100):
        rl.tick(None, StatusState(), None)

    # Should have produced 0 additional writes (all debounced)
    assert term.write_calls == before


@pytest.mark.e2e
def test_tool_call_in_committed_when_turn_finalized():
    term, fc, rl = _make_stack()
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    tc = model.add_tool_call('a1', 'tool-001', 'read_file', {'path': 'src/main.py'})
    model.finish_tool_call('tool-001', success=True, duration_ms=12, error=None)
    model.append_line('a1', 'Read the file.')
    model.turns[-1].finalized = True
    rl.force_commit(model, StatusState(), None)

    committed = ' '.join(term.committed)
    assert 'read_file' in committed
    assert 'src/main.py' in committed


@pytest.mark.e2e
def test_partial_text_cleared_after_turn_end():
    term, fc, rl = _make_stack()
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')

    # Streaming
    status = StatusState(active=True, partial_text='chunk1 chunk2')
    rl.force_commit(model, status, None)
    bottom1 = list(term.bottom)

    # Turn ends
    model.append_line('a1', 'chunk1 chunk2')
    model.turns[-1].finalized = True
    status = StatusState(active=False, partial_text='')
    rl.force_commit(model, status, None)
    bottom2 = list(term.bottom)

    # Bottom should no longer contain the streaming text
    bottom2_text = ' '.join(_strip_ansi(r) for r in bottom2)
    assert 'chunk1 chunk2' not in bottom2_text


@pytest.mark.e2e
def test_reset_starts_fresh():
    term, fc, rl = _make_stack()
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    model.append_line('a1', 'old session content')
    model.turns[-1].finalized = True
    rl.force_commit(model, StatusState(), None)

    rl.reset()
    assert rl._committed_count == 0
    assert rl._frame_num == 0

    # After reset, a fresh model produces 0 committed
    fresh_model = TranscriptModel()
    rl.force_commit(fresh_model, StatusState(), None)
    assert rl._committed_count == 0


@pytest.mark.e2e
def test_concurrent_tool_calls_all_committed():
    """Multiple tool calls in a single turn all appear in committed."""
    term, fc, rl = _make_stack()
    model = TranscriptModel()
    model.append_turn('a1', 'assistant')
    for i in range(5):
        model.add_tool_call('a1', f'tool-{i:03d}', f'tool_{i}', {'n': i})
        model.finish_tool_call(f'tool-{i:03d}', success=True, duration_ms=i * 10, error=None)
    model.append_line('a1', 'All tools done.')
    model.turns[-1].finalized = True
    rl.force_commit(model, StatusState(), None)

    committed = ' '.join(term.committed)
    for i in range(5):
        assert f'tool_{i}' in committed, f'tool_{i} missing from committed'


@pytest.mark.e2e
def test_approval_gate_in_bottom_block():
    """ApprovalGate content appears in the bottom block, not committed."""
    from agenthicc.tui.frame_composer import DropdownState, DropdownItem
    term, fc, rl = _make_stack()
    dd = DropdownState(
        items=[
            DropdownItem('[Y] Allow', 'allow'),
            DropdownItem('[N] Deny', 'deny'),
        ],
        trigger='/',
    )
    inp = InputState(dropdown=dd)
    rl.force_commit(None, StatusState(), inp)

    bottom_text = ' '.join(_strip_ansi(r) for r in term.bottom)
    assert 'Allow' in bottom_text or 'Deny' in bottom_text
    assert term.committed == [], 'Approval gate must not go to scrollback'


@pytest.mark.e2e
def test_bottom_block_height_bounded():
    """Bottom block must never exceed a sane height (≤ rows/2)."""
    term = FakeTerminal(rows=24, cols=80)
    fc = FrameComposer()
    rl = RenderLoop(term, fc)

    # Inject a huge dropdown
    from agenthicc.tui.frame_composer import DropdownState, DropdownItem
    dd = DropdownState(
        items=[DropdownItem(f'item_{i}', f'item_{i}') for i in range(50)],
        trigger='@',
    )
    inp = InputState(dropdown=dd)
    rl.force_commit(None, StatusState(), inp)

    assert term._bottom_height <= 24, f'Bottom block taller than terminal: {term._bottom_height}'
```

---

## 9. TranscriptModel — Required Interface

The `FrameComposer` imports `_render_turn` from `transcript.py`. That function must exist. The `TranscriptModel` must expose the following interface (existing code may already have compatible methods; the names and signatures below are authoritative):

```python
# src/agenthicc/tui/transcript.py (additions/clarifications)

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ToolCallEntry:
    tool_use_id: str
    name: str
    args: dict
    state: str = 'pending'        # 'pending' | 'running' | 'success' | 'error'
    duration_ms: int = 0
    error: str | None = None
    output_lines: list[str] = field(default_factory=list)
    expanded: bool = False


@dataclass
class AgentTurnEntry:
    agent_id: str
    agent_name: str
    timestamp: float
    lines: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    finalized: bool = False


class TranscriptModel:
    def __init__(self) -> None:
        self.turns: list[AgentTurnEntry] = []

    def append_turn(self, agent_id: str, agent_name: str) -> None:
        import time
        self.turns.append(AgentTurnEntry(
            agent_id=agent_id,
            agent_name=agent_name,
            timestamp=time.time(),
        ))

    def append_line(self, agent_id: str, line: str) -> None:
        for turn in reversed(self.turns):
            if turn.agent_id == agent_id:
                turn.lines.append(line)
                return

    def add_tool_call(
        self, agent_id: str, tool_use_id: str, name: str, args: dict
    ) -> ToolCallEntry:
        tc = ToolCallEntry(tool_use_id=tool_use_id, name=name, args=args, state='pending')
        for turn in reversed(self.turns):
            if turn.agent_id == agent_id:
                turn.tool_calls.append(tc)
                break
        return tc

    def finish_tool_call(
        self,
        tool_use_id: str,
        success: bool,
        duration_ms: int,
        error: str | None,
    ) -> None:
        for turn in self.turns:
            for tc in turn.tool_calls:
                if tc.tool_use_id == tool_use_id:
                    tc.state = 'success' if success else 'error'
                    tc.duration_ms = duration_ms
                    tc.error = error
                    return


def _render_turn(turn: AgentTurnEntry, cols: int = 80) -> list[str]:
    """Render a finalized AgentTurnEntry as a list of ANSI-formatted lines."""
    import time as _time
    lines: list[str] = []

    ts = _time.strftime('%H:%M:%S', _time.localtime(turn.timestamp))
    lines.append(
        f'\x1b[35;1m●\x1b[0m \x1b[35m{turn.agent_name}\x1b[0m'
        f'  \x1b[2m{ts}\x1b[0m'
    )

    for line in turn.lines:
        lines.append(f'  {line}')

    for tc in turn.tool_calls:
        lines.extend(_render_tool_call(tc))

    lines.append('')  # blank line separator between turns
    return lines


def _render_tool_call(tc: ToolCallEntry) -> list[str]:
    args_str = ', '.join(f"{k}='{v}'" for k, v in list(tc.args.items())[:3])
    call_str = f'{tc.name}({args_str})'

    if tc.state == 'success':
        status = f'\x1b[32m✓\x1b[0m \x1b[2m{tc.duration_ms}ms\x1b[0m'
    elif tc.state == 'error':
        err_msg = tc.error or 'error'
        status = f'\x1b[31m✗\x1b[0m \x1b[2m{tc.duration_ms}ms\x1b[0m  \x1b[31m{err_msg}\x1b[0m'
    elif tc.state == 'running':
        status = '\x1b[36m⧗\x1b[0m'
    else:
        status = '\x1b[2m○\x1b[0m'

    return [f'  \x1b[2m⎿\x1b[0m {call_str}  {status}']
```

---

## 10. Acceptance Criteria

Every item is binary pass/fail. The implementation is not complete until all pass.

### 10.1 Hard constraint verification (automated)

```bash
# Run in CI — must return exit code 0
uv run pytest tests/unit/test_terminal.py::test_no_smcup_emitted_by_terminal -v
uv run pytest tests/integration/test_tui_rendering.py::test_no_smcup_in_any_terminal_output -v
uv run pytest tests/e2e/test_tui_e2e.py::test_smcup_never_in_output -v

# Grep entire test output capture for smcup — must return exit code 1 (not found)
uv run python -c "
import io, sys
sys.path.insert(0, 'src')
from agenthicc.tui.terminal import Terminal
from agenthicc.tui.frame_composer import FrameComposer, StatusState, InputState
from agenthicc.tui.render_loop import RenderLoop
from agenthicc.tui.transcript import TranscriptModel

buf = io.StringIO()
term = Terminal(out=buf)
fc = FrameComposer()
rl = RenderLoop(term, fc)
model = TranscriptModel()
for i in range(10):
    model.append_turn(f'a{i}', 'assistant')
    model.append_line(f'a{i}', f'content {i}')
    model.turns[-1].finalized = True
    rl.force_commit(model, StatusState(), InputState())
term.teardown()
output = buf.getvalue()
assert '\x1b[?1049h' not in output, 'SMCUP FOUND'
assert '\x1b[?1049l' not in output, 'RMCUP FOUND'
print('PASS: no smcup/rmcup')
"
```

### 10.2 Full acceptance checklist

| # | Criterion | Test |
|---|-----------|------|
| 1 | `\x1b[?1049h` never in any output | `test_no_smcup_emitted_by_terminal`, `test_no_smcup_in_any_terminal_output`, `test_smcup_never_in_output` |
| 2 | `\x1b[?1049l` never in any output | Same tests |
| 3 | `\x1b[?1047h` and `\x1b[?47h` never appear | `test_no_alt_screen_variant_1047` |
| 4 | DECSTBM scroll region never appears | `test_no_decstbm_scroll_region`, `test_no_decstbm_emitted` |
| 5 | `_erase_bottom` goes up exactly N-1 rows | `test_erase_bottom_n_minus_1_invariant` |
| 6 | `set_bottom` has no trailing newline | `test_set_bottom_does_not_add_trailing_newline` |
| 7 | `size.rows` = `.lines`, `size.cols` = `.columns` | `test_size_uses_lines_not_columns` |
| 8 | `commit_lines` appends `\n` per line | `test_commit_lines_writes_newline_per_line` |
| 9 | Committed lines never re-rendered | `test_render_loop_does_not_repeat_committed` |
| 10 | Streaming text in bottom only, not committed | `test_streaming_lifecycle`, `test_streaming_text_in_bottom_not_committed` |
| 11 | Bottom block replaced on each `set_bottom` | `test_bottom_block_replaced_not_accumulated` |
| 12 | Single `write()` per frame | `test_single_write_per_set_bottom`, `test_render_loop_single_write_per_frame` |
| 13 | 200 turns without OOM | `test_long_session_200_turns` |
| 14 | `tick()` debounces at 50ms | `test_render_loop_tick_debounces`, `test_tick_debounce_limits_write_rate` |
| 15 | `force_commit` bypasses debounce | `test_render_loop_force_commit_bypasses_debounce` |
| 16 | SIGWINCH sets dirty flag, not I/O | `test_size_dirty_flag_triggers_requeries` |
| 17 | `FakeTerminal` never writes to stdout | `test_fake_terminal_no_ansi_output` |
| 18 | Input prompt visible in bottom rows | `test_input_bar_always_at_bottom` (integration), `test_input_prompt_in_bottom_rows` |
| 19 | Divider row present in bottom block | `test_frame_composer_bottom_has_divider` |
| 20 | Works in tmux (no smcup, no scroll region) | `test_no_smcup_in_any_terminal_output` (run with `TMUX=1` in env) |

### 10.3 Manual verification

1. Run `AGENTHICC_DEBUG_NO_ALTSCREEN=1 uv run agenthicc` — if any dependency emits smcup, a RuntimeError with traceback is raised immediately.
2. In a tmux session: `uv run agenthicc`. Scroll up with `Ctrl-b [` — committed transcript is visible.
3. Over SSH: connect from a remote machine; resize the terminal window — bottom block reflows within 50ms.
4. `NO_COLOR=1 uv run agenthicc` — no ANSI color codes in output; symbols (●, ✓, ✗, ⎿) still present.
5. `uv run agenthicc 2>/dev/null | grep -c $'\x1b\[\?1049h'` — must output `0`.

---

## 11. File Locations Summary

| File | Purpose |
|------|---------|
| `src/agenthicc/tui/terminal.py` | `Terminal`, `FakeTerminal`, `Key`, `Size`, `_clip_to_cols`, `_read_key_from_fd` |
| `src/agenthicc/tui/frame_composer.py` | `FrameComposer`, `Frame`, `StatusState`, `InputState`, `DropdownState`, `DropdownItem` |
| `src/agenthicc/tui/render_loop.py` | `RenderLoop` |
| `src/agenthicc/tui/transcript.py` | `TranscriptModel`, `AgentTurnEntry`, `ToolCallEntry`, `_render_turn` |
| `tests/unit/test_terminal.py` | 55 unit tests |
| `tests/integration/test_tui_rendering.py` | 20 integration tests (pyte) |
| `tests/e2e/test_tui_e2e.py` | 15 e2e tests |

---

## 12. Implementation Order

Implement in this exact order to avoid circular dependency issues:

1. `src/agenthicc/tui/terminal.py` — `Size`, `Key`, helpers, `Terminal`, `FakeTerminal`
2. `src/agenthicc/tui/transcript.py` — `ToolCallEntry`, `AgentTurnEntry`, `TranscriptModel`, `_render_turn`
3. `src/agenthicc/tui/frame_composer.py` — `StatusState`, `InputState`, `DropdownState`, `Frame`, `FrameComposer`
4. `src/agenthicc/tui/render_loop.py` — `RenderLoop`
5. `tests/unit/test_terminal.py`
6. `tests/integration/test_tui_rendering.py`
7. `tests/e2e/test_tui_e2e.py`

Run after each file:
```bash
uv run pytest tests/unit/test_terminal.py -q
uv run ruff check src/agenthicc/tui/ tests/
```
