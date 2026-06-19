"""Windows terminal backend — uses msvcrt (PRD-106).

All ``msvcrt`` usage is confined to this module.  No other file in the
application may import ``msvcrt`` directly.

Extended key mapping
--------------------
Windows sends two-character sequences for special keys.  The first character
is ``\\x00`` or ``\\xe0``; the second is a scan code.  The table below
normalises these into platform-independent ``Key`` values.

    Prefix  Scan  Key
    ──────  ────  ──────────
    \\xe0    H     UP
    \\xe0    P     DOWN
    \\xe0    K     LEFT
    \\xe0    M     RIGHT
    \\xe0    G     HOME
    \\xe0    O     END
    \\xe0    S     (Delete — ignored)
    \\x00    \\x0f  SHIFT_TAB
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Generator

from agenthicc.tui.cbreak_reader import Key

__all__ = ["WindowsBackend"]

# ── extended key scan-code table ─────────────────────────────────────────────
# Prefix \xe0 — cursor and editing keys on modern keyboards / terminals.
_EXT_E0: dict[str, Key] = {
    "H": Key.UP,
    "P": Key.DOWN,
    "K": Key.LEFT,
    "M": Key.RIGHT,
    "G": Key.HOME,
    "O": Key.END,
    # "S": Key.CHAR,   # Delete — treat as no-op (matches POSIX behavior)
}

# Prefix \x00 — function / special keys on legacy keyboards.
_EXT_00: dict[str, Key] = {
    "\x0f": Key.SHIFT_TAB,   # Shift+Tab
    "H":    Key.UP,           # also routed here on some terminals
    "P":    Key.DOWN,
    "K":    Key.LEFT,
    "M":    Key.RIGHT,
    "G":    Key.HOME,
    "O":    Key.END,
}


class WindowsBackend:
    """Terminal backend for Windows using the ``msvcrt`` standard-library module.

    ``msvcrt.getwch()`` reads one Unicode character directly from the Windows
    console without waiting for Enter and without echoing — no additional
    console-mode setup is required.
    """

    # ── TerminalBackend interface ─────────────────────────────────────────────

    def is_interactive(self) -> bool:
        """True when running inside an interactive Windows console."""
        try:
            return sys.stdin.isatty()
        except Exception:  # noqa: BLE001
            return False

    def read_key(self) -> tuple[Key, str]:
        """Read one keystroke; blocks until a key is available."""
        import msvcrt  # noqa: PLC0415
        ch = msvcrt.getwch()

        # ── single-character controls ─────────────────────────────────────────
        if ch == "\x03":            return (Key.CTRL_C, "")
        if ch == "\x04":            return (Key.CTRL_D, "")
        if ch == "\r":              return (Key.ENTER, "")
        if ch == "\n":              return (Key.CTRL_ENTER, "")
        if ch == "\t":              return (Key.TAB, "")
        if ch in ("\x7f", "\x08"): return (Key.BACKSPACE, "")
        if ch == "\x15":            return (Key.CTRL_U, "")
        if ch == "\x16":            return (Key.CTRL_V, "")
        if ch == "@":               return (Key.AT, "")
        if ch == "\x1b":            return (Key.ESC, "")

        # ── extended two-byte sequences ───────────────────────────────────────
        if ch == "\xe0":
            ext = msvcrt.getwch()
            mapped = _EXT_E0.get(ext)
            return (mapped, "") if mapped is not None else (Key.ESC, "")

        if ch == "\x00":
            ext = msvcrt.getwch()
            mapped = _EXT_00.get(ext)
            return (mapped, "") if mapped is not None else (Key.ESC, "")

        # ── printable / Unicode ───────────────────────────────────────────────
        if ch.isprintable():
            return (Key.CHAR, ch)
        return (Key.ESC, "")

    @contextmanager
    def enter_raw_mode(self) -> Generator[None, None, None]:
        """No-op: ``msvcrt.getwch()`` already bypasses line buffering."""
        yield

    def restore(self) -> None:
        """No-op: nothing was configured, nothing to restore."""
