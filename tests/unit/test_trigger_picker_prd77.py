"""Unit tests for PRD-77: _init_trigger walk and Enter submission."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

pytestmark = pytest.mark.unit


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_registry(*chars: str):
    """Build a minimal TriggerManager-like mock for given trigger chars."""
    from agenthicc.tui.trigger import TriggerResult

    handlers = {}
    for ch in chars:
        h = MagicMock()
        h.char = ch
        h.can_activate.side_effect = lambda buf, _ch=ch: (
            (not buf or buf[-1].isspace())  # @ activates at start or after space
            if _ch == "@"
            else (not buf or buf[-1] == "\n")  # / activates at start or after newline
        )
        h.get_matches.return_value = []
        h.get_hint.return_value = None
        h.get_lines.side_effect = lambda item, w: [item.display[:w]]
        h.on_select.side_effect = lambda item, frag, buf, _ch=ch: TriggerResult(
            buffer=buf + [_ch] + list(frag) if item is None else buf + list(_ch + item.value)
        )
        h.on_cancel.side_effect = lambda frag, buf, _ch=ch: buf + [_ch] + list(frag)
        handlers[ch] = h

    registry = MagicMock()
    registry.chars = frozenset(chars)
    registry.get.side_effect = lambda c: handlers.get(c)
    return registry, handlers


def _make_overlay(initial_buf: list[str], registry, on_complete=None):
    """Create a TriggerPickerOverlay with the given initial buffer."""
    from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

    completed = []
    cb = on_complete or (lambda r: completed.append(r))
    overlay = TriggerPickerOverlay(
        initial_buf=initial_buf,
        registry=registry,
        cwd=Path("."),
        on_complete=cb,
    )
    return overlay, completed


# ── _init_trigger: walk-backward fix (Bug 1) ─────────────────────────────────


def test_init_trigger_finds_at_when_last_char_is_dot():
    """initial_buf ending in '.' must still find '@' and use AtMentionTrigger."""
    registry, handlers = _make_registry("@", "/")

    # Simulate: user typed @docs/ (selected from overlay), then typed "."
    # InsertCapability builds initial = ["@","d","o","c","s","/","."]
    overlay, _ = _make_overlay(["@", "d", "o", "c", "s", "/", "."], registry)

    assert overlay._trigger is not None, "trigger must be activated"
    assert overlay._trigger.char == "@", "trigger char must be @"
    assert overlay._trigger.handler is handlers["@"], "handler must be AtMentionTrigger"
    assert overlay._trigger.fragment == "docs/.", "fragment must be 'docs/.'"
    assert overlay._buf.buf == [], "overlay buf must be pre-trigger content (empty)"


def test_init_trigger_finds_slash_at_start():
    """initial_buf ['/', 'c', 'o', 'n', 'f'] → trigger char '/', fragment 'conf'."""
    registry, handlers = _make_registry("@", "/")

    overlay, _ = _make_overlay(["/", "c", "o", "n", "f"], registry)

    assert overlay._trigger is not None
    assert overlay._trigger.char == "/"
    assert overlay._trigger.handler is handlers["/"]
    assert overlay._trigger.fragment == "conf"
    assert overlay._buf.buf == []


def test_init_trigger_last_char_is_trigger():
    """Standard case: last char IS the trigger — must still work correctly."""
    registry, handlers = _make_registry("@", "/")

    overlay, _ = _make_overlay(["@"], registry)

    assert overlay._trigger is not None
    assert overlay._trigger.char == "@"
    assert overlay._trigger.fragment == ""
    assert overlay._buf.buf == []


def test_second_at_is_inserted_as_literal_fragment_text():
    """A second ``@`` must not be consumed while the first picker is open."""
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.trigger import TriggerResult

    registry, handlers = _make_registry("@")
    handlers["@"].get_matches.return_value = []
    overlay, completed = _make_overlay(["@"], registry)

    overlay.handle_key(Key.AT, "")

    assert overlay._trigger is not None
    assert overlay._trigger.fragment == "@"
    assert completed == []

    # Enter with no matches commits the literal text, proving that the second
    # character survived the overlay instead of merely changing its state.
    overlay.handle_key(Key.ENTER, "")
    assert len(completed) == 1
    result = completed[0]
    assert isinstance(result, TriggerResult)
    assert "".join(result.buffer) == "@@"
    assert result.submit is True


def test_init_trigger_slash_not_activated_mid_word():
    """'/' mid-word (can_activate returns False) must be skipped; '@' found instead."""
    registry, handlers = _make_registry("@", "/")
    # "@docs/" — slash after non-empty non-newline buf → SlashCommand can't activate
    # but "@" at position 0 with empty pre → AtMention activates
    overlay, _ = _make_overlay(["@", "d", "o", "c", "s", "/"], registry)

    assert overlay._trigger is not None
    assert overlay._trigger.char == "@"
    assert overlay._trigger.fragment == "docs/"


def test_init_trigger_no_trigger_in_buf():
    """Buffer with no trigger chars → trigger stays None."""
    registry, handlers = _make_registry("@", "/")

    overlay, _ = _make_overlay(["h", "e", "l", "l", "o"], registry)

    assert overlay._trigger is None


def test_init_trigger_stops_at_space():
    """Walk stops at whitespace — trigger before the space is not found."""
    registry, handlers = _make_registry("@", "/")

    # "hello @docs" — space before "@"; can_activate(["h","e","l","l","o"," "]) → True
    # but the walk stops at the space before it reaches "@"
    # Actually: walk goes right-to-left; hits "s","c","o","d" (no chars), then "@"
    # which is in registry. pre = ["h","e","l","l","o"," "] → can_activate → True
    # So this SHOULD work (space is in pre, last char of pre is space)
    overlay, _ = _make_overlay(["h", "e", "l", "l", "o", " ", "@", "d", "o", "c", "s"], registry)

    assert overlay._trigger is not None
    assert overlay._trigger.char == "@"
    assert overlay._trigger.fragment == "docs"
    assert overlay._buf.buf == ["h", "e", "l", "l", "o", " "]


# ── Enter vs Tab separation (Bug 2) ───────────────────────────────────────────


def test_enter_with_no_matches_sets_submit_true():
    """Enter with no dropdown matches → TriggerResult.submit is True."""
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.trigger import TriggerResult

    registry, handlers = _make_registry("@")
    handlers["@"].get_matches.return_value = []  # no matches

    overlay, completed = _make_overlay(["@", "d", "o", "c", "s", "/", "."], registry)

    assert overlay._trigger is not None
    assert overlay._matches == []

    overlay.handle_key(Key.ENTER, "")

    assert len(completed) == 1
    result = completed[0]
    assert isinstance(result, TriggerResult)
    assert result.submit is True, "Enter with no match must set submit=True"


def test_enter_with_match_does_not_submit():
    """Enter with a dropdown match selected → commit only, submit=False."""
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.trigger import TriggerResult, MatchItem

    registry, handlers = _make_registry("@")
    match_item = MatchItem(display="docs/index.md", value="docs/index.md")
    handlers["@"].get_matches.return_value = [match_item]
    handlers["@"].on_select.side_effect = lambda item, frag, buf: TriggerResult(
        buffer=buf + list("@" + item.value)
    )

    overlay, completed = _make_overlay(["@", "d", "o", "c", "s", "/"], registry)

    assert len(overlay._matches) == 1

    overlay.handle_key(Key.ENTER, "")

    assert len(completed) == 1
    result = completed[0]
    assert isinstance(result, TriggerResult)
    assert result.submit is False, "Enter with match must NOT set submit=True"


def test_tab_with_no_matches_does_not_submit():
    """Tab with no matches → commit text without submitting."""
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.trigger import TriggerResult

    registry, handlers = _make_registry("@")
    handlers["@"].get_matches.return_value = []

    overlay, completed = _make_overlay(["@", "d", "o", "c", "s", "/", "."], registry)

    overlay.handle_key(Key.TAB, "")

    assert len(completed) == 1
    result = completed[0]
    assert isinstance(result, TriggerResult)
    assert result.submit is False, "Tab must never set submit=True"


def test_tab_with_match_does_not_submit():
    """Tab with a match → commits selection without submitting."""
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.trigger import TriggerResult, MatchItem

    registry, handlers = _make_registry("@")
    match_item = MatchItem(display="docs/index.md", value="docs/index.md")
    handlers["@"].get_matches.return_value = [match_item]
    handlers["@"].on_select.side_effect = lambda item, frag, buf: TriggerResult(
        buffer=buf + list("@" + item.value)
    )

    overlay, completed = _make_overlay(["@", "d", "o", "c", "s", "/"], registry)

    overlay.handle_key(Key.TAB, "")

    assert len(completed) == 1
    result = completed[0]
    assert result.submit is False


def test_space_is_inserted_without_selecting_a_directory_child(tmp_path: Path, monkeypatch) -> None:
    """SPACE updates the fragment and never commits the highlighted row."""
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.trigger import TriggerManager
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger

    directory = tmp_path / "beyond-35-build-strength-that-lasts"
    directory.mkdir()
    (directory / "__pycache__").mkdir()
    (directory / "assets").mkdir()
    (directory / "README.md").write_text("book", encoding="utf-8")

    registry = TriggerManager()
    handler = AtMentionTrigger()
    registry.register(handler)
    completed = []
    from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

    def fail_on_select(item, fragment, buf):
        raise AssertionError("SPACE must not select a mention completion")

    monkeypatch.setattr(handler, "on_select", fail_on_select)

    initial = ["@", *directory.name, "/"]
    overlay = TriggerPickerOverlay(
        initial_buf=initial,
        registry=registry,
        cwd=tmp_path,
        on_complete=completed.append,
    )

    assert overlay._matches[0].value == f"{directory.name}/__pycache__/"
    overlay.handle_key(Key.CHAR, " ")

    assert completed == []
    assert overlay._trigger is not None
    assert overlay._trigger.fragment == f"{directory.name}/ "
    assert overlay._matches == []


def test_space_is_inserted_without_selecting_a_command() -> None:
    """A highlighted slash-command suggestion is not committed by SPACE."""
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.trigger import MatchItem, TriggerManager, TriggerResult
    from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

    class Handler:
        char = "/"
        label = "Command"

        def get_matches(self, fragment, ctx):
            return [MatchItem(display="/build", value="build")] if fragment == "b" else []

        def on_select(self, item, fragment, buf):
            raise AssertionError("SPACE must not select a completion")

        def on_cancel(self, fragment, buf):
            return buf + ["/"] + list(fragment)

        def can_activate(self, buf):
            return not buf

        def get_hint(self, item):
            return None

        def get_lines(self, item, available_width):
            return [item.display]

    manager = TriggerManager()
    manager.register(Handler())
    completed: list[TriggerResult | None] = []
    overlay = TriggerPickerOverlay(
        initial_buf=list("/b"),
        registry=manager,
        cwd=Path("."),
        on_complete=completed.append,
    )

    overlay.handle_key(Key.CHAR, " ")

    assert completed == []
    assert overlay._trigger is not None
    assert overlay._trigger.fragment == "b "


# ── End-to-end scenario: Bug 1 (select then type) ────────────────────────────


def test_bug1_select_then_type_dot_then_enter():
    """Regression: @do → select @docs/ → type . → Enter should NOT clear buffer."""
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.trigger import TriggerResult

    registry, handlers = _make_registry("@")
    handlers["@"].get_matches.return_value = []

    # Simulate: session buf = ["@","d","o","c","s","/"] after overlay selection.
    # InsertCapability reopens with initial = [..., "."]
    overlay, completed = _make_overlay(["@", "d", "o", "c", "s", "/", "."], registry)

    assert overlay._trigger is not None, "trigger must activate on @"
    assert overlay._trigger.fragment == "docs/.", "fragment must include all chars"

    overlay.handle_key(Key.ENTER, "")

    assert len(completed) == 1
    result = completed[0]
    assert isinstance(result, TriggerResult)
    # Buffer must reconstruct to @docs/. — NOT empty
    assert "".join(result.buffer) == "@docs/.", (
        f"expected '@docs/.' but got {''.join(result.buffer)!r}"
    )
    assert result.submit is True, "Enter with no match must auto-submit"


# ── End-to-end scenario: Bug 2 (type full path, single Enter) ────────────────


def test_bug2_full_path_typed_single_enter_submits():
    """Regression: @docs/. typed in full → one Enter must submit."""
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.trigger import TriggerResult

    registry, handlers = _make_registry("@")
    handlers["@"].get_matches.return_value = []

    # Standard open: initial = ["@"]
    overlay, completed = _make_overlay(["@"], registry)

    # User types d o c s / .
    for char in "docs/.":
        overlay.handle_key(Key.CHAR, char)

    assert overlay._trigger.fragment == "docs/."

    overlay.handle_key(Key.ENTER, "")

    assert len(completed) == 1
    result = completed[0]
    assert isinstance(result, TriggerResult)
    assert "".join(result.buffer) == "@docs/."
    assert result.submit is True
