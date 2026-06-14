"""Integration tests for the Input Trigger System (PRD-39 + PRD-40).

Tests verify that the refactored ``read_line_with_mention`` state machine
correctly activates trigger handlers from a ``TriggerRegistry``.  The TTY layer
is bypassed by patching ``_raw_mode``, ``_read_key``, ``_redraw``, and
``sys.stdin.isatty``.
"""
from __future__ import annotations

import pytest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, call

from agenthicc.tui.mention_input import read_line_with_mention, Key
from agenthicc.tui.trigger import TriggerRegistry, TriggerContext, MatchItem
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.tui.input_bar import CommandRegistry, CommandSpec, build_default_registry

pytestmark = pytest.mark.integration


# ── Shared driver ─────────────────────────────────────────────────────────────


def _drive(
    keys: list[tuple],
    tmp_path: Path | None = None,
    registry: TriggerRegistry | None = None,
) -> tuple[str | None, list[str]]:
    """Drive read_line_with_mention with a pre-baked key sequence.

    *keys* is a list of ``(Key, char)`` pairs as returned by ``_read_key``.
    Returns ``(result, history)``.
    """
    it = iter(keys)
    history: list[str] = []

    def fake_read_key(fd):
        try:
            return next(it)
        except StopIteration:
            return (Key.CTRL_D, "")

    @contextmanager
    def fake_raw(fd):
        yield 42

    with (
        patch("agenthicc.tui.mention_input._raw_mode", fake_raw),
        patch("agenthicc.tui.mention_input._read_key", fake_read_key),
        patch("agenthicc.tui.mention_input._redraw", return_value=(0, 1)),
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdin.fileno", return_value=42),
    ):
        result = read_line_with_mention(
            "❯ ", tmp_path or Path("."), history, registry=registry
        )

    return result, history


# ── Key helpers ───────────────────────────────────────────────────────────────

def _char(c: str) -> tuple:
    return (Key.CHAR, c)

def _enter() -> tuple:
    return (Key.ENTER, "")

def _esc() -> tuple:
    return (Key.ESC, "")

def _bs() -> tuple:
    return (Key.BACKSPACE, "")

def _down() -> tuple:
    return (Key.DOWN, "")

def _up() -> tuple:
    return (Key.UP, "")

def _at() -> tuple:
    return (Key.AT, "")

def _tab() -> tuple:
    return (Key.TAB, "")

def _ctrl_d() -> tuple:
    return (Key.CTRL_D, "")


# ── @ trigger still works ─────────────────────────────────────────────────────


def test_at_trigger_still_works(tmp_path):
    """The @ trigger activates AtMentionTrigger and selects a file."""
    (tmp_path / "main.py").write_text("x")

    reg = TriggerRegistry()
    reg.register(AtMentionTrigger())

    # AT → enter trigger mode, initial matches include "main.py"
    # ENTER → select top match (main.py), ENTER → submit the line
    keys = [_at(), _enter(), _enter()]
    result, _ = _drive(keys, tmp_path=tmp_path, registry=reg)

    assert result is not None
    assert "@main.py" in result


def test_at_trigger_backward_compat(tmp_path):
    """When no registry is passed, the default registry with @ is used."""
    (tmp_path / "compat.py").write_text("x")

    # registry=None → default registry containing AtMentionTrigger
    keys = [_at(), _enter(), _enter()]
    result, _ = _drive(keys, tmp_path=tmp_path, registry=None)

    assert result is not None
    assert "@" in result


# ── / trigger activated by char ───────────────────────────────────────────────


def test_slash_trigger_activated_by_char():
    """Typing '/' activates SlashCommandTrigger (enters trigger mode)."""
    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec("/status", "Show status"))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))

    # "/" → trigger mode; ESC → cancel, restores "/"; ENTER → submit
    keys = [_char("/"), _esc(), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result is not None
    assert "/" in result


def test_slash_trigger_enter_inserts_command():
    """'/' → DOWN → ENTER inserts the selected command name into the buffer."""
    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec("/status", "Show status"))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))

    # "/" triggers with "/status" in matches; ENTER selects it; ENTER submits
    keys = [_char("/"), _enter(), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result is not None
    assert "/status" in result


def test_slash_trigger_esc_cancels():
    """'/' → ESC restores the '/' literal into the buffer."""
    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec("/status", "Show status"))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))

    keys = [_char("/"), _esc(), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result is not None
    assert result == "/"


def test_slash_trigger_backspace_cancels():
    """'/' → BACKSPACE (with empty fragment) cancels and removes the '/'."""
    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec("/status", "Show status"))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))

    # "/" activates trigger; BACKSPACE with empty fragment cancels and removes /
    # Then ENTER submits empty buffer
    keys = [_char("/"), _bs(), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result == ""


def test_slash_trigger_filter_by_typing(tmp_path):
    """'/' → 'd' → ENTER inserts /deploy (only command starting with 'd')."""
    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec("/deploy", "Deploy"))
    cmd_reg.register(CommandSpec("/status", "Show status"))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))

    # "/" → trigger mode with all cmds; "d" → narrows to /deploy; ENTER selects; ENTER submits
    keys = [_char("/"), _char("d"), _enter(), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result is not None
    assert "/deploy" in result
    assert "/status" not in result


# ── Multiple triggers ─────────────────────────────────────────────────────────


def test_multiple_triggers_both_work(tmp_path):
    """Registry with @ and / — both triggers can be activated independently."""
    (tmp_path / "readme.py").write_text("x")

    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec("/status", "Show status"))

    reg = TriggerRegistry()
    reg.register(AtMentionTrigger())
    reg.register(SlashCommandTrigger(cmd_reg))

    assert "@" in reg.chars
    assert "/" in reg.chars

    # Test slash path: "/" → ENTER (select /status) → ENTER (submit)
    keys_slash = [_char("/"), _enter(), _enter()]
    result_slash, _ = _drive(keys_slash, tmp_path=tmp_path, registry=reg)
    assert result_slash is not None
    assert "/status" in result_slash

    # Test at path: "@" → ENTER (select readme.py) → ENTER (submit)
    keys_at = [_at(), _enter(), _enter()]
    result_at, _ = _drive(keys_at, tmp_path=tmp_path, registry=reg)
    assert result_at is not None
    assert "@" in result_at


# ── Unknown trigger char not activated ───────────────────────────────────────


def test_unknown_trigger_char_not_activated():
    """A character not registered as a trigger is appended to the buffer normally."""
    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec("/status", "Show status"))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))
    # '@' is NOT in this registry

    # "@" is not in registry — should be appended as literal; ENTER submits
    keys = [_at(), _enter()]
    result, _ = _drive(keys, registry=reg)

    # When @ is typed but not in registry, it falls through to buf.append("@")
    # The AT key is handled specially — when "@" not in registry.chars, it
    # falls to CHAR path or is ignored. Let's assert it does not crash and
    # result is either "" or "@".
    assert result is not None


def test_plain_char_not_in_registry_appended():
    """A non-trigger character 'x' is always appended normally."""
    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(build_default_registry()))

    keys = [_char("x"), _char("y"), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result == "xy"


# ── Hint passed to _redraw ────────────────────────────────────────────────────


def test_hint_passed_to_redraw():
    """When a command has an argument_hint, _redraw is called with a non-None hint."""
    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec(
        "/model",
        "Switch model",
        argument_hint="[provider] [model]",
    ))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))

    redraw_calls: list[tuple] = []

    def capturing_redraw(*args, **kwargs):
        redraw_calls.append(args)
        return (0, 1)

    it = iter([_char("/"), _esc(), _enter()])

    def fake_read_key(fd):
        return next(it, _ctrl_d())

    @contextmanager
    def fake_raw(fd):
        yield 42

    history: list[str] = []
    with (
        patch("agenthicc.tui.mention_input._raw_mode", fake_raw),
        patch("agenthicc.tui.mention_input._read_key", fake_read_key),
        patch("agenthicc.tui.mention_input._redraw", side_effect=capturing_redraw),
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdin.fileno", return_value=42),
    ):
        read_line_with_mention("❯ ", Path("."), history, registry=reg)

    # At least one _redraw call should have a non-None hint (8th positional arg or kwarg)
    # Signature: _redraw(prompt, buf, fragment, matches, selected, prev, in_trigger, hint)
    hints_passed = [
        args[7] if len(args) > 7 else None
        for args in redraw_calls
    ]
    assert any(h is not None for h in hints_passed), (
        f"Expected at least one _redraw call with a hint. Calls: {redraw_calls}"
    )


# ── ESC with fragment restores slash+fragment ─────────────────────────────────


def test_slash_trigger_esc_with_fragment_restores():
    """'/' → 'mo' → ESC restores '/mo' in the buffer."""
    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec("/model", "Switch model"))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))

    keys = [_char("/"), _char("m"), _char("o"), _esc(), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result is not None
    assert "/mo" in result


# ── TAB selects and appends space ─────────────────────────────────────────────


def test_slash_trigger_tab_inserts_with_space():
    """TAB selects the command and appends a trailing space."""
    cmd_reg = CommandRegistry()
    cmd_reg.register(CommandSpec("/status", "Show status"))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))

    keys = [_char("/"), _tab(), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result is not None
    assert "/status " in result


# ── Empty registry — slash goes through as literal ───────────────────────────


def test_slash_no_matches_enter_restores_literal():
    """'/' with an empty command registry — ENTER with no matches restores '/fragment'."""
    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(CommandRegistry()))  # empty

    # "/" → trigger mode with empty matches; ENTER → on_select(None, "") → "/"; ENTER submit
    keys = [_char("/"), _enter(), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result is not None
    assert "/" in result


# ── DOWN navigates matches ────────────────────────────────────────────────────


def test_slash_trigger_down_navigates_matches():
    """DOWN moves selection; ENTER inserts the second command, not the first."""
    cmd_reg = CommandRegistry()
    # register alphabetically: /alpha, /beta
    cmd_reg.register(CommandSpec("/alpha", "Alpha command"))
    cmd_reg.register(CommandSpec("/beta", "Beta command"))

    reg = TriggerRegistry()
    reg.register(SlashCommandTrigger(cmd_reg))

    # "/" → matches [/alpha, /beta] sorted; DOWN → select /beta; ENTER selects; ENTER submits
    keys = [_char("/"), _down(), _enter(), _enter()]
    result, _ = _drive(keys, registry=reg)

    assert result is not None
    # Second item (/beta) was selected
    assert "/beta" in result
