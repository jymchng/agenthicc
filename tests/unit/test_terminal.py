"""Unit tests for agenthicc.tui.terminal (PRD-62)."""

from __future__ import annotations

import os
import shutil

import pytest

from agenthicc.tui.terminal import (
    FakeTerminal,
    Key,
    Size,
    Terminal,
    truncate_to_cols,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Size — correct field mapping from shutil.get_terminal_size
# ---------------------------------------------------------------------------


def test_size_uses_lines_not_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Size.rows must come from .lines and Size.cols from .columns.

    shutil.get_terminal_size() returns os.terminal_size((columns, lines))
    i.e. width first, height second.  Unpacking as (rows, cols) would swap
    them — verify we read by attribute name instead.
    """
    # terminal_size(columns=120, lines=40)
    fake_size = os.terminal_size((120, 40))
    monkeypatch.setattr(
        shutil, "get_terminal_size", lambda *args, **kwargs: fake_size
    )

    t = Terminal()
    s = t.size
    assert s.cols == 120, f"cols should be 120 (columns), got {s.cols}"
    assert s.rows == 40, f"rows should be 40 (lines), got {s.rows}"


# ---------------------------------------------------------------------------
# FakeTerminal.commit_lines
# ---------------------------------------------------------------------------


def test_fake_terminal_commit_lines() -> None:
    t = FakeTerminal()
    t.commit_lines(["a", "b"])
    assert t.committed == ["a", "b"]


def test_fake_terminal_commit_lines_clears_bottom() -> None:
    t = FakeTerminal()
    t.set_bottom(["prompt"])
    t.commit_lines(["line"])
    assert t.bottom == []


# ---------------------------------------------------------------------------
# FakeTerminal.set_bottom
# ---------------------------------------------------------------------------


def test_fake_terminal_set_bottom() -> None:
    t = FakeTerminal()
    t.set_bottom(["x", "y"])
    assert t.bottom == [truncate_to_cols("x", 80), truncate_to_cols("y", 80)]


def test_set_bottom_truncates() -> None:
    """A line longer than the terminal width must be truncated."""
    t = FakeTerminal(cols=10)
    long_line = "x" * 100
    t.set_bottom([long_line])
    # The stored string includes the reset escape \x1b[0m at the end;
    # visible chars must be at most 10.
    stored = t.bottom[0]
    # Strip all ANSI sequences to count visible chars
    import re
    visible = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", stored)
    assert len(visible) <= 10, f"Expected at most 10 visible chars, got {len(visible)}"


# ---------------------------------------------------------------------------
# truncate_to_cols
# ---------------------------------------------------------------------------


def test_truncate_to_cols_counts_only_visible() -> None:
    """ANSI colour codes must not count toward visible width."""
    coloured = "\x1b[32mhello\x1b[0m"  # 5 visible chars
    result = truncate_to_cols(coloured, 3)
    # Strip ANSI to find visible content
    import re
    visible = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result)
    assert len(visible) <= 3, f"Expected at most 3 visible chars, got {len(visible)!r}"
    assert visible == "hel"


def test_truncate_no_truncation_when_fits() -> None:
    """When text fits within max_visible, all visible chars must be preserved."""
    text = "hello"
    result = truncate_to_cols(text, 10)
    import re
    visible = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result)
    assert visible == "hello"


def test_truncate_to_cols_appends_reset() -> None:
    """truncate_to_cols must always append \\x1b[0m."""
    result = truncate_to_cols("hi", 5)
    assert result.endswith("\x1b[0m")


def test_truncate_to_cols_zero_width() -> None:
    result = truncate_to_cols("hello", 0)
    assert result == "\x1b[0m"


# ---------------------------------------------------------------------------
# Key enum
# ---------------------------------------------------------------------------


def test_key_enum_values() -> None:
    assert Key.SHIFT_TAB == "SHIFT_TAB"
    assert Key.NEWLINE == "NEWLINE"


def test_key_enum_all_members() -> None:
    expected = {
        "UP", "DOWN", "LEFT", "RIGHT", "ENTER", "TAB", "ESC",
        "BACKSPACE", "CTRL_C", "CTRL_D", "CTRL_U", "SHIFT_TAB",
        "NEWLINE", "AT", "CHAR",
    }
    actual = {k.value for k in Key}
    assert actual == expected


# ---------------------------------------------------------------------------
# on_resize marks size dirty
# ---------------------------------------------------------------------------


def test_on_resize_marks_dirty() -> None:
    t = FakeTerminal()
    assert not t._size_dirty
    t.on_resize()
    assert t._size_dirty


def test_on_resize_size_property_clears_dirty() -> None:
    """Accessing .size after on_resize() must clear _size_dirty."""
    t = FakeTerminal(rows=24, cols=80)
    t.on_resize()
    assert t._size_dirty
    _ = t.size
    assert not t._size_dirty


# ---------------------------------------------------------------------------
# FakeTerminal context manager is a no-op
# ---------------------------------------------------------------------------


def test_fake_terminal_context_manager() -> None:
    t = FakeTerminal()
    with t as ctx:
        assert ctx is t
    # No exception — __exit__ is a no-op


# ---------------------------------------------------------------------------
# Size dataclass
# ---------------------------------------------------------------------------


def test_size_dataclass_frozen() -> None:
    s = Size(rows=24, cols=80)
    with pytest.raises((AttributeError, TypeError)):
        s.rows = 50  # type: ignore[misc]


def test_size_fields() -> None:
    s = Size(rows=10, cols=200)
    assert s.rows == 10
    assert s.cols == 200
