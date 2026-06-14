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
"""
from __future__ import annotations

import os
import select
from contextlib import contextmanager
from enum import Enum
from typing import Generator


# ── Key enum ──────────────────────────────────────────────────────────────────

class Key(str, Enum):
    UP         = "UP"
    DOWN       = "DOWN"
    LEFT       = "LEFT"
    RIGHT      = "RIGHT"
    HOME       = "HOME"
    END        = "END"
    ENTER      = "ENTER"
    CTRL_ENTER = "CTRL_ENTER"   # insert newline (multi-line input)
    CTRL_V     = "CTRL_V"       # expand condensed paste
    PASTE      = "PASTE"        # bracketed paste event; ch carries pasted text
    TAB        = "TAB"
    ESC        = "ESC"
    BACKSPACE  = "BACKSPACE"
    CTRL_C     = "CTRL_C"
    CTRL_D     = "CTRL_D"
    CTRL_U     = "CTRL_U"
    SHIFT_TAB  = "SHIFT_TAB"
    AT         = "AT"
    CHAR       = "CHAR"


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
    """
    import termios   # noqa: PLC0415
    import sys       # noqa: PLC0415
    import tty       # noqa: PLC0415

    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)                      # enable CBREAK
        cur = list(termios.tcgetattr(fd))      # read POST-CBREAK settings (NOT old)
        cur[0] &= ~termios.ICRNL               # Enter stays as \r
        cur[3] &= ~(termios.ECHOCTL | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, cur)
        sys.stdout.write("\x1b[?25l\x1b[?2004h")  # hide cursor + bracketed paste ON
        sys.stdout.flush()
        yield fd
    finally:
        sys.stdout.write("\x1b[?2004l\x1b[?25h")  # bracketed paste OFF + show cursor
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Keystroke reader ──────────────────────────────────────────────────────────

def read_key(fd: int) -> tuple[Key, str]:
    """Read one keystroke from *fd* and return ``(Key, char_or_empty)``.

    Uses ``os.read(fd, 1)`` for raw bytes.  Escape sequences are parsed with a
    50 ms peek timeout so a lone ESC is distinguished from cursor-key sequences.
    Bracketed paste payloads are read in full (up to a 2 s timeout).
    """
    b = os.read(fd, 1)

    if b == b"\x03":  return (Key.CTRL_C, "")
    if b == b"\x04":  return (Key.CTRL_D, "")
    if b == b"\r":    return (Key.ENTER, "")
    if b == b"\n":    return (Key.CTRL_ENTER, "")   # Ctrl+J — insert newline
    if b == b"\t":    return (Key.TAB, "")
    if b in (b"\x7f", b"\x08"):  return (Key.BACKSPACE, "")
    if b == b"\x15":  return (Key.CTRL_U, "")
    if b == b"\x16":  return (Key.CTRL_V, "")
    if b == b"@":     return (Key.AT, "")

    if b == b"\x1b":
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            return (Key.ESC, "")
        b2 = os.read(fd, 1)
        if b2 != b"[":
            return (Key.ESC, "")

        # CSI sequence — read until the final byte (letter or '~').
        seq = b""
        while True:
            r_s, _, _ = select.select([fd], [], [], 0.05)
            if not r_s:
                break
            b_s = os.read(fd, 1)
            seq += b_s
            if b_s[-1:] in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz~":
                break

        if seq == b"A":    return (Key.UP, "")
        if seq == b"B":    return (Key.DOWN, "")
        if seq == b"C":    return (Key.RIGHT, "")
        if seq == b"D":    return (Key.LEFT, "")
        if seq == b"H":    return (Key.HOME, "")
        if seq == b"F":    return (Key.END, "")
        if seq == b"Z":    return (Key.SHIFT_TAB, "")
        if seq == b"1~":   return (Key.HOME, "")
        if seq == b"3~":   return (Key.CHAR, "")    # Delete — ignore
        if seq == b"4~":   return (Key.END, "")
        # Kitty KP Ctrl+Enter: \x1b[13;5u or \x1b[13;1u (disambiguate mode)
        if seq in (b"13;5u", b"13;1u", b"13u"):  return (Key.ENTER, "")

        # Bracketed paste: \x1b[200~ starts the payload; read until \x1b[201~.
        if seq == b"200~":
            _TERM = b"\x1b[201~"
            paste_bytes = b""
            while True:
                r_p, _, _ = select.select([fd], [], [], 2.0)
                if not r_p:
                    break
                paste_bytes += os.read(fd, 4096)
                if _TERM in paste_bytes:
                    paste_bytes = paste_bytes[:paste_bytes.index(_TERM)]
                    break
            try:
                pasted = paste_bytes.decode("utf-8")
            except UnicodeDecodeError:
                pasted = paste_bytes.decode("utf-8", errors="replace")
            return (Key.PASTE, pasted)

        return (Key.ESC, "")

    # Printable or multi-byte UTF-8
    raw = b
    first = b[0]
    if   first & 0b11100000 == 0b11000000:  n_extra = 1
    elif first & 0b11110000 == 0b11100000:  n_extra = 2
    elif first & 0b11111000 == 0b11110000:  n_extra = 3
    else:                                    n_extra = 0

    for _ in range(n_extra):
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            raw += os.read(fd, 1)

    try:
        ch = raw.decode("utf-8")
    except UnicodeDecodeError:
        return (Key.ESC, "")

    if ch.isprintable():
        return (Key.CHAR, ch)
    return (Key.ESC, "")
