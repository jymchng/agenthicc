"""Windows terminal backend — uses msvcrt (PRD-106).

All ``msvcrt`` usage is confined to this module.  No other file in the
application may import ``msvcrt`` directly.

Two input formats are handled (PRD-106 Amendment — ConPTY support)
-------------------------------------------------------------------

**BIOS scan codes** — legacy CMD, old PowerShell without VT processing:

    Prefix  Scan  Key
    ──────  ────  ──────────
    \\xe0    H     UP
    \\xe0    P     DOWN
    \\xe0    K     LEFT
    \\xe0    M     RIGHT
    \\xe0    G     HOME
    \\xe0    O     END
    \\x00    \\x0f  SHIFT_TAB

**VT/ANSI CSI sequences** — modern ConPTY environments (Windows Terminal,
VS Code integrated terminal, new PowerShell with VT processing enabled).
ConPTY translates keyboard input to the same escape sequences as Linux/macOS,
so ``\\x1b[Z`` (Shift+Tab) must be parsed instead of being truncated to ESC.

    Sequence  Key
    ────────  ──────────
    \\x1b[Z   SHIFT_TAB
    \\x1b[A   UP
    \\x1b[B   DOWN
    \\x1b[C   RIGHT
    \\x1b[D   LEFT
    \\x1b[H   HOME
    \\x1b[F   END

Detection: after reading ``\\x1b``, ``msvcrt.kbhit()`` is polled.  If more
characters are pending the sequence is read; if not, a lone ESC is returned.
``select.select`` is not available for Windows console handles; ``kbhit()`` is
the correct Windows equivalent.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Generator

from agenthicc.tui.cbreak_reader import Key

__all__ = ["WindowsBackend"]

# ── BIOS scan-code tables (legacy CMD / old PowerShell) ──────────────────────

# Prefix \xe0 — cursor and editing keys on modern keyboards.
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
    "\x0f": Key.SHIFT_TAB,   # Shift+Tab (BIOS scan code 15)
    "H":    Key.UP,
    "P":    Key.DOWN,
    "K":    Key.LEFT,
    "M":    Key.RIGHT,
    "G":    Key.HOME,
    "O":    Key.END,
}

# ── VT/ANSI CSI table (ConPTY: Windows Terminal, VS Code, new PowerShell) ────
# ConPTY translates keyboard input to the same escape sequences used on Linux.
# These are the sequences that arrive after \x1b[ has been consumed.
_CSI_KEYS: dict[str, Key] = {
    "Z":  Key.SHIFT_TAB,   # \x1b[Z  — the primary fix; fixes mode cycling on ConPTY
    "A":  Key.UP,           # \x1b[A
    "B":  Key.DOWN,         # \x1b[B
    "C":  Key.RIGHT,        # \x1b[C
    "D":  Key.LEFT,         # \x1b[D
    "H":  Key.HOME,         # \x1b[H
    "F":  Key.END,          # \x1b[F
    "1~": Key.HOME,         # \x1b[1~
    "4~": Key.END,          # \x1b[4~
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
        if ch == "\x1b":
            # ConPTY (Windows Terminal, VS Code, new PowerShell) delivers VT
            # sequences such as \x1b[Z for Shift+Tab rather than BIOS scan
            # codes.  Poll kbhit() — the Windows equivalent of select() for
            # console handles — to distinguish a lone ESC from a CSI sequence.
            if not msvcrt.kbhit():
                return (Key.ESC, "")
            nxt = msvcrt.getwch()
            if nxt != "[":
                # e.g. \x1b\x1b (alt+esc) or unknown Alt sequence
                return (Key.ESC, "")
            # Accumulate the CSI parameter + final byte until a letter or ~.
            seq = ""
            while msvcrt.kbhit():
                c = msvcrt.getwch()
                seq += c
                if c.isalpha() or c == "~":
                    break
            return (_CSI_KEYS.get(seq, Key.ESC), "")

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
        """Flush stdout so the initial Live block is immediately visible.

        On POSIX, ``cbreak_reader.raw_mode()`` writes ANSI escape codes and
        calls ``sys.stdout.flush()`` before yielding.  That flush has a critical
        side effect: it drains whatever Rich had queued in the OS stdout buffer
        from ``live.start()``, making the input bar (``❯ ▌``) immediately
        visible on screen.

        Without this flush, the Windows backend's no-op ``enter_raw_mode()``
        leaves Rich's rendered content sitting in the OS output buffer until the
        first keypress — the input bar is present but invisible.

        ConPTY environments (Windows Terminal, VS Code, new PowerShell) honour
        the VT sequences; legacy CMD/PowerShell ignores them harmlessly.
        """
        sys.stdout.write(
            "\x1b[?25l"    # hide OS cursor (matches POSIX raw_mode behaviour)
            "\x1b[?2004h"  # enable bracketed paste
        )
        sys.stdout.flush()  # ← flushes Rich's buffered Live block to the terminal
        try:
            yield
        finally:
            try:
                sys.stdout.write(
                    "\x1b[m"       # reset SGR attributes
                    "\x1b[?2004l"  # disable bracketed paste
                    "\x1b[?25h"    # restore cursor visibility
                )
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass

    def restore(self) -> None:
        """Best-effort terminal restore (cursor + paste mode)."""
        try:
            sys.stdout.write("\x1b[m\x1b[?2004l\x1b[?25h")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
