"""Unit tests for the Windows terminal backend key decoding (PRD-127).

These run on any platform — the decoder is a pure function and the read loop is
driven through the monkeypatchable ``_next_input_event`` boundary, so no real
Windows console or ctypes call is exercised.
"""
from __future__ import annotations

import pytest

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.terminal.windows_backend import (
    WindowsBackend,
    _decode_key_event,
    _KEY_EVENT,
    _SHIFT_PRESSED,
    _LEFT_CTRL_PRESSED,
    _RIGHT_CTRL_PRESSED,
    _VK_TAB,
    _VK_RETURN,
    _VK_BACK,
    _VK_ESCAPE,
    _VK_UP,
    _VK_DOWN,
    _VK_LEFT,
    _VK_RIGHT,
    _VK_HOME,
    _VK_END,
)

pytestmark = pytest.mark.unit


# ── _decode_key_event — the core fix ──────────────────────────────────────────

class TestDecodeKeyEvent:
    def test_shift_tab(self) -> None:
        # The bug this PRD fixes: Shift+Tab must be distinct from Tab.
        assert _decode_key_event(_VK_TAB, "", _SHIFT_PRESSED) == (Key.SHIFT_TAB, "")

    def test_shift_tab_with_tab_unicode_still_shift(self) -> None:
        # Even if the console also reports UnicodeChar="\t", the VK+SHIFT check
        # (which runs first) wins.
        assert _decode_key_event(_VK_TAB, "\t", _SHIFT_PRESSED) == (Key.SHIFT_TAB, "")

    def test_plain_tab(self) -> None:
        assert _decode_key_event(_VK_TAB, "\t", 0) == (Key.TAB, "")

    def test_enter(self) -> None:
        assert _decode_key_event(_VK_RETURN, "\r", 0) == (Key.ENTER, "")

    def test_ctrl_enter_left(self) -> None:
        assert _decode_key_event(_VK_RETURN, "", _LEFT_CTRL_PRESSED) == (Key.CTRL_ENTER, "")

    def test_ctrl_enter_right(self) -> None:
        assert _decode_key_event(_VK_RETURN, "", _RIGHT_CTRL_PRESSED) == (Key.CTRL_ENTER, "")

    def test_backspace_vk(self) -> None:
        assert _decode_key_event(_VK_BACK, "\x08", 0) == (Key.BACKSPACE, "")

    def test_escape_vk(self) -> None:
        assert _decode_key_event(_VK_ESCAPE, "\x1b", 0) == (Key.ESC, "")

    @pytest.mark.parametrize("vk,expected", [
        (_VK_UP, Key.UP), (_VK_DOWN, Key.DOWN), (_VK_LEFT, Key.LEFT),
        (_VK_RIGHT, Key.RIGHT), (_VK_HOME, Key.HOME), (_VK_END, Key.END),
    ])
    def test_navigation_keys(self, vk: int, expected: Key) -> None:
        assert _decode_key_event(vk, "", 0) == (expected, "")

    @pytest.mark.parametrize("cp,expected", [
        (0x03, Key.CTRL_C), (0x04, Key.CTRL_D),
        (0x15, Key.CTRL_U), (0x16, Key.CTRL_V),
    ])
    def test_control_chars(self, cp: int, expected: Key) -> None:
        # vk for a letter (e.g. 'C'=0x43) — not a special VK — falls to UnicodeChar.
        assert _decode_key_event(0x43, chr(cp), _LEFT_CTRL_PRESSED) == (expected, "")

    def test_at_sign(self) -> None:
        assert _decode_key_event(0x32, "@", _SHIFT_PRESSED) == (Key.AT, "")

    def test_printable_char(self) -> None:
        assert _decode_key_event(0x41, "a", 0) == (Key.CHAR, "a")

    def test_printable_unicode(self) -> None:
        assert _decode_key_event(0x00, "é", 0) == (Key.CHAR, "é")

    def test_backspace_via_unicode_del(self) -> None:
        assert _decode_key_event(0x00, "\x7f", 0) == (Key.BACKSPACE, "")

    def test_modifier_only_returns_none(self) -> None:
        # VK_SHIFT (0x10) with no char → ignore.
        assert _decode_key_event(0x10, "", _SHIFT_PRESSED) is None

    def test_unmapped_no_char_returns_none(self) -> None:
        assert _decode_key_event(0x5B, "", 0) is None  # VK_LWIN, no char

    def test_non_printable_char_returns_none(self) -> None:
        assert _decode_key_event(0x00, "\x00", 0) is None


# ── _read_key_console loop (drive via patched _next_input_event) ──────────────

class TestReadKeyConsoleLoop:
    def _backend_with_events(self, events: list) -> WindowsBackend:
        backend = WindowsBackend()
        it = iter(events)
        backend._next_input_event = lambda: next(it)  # type: ignore[method-assign]
        return backend

    def test_returns_first_decodable_keydown(self) -> None:
        backend = self._backend_with_events([
            (_KEY_EVENT, True, _VK_TAB, "", _SHIFT_PRESSED),
        ])
        assert backend._read_key_console() == (Key.SHIFT_TAB, "")

    def test_skips_key_up_events(self) -> None:
        backend = self._backend_with_events([
            (_KEY_EVENT, False, _VK_TAB, "", _SHIFT_PRESSED),   # key-up → skip
            (_KEY_EVENT, True,  _VK_TAB, "", _SHIFT_PRESSED),   # key-down → use
        ])
        assert backend._read_key_console() == (Key.SHIFT_TAB, "")

    def test_skips_non_key_events(self) -> None:
        backend = self._backend_with_events([
            (0x0002, True, 0, "", 0),                           # MOUSE_EVENT → skip
            (_KEY_EVENT, True, _VK_RETURN, "\r", 0),            # key-down → use
        ])
        assert backend._read_key_console() == (Key.ENTER, "")

    def test_skips_modifier_only_keydown(self) -> None:
        backend = self._backend_with_events([
            (_KEY_EVENT, True, 0x10, "", _SHIFT_PRESSED),       # bare Shift → None → skip
            (_KEY_EVENT, True, 0x41, "a", 0),                  # 'a' → use
        ])
        assert backend._read_key_console() == (Key.CHAR, "a")

    def test_skips_failed_reads(self) -> None:
        backend = self._backend_with_events([
            None,                                               # read failed → skip
            (_KEY_EVENT, True, _VK_TAB, "", 0),                # plain Tab → use
        ])
        assert backend._read_key_console() == (Key.TAB, "")


# ── getwch fallback still decodes basic keys ──────────────────────────────────

class TestGetwchFallback:
    def test_fallback_used_when_no_console(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = WindowsBackend()
        monkeypatch.setattr(backend, "_console_handle", lambda: None)
        monkeypatch.setattr(backend, "_read_key_getwch", lambda: (Key.CHAR, "x"))
        assert backend.read_key() == (Key.CHAR, "x")

    def test_console_path_used_when_handle_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = WindowsBackend()
        monkeypatch.setattr(backend, "_console_handle", lambda: 123)
        monkeypatch.setattr(backend, "_read_key_console", lambda: (Key.SHIFT_TAB, ""))
        assert backend.read_key() == (Key.SHIFT_TAB, "")


# ── module import safety (no ctypes.wintypes at import time) ──────────────────

class TestImportSafety:
    def test_structures_importable_off_windows(self) -> None:
        # If the module used ctypes.wintypes it would have failed to import on
        # Linux; reaching here means the portable c_* types worked.
        from agenthicc.tui.terminal.windows_backend import (
            _INPUT_RECORD, _KEY_EVENT_RECORD, _COORD,
        )
        rec = _KEY_EVENT_RECORD()
        assert rec.wVirtualKeyCode == 0
        assert _INPUT_RECORD().EventType == 0
        assert _COORD().X == 0
