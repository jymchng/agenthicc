"""Unit tests for the slash-command trigger (PRD-40).

Covers SlashCommandTrigger (get_matches, on_select, on_cancel, get_hint) and
CommandRegistry (register, dedup, groups, unregister, matches, len) including
the build_default_registry factory.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from agenthicc.tui.trigger import TriggerContext, MatchItem
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.tui.input_bar import CommandRegistry, CommandSpec, build_default_registry, BUILTIN_COMMANDS

pytestmark = pytest.mark.unit

CTX = TriggerContext(cwd=Path("."))


# ── Helpers ───────────────────────────────────────────────────────────────────


def _reg(*specs: CommandSpec) -> CommandRegistry:
    r = CommandRegistry()
    r.register_many(list(specs))
    return r


# ── SlashCommandTrigger.get_matches ───────────────────────────────────────────


def test_get_matches_empty_fragment_returns_all():
    reg = _reg(
        CommandSpec("/status", "Show status"),
        CommandSpec("/model", "Switch model"),
    )
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("", CTX)
    values = [m.value for m in matches]
    assert "/model" in values
    assert "/status" in values


def test_get_matches_filters_by_prefix():
    reg = _reg(
        CommandSpec("/deploy", "Deploy"),
        CommandSpec("/debug", "Debug"),
        CommandSpec("/cancel", "Cancel"),
    )
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("dep", CTX)
    values = [m.value for m in matches]
    assert "/deploy" in values
    assert "/debug" not in values
    assert "/cancel" not in values


def test_get_matches_no_registry_returns_empty():
    t = SlashCommandTrigger(None)
    matches = t.get_matches("", CTX)
    assert matches == []


def test_get_matches_empty_registry_returns_empty():
    t = SlashCommandTrigger(CommandRegistry())
    matches = t.get_matches("", CTX)
    assert matches == []


def test_get_matches_prefix_slash_included():
    """get_matches prepends '/' to the fragment before calling registry.matches."""
    reg = _reg(CommandSpec("/model", "Switch model"))
    t = SlashCommandTrigger(reg)
    # Fragment "mod" → partial "/mod" → matches "/model"
    matches = t.get_matches("mod", CTX)
    assert len(matches) == 1
    assert matches[0].value == "/model"


def test_get_matches_display_contains_name_and_description():
    reg = _reg(CommandSpec("/status", "Show running agents"))
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("", CTX)
    assert matches
    # display should contain both the command name and description
    assert "/status" in matches[0].display
    assert "Show running agents" in matches[0].display


def test_get_matches_sorted_alphabetically():
    reg = _reg(
        CommandSpec("/zebra", "Z command"),
        CommandSpec("/alpha", "A command"),
        CommandSpec("/middle", "M command"),
    )
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("", CTX)
    values = [m.value for m in matches]
    assert values == sorted(values)


# ── SlashCommandTrigger.on_select ────────────────────────────────────────────


def test_on_select_inserts_command():
    t = SlashCommandTrigger(_reg(CommandSpec("/model", "Switch")))
    item = MatchItem(display="/model  Switch", value="/model")
    buf = t.on_select(item, "mod", [])
    assert "".join(buf) == "/model"


def test_on_select_inserts_into_existing_buf():
    t = SlashCommandTrigger(_reg(CommandSpec("/model", "Switch")))
    item = MatchItem(display="/model  Switch", value="/model")
    buf = t.on_select(item, "mod", list("run "))
    assert "".join(buf) == "run /model"


def test_on_select_none_restores_literal():
    t = SlashCommandTrigger(CommandRegistry())
    buf = t.on_select(None, "dep", [])
    assert "".join(buf) == "/dep"


def test_on_select_none_empty_fragment():
    t = SlashCommandTrigger(CommandRegistry())
    buf = t.on_select(None, "", [])
    assert "".join(buf) == "/"


def test_on_select_none_preserves_existing_buf():
    t = SlashCommandTrigger(CommandRegistry())
    buf = t.on_select(None, "foo", list("hello "))
    assert "".join(buf) == "hello /foo"


# ── SlashCommandTrigger.on_cancel ────────────────────────────────────────────


def test_on_cancel_restores_slash_fragment():
    t = SlashCommandTrigger(CommandRegistry())
    buf = t.on_cancel("mod", [])
    assert "".join(buf) == "/mod"


def test_on_cancel_empty_fragment():
    t = SlashCommandTrigger(CommandRegistry())
    buf = t.on_cancel("", [])
    assert "".join(buf) == "/"


def test_on_cancel_preserves_existing_buf():
    t = SlashCommandTrigger(CommandRegistry())
    buf = t.on_cancel("cancel", list("please "))
    assert "".join(buf) == "please /cancel"


# ── SlashCommandTrigger.get_hint ─────────────────────────────────────────────


def test_get_hint_returns_argument_hint_when_set():
    reg = _reg(CommandSpec("/model", "Switch model", argument_hint="[provider] [model]"))
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("mod", CTX)
    assert matches
    hint = t.get_hint(matches[0])
    assert hint is not None
    assert "[provider]" in hint
    assert "[model]" in hint


def test_get_hint_contains_description():
    reg = _reg(CommandSpec("/model", "Switch model", argument_hint="[provider] [model]"))
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("mod", CTX)
    assert matches
    hint = t.get_hint(matches[0])
    assert "Switch model" in hint


def test_get_hint_no_argument_hint_still_returns_something():
    """Commands without argument_hint still get a hint (just name + description)."""
    reg = _reg(CommandSpec("/status", "Show status"))
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("", CTX)
    assert matches
    hint = t.get_hint(matches[0])
    assert hint is not None
    assert "/status" in hint


def test_get_hint_none_when_no_item():
    t = SlashCommandTrigger(CommandRegistry())
    assert t.get_hint(None) is None


def test_get_hint_none_when_item_hint_empty():
    """An item with an empty hint string causes get_hint to return None."""
    t = SlashCommandTrigger(CommandRegistry())
    item = MatchItem(display="x", value="x", hint="")
    assert t.get_hint(item) is None


# ── CommandRegistry ───────────────────────────────────────────────────────────


def test_command_registry_register_and_get():
    reg = CommandRegistry()
    spec = CommandSpec("/status", "Show status")
    reg.register(spec)
    result = reg.get("/status")
    assert result is not None
    assert result.name == "/status"


def test_command_registry_get_missing_returns_none():
    reg = CommandRegistry()
    assert reg.get("/missing") is None


def test_command_registry_dedup_last_wins():
    reg = CommandRegistry()
    reg.register(CommandSpec("/cmd", "v1"))
    reg.register(CommandSpec("/cmd", "v2"))
    assert reg.get("/cmd").description == "v2"


def test_command_registry_groups_order():
    reg = CommandRegistry()
    reg.register(CommandSpec("/a", "A", group="Built-in"))
    reg.register(CommandSpec("/b", "B", group="Skills"))
    reg.register(CommandSpec("/c", "C", group="MCP"))
    groups = reg.groups()
    assert "Built-in" in groups
    assert "Skills" in groups
    assert "MCP" in groups
    assert groups.index("Built-in") < groups.index("Skills")
    assert groups.index("Skills") < groups.index("MCP")


def test_command_registry_groups_only_present():
    """groups() only returns groups that actually have commands."""
    reg = CommandRegistry()
    reg.register(CommandSpec("/a", "A", group="Built-in"))
    groups = reg.groups()
    assert "Skills" not in groups
    assert "Plugins" not in groups
    assert "Built-in" in groups


def test_command_registry_unregister():
    reg = CommandRegistry()
    reg.register(CommandSpec("/foo", "Foo"))
    reg.unregister("/foo")
    assert reg.get("/foo") is None


def test_command_registry_unregister_removes_aliases():
    reg = CommandRegistry()
    reg.register(CommandSpec("/foo", "Foo", aliases=("/f",)))
    reg.unregister("/foo")
    assert reg.get("/f") is None


def test_command_registry_matches_partial():
    reg = CommandRegistry()
    reg.register(CommandSpec("/deploy", "Deploy"))
    reg.register(CommandSpec("/debug", "Debug"))
    reg.register(CommandSpec("/status", "Status"))
    results = reg.matches("/de")
    names = [c.name for c in results]
    assert "/deploy" in names
    assert "/debug" in names
    assert "/status" not in names


def test_command_registry_matches_full_slash():
    reg = CommandRegistry()
    reg.register(CommandSpec("/status", "Status"))
    reg.register(CommandSpec("/model", "Model"))
    results = reg.matches("/")
    assert len(results) == 2


def test_command_registry_matches_via_alias():
    reg = CommandRegistry()
    reg.register(CommandSpec("/status", "Status", aliases=("/st",)))
    results = reg.matches("/st")
    names = [c.name for c in results]
    assert "/status" in names


def test_command_registry_len():
    reg = CommandRegistry()
    assert len(reg) == 0
    reg.register(CommandSpec("/a", "A"))
    reg.register(CommandSpec("/b", "B"))
    assert len(reg) == 2


def test_command_registry_len_after_unregister():
    reg = CommandRegistry()
    reg.register(CommandSpec("/a", "A"))
    reg.register(CommandSpec("/b", "B"))
    reg.unregister("/a")
    assert len(reg) == 1


def test_command_registry_all_commands_sorted():
    reg = CommandRegistry()
    reg.register(CommandSpec("/zebra", "Z"))
    reg.register(CommandSpec("/alpha", "A"))
    names = [c.name for c in reg.all_commands()]
    assert names == sorted(names)


def test_command_registry_commands_for_group():
    reg = CommandRegistry()
    reg.register(CommandSpec("/a", "A", group="Built-in"))
    reg.register(CommandSpec("/b", "B", group="Skills"))
    reg.register(CommandSpec("/c", "C", group="Built-in"))
    builtin = reg.commands_for_group("Built-in")
    assert len(builtin) == 2
    assert all(c.group == "Built-in" for c in builtin)


def test_command_registry_register_many():
    reg = CommandRegistry()
    reg.register_many([
        CommandSpec("/a", "A"),
        CommandSpec("/b", "B"),
        CommandSpec("/c", "C"),
    ])
    assert len(reg) == 3


# ── build_default_registry ────────────────────────────────────────────────────


def test_build_default_registry_has_builtin_commands():
    reg = build_default_registry()
    assert len(reg) >= len(BUILTIN_COMMANDS)
    # Spot-check a few built-ins
    assert reg.get("/status") is not None
    assert reg.get("/model") is not None
    assert reg.get("/help") is not None


def test_build_default_registry_model_has_argument_hint():
    reg = build_default_registry()
    spec = reg.get("/model")
    assert spec is not None
    assert spec.argument_hint != ""
    assert "provider" in spec.argument_hint or "model" in spec.argument_hint


def test_build_default_registry_mcp_group():
    reg = build_default_registry()
    spec = reg.get("/mcp")
    assert spec is not None
    assert spec.group == "MCP"


def test_build_default_registry_groups_include_builtin():
    reg = build_default_registry()
    groups = reg.groups()
    assert "Built-in" in groups


# ── SlashCommandTrigger char ──────────────────────────────────────────────────


def test_slash_trigger_char():
    t = SlashCommandTrigger()
    assert t.char == "/"


def test_slash_trigger_no_registry_empty_matches():
    """SlashCommandTrigger with no registry produces no matches."""
    t = SlashCommandTrigger()
    assert t.get_matches("", CTX) == []
    assert t.get_matches("model", CTX) == []
