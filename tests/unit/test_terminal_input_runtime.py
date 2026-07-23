"""Terminal input edge paths that are safe to exercise on Linux CI."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from agenthicc.tui.cbreak_reader import Key, read_key
from agenthicc.tui.terminal import backend
from agenthicc.tui.terminal.posix_backend import PosixBackend
from agenthicc.tui.terminal.windows_backend import WindowsBackend

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "byte, expected",
    [
        (b"\x03", Key.CTRL_C),
        (b"\x04", Key.CTRL_D),
        (b"\r", Key.ENTER),
        (b"\n", Key.CTRL_ENTER),
        (b"\t", Key.TAB),
        (b"\x7f", Key.BACKSPACE),
        (b"\x15", Key.CTRL_U),
        (b"\x16", Key.CTRL_V),
        (b"@", Key.AT),
    ],
)
def test_read_key_control_bytes(
    monkeypatch: pytest.MonkeyPatch, byte: bytes, expected: Key
) -> None:
    monkeypatch.setattr("os.read", lambda _fd, _size: byte)
    assert read_key(0) == (expected, "")


@pytest.mark.parametrize(
    "sequence, expected",
    [
        (b"A", Key.UP),
        (b"B", Key.DOWN),
        (b"C", Key.RIGHT),
        (b"D", Key.LEFT),
        (b"H", Key.HOME),
        (b"F", Key.END),
        (b"Z", Key.SHIFT_TAB),
        (b"1~", Key.HOME),
        (b"3~", Key.CHAR),
        (b"4~", Key.END),
        (b"13u", Key.ENTER),
    ],
)
def test_read_key_escape_sequences(
    monkeypatch: pytest.MonkeyPatch, sequence: bytes, expected: Key
) -> None:
    values = iter([b"\x1b", b"[", *[bytes([c]) for c in sequence]])
    monkeypatch.setattr("os.read", lambda _fd, _size: next(values))
    monkeypatch.setattr("select.select", lambda *_args: ([0], [], []))
    assert read_key(0)[0] == expected


def test_read_key_escape_paste_and_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter([b"\x1b", b"[", b"2", b"0", b"0", b"~", b"hello\x1b[201~"])
    monkeypatch.setattr("os.read", lambda _fd, _size: next(values))
    monkeypatch.setattr("select.select", lambda *_args: ([0], [], []))
    assert read_key(0) == (Key.PASTE, "hello")

    values = iter([b"\xc3", b"\xa9"])
    monkeypatch.setattr("os.read", lambda _fd, _size: next(values))
    assert read_key(0) == (Key.CHAR, "é")

    monkeypatch.setattr("os.read", lambda _fd, _size: b"\xff")
    assert read_key(0)[0] is Key.ESC


def test_read_key_timeout_invalid_paste_and_long_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter([b"\x1b", b"[", b"A"])
    monkeypatch.setattr("os.read", lambda _fd, _size: next(values))
    monkeypatch.setattr("select.select", lambda *_args: ([], [], []))
    assert read_key(0) == (Key.ESC, "")

    values = iter([b"\x1b", b"[", b"2", b"0", b"0", b"~"])
    monkeypatch.setattr("os.read", lambda _fd, _size: next(values))
    select_calls = iter([([0], [], [])] * 5 + [([], [], [])])
    monkeypatch.setattr("select.select", lambda *_args: next(select_calls))
    assert read_key(0) == (Key.PASTE, "")

    values = iter([b"\x1b", b"[", b"2", b"0", b"0", b"~", b"\xff\x1b[201~"])
    monkeypatch.setattr("os.read", lambda _fd, _size: next(values))
    monkeypatch.setattr("select.select", lambda *_args: ([0], [], []))
    assert read_key(0) == (Key.PASTE, "�")

    values = iter([b"\xf0", b"\x9f", b"\x98", b"\x80"])
    monkeypatch.setattr("os.read", lambda _fd, _size: next(values))
    assert read_key(0) == (Key.CHAR, "😀")
    monkeypatch.setattr("os.read", lambda _fd, _size: b"\x01")
    assert read_key(0)[0] is Key.ESC


def test_raw_mode_applies_and_restores_cbreak_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    termios = types.ModuleType("termios")
    termios.ECHO = 0x08
    termios.ICANON = 0x02
    termios.TCSANOW = 0
    termios.TCSAFLUSH = 1
    termios.ICRNL = 0x100
    termios.ECHOCTL = 0x200
    termios.ISIG = 0x400
    attrs = [[0, 0, 0, termios.ECHO | termios.ICANON], [0, 0, 0, termios.ECHO | termios.ICANON]]
    set_calls: list[tuple[int, int, list[int]]] = []

    def tcgetattr(_fd: int) -> list[int]:
        return list(attrs.pop(0)) if attrs else [0, 0, 0, termios.ECHO]

    def tcsetattr(fd: int, action: int, value: list[int]) -> None:
        set_calls.append((fd, action, value))

    termios.tcgetattr = tcgetattr  # type: ignore[attr-defined]
    termios.tcsetattr = tcsetattr  # type: ignore[attr-defined]
    tty = types.ModuleType("tty")
    tty.setcbreak = lambda _fd: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "termios", termios)
    monkeypatch.setitem(sys.modules, "tty", tty)
    from agenthicc.tui.cbreak_reader import raw_mode

    with raw_mode(4) as fd:
        assert fd == 4
    assert len(set_calls) >= 2

    failing = types.ModuleType("termios")
    failing.tcgetattr = lambda _fd: (_ for _ in ()).throw(OSError("pipe"))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "termios", failing)
    with raw_mode(4) as fd:
        assert fd == 4


def test_posix_backend_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    posix = PosixBackend()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert posix.is_interactive() is False
    with pytest.raises(OSError):
        posix.read_key()
    with posix.enter_raw_mode():
        pass
    posix.restore()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(posix, "_resolve_fd", lambda: 4)
    monkeypatch.setattr(
        "agenthicc.tui.terminal.posix_backend._read_key", lambda fd: (Key.CHAR, "x")
    )
    assert posix.is_interactive() is True
    assert posix.read_key() == (Key.CHAR, "x")


def test_backend_factory_and_posix_fd_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.name", "posix")
    assert isinstance(backend.get_backend(), PosixBackend)
    posix = PosixBackend()
    monkeypatch.setattr("sys.stdin.fileno", lambda: (_ for _ in ()).throw(OSError("pipe")))
    assert posix._resolve_fd() is None


def _fake_msvcrt(monkeypatch: pytest.MonkeyPatch, values: list[str]) -> WindowsBackend:
    it = iter(values)
    module = types.ModuleType("msvcrt")
    module.getwch = lambda: next(it)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "msvcrt", module)
    return WindowsBackend()


@pytest.mark.parametrize(
    "values, expected",
    [
        (["\x03"], Key.CTRL_C),
        (["\x04"], Key.CTRL_D),
        (["\r"], Key.ENTER),
        (["\n"], Key.CTRL_ENTER),
        (["\t"], Key.TAB),
        (["\x7f"], Key.BACKSPACE),
        (["\x15"], Key.CTRL_U),
        (["\x16"], Key.CTRL_V),
        (["@"], Key.AT),
        (["\x1b"], Key.ESC),
        (["x"], Key.CHAR),
        (["\xe0", "H"], Key.UP),
        (["\x00", "\x0f"], Key.SHIFT_TAB),
        (["\xe0", "?"], Key.ESC),
    ],
)
def test_windows_getwch_fallback(
    monkeypatch: pytest.MonkeyPatch, values: list[str], expected: Key
) -> None:
    assert _fake_msvcrt(monkeypatch, values)._read_key_getwch()[0] is expected


def test_windows_console_and_raw_mode_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    win = WindowsBackend()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert win.is_interactive() is True
    monkeypatch.setattr("sys.stdin.isatty", lambda: (_ for _ in ()).throw(OSError("no stdin")))
    assert win.is_interactive() is False

    class Kernel:
        def __init__(self) -> None:
            self.mode = 0xFF
            self.set_modes: list[int] = []

        def GetStdHandle(self, _handle: int) -> int:
            return 7

        def GetConsoleMode(self, _handle: int, pointer: object) -> int:
            pointer._obj.value = self.mode  # type: ignore[attr-defined]
            return 1

        def SetConsoleMode(self, _handle: int, mode: int) -> int:
            self.set_modes.append(mode)
            return 1

        def ReadConsoleInputW(
            self, _handle: int, pointer: object, _count: int, read: object
        ) -> int:
            record = pointer._obj  # type: ignore[attr-defined]
            record.EventType = 1
            record.Event.KeyEvent.bKeyDown = 1
            record.Event.KeyEvent.wVirtualKeyCode = 9
            record.Event.KeyEvent.UnicodeChar = "\0"
            record.Event.KeyEvent.dwControlKeyState = 255
            read._obj.value = 1  # type: ignore[attr-defined]
            return 1

    kernel = Kernel()
    monkeypatch.setattr(
        "agenthicc.tui.terminal.windows_backend.ctypes.windll",
        SimpleNamespace(kernel32=kernel),
        raising=False,
    )
    assert win._console_handle() == 7
    assert win._next_input_event() == (1, True, 9, "\0", 255)
    assert win._read_key_console() == (Key.SHIFT_TAB, "")
    win._set_raw_input_mode()
    assert win._orig_mode == 255
    with win.enter_raw_mode():
        pass
    assert win._orig_mode is None
    win.restore()
