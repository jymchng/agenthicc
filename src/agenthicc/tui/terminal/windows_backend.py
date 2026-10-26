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
import importlib
import threading
from types import ModuleType
from typing import cast
import sys
from contextlib import contextmanager
from typing import Generator

from typing import NamedTuple

from agenthicc.tui.cbreak_reader import Key, MouseEvent, decode_control


def _kernel32() -> ctypes.CDLL:
    """The Windows kernel32 library; ``AttributeError`` where there is none.

    ``ctypes.windll`` exists only on Windows, so it is reached through the
    module object rather than a platform-specific attribute.
    """
    module: ModuleType = ctypes
    return cast(ctypes.CDLL, module.windll.kernel32)


def _getwch() -> str:
    """One character from ``msvcrt.getwch`` (the legacy console fallback)."""
    module = importlib.import_module("msvcrt")
    return cast(str, module.getwch())


__all__ = ["WindowsBackend"]

# ── Windows console constants ─────────────────────────────────────────────────

_STD_INPUT_HANDLE = -10

_KEY_EVENT = 0x0001
_MOUSE_EVENT = 0x0002
_FOCUS_EVENT = 0x0010

# dwControlKeyState bits
_RIGHT_ALT_PRESSED = 0x0001
_LEFT_ALT_PRESSED = 0x0002
_RIGHT_CTRL_PRESSED = 0x0004
_LEFT_CTRL_PRESSED = 0x0008
_SHIFT_PRESSED = 0x0010

# Console input mode bits (cleared for raw single-key reads)
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_MOUSE_INPUT = 0x0010
_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080

# MOUSE_EVENT_RECORD button state and event flags
_FROM_LEFT_1ST_BUTTON = 0x0001
_RIGHTMOST_BUTTON = 0x0002
_FROM_LEFT_2ND_BUTTON = 0x0004
_MOUSE_MOVED = 0x0001
_MOUSE_WHEELED = 0x0004

# WaitForSingleObject results and the short polling interval used to make a
# ReadConsoleInputW worker cancellable when the asyncio task is interrupted.
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_INPUT_POLL_TIMEOUT_MS = 50

# Virtual key codes
_VK_BACK = 0x08
_VK_TAB = 0x09
_VK_RETURN = 0x0D
_VK_ESCAPE = 0x1B
_VK_END = 0x23
_VK_HOME = 0x24
_VK_PRIOR = 0x21
_VK_NEXT = 0x22
_VK_LEFT = 0x25
_VK_UP = 0x26
_VK_RIGHT = 0x27
_VK_DOWN = 0x28
_VK_INSERT = 0x2D
_VK_DELETE = 0x2E
_VK_A = 0x41
_VK_Z = 0x5A
_VK_F1 = 0x70
_VK_F12 = 0x7B

# Keys that keep their meaning under a modifier: name used for MODIFIED keys.
_VK_KEYS: dict[int, tuple[Key, str]] = {
    _VK_BACK: (Key.BACKSPACE, "backspace"),
    _VK_ESCAPE: (Key.ESC, "escape"),
    _VK_END: (Key.END, "end"),
    _VK_HOME: (Key.HOME, "home"),
    _VK_PRIOR: (Key.PAGE_UP, "pageup"),
    _VK_NEXT: (Key.PAGE_DOWN, "pagedown"),
    _VK_LEFT: (Key.LEFT, "left"),
    _VK_UP: (Key.UP, "up"),
    _VK_RIGHT: (Key.RIGHT, "right"),
    _VK_DOWN: (Key.DOWN, "down"),
    _VK_INSERT: (Key.INSERT, "insert"),
    _VK_DELETE: (Key.DELETE, "delete"),
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


class _MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition", _COORD),
        ("dwButtonState", ctypes.c_ulong),  # DWORD; high word: wheel delta
        ("dwControlKeyState", ctypes.c_ulong),  # DWORD
        ("dwEventFlags", ctypes.c_ulong),  # DWORD
    ]


class _FOCUS_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bSetFocus", ctypes.c_int)]  # BOOL


class _INPUT_RECORD(ctypes.Structure):
    class _EventUnion(ctypes.Union):
        # The real union also holds WINDOW_BUFFER_SIZE/MENU records. _pad
        # guarantees the union is large enough that ReadConsoleInputW never
        # writes past the buffer.
        _fields_ = [
            ("KeyEvent", _KEY_EVENT_RECORD),
            ("MouseEvent", _MOUSE_EVENT_RECORD),
            ("FocusEvent", _FOCUS_EVENT_RECORD),
            ("_pad", ctypes.c_byte * 20),
        ]

    _fields_ = [("EventType", ctypes.c_ushort), ("Event", _EventUnion)]


# ── Pure decoder (no Windows API — fully testable on Linux) ───────────────────


class _ConsoleEvent(NamedTuple):
    """The fields of one console input record that the decoders use."""

    event_type: int
    key_down: bool = False
    vk: int = 0
    char: str = ""
    ctrl_state: int = 0
    column: int = 0
    row: int = 0
    buttons: int = 0
    flags: int = 0
    focused: bool = False


def _modifier_prefix(ctrl_state: int) -> str:
    ctrl = bool(ctrl_state & (_LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED))
    alt = bool(ctrl_state & (_LEFT_ALT_PRESSED | _RIGHT_ALT_PRESSED))
    shift = bool(ctrl_state & _SHIFT_PRESSED)
    return ("ctrl+" if ctrl else "") + ("alt+" if alt else "") + ("shift+" if shift else "")


def _decode_key_event(
    vk: int,
    unicode_char: str,
    ctrl_state: int,
) -> tuple[Key, str] | None:
    """Decode one console key-down event into a ``(Key, ch)`` pair.

    Virtual-key checks come first so modifier combinations (Shift+Tab,
    Ctrl+Enter, Ctrl+Left) are caught before the ``UnicodeChar`` fallback. The
    results match the POSIX decoder's (AGX-13).

    :param vk: ``wVirtualKeyCode`` from the key event.
    :param unicode_char: ``UnicodeChar`` from the key event ("" for none).
    :param ctrl_state: ``dwControlKeyState`` bitmask.
    :return: A ``(Key, ch)`` tuple, or ``None`` for modifier-only / unmapped
        keys the caller should skip.
    """
    shift = bool(ctrl_state & _SHIFT_PRESSED)
    ctrl = bool(ctrl_state & (_LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED))
    alt = bool(ctrl_state & (_LEFT_ALT_PRESSED | _RIGHT_ALT_PRESSED))
    # AltGr arrives as Ctrl+Alt and types a printable character (for example
    # "@" on a German layout); it is text, not a shortcut.
    if ctrl and alt and unicode_char and unicode_char.isprintable():
        return (Key.AT, "") if unicode_char == "@" else (Key.CHAR, unicode_char)

    # ── virtual-key codes (carry the modifier state) ──────────────────────────
    if vk == _VK_TAB:
        if ctrl or alt:
            return (Key.MODIFIED, _modifier_prefix(ctrl_state) + "tab")
        return (Key.SHIFT_TAB, "") if shift else (Key.TAB, "")
    if vk == _VK_RETURN:
        if alt:
            return (Key.MODIFIED, _modifier_prefix(ctrl_state) + "enter")
        return (Key.CTRL_ENTER, "") if ctrl else (Key.ENTER, "")
    mapped = _VK_KEYS.get(vk)
    if mapped is not None:
        prefix = _modifier_prefix(ctrl_state)
        return (Key.MODIFIED, prefix + mapped[1]) if prefix else (mapped[0], "")
    if _VK_F1 <= vk <= _VK_F12:
        name = f"f{vk - _VK_F1 + 1}"
        prefix = _modifier_prefix(ctrl_state)
        return (Key.MODIFIED, prefix + name) if prefix else (Key.FUNCTION, name)
    if ctrl and _VK_A <= vk <= _VK_Z:
        if alt:
            return (Key.MODIFIED, f"ctrl+alt+{chr(vk).lower()}")
        # The control character is authoritative; the virtual key is the
        # fallback for consoles that report none.
        if unicode_char and 0 < ord(unicode_char) < 0x20:
            return decode_control(ord(unicode_char))
        return decode_control(vk - _VK_A + 1)

    # ── UnicodeChar (control characters + printable) ──────────────────────────
    if unicode_char:
        cp = ord(unicode_char)
        if cp == 0:
            return (Key.MODIFIED, "ctrl+space") if ctrl and vk == 0x20 else None
        if cp < 0x20 or cp == 0x7F:
            return decode_control(cp)
        if alt and unicode_char.isprintable():
            return (Key.ALT, unicode_char)
        if unicode_char == "@":
            return (Key.AT, "")
        if unicode_char.isprintable():
            return (Key.CHAR, unicode_char)

    return None


def _decode_mouse_event(
    column: int, row: int, buttons: int, flags: int, previous_button: int | None
) -> MouseEvent | None:
    """Decode a MOUSE_EVENT record as an SGR-style event (1-based position).

    *previous_button* is the button last reported pressed, so a release can
    name it; motion without a button change is not reported.
    """
    if flags & _MOUSE_WHEELED:
        delta = (buttons >> 16) & 0xFFFF
        delta = delta - 0x10000 if delta & 0x8000 else delta
        button = MouseEvent.WHEEL_UP if delta > 0 else MouseEvent.WHEEL_DOWN
        return MouseEvent(button, column + 1, row + 1, pressed=True)
    if flags & _MOUSE_MOVED:
        return None
    if buttons & _FROM_LEFT_1ST_BUTTON:
        return MouseEvent(0, column + 1, row + 1, pressed=True)
    if buttons & _FROM_LEFT_2ND_BUTTON:
        return MouseEvent(1, column + 1, row + 1, pressed=True)
    if buttons & _RIGHTMOST_BUTTON:
        return MouseEvent(2, column + 1, row + 1, pressed=True)
    if previous_button is None:
        return None
    return MouseEvent(previous_button, column + 1, row + 1, pressed=False)


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
        self._stop_requested = threading.Event()
        self._mouse_reporting = False
        self._mouse_button: int | None = None

    # ── TerminalBackend interface ─────────────────────────────────────────────

    def is_interactive(self) -> bool:
        """True when running inside an interactive Windows console."""
        try:
            return sys.stdin.isatty()
        except Exception:  # noqa: BLE001
            return False

    def read_key(self) -> tuple[Key, str]:
        """Read one keystroke; blocks until a decodable key is available."""
        if self._stop_requested.is_set():
            raise OSError("terminal input stopped")
        if self._console_handle() is not None:
            return self._read_key_console()
        return self._read_key_getwch()

    # ── console-API path (primary) ────────────────────────────────────────────

    def _console_handle(self) -> int | None:
        """Return the stdin console handle, or ``None`` if not a real console."""
        try:
            k32 = _kernel32()
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
        return int(handle)

    def _next_input_event(self) -> _ConsoleEvent | None:
        """Read one console input record.

        :return: the record's fields, or ``None`` when the read fails or times
            out. Isolated so tests can drive :meth:`_read_key_console` without
            the Windows API.
        """
        k32 = _kernel32()
        handle = k32.GetStdHandle(_STD_INPUT_HANDLE)
        try:
            wait = k32.WaitForSingleObject
        except AttributeError:
            wait = None
        if wait is not None:
            result = int(wait(handle, _INPUT_POLL_TIMEOUT_MS))
            if result == _WAIT_TIMEOUT:
                return None
            if result != _WAIT_OBJECT_0:
                return None
        record = _INPUT_RECORD()
        read = ctypes.c_ulong(0)
        ok = k32.ReadConsoleInputW(handle, ctypes.byref(record), 1, ctypes.byref(read))
        if not ok or read.value == 0:
            return None
        event_type = int(record.EventType)
        if event_type == _MOUSE_EVENT:
            me = record.Event.MouseEvent
            return _ConsoleEvent(
                event_type,
                column=int(me.dwMousePosition.X),
                row=int(me.dwMousePosition.Y),
                buttons=int(me.dwButtonState),
                ctrl_state=int(me.dwControlKeyState),
                flags=int(me.dwEventFlags),
            )
        if event_type == _FOCUS_EVENT:
            return _ConsoleEvent(event_type, focused=bool(record.Event.FocusEvent.bSetFocus))
        ke = record.Event.KeyEvent
        return _ConsoleEvent(
            event_type,
            key_down=bool(ke.bKeyDown),
            vk=int(ke.wVirtualKeyCode),
            char=ke.UnicodeChar or "",
            ctrl_state=int(ke.dwControlKeyState),
        )

    def _read_key_console(self) -> tuple[Key, str]:
        """Loop over console input records until one decodes to a key."""
        while not self._stop_requested.is_set():
            ev = self._next_input_event()
            if ev is None:
                continue
            if ev.event_type == _FOCUS_EVENT:
                return (Key.FOCUS_IN, "") if ev.focused else (Key.FOCUS_OUT, "")
            if ev.event_type == _MOUSE_EVENT:
                if not self._mouse_reporting:
                    continue
                mouse = _decode_mouse_event(
                    ev.column, ev.row, ev.buttons, ev.flags, self._mouse_button
                )
                if mouse is None:
                    continue
                if not mouse.wheel:
                    self._mouse_button = mouse.button if mouse.pressed else None
                return (Key.MOUSE, mouse.encode())
            # Only key-down KEY_EVENTs carry keystrokes; skip buffer-resize
            # and menu records and key-up events.
            if ev.event_type != _KEY_EVENT or not ev.key_down:
                continue
            decoded = _decode_key_event(ev.vk, ev.char, ev.ctrl_state)
            if decoded is not None:
                return decoded
        raise OSError("terminal input stopped")

    # ── getwch fallback path (no real console) ────────────────────────────────

    def _read_key_getwch(self) -> tuple[Key, str]:
        """Legacy ``msvcrt.getwch()`` decode for non-console environments."""
        ch = _getwch()

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
            ext = _getwch()
            mapped = _EXT_E0.get(ext)
            return (mapped, "") if mapped is not None else (Key.ESC, "")
        if ch == "\x00":
            ext = _getwch()
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
        self._stop_requested.clear()
        self._set_raw_input_mode()
        sys.stdout.write(
            "\x1b[?25l"  # hide OS cursor (matches POSIX raw_mode)
            "\x1b[?2004h"  # enable bracketed paste (harmless on legacy CMD)
        )
        sys.stdout.flush()
        try:
            yield
        finally:
            # A read may be running in the executor while the asyncio task is
            # being cancelled.  Signal it before restoring the console so the
            # polling loop exits instead of keeping asyncio.run() alive.
            self._stop_requested.set()
            self._restore_input_mode()
            try:
                sys.stdout.write("\x1b[m\x1b[?2004l\x1b[?25h")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass

    def _set_raw_input_mode(self) -> None:
        """Save and clear cooked-input flags on the console input handle."""
        try:
            k32 = _kernel32()
        except (AttributeError, OSError):
            return
        handle = k32.GetStdHandle(_STD_INPUT_HANDLE)
        mode = ctypes.c_ulong()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        self._orig_mode = mode.value
        k32.SetConsoleMode(handle, self._raw_mode_flags(mode.value))

    def _raw_mode_flags(self, mode: int) -> int:
        raw = mode & ~(_ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT | _ENABLE_PROCESSED_INPUT)
        if self._mouse_reporting:
            # Quick-edit mode would take the mouse for selection.
            return (raw | _ENABLE_MOUSE_INPUT | _ENABLE_EXTENDED_FLAGS) & ~_ENABLE_QUICK_EDIT_MODE
        return raw

    def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
        """Turn mouse input on or off (AGX-13); focus records always arrive."""
        del focus
        if mouse is None or mouse == self._mouse_reporting:
            return
        self._mouse_reporting = mouse
        self._mouse_button = None
        if self._orig_mode is None:
            return  # applied when raw mode starts
        try:
            k32 = _kernel32()
            handle = k32.GetStdHandle(_STD_INPUT_HANDLE)
            k32.SetConsoleMode(handle, self._raw_mode_flags(self._orig_mode))
        except (AttributeError, OSError):
            pass

    def _restore_input_mode(self) -> None:
        """Restore the saved console input mode, if one was captured."""
        if self._orig_mode is None:
            return
        try:
            k32 = _kernel32()
            handle = k32.GetStdHandle(_STD_INPUT_HANDLE)
            k32.SetConsoleMode(handle, self._orig_mode)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._orig_mode = None

    def close(self) -> None:
        """Nothing to release: console reads hold no descriptors of their own."""

    def restore(self) -> None:
        """Best-effort terminal restore (input mode + cursor + paste)."""
        self._stop_requested.set()
        self._restore_input_mode()
        try:
            sys.stdout.write("\x1b[m\x1b[?2004l\x1b[?25h")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
