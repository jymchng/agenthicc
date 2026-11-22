"""Unit tests for the slash-command trigger (PRD-40).

Covers SlashCommandTrigger (get_matches, on_select, on_enter, get_hint) and
CommandRegistry (register, dedup, groups, unregister, matches, len) including
the build_default_registry factory.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from agenthicc.tui.trigger import TriggerContext, MatchItem
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.tui.input.completions import (
    CommandRegistry,
    CommandSpec,
    build_default_registry,
    BUILTIN_COMMANDS,
)
from agenthicc.commands import (
    BUILTIN_COMMANDS as CANONICAL_BUILTIN_COMMANDS,
    build_builtin_registry,
)

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


def test_get_matches_rows_carry_name_and_description():
    reg = _reg(CommandSpec("/status", "Show running agents"))
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("", CTX)
    assert matches
    assert matches[0].label == "/status"
    assert matches[0].detail == "Show running agents"


def test_get_matches_sorted_alphabetically_within_a_group():
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
    from agenthicc.tui.trigger import TriggerResult

    t = SlashCommandTrigger(_reg(CommandSpec("/model", "Switch")))
    item = MatchItem(value="/model", label="/model", detail="Switch")
    result = t.on_select(item, "mod", [])
    assert isinstance(result, TriggerResult)
    assert "".join(result.buffer) == "/model"
    assert result.submit is False


def test_on_select_inserts_into_existing_buf():
    from agenthicc.tui.trigger import TriggerResult

    t = SlashCommandTrigger(_reg(CommandSpec("/model", "Switch")))
    item = MatchItem(value="/model", label="/model", detail="Switch")
    result = t.on_select(item, "mod", list("run "))
    assert isinstance(result, TriggerResult)
    assert "".join(result.buffer) == "run /model"


def test_on_select_none_restores_literal():
    from agenthicc.tui.trigger import TriggerResult

    t = SlashCommandTrigger(CommandRegistry())
    result = t.on_select(None, "dep", [])
    assert isinstance(result, TriggerResult)
    assert "".join(result.buffer) == "/dep"


def test_on_select_none_empty_fragment():
    from agenthicc.tui.trigger import TriggerResult

    t = SlashCommandTrigger(CommandRegistry())
    result = t.on_select(None, "", [])
    assert isinstance(result, TriggerResult)
    assert "".join(result.buffer) == "/"


def test_on_select_none_preserves_existing_buf():
    from agenthicc.tui.trigger import TriggerResult

    t = SlashCommandTrigger(CommandRegistry())
    result = t.on_select(None, "foo", list("hello "))
    assert isinstance(result, TriggerResult)
    assert "".join(result.buffer) == "hello /foo"


# ── SlashCommandTrigger.on_enter (TUX-04 §2.5) ──────────────────────────────


def _enter_registry():
    from agenthicc.commands import Command, UnifiedCommandRegistry

    registry = UnifiedCommandRegistry()
    registry.register(Command("/status", "Show status"))
    registry.register(Command("/help", "Help", argument_hint="[/command]"))
    registry.register(Command("/open", "Open a thing", argument_hint="<name> | list"))
    registry.register(
        Command("/env", "Pick an environment", completions_factory=lambda _p: ["dev", "prod"])
    )
    return registry


@pytest.mark.parametrize("name", ["/status", "/help"])
def test_on_enter_places_a_picked_command_without_running_it(name: str):
    t = SlashCommandTrigger(_enter_registry())
    result = t.on_enter(MatchItem(value=name, label=name), name[1:3], list("x\n"))
    assert "".join(result.buffer) == f"x\n{name}"
    assert result.submit is False and result.keep_open is False


@pytest.mark.parametrize("name", ["/status", "/help"])
def test_on_enter_sends_a_command_that_was_typed_in_full(name: str):
    t = SlashCommandTrigger(_enter_registry())
    result = t.on_enter(MatchItem(value=name, label=name), name[1:], [])
    assert "".join(result.buffer) == name
    assert result.submit is True


@pytest.mark.parametrize("name", ["/open", "/env"])
def test_on_enter_completes_a_command_that_needs_an_argument(name: str):
    t = SlashCommandTrigger(_enter_registry())
    result = t.on_enter(MatchItem(value=name, label=name), "", [])
    assert "".join(result.buffer) == f"{name} "
    assert result.keep_open is True and result.submit is False


def test_on_enter_places_a_chosen_argument_completion_without_running_it():
    t = SlashCommandTrigger(_enter_registry())
    result = t.on_enter(MatchItem(value="/env dev", label="/env dev"), "env d", [])
    assert "".join(result.buffer) == "/env dev"
    assert result.submit is False and result.keep_open is False


def test_on_enter_sends_an_argument_completion_typed_in_full():
    t = SlashCommandTrigger(_enter_registry())
    result = t.on_enter(MatchItem(value="/env dev", label="/env dev"), "env dev", [])
    assert result.submit is True


def test_on_enter_without_a_match_sends_the_literal_text():
    t = SlashCommandTrigger(_enter_registry())
    result = t.on_enter(None, "unknown", list("please "))
    assert "".join(result.buffer) == "please /unknown"
    assert result.submit is True


# ── SlashCommandTrigger.get_hint ─────────────────────────────────────────────


def test_get_hint_is_none_the_row_shows_its_detail():
    """TUX-05: the repeated hint line under the picker is gone."""
    reg = _reg(CommandSpec("/model", "Switch model", argument_hint="[provider] [model]"))
    t = SlashCommandTrigger(reg)
    assert t.get_hint(t.get_matches("mod", CTX)[0]) is None
    assert t.get_hint(None) is None


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
    reg.register(CommandSpec("/a", "A", group="Session"))
    reg.register(CommandSpec("/b", "B", group="Skills"))
    reg.register(CommandSpec("/c", "C", group="MCP"))
    groups = reg.groups()
    assert "Session" in groups
    assert "Skills" in groups
    assert "MCP" in groups
    assert groups.index("Session") < groups.index("Skills")
    assert groups.index("Skills") < groups.index("MCP")


def test_command_registry_groups_only_present():
    """groups() only returns groups that actually have commands."""
    reg = CommandRegistry()
    reg.register(CommandSpec("/a", "A", group="Session"))
    groups = reg.groups()
    assert "Skills" not in groups
    assert "Plugins" not in groups
    assert "Session" in groups


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
    reg.register(CommandSpec("/a", "A", group="Session"))
    reg.register(CommandSpec("/b", "B", group="Skills"))
    reg.register(CommandSpec("/c", "C", group="Session"))
    session = reg.commands_for_group("Session")
    assert len(session) == 2
    assert all(c.group == "Session" for c in session)


def test_command_registry_register_many():
    reg = CommandRegistry()
    reg.register_many(
        [
            CommandSpec("/a", "A"),
            CommandSpec("/b", "B"),
            CommandSpec("/c", "C"),
        ]
    )
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
    assert groups[:4] == ["Session", "Agents", "Workflows", "Configuration"]


def test_legacy_completion_registry_uses_canonical_command_objects():
    """The compatibility registry must not maintain a separate built-in list."""
    reg = build_default_registry()
    assert BUILTIN_COMMANDS is CANONICAL_BUILTIN_COMMANDS
    assert [cmd.name for cmd in reg.all_commands()] == sorted(
        cmd.name for cmd in CANONICAL_BUILTIN_COMMANDS
    )
    for cmd in CANONICAL_BUILTIN_COMMANDS:
        assert reg.get(cmd.name) is cmd


def test_picker_exposes_every_canonical_builtin_command():
    """Every canonical built-in, including session-intercepted commands, is visible."""
    trigger = SlashCommandTrigger(build_builtin_registry())
    matches = trigger.get_matches("", CTX)
    assert {match.value for match in matches} == {
        command.name for command in CANONICAL_BUILTIN_COMMANDS
    }
    assert "/workflow" in {match.value for match in matches}
    assert "/compact" in {match.value for match in matches}


@pytest.mark.parametrize(
    ("query", "value"),
    [
        ("workflow st", "/workflow stack"),
        ("workflow ret", "/workflow return --abandon"),
        ("rel", "/reload"),
        ("cat", "/catalog"),
        ("authored li", "/authored list"),
    ],
)
def test_picker_exposes_workflow_composition_controls(query: str, value: str) -> None:
    picker = SlashCommandTrigger(build_builtin_registry())
    assert value in {match.value for match in picker.get_matches(query, CTX)}


# ── SlashCommandTrigger char ──────────────────────────────────────────────────


def test_slash_trigger_char():
    t = SlashCommandTrigger()
    assert t.char == "/"


def test_slash_trigger_no_registry_empty_matches():
    """SlashCommandTrigger with no registry produces no matches."""
    t = SlashCommandTrigger()
    assert t.get_matches("", CTX) == []
    assert t.get_matches("model", CTX) == []
