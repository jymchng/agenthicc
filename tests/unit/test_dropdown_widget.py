"""Unit tests for DropdownWidget (PRD-41).

Tests cover:
  - Initial match population from the handler
  - ESC returns done with cancel action and buf_suffix
  - ENTER selects the first item
  - TAB selects and sets add_space=True
  - CHAR filtering narrows matches
  - BACKSPACE shortens fragment and re-filters
  - BACKSPACE past trigger char returns done with backspace_past_trigger
  - DOWN moves the selection index
  - edit_field_value is always None (input bar shows trigger+fragment)
"""
from __future__ import annotations

import pytest

from agenthicc.tui.trigger import TriggerContext, MatchItem
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.widgets.dropdown import DropdownWidget
from agenthicc.tui.menu import MenuResultKind
from agenthicc.tui.mention_input import Key

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_widget(tmp_path, fragment: str = "") -> DropdownWidget:
    """Create a DropdownWidget backed by an AtMentionTrigger for *tmp_path*."""
    # Create a few files/dirs so get_matches returns something.
    (tmp_path / "alpha.py").write_text("a")
    (tmp_path / "beta.py").write_text("b")
    (tmp_path / "gamma").mkdir()

    handler = AtMentionTrigger()
    ctx = TriggerContext(cwd=tmp_path)
    return DropdownWidget(handler, ctx, initial_fragment=fragment)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dropdown_initial_matches_from_handler(tmp_path):
    """Widget populates matches from handler.get_matches on construction."""
    widget = _make_widget(tmp_path)
    # With an empty fragment every non-hidden entry should match.
    assert len(widget._matches) > 0


def test_dropdown_esc_returns_cancel_with_buf_suffix(tmp_path):
    """ESC returns MenuResult.done with action='cancel' and a buf_suffix list."""
    widget = _make_widget(tmp_path)
    result = widget.handle_key(Key.ESC, "")
    assert result.kind == MenuResultKind.DONE
    assert result.data["action"] == "cancel"
    assert "buf_suffix" in result.data


def test_dropdown_enter_selects_first_item(tmp_path):
    """ENTER returns done with action='select' and the first item's value."""
    widget = _make_widget(tmp_path)
    first_item = widget._matches[0]
    result = widget.handle_key(Key.ENTER, "")
    assert result.kind == MenuResultKind.DONE
    assert result.data["action"] == "select"
    # buf_suffix should contain the selected path (from on_select)
    suffix_str = "".join(result.data["buf_suffix"])
    assert first_item.value in suffix_str


def test_dropdown_tab_adds_space(tmp_path):
    """TAB selection sets add_space=True in the result data."""
    widget = _make_widget(tmp_path)
    result = widget.handle_key(Key.TAB, "")
    assert result.kind == MenuResultKind.DONE
    assert result.data["action"] == "select"
    assert result.data["add_space"] is True


def test_dropdown_char_filters_matches(tmp_path):
    """Typing a character narrows the match list to items starting with that char."""
    widget = _make_widget(tmp_path)
    initial_count = len(widget._matches)
    # Type 'a' — should match only "alpha.py" (and possibly children)
    result = widget.handle_key(Key.CHAR, "a")
    assert result.kind == MenuResultKind.CONTINUE
    assert len(widget._matches) < initial_count
    for m in widget._matches:
        assert m.display.startswith("a"), f"Unexpected match: {m.display}"


def test_dropdown_backspace_shortens_fragment(tmp_path):
    """BACKSPACE removes the last char from the fragment and re-queries matches."""
    (tmp_path / "abc.txt").write_text("x")
    handler = AtMentionTrigger()
    ctx = TriggerContext(cwd=tmp_path)
    widget = DropdownWidget(handler, ctx, initial_fragment="ab")
    before_matches = len(widget._matches)

    result = widget.handle_key(Key.BACKSPACE, "")
    assert result.kind == MenuResultKind.CONTINUE
    assert widget._fragment == "a"
    # Fragment is shorter — match set may expand.
    assert widget._matches is not None  # re-queried, not stale


def test_dropdown_backspace_past_trigger_returns_done(tmp_path):
    """BACKSPACE with an empty fragment returns done with backspace_past_trigger."""
    handler = AtMentionTrigger()
    ctx = TriggerContext(cwd=tmp_path)
    # Empty fragment — one backspace dismisses the dropdown.
    widget = DropdownWidget(handler, ctx, initial_fragment="")
    result = widget.handle_key(Key.BACKSPACE, "")
    assert result.kind == MenuResultKind.DONE
    assert result.data["action"] == "backspace_past_trigger"


def test_dropdown_down_moves_selection(tmp_path):
    """DOWN key increments the selected index (wrapping)."""
    widget = _make_widget(tmp_path)
    assert widget._selected == 0
    widget.handle_key(Key.DOWN, "")
    assert widget._selected == 1 % len(widget._matches)


def test_dropdown_edit_field_value_is_none(tmp_path):
    """edit_field_value is always None for DropdownWidget."""
    widget = _make_widget(tmp_path)
    assert widget.edit_field_value is None
