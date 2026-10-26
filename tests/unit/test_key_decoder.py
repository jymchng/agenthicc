"""Every key the AGX PRDs need decodes the same on POSIX and Windows (AGX-13 §5.3)."""

from __future__ import annotations

import os

import pytest

from agenthicc.tui.cbreak_reader import Key, MouseEvent, key_name, read_key
from agenthicc.tui.terminal import windows_backend as win

pytestmark = pytest.mark.unit


def _posix(data: bytes) -> tuple[Key, str]:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, data)
        return read_key(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


# ── POSIX byte sequences ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "name"),
    [
        # Ctrl+A … Ctrl+Z (Ctrl+H/I/J/M keep Backspace, Tab, newline, Enter)
        *(
            (bytes([code]), f"ctrl+{chr(code + 96)}")
            for code in range(1, 27)
            if code not in (8, 9, 10, 13)
        ),
        (b"\x08", "backspace"),
        (b"\x09", "tab"),
        (b"\x0a", "ctrl+j"),
        (b"\x0d", "enter"),
        # Alt+key: ESC prefix
        (b"\x1bx", "alt+x"),
        (b"\x1bX", "alt+shift+x"),
        (b"\x1b\x7f", "alt+backspace"),
        (b"\x1b\r", "alt+enter"),
        # Modified arrows, Home, End
        (b"\x1b[1;2A", "shift+up"),
        (b"\x1b[1;3B", "alt+down"),
        (b"\x1b[1;5C", "ctrl+right"),
        (b"\x1b[1;6D", "ctrl+shift+left"),
        (b"\x1b[1;2H", "shift+home"),
        (b"\x1b[1;5F", "ctrl+end"),
        # Delete, Insert, function keys
        (b"\x1b[3~", "delete"),
        (b"\x1b[2~", "insert"),
        (b"\x1b[3;5~", "ctrl+delete"),
        (b"\x1bOP", "f1"),
        (b"\x1bOS", "f4"),
        (b"\x1b[15~", "f5"),
        (b"\x1b[21~", "f10"),
        (b"\x1b[24~", "f12"),
        (b"\x1b[1;5P", "ctrl+f1"),
        (b"\x1b[24;2~", "shift+f12"),
        # Focus reports
        (b"\x1b[I", "focus_in"),
        (b"\x1b[O", "focus_out"),
    ],
)
def test_posix_sequences_decode_to_named_keys(data: bytes, name: str) -> None:
    key, ch = _posix(data)
    assert key_name(key, ch) == name


@pytest.mark.parametrize(
    ("data", "button", "column", "row", "pressed", "wheel"),
    [
        (b"\x1b[<0;12;5M", 0, 12, 5, True, 0),
        (b"\x1b[<0;12;5m", 0, 12, 5, False, 0),
        (b"\x1b[<2;1;1M", 2, 1, 1, True, 0),
        (b"\x1b[<64;3;9M", 64, 3, 9, True, -1),
        (b"\x1b[<65;3;9M", 65, 3, 9, True, 1),
    ],
)
def test_posix_sgr_mouse_reports(
    data: bytes, button: int, column: int, row: int, pressed: bool, wheel: int
) -> None:
    key, ch = _posix(data)
    assert key is Key.MOUSE
    event = MouseEvent.decode(ch)
    assert event is not None
    assert (event.button, event.column, event.row, event.pressed, event.wheel) == (
        button,
        column,
        row,
        pressed,
        wheel,
    )


@pytest.mark.parametrize("data", [b"\x1b[99~", b"\x1b[1;5Q", b"\x1b[<1;2M", b"\xff"])
def test_unknown_input_is_never_an_escape_press(data: bytes) -> None:
    key, _ch = _posix(data)
    assert key is not Key.ESC
    if data != b"\x1b[1;5Q":
        assert key is Key.UNKNOWN


# ── Windows key records ──────────────────────────────────────────────────────

_CTRL = win._LEFT_CTRL_PRESSED
_ALT = win._LEFT_ALT_PRESSED
_SHIFT = win._SHIFT_PRESSED


@pytest.mark.parametrize(
    ("vk", "char", "state", "name"),
    [
        (0x47, "\x07", _CTRL, "ctrl+g"),
        (0x4F, "\x0f", _CTRL, "ctrl+o"),
        (0x52, "", _CTRL, "ctrl+r"),  # no control character reported
        (0x48, "\x08", _CTRL, "backspace"),
        (0x58, "x", _ALT, "alt+x"),
        (0x58, "X", _ALT | _SHIFT, "alt+shift+x"),
        (win._VK_UP, "", _SHIFT, "shift+up"),
        (win._VK_LEFT, "", _CTRL | _SHIFT, "ctrl+shift+left"),
        (win._VK_HOME, "", _ALT, "alt+home"),
        (win._VK_DELETE, "", 0, "delete"),
        (win._VK_INSERT, "", 0, "insert"),
        (win._VK_DELETE, "", _CTRL, "ctrl+delete"),
        (win._VK_F1, "", 0, "f1"),
        (win._VK_F12, "", 0, "f12"),
        (win._VK_F1 + 4, "", _CTRL, "ctrl+f5"),
        (win._VK_TAB, "\t", _SHIFT, "shift+tab"),
        (win._VK_RETURN, "\r", _ALT, "alt+enter"),
        (0x20, "\x00", _CTRL, "ctrl+space"),
    ],
)
def test_windows_key_records_decode_like_posix(vk: int, char: str, state: int, name: str) -> None:
    decoded = win._decode_key_event(vk, char, state)
    assert decoded is not None
    assert key_name(*decoded) == name


def test_altgr_text_is_typed_not_a_shortcut() -> None:
    # AltGr arrives as Ctrl+Alt with a printable character.
    assert win._decode_key_event(0x51, "@", _CTRL | _ALT) == (Key.AT, "")
    assert win._decode_key_event(0x45, "€", _CTRL | _ALT) == (Key.CHAR, "€")


def test_windows_focus_records() -> None:
    backend = win.WindowsBackend()
    events = iter(
        [
            win._ConsoleEvent(win._FOCUS_EVENT, focused=True),
            win._ConsoleEvent(win._FOCUS_EVENT, focused=False),
        ]
    )
    backend._next_input_event = lambda: next(events)  # type: ignore[method-assign]
    assert backend._read_key_console() == (Key.FOCUS_IN, "")
    assert backend._read_key_console() == (Key.FOCUS_OUT, "")


def test_windows_mouse_records_follow_reporting() -> None:
    backend = win.WindowsBackend()
    events = iter(
        [
            win._ConsoleEvent(win._MOUSE_EVENT, column=4, row=2, buttons=1),
            win._ConsoleEvent(win._MOUSE_EVENT, column=4, row=2, buttons=1, flags=win._MOUSE_MOVED),
            win._ConsoleEvent(win._MOUSE_EVENT, column=4, row=2, buttons=0),
            win._ConsoleEvent(
                win._MOUSE_EVENT, column=0, row=0, buttons=0xFF880000, flags=win._MOUSE_WHEELED
            ),
            win._ConsoleEvent(win._KEY_EVENT, key_down=True, vk=0x41, char="a"),
        ]
    )
    backend._next_input_event = lambda: next(events)  # type: ignore[method-assign]
    backend.set_reporting(mouse=True)
    press = MouseEvent.decode(backend._read_key_console()[1])
    release = MouseEvent.decode(backend._read_key_console()[1])
    wheel = MouseEvent.decode(backend._read_key_console()[1])
    assert press is not None and (press.button, press.column, press.row, press.pressed) == (
        0,
        5,
        3,
        True,
    )
    assert release is not None and (release.button, release.pressed) == (0, False)
    assert wheel is not None and wheel.wheel == 1  # a negative delta scrolls down

    # Without mouse reporting, mouse records are skipped.
    quiet = win.WindowsBackend()
    records = iter(
        [
            win._ConsoleEvent(win._MOUSE_EVENT, column=1, row=1, buttons=1),
            win._ConsoleEvent(win._KEY_EVENT, key_down=True, vk=0x41, char="a"),
        ]
    )
    quiet._next_input_event = lambda: next(records)  # type: ignore[method-assign]
    assert quiet._read_key_console() == (Key.CHAR, "a")


def test_windows_raw_mode_takes_the_mouse_only_when_reporting() -> None:
    backend = win.WindowsBackend()
    base = win._ENABLE_QUICK_EDIT_MODE | win._ENABLE_LINE_INPUT
    assert backend._raw_mode_flags(base) & win._ENABLE_MOUSE_INPUT == 0
    assert backend._raw_mode_flags(base) & win._ENABLE_QUICK_EDIT_MODE
    backend.set_reporting(mouse=True)
    flags = backend._raw_mode_flags(base)
    assert flags & win._ENABLE_MOUSE_INPUT and flags & win._ENABLE_EXTENDED_FLAGS
    assert not flags & win._ENABLE_QUICK_EDIT_MODE
