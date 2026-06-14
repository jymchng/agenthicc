"""Unit tests for agenthicc.tui.symbols."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from agenthicc.tui.symbols import (
    AGENT_BULLET,
    AGENT_COLORS,
    DIVIDER_CHAR,
    DOUBLE_DIVIDER,
    MODE_COLORS,
    MODE_SYMBOLS,
    SPINNER_FRAMES,
    TOOL_APPROVAL,
    TOOL_ERROR,
    TOOL_PENDING,
    TOOL_RUNNING,
    TOOL_SUCCESS,
    USER_BULLET,
    _unicode_safe,
)


# ---------------------------------------------------------------------------
# SPINNER_FRAMES
# ---------------------------------------------------------------------------


class TestSpinnerFrames:
    def test_is_list(self) -> None:
        assert isinstance(SPINNER_FRAMES, list)

    def test_length_is_8(self) -> None:
        assert len(SPINNER_FRAMES) == 8

    def test_all_strings(self) -> None:
        for frame in SPINNER_FRAMES:
            assert isinstance(frame, str)

    def test_all_non_empty(self) -> None:
        for frame in SPINNER_FRAMES:
            assert len(frame) > 0

    def test_all_different(self) -> None:
        # All frames should be distinct
        assert len(set(SPINNER_FRAMES)) == len(SPINNER_FRAMES)


# ---------------------------------------------------------------------------
# Tool call state symbols
# ---------------------------------------------------------------------------


class TestToolSymbols:
    def test_tool_pending_is_str(self) -> None:
        assert isinstance(TOOL_PENDING, str)

    def test_tool_running_is_str(self) -> None:
        assert isinstance(TOOL_RUNNING, str)

    def test_tool_success_is_str(self) -> None:
        assert isinstance(TOOL_SUCCESS, str)

    def test_tool_error_is_str(self) -> None:
        assert isinstance(TOOL_ERROR, str)

    def test_tool_approval_is_str(self) -> None:
        assert isinstance(TOOL_APPROVAL, str)

    def test_symbols_are_distinct(self) -> None:
        symbols = {TOOL_PENDING, TOOL_RUNNING, TOOL_SUCCESS, TOOL_ERROR, TOOL_APPROVAL}
        assert len(symbols) == 5


# ---------------------------------------------------------------------------
# Agent/User bullets
# ---------------------------------------------------------------------------


class TestBullets:
    def test_agent_bullet_is_str(self) -> None:
        assert isinstance(AGENT_BULLET, str)

    def test_user_bullet_is_str(self) -> None:
        assert isinstance(USER_BULLET, str)

    def test_bullets_are_distinct(self) -> None:
        assert AGENT_BULLET != USER_BULLET


# ---------------------------------------------------------------------------
# Dividers
# ---------------------------------------------------------------------------


class TestDividers:
    def test_divider_char_is_str(self) -> None:
        assert isinstance(DIVIDER_CHAR, str)

    def test_double_divider_is_str(self) -> None:
        assert isinstance(DOUBLE_DIVIDER, str)

    def test_dividers_are_distinct(self) -> None:
        assert DIVIDER_CHAR != DOUBLE_DIVIDER


# ---------------------------------------------------------------------------
# MODE_SYMBOLS — all required keys
# ---------------------------------------------------------------------------


class TestModeSymbols:
    REQUIRED_MODES = {"Auto", "Plan", "Ask", "Review", "Safe", "Debug"}

    def test_is_dict(self) -> None:
        assert isinstance(MODE_SYMBOLS, dict)

    def test_all_required_keys_present(self) -> None:
        for mode in self.REQUIRED_MODES:
            assert mode in MODE_SYMBOLS, f"Missing mode key: {mode!r}"

    def test_all_values_are_strings(self) -> None:
        for mode, symbol in MODE_SYMBOLS.items():
            assert isinstance(symbol, str), f"Symbol for {mode!r} is not str: {symbol!r}"

    def test_all_values_non_empty(self) -> None:
        for mode, symbol in MODE_SYMBOLS.items():
            assert len(symbol) > 0, f"Symbol for {mode!r} is empty"

    def test_six_modes(self) -> None:
        assert len(MODE_SYMBOLS) == 6


# ---------------------------------------------------------------------------
# MODE_COLORS — all required keys
# ---------------------------------------------------------------------------


class TestModeColors:
    REQUIRED_MODES = {"Auto", "Plan", "Ask", "Review", "Safe", "Debug"}

    def test_is_dict(self) -> None:
        assert isinstance(MODE_COLORS, dict)

    def test_all_required_keys_present(self) -> None:
        for mode in self.REQUIRED_MODES:
            assert mode in MODE_COLORS, f"Missing color key: {mode!r}"

    def test_all_values_are_strings(self) -> None:
        for mode, color in MODE_COLORS.items():
            assert isinstance(color, str), f"Color for {mode!r} is not str"

    def test_matches_mode_symbols_keys(self) -> None:
        assert set(MODE_COLORS.keys()) == set(MODE_SYMBOLS.keys())


# ---------------------------------------------------------------------------
# AGENT_COLORS
# ---------------------------------------------------------------------------


class TestAgentColors:
    def test_is_list(self) -> None:
        assert isinstance(AGENT_COLORS, list)

    def test_non_empty(self) -> None:
        assert len(AGENT_COLORS) > 0

    def test_all_strings(self) -> None:
        for color in AGENT_COLORS:
            assert isinstance(color, str)

    def test_at_least_six(self) -> None:
        assert len(AGENT_COLORS) >= 6


# ---------------------------------------------------------------------------
# _unicode_safe
# ---------------------------------------------------------------------------


class TestUnicodeSafe:
    def test_returns_preferred_when_utf8_in_lang(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_ALL", "")
        result = _unicode_safe("★", "*")
        assert result == "★"

    def test_returns_preferred_when_utf8_uppercase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Uppercase UTF should also match
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_ALL", "")
        result = _unicode_safe("☃", "x")
        assert result == "☃"

    def test_returns_fallback_when_no_utf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANG", "C")
        monkeypatch.setenv("LC_ALL", "C")
        result = _unicode_safe("★", "*")
        assert result == "*"

    def test_returns_fallback_when_lang_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANG", "")
        monkeypatch.setenv("LC_ALL", "")
        result = _unicode_safe("→", ">")
        assert result == ">"

    def test_lc_all_utf_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANG", "C")
        monkeypatch.setenv("LC_ALL", "en_US.utf8")
        result = _unicode_safe("★", "*")
        assert result == "★"

    def test_returns_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_ALL", "")
        result = _unicode_safe("a", "b")
        assert isinstance(result, str)
