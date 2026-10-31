"""Multiline cursor navigation for the agents hub editor."""

from __future__ import annotations

import pytest

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.agents_hub.editor import EditResult, LineEditor

pytestmark = pytest.mark.unit


def test_multiline_vertical_navigation_preserves_column_and_clamps() -> None:
    editor = LineEditor("abc\nde\nxyz", multiline=True)
    editor.cursor = 2

    assert editor.handle(Key.DOWN, "") is EditResult.MOVED
    assert editor.cursor == 6
    assert editor.handle(Key.DOWN, "") is EditResult.MOVED
    assert editor.cursor == 9
    assert editor.handle(Key.UP, "") is EditResult.MOVED
    assert editor.cursor == 6
    assert editor.handle(Key.UP, "") is EditResult.MOVED
    assert editor.cursor == 2


def test_multiline_home_end_and_vertical_boundaries_are_line_local() -> None:
    editor = LineEditor("abc\ndef", multiline=True)
    editor.cursor = 1

    editor.handle(Key.UP, "")
    assert editor.cursor == 1
    editor.handle(Key.DOWN, "")
    assert editor.cursor == 5
    editor.handle(Key.HOME, "")
    assert editor.cursor == 4
    editor.handle(Key.END, "")
    assert editor.cursor == 7
    editor.handle(Key.DOWN, "")
    assert editor.cursor == 7
