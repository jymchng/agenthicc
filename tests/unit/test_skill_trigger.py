"""Unit coverage for the dollar-prefixed explicit skill trigger."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.commands import Command, UnifiedCommandRegistry
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.input.completions import CommandSpec, CommandRegistry
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerResult
from agenthicc.tui.triggers.slash_command import SkillTrigger, SlashCommandTrigger
from agenthicc.tui.trigger import TriggerManager

pytestmark = pytest.mark.unit

CTX = TriggerContext(cwd=Path("."))


def _registry() -> UnifiedCommandRegistry:
    registry = UnifiedCommandRegistry()
    registry.register(Command("/status", "Show status"))
    registry.register(
        Command(
            "$review-code",
            "Review implementation changes",
            aliases=("$review", "$inspect"),
            group="Skills",
            source_id="skill:review-code",
        )
    )
    return registry


def test_skill_trigger_uses_dollar_and_filters_to_skill_entries() -> None:
    trigger = SkillTrigger(_registry())

    assert trigger.char == "$"
    assert trigger.label == "Skill"
    values = [item.value for item in trigger.get_matches("", CTX)]
    assert values == ["$review-code", "$review", "$inspect"]


def test_skill_trigger_matches_canonical_name_and_alias_prefix() -> None:
    trigger = SkillTrigger(_registry())

    assert [item.value for item in trigger.get_matches("review", CTX)] == [
        "$review-code",
        "$review",
    ]
    # Registry matching follows aliases while the picker keeps the canonical
    # command as the inserted value, just like the command picker.
    assert [item.value for item in trigger.get_matches("inspect", CTX)] == ["$inspect"]
    assert trigger.get_matches("status", CTX) == []


def test_slash_trigger_excludes_stale_slash_named_skill_records() -> None:
    registry = UnifiedCommandRegistry()
    registry.register(Command("/status", "Show status"))
    registry.register(
        Command("/old-review", "Old skill", group="Skills", source_id="skill:old-review")
    )

    values = [item.value for item in SlashCommandTrigger(registry).get_matches("", CTX)]
    assert values == ["/status"]


def test_skill_trigger_selection_and_cancel_preserve_dollar_prefix() -> None:
    trigger = SkillTrigger(CommandRegistry())
    item = MatchItem(display="$review-code  Review", value="$review-code")

    selected = trigger.on_select(item, "review", list("run "))
    assert isinstance(selected, TriggerResult)
    assert "".join(selected.buffer) == "run $review-code"
    assert selected.submit is False

    literal = trigger.on_select(None, "unknown", [])
    assert "".join(literal.buffer) == "$unknown"
    assert "".join(trigger.on_cancel("unknown", list("run "))) == "run $unknown"


def test_skill_trigger_reuses_picker_hint_and_wrapping() -> None:
    registry = UnifiedCommandRegistry()
    registry.register(
        Command(
            "$review-code",
            "A deliberately long description that should wrap in a narrow picker",
            argument_hint="[path]",
            group="Skills",
            source_id="skill:review-code",
        )
    )
    trigger = SkillTrigger(registry)
    item = trigger.get_matches("", CTX)[0]

    assert "$review-code" in item.hint
    assert "[path]" in item.hint
    assert len(trigger.get_lines(item, available_width=48)) > 1


def test_skill_trigger_has_same_line_boundary_activation_rules() -> None:
    trigger = SkillTrigger()

    assert trigger.can_activate([])
    assert trigger.can_activate(["\n"])
    assert not trigger.can_activate(list("ordinary text"))


def test_skill_trigger_has_no_registry_matches() -> None:
    assert SkillTrigger().get_matches("", CTX) == []


def test_skill_trigger_registers_and_resolves_through_trigger_manager() -> None:
    manager = TriggerManager()
    manager.register(SkillTrigger(_registry()))

    assert manager.resolve(Key.CHAR, "$") == "$"
    assert manager.get("$") is not None


def test_command_registry_adapter_keeps_skill_records_out_of_slash_matches() -> None:
    registry = CommandRegistry()
    registry.register(CommandSpec("/status", "Status"))
    registry.register(CommandSpec("$review", "Review", group="Skills"))

    assert [cmd.name for cmd in registry.matches("/")] == ["/status"]
    assert [cmd.name for cmd in registry.matches("$")] == ["$review"]
