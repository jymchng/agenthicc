"""Windows terminal backend — ReadConsoleInputW via ctypes (PRD-106, PRD-127).

All Windows-specific terminal I/O is confined to this module.  No other file in
the application may import ``msvcrt`` or touch the console API directly.

Why ReadConsoleInputW instead of msvcrt.getwch (PRD-127)
--------------------------------------------------------
``msvcrt.getwch()`` reads *translated* console key events (the legacy two-byte
``\\x00`` / ``\\xe0`` + scan-code encoding).  It cannot report the SHIFT
modifier for Tab, so Shift+Tab collapses to plain Tab — mode cycling never
fires.  It also never sees VT input sequences (``\\x1b[Z``), because those only
arrive when ``ENABLE_VIRTUAL_TERMINAL_INPUT`` is set *and* raw bytes are read
via ``ReadConsole``/``ReadFile`` — which ``getwch()`` does not do.

``ReadConsoleInputW`` returns ``KEY_EVENT_RECORD`` structures carrying both
``wVirtualKeyCode`` and ``dwControlKeyState``.  Shift+Tab is then unambiguous:
``VK_TAB`` with ``SHIFT_PRESSED`` set.  This works uniformly on legacy CMD,
ConPTY, Windows Terminal, and the VS Code integrated terminal — no dependency
on the host's VT translation.

The decode logic (:func:`_decode_key_event`) is a pure function over
``(virtual_key, unicode_char, control_state)`` and is unit-tested on Linux.
Only :meth:`WindowsBackend._next_input_event` touches the Windows console API.
A ``getwch()`` fallback is retained for environments without a real console.
"""

from __future__ import annotations

import ctypes
import sys
from contextlib import contextmanager
from typing import Generator

from agenthicc.tui.cbreak_reader import Key

__all__ = ["WindowsBackend"]

# ── Windows console constants ─────────────────────────────────────────────────

_STD_INPUT_HANDLE = -10

_KEY_EVENT = 0x0001

# dwControlKeyState bits
_SHIFT_PRESSED = 0x0010
_LEFT_CTRL_PRESSED = 0x0008
_RIGHT_CTRL_PRESSED = 0x0004

# Console input mode bits (cleared for raw single-key reads)
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004

# Virtual key codes
_VK_BACK = 0x08
_VK_TAB = 0x09
_VK_RETURN = 0x0D
_VK_ESCAPE = 0x1B
_VK_END = 0x23
_VK_HOME = 0x24
_VK_LEFT = 0x25
_VK_UP = 0x26
_VK_RIGHT = 0x27
_VK_DOWN = 0x28
_VK_DELETE = 0x2E

_VK_KEYS: dict[int, Key] = {
    _VK_BACK: Key.BACKSPACE,
    _VK_ESCAPE: Key.ESC,
    _VK_END: Key.END,
    _VK_HOME: Key.HOME,
    _VK_LEFT: Key.LEFT,
    _VK_UP: Key.UP,
    _VK_RIGHT: Key.RIGHT,
    _VK_DOWN: Key.DOWN,
}


# ── ctypes structures (portable c_* types — importable on Linux) ──────────────
# NOTE: deliberately NOT ctypes.wintypes — that module only imports on Windows,
# and these structures must be importable on the Linux CI for the decode tests.


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", ctypes.c_int),  # BOOL
        ("wRepeatCount", ctypes.c_ushort),  # WORD
        ("wVirtualKeyCode", ctypes.c_ushort),  # WORD
        ("wVirtualScanCode", ctypes.c_ushort),  # WORD
        ("UnicodeChar", ctypes.c_wchar),  # WCHAR (uChar union, W variant)
        ("dwControlKeyState", ctypes.c_ulong),  # DWORD
    ]


class _INPUT_RECORD(ctypes.Structure):
    class _EventUnion(ctypes.Union):
        # The real union also holds MOUSE/WINDOW_BUFFER_SIZE/MENU/FOCUS records.
        # KEY_EVENT_RECORD is the largest we use; _pad guarantees the union is
        # large enough that ReadConsoleInputW never writes past the buffer.
        _fields_ = [
            ("KeyEvent", _KEY_EVENT_RECORD),
            ("_pad", ctypes.c_byte * 20),
        ]

    _fields_ = [("EventType", ctypes.c_ushort), ("Event", _EventUnion)]


# ── Pure decoder (no Windows API — fully testable on Linux) ───────────────────


def _decode_key_event(
    vk: int,
    unicode_char: str,
    ctrl_state: int,
) -> tuple[Key, str] | None:
    """Decode one console key-down event into a ``(Key, ch)`` pair.

    Virtual-key checks come first so modifier combinations (Shift+Tab,
    Ctrl+Enter) are caught before the ``UnicodeChar`` fallback.

    :param vk: ``wVirtualKeyCode`` from the key event.
    :param unicode_char: ``UnicodeChar`` from the key event ("" for none).
    :param ctrl_state: ``dwControlKeyState`` bitmask.
    :return: A ``(Key, ch)`` tuple, or ``None`` for modifier-only / unmapped
        keys the caller should skip.
    """
    shift = bool(ctrl_state & _SHIFT_PRESSED)
    ctrl = bool(ctrl_state & (_LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED))

    # ── virtual-key codes (carry the modifier state) ──────────────────────────
    if vk == _VK_TAB:
        return (Key.SHIFT_TAB, "") if shift else (Key.TAB, "")
    if vk == _VK_RETURN:
        return (Key.CTRL_ENTER, "") if ctrl else (Key.ENTER, "")
    mapped = _VK_KEYS.get(vk)
    if mapped is not None:
        return (mapped, "")

    # ── UnicodeChar (control characters + printable) ──────────────────────────
    if unicode_char:
        cp = ord(unicode_char)
        if cp == 0x03:
            return (Key.CTRL_C, "")
        if cp == 0x04:
            return (Key.CTRL_D, "")
        if cp == 0x15:
            return (Key.CTRL_U, "")
        if cp == 0x16:
            return (Key.CTRL_V, "")
        if cp in (0x08, 0x7F):
            return (Key.BACKSPACE, "")
        if cp == 0x0D:
            return (Key.ENTER, "")
        if cp == 0x0A:
            return (Key.CTRL_ENTER, "")
        if cp == 0x09:
            return (Key.TAB, "")
        if unicode_char == "@":
            return (Key.AT, "")
        if unicode_char.isprintable():
            return (Key.CHAR, unicode_char)

    return None


# ── getwch fallback decode tables (no real console available) ─────────────────

_EXT_E0: dict[str, Key] = {
    "H": Key.UP,
    "P": Key.DOWN,
    "K": Key.LEFT,
    "M": Key.RIGHT,
    "G": Key.HOME,
    "O": Key.END,
}
_EXT_00: dict[str, Key] = {
    "\x0f": Key.SHIFT_TAB,
    "H": Key.UP,
    "P": Key.DOWN,
    "K": Key.LEFT,
    "M": Key.RIGHT,
    "G": Key.HOME,
    "O": Key.END,
}


class WindowsBackend:
    """Windows terminal backend using ``ReadConsoleInputW`` (PRD-127).

    Reads console key events directly so modifier state (notably Shift on Tab)
    is preserved.  Falls back to ``msvcrt.getwch()`` when no console handle is
    available (e.g. redirected stdin).
    """

    def __init__(self) -> None:
        self._orig_mode: int | None = None  # saved console input mode for restore

    # ── TerminalBackend interface ─────────────────────────────────────────────

    def is_interactive(self) -> bool:
        """True when running inside an interactive Windows console."""
        try:
            return sys.stdin.isatty()
        except Exception:  # noqa: BLE001
            return False

    def read_key(self) -> tuple[Key, str]:
        """Read one keystroke; blocks until a decodable key is available."""
        if self._console_handle() is not None:
            return self._read_key_console()
        return self._read_key_getwch()

    # ── console-API path (primary) ────────────────────────────────────────────

    def _console_handle(self) -> int | None:
        """Return the stdin console handle, or ``None`` if not a real console."""
        try:
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return None  # not Windows / no kernel32
        handle = k32.GetStdHandle(_STD_INPUT_HANDLE)
        if handle in (0, None) or handle == ctypes.c_void_p(-1).value:
            return None
        mode = ctypes.c_ulong()
        # GetConsoleMode fails for non-console handles (redirected stdin) → no
        # console-input path available.
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        return handle

    def _next_input_event(self) -> tuple[int, bool, int, str, int] | None:
        """Read one console input record.

        :return: ``(event_type, key_down, vk, unicode_char, ctrl_state)`` or
            ``None`` when the read fails.  Isolated so tests can drive
            :meth:`_read_key_console` without the Windows API.
        """
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.GetStdHandle(_STD_INPUT_HANDLE)
        record = _INPUT_RECORD()
        read = ctypes.c_ulong(0)
        ok = k32.ReadConsoleInputW(handle, ctypes.byref(record), 1, ctypes.byref(read))
        if not ok or read.value == 0:
            return None
        ke = record.Event.KeyEvent
        return (
            int(record.EventType),
            bool(ke.bKeyDown),
            int(ke.wVirtualKeyCode),
            ke.UnicodeChar or "",
            int(ke.dwControlKeyState),
        )

    def _read_key_console(self) -> tuple[Key, str]:
        """Loop over console input records until one decodes to a key."""
        while True:
            ev = self._next_input_event()
            if ev is None:
                continue
            event_type, key_down, vk, unicode_char, ctrl_state = ev
            # Only key-down KEY_EVENTs carry keystrokes; skip mouse / focus /
            # buffer-resize records and key-up events.
            if event_type != _KEY_EVENT or not key_down:
                continue
            decoded = _decode_key_event(vk, unicode_char, ctrl_state)
            if decoded is not None:
                return decoded

    # ── getwch fallback path (no real console) ────────────────────────────────

    def _read_key_getwch(self) -> tuple[Key, str]:
        """Legacy ``msvcrt.getwch()`` decode for non-console environments."""
        import msvcrt  # noqa: PLC0415

        ch = msvcrt.getwch()

        if ch == "\x03":
            return (Key.CTRL_C, "")
        if ch == "\x04":
            return (Key.CTRL_D, "")
        if ch == "\r":
            return (Key.ENTER, "")
        if ch == "\n":
            return (Key.CTRL_ENTER, "")
        if ch == "\t":
            return (Key.TAB, "")
        if ch in ("\x7f", "\x08"):
            return (Key.BACKSPACE, "")
        if ch == "\x15":
            return (Key.CTRL_U, "")
        if ch == "\x16":
            return (Key.CTRL_V, "")
        if ch == "@":
            return (Key.AT, "")
        if ch == "\xe0":
            ext = msvcrt.getwch()
            mapped = _EXT_E0.get(ext)
            return (mapped, "") if mapped is not None else (Key.ESC, "")
        if ch == "\x00":
            ext = msvcrt.getwch()
            mapped = _EXT_00.get(ext)
            return (mapped, "") if mapped is not None else (Key.ESC, "")
        if ch == "\x1b":
            return (Key.ESC, "")
        if ch.isprintable():
            return (Key.CHAR, ch)
        return (Key.ESC, "")

    # ── raw-mode lifecycle ────────────────────────────────────────────────────

    @contextmanager
    def enter_raw_mode(self) -> Generator[None, None, None]:
        """Configure the console for raw single-key reads and flush the UI.

        Clears ``ENABLE_LINE_INPUT``, ``ENABLE_ECHO_INPUT``, and
        ``ENABLE_PROCESSED_INPUT`` on the console input handle so keystrokes
        (including Ctrl+C) arrive as individual key events — mirroring the POSIX
        backend clearing ``ICANON`` / ``ECHO`` / ``ISIG``.  The original mode is
        restored on exit.

        The stdout flush drains Rich's buffered Live block so the input bar
        (``❯ ▌``) is immediately visible (without it the bar sits invisible in
        the OS buffer until the first keypress).
        """
        self._set_raw_input_mode()
        sys.stdout.write(
            "\x1b[?25l"  # hide OS cursor (matches POSIX raw_mode)
            "\x1b[?2004h"  # enable bracketed paste (harmless on legacy CMD)
        )
        sys.stdout.flush()
        try:
            yield
        finally:
            self._restore_input_mode()
            try:
                sys.stdout.write("\x1b[m\x1b[?2004l\x1b[?25h")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass

    def _set_raw_input_mode(self) -> None:
        """Save and clear cooked-input flags on the console input handle."""
        try:
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return
        handle = k32.GetStdHandle(_STD_INPUT_HANDLE)
        mode = ctypes.c_ulong()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        self._orig_mode = mode.value
        raw = mode.value & ~(_ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT | _ENABLE_PROCESSED_INPUT)
        k32.SetConsoleMode(handle, raw)

    def _restore_input_mode(self) -> None:
        """Restore the saved console input mode, if one was captured."""
        if self._orig_mode is None:
            return
        try:
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = k32.GetStdHandle(_STD_INPUT_HANDLE)
            k32.SetConsoleMode(handle, self._orig_mode)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._orig_mode = None

    def restore(self) -> None:
        """Best-effort terminal restore (input mode + cursor + paste)."""
        self._restore_input_mode()
        try:
            sys.stdout.write("\x1b[m\x1b[?2004l\x1b[?25h")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
