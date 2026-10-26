"""Shared CBREAK-mode terminal primitives.

Both :mod:`~agenthicc.tui.mention_input` (idle CBREAK prompt) and
:mod:`~agenthicc.tui.streaming_input` (agent-turn keystroke capture) need
identical terminal-setup / teardown logic and the same ``Key`` enum.  This
module provides them in one place so the two callers can never drift apart.

Exports
-------
raw_mode(fd)    Context manager — enable CBREAK, yield fd, restore on exit.
Key             Enum of all logical keystrokes the TUI handles.
read_key(fd)    Read one keystroke and return ``(Key, char_or_empty)``.
key_name(k, ch) The canonical binding name of a keystroke (``"ctrl+g"``, ``"shift+up"``).
set_reporting   Enable or disable focus and mouse reporting (AGX-13).

Decoding (AGX-13) is a set of pure functions over bytes —
:func:`decode_control`, :func:`decode_csi`, :func:`decode_ss3` and
:func:`decode_alt` — so every sequence can be tested without a terminal.
"""

from __future__ import annotations

import os
import select
from contextlib import contextmanager
from enum import Enum
from typing import Generator


# ── Key enum ──────────────────────────────────────────────────────────────────


class Key(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    HOME = "HOME"
    END = "END"
    ENTER = "ENTER"
    CTRL_ENTER = "CTRL_ENTER"  # insert newline (multi-line input)
    CTRL_V = "CTRL_V"  # expand condensed paste
    PASTE = "PASTE"  # bracketed paste event; ch carries pasted text
    TAB = "TAB"
    ESC = "ESC"
    BACKSPACE = "BACKSPACE"
    CTRL_C = "CTRL_C"
    CTRL_D = "CTRL_D"
    CTRL_U = "CTRL_U"
    CTRL_X = "CTRL_X"
    SHIFT_TAB = "SHIFT_TAB"
    PAGE_UP = "PAGE_UP"
    PAGE_DOWN = "PAGE_DOWN"
    AT = "AT"
    CHAR = "CHAR"
    # AGX-13: the rest of the keyboard.
    CTRL_A = "CTRL_A"
    CTRL_B = "CTRL_B"
    CTRL_E = "CTRL_E"
    CTRL_F = "CTRL_F"
    CTRL_G = "CTRL_G"
    CTRL_K = "CTRL_K"
    CTRL_L = "CTRL_L"
    CTRL_N = "CTRL_N"
    CTRL_O = "CTRL_O"
    CTRL_P = "CTRL_P"
    CTRL_Q = "CTRL_Q"
    CTRL_R = "CTRL_R"
    CTRL_S = "CTRL_S"
    CTRL_T = "CTRL_T"
    CTRL_W = "CTRL_W"
    CTRL_Y = "CTRL_Y"
    CTRL_Z = "CTRL_Z"
    DELETE = "DELETE"
    INSERT = "INSERT"
    FUNCTION = "FUNCTION"  # ch: "f1" … "f12"
    ALT = "ALT"  # ch: the character typed with Alt (Option)
    MODIFIED = "MODIFIED"  # ch: canonical name, e.g. "shift+up" or "ctrl+delete"
    FOCUS_IN = "FOCUS_IN"
    FOCUS_OUT = "FOCUS_OUT"
    MOUSE = "MOUSE"  # ch: MouseEvent.encode()
    UNKNOWN = "UNKNOWN"  # an escape sequence nothing binds; never an Esc press


# ── CBREAK context manager ────────────────────────────────────────────────────


@contextmanager
def raw_mode(fd: int) -> Generator[int, None, None]:
    """Enable CBREAK on *fd*, yield it, restore the original settings on exit.

    **Why read POST-CBREAK settings:** ``tty.setcbreak`` only clears ``ECHO``
    and ``ICANON``.  Reading the termios *after* ``setcbreak`` and patching
    on top of those settings means our changes are layered correctly.  Reading
    *before* setcbreak and reapplying would silently undo CBREAK — that is the
    classic bug this design explicitly avoids.

    Additional patches applied on top of CBREAK:

    * Clear ``ICRNL`` — stop the terminal from translating ``\\r`` → ``\\n`` so
      Enter (``\\r``) is always distinct from Ctrl+J (``\\n``).
    * Clear ``ISIG`` — deliver Ctrl+C as ``\\x03`` to stdin instead of raising
      ``SIGINT``.  Our state machine handles ``\\x03`` gracefully; the raw signal
      would bypass that.
    * Clear ``ECHOCTL`` — suppress ``^C`` echo artefacts.
    * Write ``\\x1b[?25l`` (hide OS cursor) and ``\\x1b[?2004h`` (enable bracketed
      paste) while active; restore on exit.
    * On the first entry in this process, ask the terminal whether it supports
      synchronized output (DECRQM for mode 2026); :func:`read_key` consumes the
      answer (AGX-11).
    """
    import atexit
    import sys

    # Guard: termios and tty are Unix-only.  When unavailable (Windows, some CI
    # environments) yield the fd without raw-mode setup and return — the keyboard
    # read loop still runs in line-buffered mode rather than crashing.
    try:
        import termios
        import tty
    except ImportError:
        yield fd
        return

    # Belt-and-suspenders: if ECHO is already off when we enter raw_mode it
    # means a previous raw_mode context hasn't finished its cleanup yet (race
    # condition between asyncio task cancellation and thread-pool startup).
    # Re-enable ECHO + ICANON BEFORE capturing "old" so that _restore() will
    # always put the terminal back to a usable state.
    try:
        _pre = termios.tcgetattr(fd)
        if not (_pre[3] & termios.ECHO):
            _pre[3] |= termios.ECHO | termios.ICANON
            termios.tcsetattr(fd, termios.TCSANOW, _pre)
    except Exception:  # noqa: BLE001
        pass

    # Guard: tcgetattr fails with "Inappropriate ioctl for device" when fd is a
    # redirected pipe or non-TTY.  Degrade to passthrough instead of crashing.
    try:
        old = termios.tcgetattr(fd)
    except Exception:  # noqa: BLE001
        yield fd
        return

    def _restore() -> None:
        """Restore terminal to pre-CBREAK state.

        Called both from the normal ``finally`` path and (as a safety net) from
        ``atexit`` in case the thread is killed before ``finally`` runs — e.g.
        when the main asyncio thread catches ``KeyboardInterrupt`` and exits
        while this worker thread is still alive.  ``TCSAFLUSH`` discards
        buffered input so the terminal is immediately usable.
        """
        try:
            sys.stdout.write(
                "\x1b[m"  # reset all SGR attributes (colour, bold, dim…)
                "\x1b[?2004l"  # bracketed paste OFF
                "\x1b[?25h"  # show cursor
            )
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_restore)
    try:
        tty.setcbreak(fd)  # enable CBREAK
        cur = list(termios.tcgetattr(fd))  # read POST-CBREAK settings (NOT old)
        cur[0] &= ~termios.ICRNL  # Enter stays as \r
        cur[3] &= ~(termios.ECHOCTL | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, cur)
        from agenthicc.tui.terminal.capabilities import DECRQM_SYNCHRONIZED_OUTPUT, capabilities

        query = DECRQM_SYNCHRONIZED_OUTPUT if capabilities.claim_query() else ""
        sys.stdout.write("\x1b[?25l\x1b[?2004h" + query)  # hide cursor + bracketed paste ON
        sys.stdout.flush()
        yield fd
    finally:
        atexit.unregister(_restore)  # normal exit — remove so atexit doesn't double-restore
        _restore()


# ── Keystroke reader ──────────────────────────────────────────────────────────

# C0 control bytes → keys. Ctrl+H, Ctrl+I, Ctrl+J and Ctrl+M keep their
# traditional meanings (Backspace, Tab, newline, Enter).
_CONTROL_KEYS: dict[int, Key] = {
    0x01: Key.CTRL_A,
    0x02: Key.CTRL_B,
    0x03: Key.CTRL_C,
    0x04: Key.CTRL_D,
    0x05: Key.CTRL_E,
    0x06: Key.CTRL_F,
    0x07: Key.CTRL_G,
    0x08: Key.BACKSPACE,
    0x09: Key.TAB,
    0x0A: Key.CTRL_ENTER,
    0x0B: Key.CTRL_K,
    0x0C: Key.CTRL_L,
    0x0D: Key.ENTER,
    0x0E: Key.CTRL_N,
    0x0F: Key.CTRL_O,
    0x10: Key.CTRL_P,
    0x11: Key.CTRL_Q,
    0x12: Key.CTRL_R,
    0x13: Key.CTRL_S,
    0x14: Key.CTRL_T,
    0x15: Key.CTRL_U,
    0x16: Key.CTRL_V,
    0x17: Key.CTRL_W,
    0x18: Key.CTRL_X,
    0x19: Key.CTRL_Y,
    0x1A: Key.CTRL_Z,
    0x1B: Key.ESC,
    0x7F: Key.BACKSPACE,
}
_CONTROL_NAMES: dict[int, str] = {
    0x00: "ctrl+space",
    0x1C: "ctrl+\\",
    0x1D: "ctrl+]",
    0x1E: "ctrl+^",
    0x1F: "ctrl+_",
}
_CTRL_LETTER_KEYS = frozenset(
    key for key in Key if key.value.startswith("CTRL_") and len(key.value) == len("CTRL_A")
)

# Final byte of an unmodified or modified (``CSI 1 ; m X``) cursor sequence.
_CURSOR_FINALS: dict[bytes, tuple[Key, str]] = {
    b"A": (Key.UP, "up"),
    b"B": (Key.DOWN, "down"),
    b"C": (Key.RIGHT, "right"),
    b"D": (Key.LEFT, "left"),
    b"H": (Key.HOME, "home"),
    b"F": (Key.END, "end"),
}
# SS3 (``ESC O x``) and ``CSI 1 ; m P…S``: F1–F4.
_SS3_FUNCTIONS = {b"P": "f1", b"Q": "f2", b"R": "f3", b"S": "f4"}
# ``CSI n ~`` (and ``CSI n ; m ~``) keys.
_TILDE_KEYS: dict[int, tuple[Key, str]] = {
    1: (Key.HOME, "home"),
    2: (Key.INSERT, "insert"),
    3: (Key.DELETE, "delete"),
    4: (Key.END, "end"),
    5: (Key.PAGE_UP, "pageup"),
    6: (Key.PAGE_DOWN, "pagedown"),
    7: (Key.HOME, "home"),
    8: (Key.END, "end"),
    **{code: (Key.FUNCTION, f"f{index + 1}") for index, code in enumerate((11, 12, 13, 14, 15))},
    **{code: (Key.FUNCTION, f"f{index + 6}") for index, code in enumerate((17, 18, 19, 20, 21))},
    23: (Key.FUNCTION, "f11"),
    24: (Key.FUNCTION, "f12"),
}


def _modifiers(parameter: int) -> str:
    """Return the ``ctrl+alt+shift+`` prefix for an xterm modifier parameter."""
    bits = max(0, parameter - 1)
    return "".join(
        prefix
        for bit, prefix in ((4, "ctrl+"), (2, "alt+"), (1, "shift+"), (8, "meta+"))
        if bits & bit
    )


def _named(key: Key, name: str, modifier: int) -> tuple[Key, str]:
    """Return *key* unmodified, or a ``MODIFIED`` key carrying the full name."""
    prefix = _modifiers(modifier)
    if not prefix:
        return (key, name if key is Key.FUNCTION else "")
    if prefix == "shift+" and key is Key.TAB:
        return (Key.SHIFT_TAB, "")
    return (Key.MODIFIED, prefix + name)


def decode_control(byte: int) -> tuple[Key, str]:
    """Decode one C0 control byte (or DEL)."""
    key = _CONTROL_KEYS.get(byte)
    if key is not None:
        return (key, "")
    name = _CONTROL_NAMES.get(byte)
    return (Key.MODIFIED, name) if name is not None else (Key.UNKNOWN, "")


def decode_csi(sequence: bytes) -> tuple[Key, str]:
    """Decode the bytes after ``ESC [`` up to and including the final byte."""
    final = sequence[-1:]
    body = sequence[:-1]
    if sequence.startswith(b"<") and final in (b"M", b"m"):
        event = MouseEvent.parse(sequence[1:-1], pressed=final == b"M")
        return (Key.MOUSE, event.encode()) if event is not None else (Key.UNKNOWN, "")
    if final == b"I" and not body:
        return (Key.FOCUS_IN, "")
    if final == b"O" and not body:
        return (Key.FOCUS_OUT, "")
    if final == b"Z" and not body:
        return (Key.SHIFT_TAB, "")
    if sequence in (b"13;5u", b"13;1u", b"13u"):
        # Kitty keypad Ctrl+Enter in disambiguate mode submits like Enter.
        return (Key.ENTER, "")
    fields = body.split(b";") if body else []
    if not all(field.isdigit() for field in fields):
        return (Key.UNKNOWN, "")
    numbers = [int(field) for field in fields]
    modifier = numbers[1] if len(numbers) > 1 else 1
    cursor = _CURSOR_FINALS.get(final)
    if cursor is not None and (not numbers or numbers[0] == 1):
        return _named(cursor[0], cursor[1], modifier)
    function = _SS3_FUNCTIONS.get(final)
    if function is not None and numbers and numbers[0] == 1:
        return _named(Key.FUNCTION, function, modifier)
    if final == b"~" and numbers:
        tilde = _TILDE_KEYS.get(numbers[0])
        if tilde is not None:
            return _named(tilde[0], tilde[1], modifier)
    return (Key.UNKNOWN, "")


def decode_ss3(final: bytes) -> tuple[Key, str]:
    """Decode ``ESC O x``: F1–F4, or a cursor key in application mode."""
    function = _SS3_FUNCTIONS.get(final)
    if function is not None:
        return (Key.FUNCTION, function)
    cursor = _CURSOR_FINALS.get(final)
    return (cursor[0], "") if cursor is not None else (Key.UNKNOWN, "")


def decode_alt(data: bytes) -> tuple[Key, str]:
    """Decode the byte(s) typed after ``ESC`` with Alt (Option) held."""
    if len(data) == 1 and (data[0] < 0x20 or data[0] == 0x7F):
        key, ch = decode_control(data[0])
        base = key_name(key, ch)
        return (Key.MODIFIED, f"alt+{base}") if base else (Key.UNKNOWN, "")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return (Key.UNKNOWN, "")
    return (Key.ALT, text) if text.isprintable() else (Key.UNKNOWN, "")


_KEY_NAMES: dict[Key, str] = {
    Key.UP: "up",
    Key.DOWN: "down",
    Key.LEFT: "left",
    Key.RIGHT: "right",
    Key.HOME: "home",
    Key.END: "end",
    Key.ENTER: "enter",
    Key.CTRL_ENTER: "ctrl+j",
    Key.TAB: "tab",
    Key.SHIFT_TAB: "shift+tab",
    Key.ESC: "escape",
    Key.BACKSPACE: "backspace",
    Key.DELETE: "delete",
    Key.INSERT: "insert",
    Key.PAGE_UP: "pageup",
    Key.PAGE_DOWN: "pagedown",
    Key.AT: "@",
    Key.FOCUS_IN: "focus_in",
    Key.FOCUS_OUT: "focus_out",
}


def key_name(key: Key, ch: str = "") -> str:
    """Return the canonical binding name of a keystroke (AGX-13).

    Printable characters name themselves (``" "`` is ``"space"``), Ctrl
    letters are ``"ctrl+g"``, Alt characters ``"alt+x"``, and modified keys
    carry their full name (``"ctrl+shift+left"``). Returns ``""`` for input
    that cannot be bound (pastes, mouse reports, unknown sequences).
    """
    if key is Key.CHAR:
        return "space" if ch == " " else ch
    if key in _CTRL_LETTER_KEYS:
        return f"ctrl+{key.value[-1].lower()}"
    if key is Key.ALT:
        if ch.isalpha() and ch.isupper():
            return f"alt+shift+{ch.lower()}"
        return f"alt+{'space' if ch == ' ' else ch}"
    if key in (Key.MODIFIED, Key.FUNCTION):
        return ch
    return _KEY_NAMES.get(key, "")


class MouseEvent:
    """One SGR mouse report: button, 1-based column and row, press or release."""

    __slots__ = ("button", "column", "row", "pressed")

    #: Button codes: 0 left, 1 middle, 2 right; 64/65 wheel up/down.
    WHEEL_UP = 64
    WHEEL_DOWN = 65

    def __init__(self, button: int, column: int, row: int, *, pressed: bool) -> None:
        self.button = button
        self.column = column
        self.row = row
        self.pressed = pressed

    @classmethod
    def parse(cls, body: bytes, *, pressed: bool) -> "MouseEvent | None":
        fields = body.split(b";")
        if len(fields) != 3 or not all(field.isdigit() for field in fields):
            return None
        button, column, row = (int(field) for field in fields)
        return cls(button, column, row, pressed=pressed)

    @classmethod
    def decode(cls, encoded: str) -> "MouseEvent | None":
        """Inverse of :meth:`encode` (the ``ch`` of a ``Key.MOUSE`` keystroke)."""
        body, _, state = encoded.rpartition(";")
        if state not in ("press", "release"):
            return None
        return cls.parse(body.encode(), pressed=state == "press")

    def encode(self) -> str:
        return f"{self.button};{self.column};{self.row};{'press' if self.pressed else 'release'}"

    @property
    def wheel(self) -> int:
        """``-1`` for wheel up, ``1`` for wheel down, ``0`` otherwise."""
        base = self.button & ~0b11100  # drop the shift/alt/ctrl bits
        return -1 if base == self.WHEEL_UP else 1 if base == self.WHEEL_DOWN else 0


#: Focus reporting (AGX-10) and SGR mouse reporting (AGX-13).
FOCUS_REPORTING_ON = "\x1b[?1004h"
FOCUS_REPORTING_OFF = "\x1b[?1004l"
MOUSE_REPORTING_ON = "\x1b[?1000h\x1b[?1006h"
MOUSE_REPORTING_OFF = "\x1b[?1006l\x1b[?1000l"


def set_reporting(*, focus: bool | None = None, mouse: bool | None = None) -> None:
    """Turn focus and/or mouse reporting on or off on the terminal."""
    import sys

    codes = ""
    if focus is not None:
        codes += FOCUS_REPORTING_ON if focus else FOCUS_REPORTING_OFF
    if mouse is not None:
        codes += MOUSE_REPORTING_ON if mouse else MOUSE_REPORTING_OFF
    if codes:
        sys.stdout.write(codes)
        sys.stdout.flush()


def _record_mode_report(body: bytes) -> None:
    """Record a DECRPM ``mode;value`` answer; ignore anything malformed."""
    from agenthicc.tui.terminal.capabilities import capabilities

    mode, separator, value = body.partition(b";")
    if separator and mode.isdigit() and value.isdigit():
        capabilities.report_mode(int(mode), int(value))


def _wait_readable(fd: int, timeout: float | None, cancel_fd: int | None) -> bool:
    """Wait for input, raising promptly if the optional cancellation pipe fires."""
    watched = [fd] if cancel_fd is None else [fd, cancel_fd]
    ready, _, _ = select.select(watched, [], [], timeout)
    if cancel_fd is not None and cancel_fd in ready:
        # Drain the wake byte so a backend may safely reuse its cancellation
        # pipe for another read after resetting its lifecycle.
        os.read(cancel_fd, 1)
        raise OSError("terminal input stopped")
    return fd in ready


def _read_input(fd: int, size: int, cancel_fd: int | None) -> bytes:
    if cancel_fd is not None:
        _wait_readable(fd, None, cancel_fd)
    return os.read(fd, size)


def _read_utf8_tail(first: int, fd: int, cancel_fd: int | None) -> bytes:
    """Read the continuation bytes of a UTF-8 character starting with *first*."""
    if first & 0b11100000 == 0b11000000:
        extra = 1
    elif first & 0b11110000 == 0b11100000:
        extra = 2
    elif first & 0b11111000 == 0b11110000:
        extra = 3
    else:
        extra = 0
    tail = b""
    for _ in range(extra):
        if _wait_readable(fd, 0.05, cancel_fd):
            tail += _read_input(fd, 1, cancel_fd)
    return tail


def read_key(fd: int, *, cancel_fd: int | None = None) -> tuple[Key, str]:
    """Read one keystroke from *fd* and return ``(Key, char_or_empty)``.

    Uses ``os.read(fd, 1)`` for raw bytes. Escape sequences are parsed with a
    50 ms peek timeout so a lone ESC is distinguished from cursor-key sequences
    and Alt combinations. Bracketed paste payloads are read in full (up to a
    2 s timeout). When ``cancel_fd`` is supplied, it is watched alongside input
    so shutdown can interrupt an otherwise idle blocking read.
    """
    b = _read_input(fd, 1, cancel_fd)
    first = b[0]

    if first == 0x1B:
        if not _wait_readable(fd, 0.05, cancel_fd):
            return (Key.ESC, "")
        b2 = _read_input(fd, 1, cancel_fd)
        if b2 == b"O":
            if not _wait_readable(fd, 0.05, cancel_fd):
                return (Key.ALT, "O")
            return decode_ss3(_read_input(fd, 1, cancel_fd))
        if b2 != b"[":
            if b2 == b"\x1b":
                return (Key.ESC, "")
            return decode_alt(b2 + _read_utf8_tail(b2[0], fd, cancel_fd))

        # CSI sequence — read until the final byte (0x40–0x7E).
        seq = b""
        while True:
            if not _wait_readable(fd, 0.05, cancel_fd):
                break
            b_s = _read_input(fd, 1, cancel_fd)
            seq += b_s
            if 0x40 <= b_s[-1] <= 0x7E:
                break
        if seq.startswith(b"?") and seq.endswith(b"$y"):
            # DECRPM: the terminal's answer to a mode query, not a keystroke.
            _record_mode_report(seq[1:-2])
            return read_key(fd, cancel_fd=cancel_fd)

        # Bracketed paste: \x1b[200~ starts the payload; read until \x1b[201~.
        if seq == b"200~":
            _TERM = b"\x1b[201~"
            paste_bytes = b""
            while True:
                if not _wait_readable(fd, 2.0, cancel_fd):
                    break
                paste_bytes += _read_input(fd, 4096, cancel_fd)
                if _TERM in paste_bytes:
                    paste_bytes = paste_bytes[: paste_bytes.index(_TERM)]
                    break
            try:
                pasted = paste_bytes.decode("utf-8")
            except UnicodeDecodeError:
                pasted = paste_bytes.decode("utf-8", errors="replace")
            return (Key.PASTE, pasted)
        if not seq:
            return (Key.ALT, "[")
        return decode_csi(seq)

    if first < 0x20 or first == 0x7F:
        return decode_control(first)
    if b == b"@":
        return (Key.AT, "")

    # Printable or multi-byte UTF-8
    raw = b + _read_utf8_tail(first, fd, cancel_fd)
    try:
        ch = raw.decode("utf-8")
    except UnicodeDecodeError:
        return (Key.UNKNOWN, "")

    if ch.isprintable():
        return (Key.CHAR, ch)
    return (Key.UNKNOWN, "")
