"""Unit tests for the Input Trigger System (PRD-39).

Tests cover TriggerRegistry, MatchItem, TriggerContext, and the TriggerHandler
protocol.  No I/O or TTY interaction is needed — all primitives are pure.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from agenthicc.tui.trigger import TriggerManager as TriggerRegistry, TriggerContext, TriggerResult, MatchItem, TriggerHandler

pytestmark = pytest.mark.unit


# ── Helper trigger used across multiple tests ─────────────────────────────────


class EchoTrigger:
    """Test trigger: '!' — returns fragment as a single match."""

    char  = "!"
    label = "Echo"

    def get_matches(self, fragment, ctx):
        if fragment:
            return [MatchItem(display=fragment, value=fragment)]
        return []

    def on_select(self, item, fragment, buf):
        return TriggerResult(buffer=buf + list("!" + (item.value if item else "")))

    def on_cancel(self, fragment, buf):
        return buf + ["!"] + list(fragment)

    def get_hint(self, item):
        return f"echo: {item.value}" if item else None

    def can_activate(self, buf):
        return True


# ── TriggerRegistry ───────────────────────────────────────────────────────────


def test_registry_register_and_get():
    reg = TriggerRegistry()
    reg.register(EchoTrigger())
    assert reg.get("!") is not None
    assert reg.get("@") is None


def test_registry_chars_property():
    reg = TriggerRegistry()
    reg.register(EchoTrigger())
    assert "!" in reg.chars
    assert isinstance(reg.chars, frozenset)


def test_registry_rejects_multi_char_trigger():
    class Bad:
        char = "@!"

        def get_matches(self, fragment, ctx): ...
        def on_select(self, item, fragment, buf): ...
        def on_cancel(self, fragment, buf): ...
        def get_hint(self, item): ...

    with pytest.raises(ValueError):
        TriggerRegistry().register(Bad())


def test_registry_rejects_empty_char():
    class Empty:
        char = ""

        def get_matches(self, fragment, ctx): ...
        def on_select(self, item, fragment, buf): ...
        def on_cancel(self, fragment, buf): ...
        def get_hint(self, item): ...

    with pytest.raises(ValueError):
        TriggerRegistry().register(Empty())


def test_registry_overwrite_same_char():
    """Registering a second handler for the same char overwrites the first."""

    class FirstTrigger:
        char = "!"

        def get_matches(self, fragment, ctx):
            return [MatchItem(display="first", value="first")]

        def on_select(self, item, fragment, buf):
            return buf

        def on_cancel(self, fragment, buf):
            return buf

        def get_hint(self, item):
            return "first"

    class SecondTrigger:
        char = "!"

        def get_matches(self, fragment, ctx):
            return [MatchItem(display="second", value="second")]

        def on_select(self, item, fragment, buf):
            return buf

        def on_cancel(self, fragment, buf):
            return buf

        def get_hint(self, item):
            return "second"

    reg = TriggerRegistry()
    reg.register(FirstTrigger())
    reg.register(SecondTrigger())
    handler = reg.get("!")
    assert handler is not None
    ctx = TriggerContext(cwd=Path("."))
    matches = handler.get_matches("x", ctx)
    assert matches[0].value == "second"


def test_registry_len():
    reg = TriggerRegistry()
    assert len(reg) == 0
    reg.register(EchoTrigger())
    assert len(reg) == 1

    class HashTrigger:
        char = "#"

        def get_matches(self, fragment, ctx):
            return []

        def on_select(self, item, fragment, buf):
            return buf

        def on_cancel(self, fragment, buf):
            return buf

        def get_hint(self, item):
            return None

    reg.register(HashTrigger())
    assert len(reg) == 2


def test_registry_chars_empty_by_default():
    reg = TriggerRegistry()
    assert reg.chars == frozenset()


def test_registry_get_unregistered_returns_none():
    reg = TriggerRegistry()
    assert reg.get("/") is None


# ── MatchItem ─────────────────────────────────────────────────────────────────


def test_match_item_defaults():
    item = MatchItem(display="src/auth.py", value="src/auth.py")
    assert item.display == "src/auth.py"
    assert item.value == "src/auth.py"
    assert item.hint == ""


def test_match_item_hint_set():
    item = MatchItem(display="x", value="x", hint="some hint")
    assert item.hint == "some hint"


def test_match_item_display_differs_from_value():
    item = MatchItem(display="+ src/auth.py", value="src/auth.py")
    assert item.display != item.value


# ── TriggerContext ────────────────────────────────────────────────────────────


def test_trigger_context_cwd():
    ctx = TriggerContext(cwd=Path("/tmp"))
    assert isinstance(ctx.cwd, Path)
    assert ctx.cwd == Path("/tmp")


def test_trigger_context_session_id_defaults_empty():
    ctx = TriggerContext(cwd=Path("."))
    assert ctx.session_id == ""


def test_trigger_context_session_id_set():
    ctx = TriggerContext(cwd=Path("."), session_id="abc-123")
    assert ctx.session_id == "abc-123"


# ── TriggerHandler protocol ───────────────────────────────────────────────────


def test_handler_protocol_satisfied():
    """EchoTrigger satisfies the TriggerHandler runtime-checkable protocol."""
    t = EchoTrigger()
    # Protocol runtime check verifies structural compatibility.
    assert hasattr(t, "char")
    assert hasattr(t, "label")
    assert hasattr(t, "get_matches")
    assert hasattr(t, "on_select")
    assert hasattr(t, "on_cancel")
    assert hasattr(t, "get_hint")


def test_echo_trigger_char():
    assert EchoTrigger.char == "!"


def test_echo_trigger_matches_with_fragment():
    t = EchoTrigger()
    ctx = TriggerContext(cwd=Path("."))
    result = t.get_matches("hello", ctx)
    assert len(result) == 1
    assert result[0].value == "hello"
    assert result[0].display == "hello"


def test_echo_trigger_no_matches_empty_fragment():
    t = EchoTrigger()
    ctx = TriggerContext(cwd=Path("."))
    result = t.get_matches("", ctx)
    assert result == []


def test_echo_trigger_on_select_with_item():
    t = EchoTrigger()
    item = MatchItem(display="world", value="world")
    result = t.on_select(item, "world", list("say "))
    assert isinstance(result, TriggerResult)
    assert "".join(result.buffer) == "say !world"
    assert result.submit is False


def test_echo_trigger_on_select_no_item():
    t = EchoTrigger()
    result = t.on_select(None, "", list("hi "))
    assert isinstance(result, TriggerResult)
    assert "".join(result.buffer) == "hi !"


def test_echo_trigger_on_cancel_restores_literal():
    t = EchoTrigger()
    buf = t.on_cancel("part", [])
    assert "".join(buf) == "!part"


def test_echo_trigger_on_cancel_empty_fragment():
    t = EchoTrigger()
    buf = t.on_cancel("", [])
    assert "".join(buf) == "!"


def test_echo_trigger_get_hint_with_item():
    t = EchoTrigger()
    item = MatchItem(display="x", value="x")
    hint = t.get_hint(item)
    assert hint is not None
    assert "echo" in hint
    assert "x" in hint


def test_echo_trigger_get_hint_no_item():
    t = EchoTrigger()
    assert t.get_hint(None) is None


# ── Multiple triggers co-existing ────────────────────────────────────────────


def test_registry_multiple_chars():
    class AtTrigger:
        char = "@"

        def get_matches(self, fragment, ctx):
            return []

        def on_select(self, item, fragment, buf):
            return buf

        def on_cancel(self, fragment, buf):
            return buf

        def get_hint(self, item):
            return None

    reg = TriggerRegistry()
    reg.register(EchoTrigger())
    reg.register(AtTrigger())

    assert "!" in reg.chars
    assert "@" in reg.chars
    assert reg.get("!") is not None
    assert reg.get("@") is not None
    assert len(reg) == 2


def test_registry_repr_contains_chars():
    reg = TriggerRegistry()
    reg.register(EchoTrigger())
    r = repr(reg)
    assert "!" in r
    assert "Trigger" in r  # TriggerManager (alias: TriggerRegistry)


# ── can_activate contract ─────────────────────────────────────────────────────


def test_slash_command_trigger_activates_on_empty_buf_or_after_newline():
    """SlashCommandTrigger activates on an empty buffer or after a newline.

    Regression: typing '@docs/index.md', backspacing to '@docs', then pressing
    '/' triggered the slash-command dropdown instead of inserting a literal '/'.
    Multi-line extension: '/cmd' at the start of a new line in a multi-line
    input should also open the command picker.
    """
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    t = SlashCommandTrigger()
    assert t.can_activate([]) is True                        # empty buf → command
    assert t.can_activate(["\n"]) is True                    # after newline → command
    assert t.can_activate(list("first line\n")) is True      # after newline → command
    assert t.can_activate(list("@docs")) is False            # mid-buffer → literal
    assert t.can_activate(list("hello world")) is False      # mid-buffer → literal


def test_at_mention_trigger_activates_after_whitespace_or_at_start():
    """AtMentionTrigger activates at position 0 or after whitespace only."""
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger
    t = AtMentionTrigger()
    assert t.can_activate([]) is True               # start of line
    assert t.can_activate([" "]) is True            # after space
    assert t.can_activate(list("word ")) is True    # after trailing space
    assert t.can_activate(list("word")) is False    # mid-word → literal '@'
    assert t.can_activate(list("foo/")) is False    # path context → literal '@'


def test_echo_trigger_default_can_activate():
    """EchoTrigger (test stub) uses the always-True default."""
    t = EchoTrigger()
    assert t.can_activate([]) is True
    assert t.can_activate(list("anything")) is True
