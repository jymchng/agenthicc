"""Unit tests for the Unified Command System (PRD-44).

Tests cover Command dataclass, UnifiedCommandRegistry, CommandDispatcher,
built-in registry, and SlashCommandTrigger integration.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Command dataclass
# ---------------------------------------------------------------------------


def test_command_register_and_get():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    cmd = Command("/test", "A test command", handler=lambda ctx: True)
    reg.register(cmd)
    assert reg.get("/test") is cmd


def test_command_opens_menu_flag():
    """opens_menu is True iff menu_factory is set."""
    from agenthicc.commands import Command

    cmd_no_menu = Command("/x", "no menu")
    cmd_with_menu = Command("/y", "with menu", menu_factory=lambda ctx: object())
    assert not cmd_no_menu.opens_menu
    assert cmd_with_menu.opens_menu


def test_command_display_row():
    from agenthicc.commands import Command

    cmd = Command("/foo", "Foo desc", argument_hint="[bar]")
    row = cmd.display_row()
    assert row == ("/foo", "[bar]", "Foo desc")


def test_command_default_group():
    from agenthicc.commands import Command

    cmd = Command("/x", "X")
    assert cmd.group == "Built-in"


def test_command_default_source_id():
    from agenthicc.commands import Command

    cmd = Command("/x", "X")
    assert cmd.source_id == "builtin"


def test_command_identifies_skill_namespace_by_group_or_source():
    from agenthicc.commands import Command

    assert Command("$review", "Review", group="Skills").is_skill is True
    assert Command("$review", "Review", source_id="skill:review").is_skill is True
    assert Command("/review", "Review", group="Plugins").is_skill is False


# ---------------------------------------------------------------------------
# UnifiedCommandRegistry — registration and lookup
# ---------------------------------------------------------------------------


def test_registry_register_and_get():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    cmd = Command("/ping", "Ping")
    reg.register(cmd)
    assert reg.get("/ping") is cmd


def test_registry_aliases():
    """Registering with aliases; get by alias resolves to the canonical command."""
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    cmd = Command("/configure", "Configure", aliases=("/cfg", "/conf"))
    reg.register(cmd)
    assert reg.get("/cfg") is cmd
    assert reg.get("/conf") is cmd
    assert reg.get("/configure") is cmd


def test_registry_unregister():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    cmd = Command("/tmp", "Temp")
    reg.register(cmd)
    reg.unregister("/tmp")
    assert reg.get("/tmp") is None


def test_registry_unregister_removes_aliases():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    cmd = Command("/configure", "Configure", aliases=("/cfg",))
    reg.register(cmd)
    reg.unregister("/configure")
    # Both canonical name and alias should be gone
    assert reg.get("/configure") is None
    assert reg.get("/cfg") is None


def test_registry_all_commands_sorted():
    """all_commands() returns commands sorted by name."""
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/z", "Z"))
    reg.register(Command("/a", "A"))
    reg.register(Command("/m", "M"))
    names = [c.name for c in reg.all_commands()]
    assert names == sorted(names)


def test_registry_len():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    assert len(reg) == 0
    reg.register(Command("/a", "A"))
    reg.register(Command("/b", "B"))
    assert len(reg) == 2


def test_registry_iter():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "A"))
    reg.register(Command("/b", "B"))
    names = {c.name for c in reg}
    assert names == {"/a", "/b"}


def test_registry_commands_for_group():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "A", group="Built-in"))
    reg.register(Command("/b", "B", group="Skills"))
    reg.register(Command("/c", "C", group="Built-in"))
    builtins = reg.commands_for_group("Built-in")
    assert len(builtins) == 2
    assert all(c.group == "Built-in" for c in builtins)


def test_registry_groups_order():
    """groups() returns Built-in before Skills before Plugins before MCP."""
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "A", group="MCP"))
    reg.register(Command("/b", "B", group="Skills"))
    reg.register(Command("/c", "C", group="Built-in"))
    groups = reg.groups()
    order = ["Built-in", "Skills", "MCP"]
    # Only groups that are present should appear; order must respect the defined ordering
    present = [g for g in order if g in groups]
    assert groups[: len(present)] == present


def test_registry_matches_partial():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/config", "Config"))
    reg.register(Command("/cancel", "Cancel"))
    reg.register(Command("/help", "Help"))
    matches = reg.matches("/con")
    names = [c.name for c in matches]
    assert "/config" in names
    assert "/cancel" not in names
    assert "/help" not in names


def test_registry_matches_via_alias():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/configure", "Configure", aliases=("/cfg",)))
    matches = reg.matches("/cfg")
    assert any(c.name == "/configure" for c in matches)


def test_registry_register_many():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    cmds = [Command("/a", "A"), Command("/b", "B"), Command("/c", "C")]
    reg.register_many(cmds)
    assert len(reg) == 3


def test_registry_replace_on_reregister():
    """Registering a command with the same name replaces the previous one."""
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/x", "First"))
    reg.register(Command("/x", "Second"))
    assert reg.get("/x").description == "Second"  # type: ignore[union-attr]
    assert len(reg) == 1


# ---------------------------------------------------------------------------
# Built-in registry
# ---------------------------------------------------------------------------


def test_builtin_registry_has_config():
    from agenthicc.commands import build_builtin_registry

    reg = build_builtin_registry()
    assert reg.get("/config") is not None


def test_builtin_registry_config_opens_menu():
    """/config command has opens_menu == True."""
    from agenthicc.commands import build_builtin_registry

    reg = build_builtin_registry()
    cfg_cmd = reg.get("/config")
    assert cfg_cmd is not None
    assert cfg_cmd.opens_menu is True


def test_builtin_registry_includes_config():
    from agenthicc.commands import build_builtin_registry

    reg = build_builtin_registry()
    cfg_cmd = reg.get("/config")
    assert cfg_cmd is not None
    assert cfg_cmd.opens_menu


def test_builtin_registry_has_standard_commands():
    from agenthicc.commands import build_builtin_registry

    reg = build_builtin_registry()
    for name in ("/help", "/status", "/model", "/skills", "/cancel", "/history"):
        assert reg.get(name) is not None, f"Expected {name} in built-in registry"


# ---------------------------------------------------------------------------
# SlashCommandTrigger with unified registry
# ---------------------------------------------------------------------------


def test_config_appears_in_slash_matches():
    from agenthicc.commands import build_builtin_registry
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext
    from pathlib import Path

    reg = build_builtin_registry()
    trigger = SlashCommandTrigger(reg)
    ctx = TriggerContext(cwd=Path("."))
    matches = trigger.get_matches("con", ctx)
    assert any("/config" in m.value for m in matches)


def test_skill_auto_registers_in_dollar_dropdown():
    from agenthicc.commands import UnifiedCommandRegistry, Command
    from agenthicc.tui.triggers.slash_command import SkillTrigger
    from agenthicc.tui.trigger import TriggerContext
    from pathlib import Path

    reg = UnifiedCommandRegistry()
    reg.register(
        Command("$git-summary", "Summarise git", group="Skills", source_id="skill:git-summary")
    )
    trigger = SkillTrigger(reg)
    ctx = TriggerContext(cwd=Path("."))
    matches = trigger.get_matches("git", ctx)
    assert any("$git-summary" in m.value for m in matches)


# ---------------------------------------------------------------------------
# CommandDispatcher
# ---------------------------------------------------------------------------


def test_dispatcher_calls_handler():
    from agenthicc.commands import UnifiedCommandRegistry, Command, CommandDispatcher

    called = []
    reg = UnifiedCommandRegistry()
    reg.register(Command("/ping", "Ping", handler=lambda ctx: called.append(ctx) or True))
    disp = CommandDispatcher(reg)
    ctx = MagicMock()
    ctx.renderer = None
    result = disp.dispatch("/ping", ctx)
    assert result is True
    assert len(called) == 1


def test_dispatcher_opens_menu_on_no_args():
    """Dispatcher calls set_pending_menu when command has menu_factory and no args."""
    from agenthicc.commands import UnifiedCommandRegistry, Command, CommandDispatcher

    widget = object()
    reg = UnifiedCommandRegistry()
    reg.register(Command("/cfg", "Config", menu_factory=lambda ctx: widget))
    disp = CommandDispatcher(reg)
    received = []
    ctx = MagicMock()
    ctx.set_pending_menu = received.append
    ctx.args = ""
    disp.dispatch("/cfg", ctx)
    assert received == [widget]


def test_dispatcher_menu_factory_wins_with_args():
    """menu_factory always fires when set, even when args are present (PRD-70).

    The factory receives ctx.args and decides what to render; handler is never
    called when menu_factory is set.
    """
    from agenthicc.commands import UnifiedCommandRegistry, Command, CommandDispatcher

    handler_called = []
    menu_args = []

    def _handler(ctx):
        handler_called.append(ctx.args)
        return True

    def _menu(ctx):
        menu_args.append(ctx.args)
        return object()

    reg = UnifiedCommandRegistry()
    reg.register(Command("/model", "Model", handler=_handler, menu_factory=_menu))
    disp = CommandDispatcher(reg)
    ctx = MagicMock()
    ctx.args = ""
    disp.dispatch("/model openai", ctx)
    # menu_factory received the args; handler was never called
    assert menu_args == ["openai"]
    assert handler_called == []


def test_dispatcher_returns_false_unknown():
    from agenthicc.commands import UnifiedCommandRegistry, CommandDispatcher

    disp = CommandDispatcher(UnifiedCommandRegistry())
    assert disp.dispatch("/unknown", MagicMock()) is False


def test_dispatcher_opens_menu():
    from agenthicc.commands import UnifiedCommandRegistry, Command, CommandDispatcher

    widget = object()
    reg = UnifiedCommandRegistry()
    reg.register(Command("/cfg", "Config", menu_factory=lambda ctx: widget))
    disp = CommandDispatcher(reg)
    received = []
    ctx = MagicMock()
    ctx.set_pending_menu = received.append
    ctx.args = ""
    disp.dispatch("/cfg", ctx)
    assert received == [widget]


def test_dispatcher_returns_false_for_unknown():
    from agenthicc.commands import UnifiedCommandRegistry, CommandDispatcher

    disp = CommandDispatcher(UnifiedCommandRegistry())
    assert disp.dispatch("/unknown", MagicMock()) is False
