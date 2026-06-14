"""Unit tests for agenthicc.tui.input.session.InputSession (PRD-57 §10.2).

All tests run without a real TTY.  The session is driven via injectable
callables: ``_fn_raw_mode`` (fake CBREAK context), ``_fn_read_key`` (pre-baked
key sequence), and ``_fn_redraw`` (no-op).
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.input.session import InputSession
from agenthicc.tui.trigger import TriggerRegistry
from agenthicc.tui.triggers.at_mention import AtMentionTrigger

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────

def _fake_raw(fd):
    @contextmanager
    def _ctx(fd):
        yield fd
    return _ctx(fd)


def _make_session(keys: list[tuple], history: list[str] | None = None, tmp_path=None):
    """Return a configured InputSession driven by *keys*."""
    it = iter(keys)

    def fake_read_key(fd):
        try:
            return next(it)
        except StopIteration:
            return (Key.CTRL_D, "")

    registry = TriggerRegistry()
    registry.register(AtMentionTrigger())

    session = InputSession(
        cwd=tmp_path or Path("."),
        history=history if history is not None else [],
        registry=registry,
        _fn_raw_mode=_fake_raw,
        _fn_read_key=fake_read_key,
        _fn_redraw=lambda *a, **kw: 0,
    )
    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=42):
        return session


# ── normal editing ────────────────────────────────────────────────────────────

class TestNormalEditing:
    def test_typing_returns_text(self, tmp_path: Path) -> None:
        session = _make_session(
            [(Key.CHAR, "h"), (Key.CHAR, "i"), (Key.ENTER, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "hi"

    def test_enter_updates_history(self, tmp_path: Path) -> None:
        history: list[str] = []
        session = _make_session(
            [(Key.CHAR, "a"), (Key.ENTER, "")],
            history=history, tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            session.run()
        assert "a" in history

    def test_empty_enter_returns_empty(self, tmp_path: Path) -> None:
        session = _make_session([(Key.ENTER, "")], tmp_path=tmp_path)
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == ""

    def test_backspace_removes_char(self, tmp_path: Path) -> None:
        session = _make_session(
            [(Key.CHAR, "h"), (Key.CHAR, "i"), (Key.BACKSPACE, ""), (Key.ENTER, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "h"

    def test_ctrl_enter_inserts_newline(self, tmp_path: Path) -> None:
        session = _make_session(
            [(Key.CHAR, "a"), (Key.CTRL_ENTER, ""), (Key.CHAR, "b"), (Key.ENTER, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "a\nb"

    def test_ctrl_u_clears(self, tmp_path: Path) -> None:
        session = _make_session(
            [(Key.CHAR, "x"), (Key.CTRL_U, ""), (Key.ENTER, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == ""


# ── cursor movement ───────────────────────────────────────────────────────────

class TestCursorMovement:
    def test_left_moves_cursor(self, tmp_path: Path) -> None:
        """After Left, inserted char goes before last char."""
        session = _make_session(
            [
                (Key.CHAR, "a"), (Key.CHAR, "b"),
                (Key.LEFT, ""),
                (Key.CHAR, "X"),
                (Key.ENTER, ""),
            ],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "aXb"

    def test_right_moves_cursor(self, tmp_path: Path) -> None:
        session = _make_session(
            [
                (Key.CHAR, "a"), (Key.CHAR, "b"),
                (Key.LEFT, ""), (Key.LEFT, ""),
                (Key.RIGHT, ""),
                (Key.CHAR, "X"),
                (Key.ENTER, ""),
            ],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "aXb"

    def test_home_moves_to_start(self, tmp_path: Path) -> None:
        session = _make_session(
            [
                (Key.CHAR, "a"), (Key.CHAR, "b"),
                (Key.HOME, ""),
                (Key.CHAR, "X"),
                (Key.ENTER, ""),
            ],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "Xab"

    def test_end_moves_to_end(self, tmp_path: Path) -> None:
        session = _make_session(
            [
                (Key.CHAR, "a"), (Key.CHAR, "b"),
                (Key.HOME, ""),
                (Key.END, ""),
                (Key.CHAR, "X"),
                (Key.ENTER, ""),
            ],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "abX"


# ── exit keys ─────────────────────────────────────────────────────────────────

class TestExitKeys:
    def test_ctrl_d_on_empty_returns_none(self, tmp_path: Path) -> None:
        session = _make_session([(Key.CTRL_D, "")], tmp_path=tmp_path)
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result is None

    def test_ctrl_d_with_text_returns_text(self, tmp_path: Path) -> None:
        session = _make_session(
            [(Key.CHAR, "x"), (Key.CTRL_D, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "x"

    def test_ctrl_c_once_continues(self, tmp_path: Path) -> None:
        """First Ctrl+C clears buf and keeps looping."""
        session = _make_session(
            [(Key.CHAR, "x"), (Key.CTRL_C, ""), (Key.CHAR, "y"), (Key.ENTER, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "y"

    def test_ctrl_c_twice_returns_none(self, tmp_path: Path) -> None:
        session = _make_session(
            [(Key.CTRL_C, ""), (Key.CTRL_C, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result is None


# ── history navigation ────────────────────────────────────────────────────────

class TestHistoryNavigation:
    def test_up_down_history(self, tmp_path: Path) -> None:
        history = ["previous"]
        session = _make_session(
            [(Key.UP, ""), (Key.ENTER, "")],
            history=history, tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "previous"

    def test_up_then_down_restores_buffer(self, tmp_path: Path) -> None:
        history = ["prev"]
        session = _make_session(
            [
                (Key.CHAR, "t"), (Key.CHAR, "y"), (Key.CHAR, "p"),
                (Key.UP, ""),
                (Key.DOWN, ""),
                (Key.ENTER, ""),
            ],
            history=history, tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "typ"


# ── trigger mode ──────────────────────────────────────────────────────────────

class TestTriggerMode:
    def test_at_activates_trigger(self, tmp_path: Path) -> None:
        """Typing @ opens the trigger; Esc cancels and inserts '@'."""
        session = _make_session(
            [(Key.AT, ""), (Key.ESC, ""), (Key.ENTER, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        # After Esc the trigger is cancelled and '@' is restored in buf
        assert "@" in result

    def test_trigger_esc_then_type(self, tmp_path: Path) -> None:
        session = _make_session(
            [
                (Key.AT, ""),
                (Key.ESC, ""),
                (Key.CHAR, "x"),
                (Key.ENTER, ""),
            ],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert "x" in result

    def test_trigger_ctrl_c_first_press_continues(self, tmp_path: Path) -> None:
        """First Ctrl+C in trigger mode cancels trigger and clears buf."""
        session = _make_session(
            [(Key.AT, ""), (Key.CTRL_C, ""), (Key.CHAR, "z"), (Key.ENTER, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result == "z"

    def test_trigger_ctrl_c_twice_returns_none(self, tmp_path: Path) -> None:
        session = _make_session(
            [(Key.AT, ""), (Key.CTRL_C, ""), (Key.CTRL_C, "")],
            tmp_path=tmp_path,
        )
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=42):
            result = session.run()
        assert result is None
