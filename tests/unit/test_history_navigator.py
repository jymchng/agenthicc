"""Unit tests for agenthicc.tui.input.history.HistoryNavigator (PRD-57 §10.2)."""
from __future__ import annotations

import pytest
from agenthicc.tui.input.history import HistoryNavigator

pytestmark = pytest.mark.unit


class TestUp:
    def test_up_returns_previous_entry(self) -> None:
        hist = HistoryNavigator(["first", "second"])
        result = hist.up([])
        assert result == list("second")

    def test_up_saves_current_buffer(self) -> None:
        hist = HistoryNavigator(["prev"])
        current = list("current")
        hist.up(current)
        # Navigate back down to retrieve the saved current
        result = hist.down([])
        assert result == current

    def test_up_at_oldest_returns_none(self) -> None:
        hist = HistoryNavigator(["only"])
        hist.up([])
        assert hist.up([]) is None

    def test_up_on_empty_history_returns_none(self) -> None:
        hist = HistoryNavigator([])
        assert hist.up([]) is None


class TestDown:
    def test_down_at_newest_returns_saved_current(self) -> None:
        hist = HistoryNavigator(["prev"])
        current = list("typed")
        hist.up(current)
        result = hist.down([])
        assert result == current

    def test_down_returns_next_entry(self) -> None:
        hist = HistoryNavigator(["a", "b"])
        hist.up([])     # → "b"
        hist.up([])     # → "a"
        result = hist.down([])
        assert result == list("b")

    def test_down_at_newest_already_returns_saved(self) -> None:
        hist = HistoryNavigator(["x"])
        saved = list("saved")
        hist.up(saved)
        hist.down([])   # back to saved
        # Already at newest; another down returns None
        assert hist.down([]) is None


class TestCommit:
    def test_commit_appends_text(self) -> None:
        hist = HistoryNavigator([])
        hist.commit("hello")
        assert hist._history == ["hello"]

    def test_commit_resets_index(self) -> None:
        hist = HistoryNavigator(["old"])
        hist.up([])          # go back
        hist.commit("new")   # commit
        # Now up should return "new" (most recent)
        result = hist.up([])
        assert result == list("new")

    def test_commit_empty_not_appended(self) -> None:
        hist = HistoryNavigator([])
        hist.commit("")
        assert hist._history == []
