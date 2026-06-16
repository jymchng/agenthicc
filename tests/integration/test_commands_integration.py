"""Integration tests for the full command dispatch pipeline (PRD-44, PRD-45, PRD-46).

Exercises build_builtin_registry, CommandDispatcher, SlashCommandTrigger, and
discover_command_plugins together to validate end-to-end behaviour.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from agenthicc.commands import build_builtin_registry, CommandDispatcher, Command
from agenthicc.commands.plugin_loader import discover_command_plugins

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 1. test_dispatch_status_calls_handler
# ---------------------------------------------------------------------------


def test_dispatch_status_calls_handler():
    """Dispatching /status returns True (handler exists and executes)."""
    reg = build_builtin_registry()
    disp = CommandDispatcher(reg)
    ctx = MagicMock()
    ctx.renderer = None
    ctx.args = ""
    ctx.session_id = ""
    # /status handler imports SlashCommandHandler — mock the renderer so it doesn't crash
    renderer = MagicMock()
    ctx.renderer = renderer
    result = disp.dispatch("/status", ctx)
    assert result is True


# ---------------------------------------------------------------------------
# 2. test_dispatch_config_opens_menu
# ---------------------------------------------------------------------------


def test_dispatch_config_opens_menu():
    """Dispatching /config with no args sets renderer._pending_menu."""
    from agenthicc.config import AgenthiccConfig

    reg = build_builtin_registry()
    disp = CommandDispatcher(reg)
    renderer = MagicMock()
    cfg = AgenthiccConfig()
    renderer._loaded_config = cfg
    ctx = MagicMock()
    ctx.renderer = renderer
    ctx.args = ""
    ctx.config = cfg
    ctx.session_id = ""
    result = disp.dispatch("/config", ctx)
    assert result is True
    assert renderer._pending_menu is not None


# ---------------------------------------------------------------------------
# 3. test_dispatch_unknown_returns_false
# ---------------------------------------------------------------------------


def test_dispatch_unknown_returns_false():
    """Dispatching an unknown command returns False."""
    reg = build_builtin_registry()
    disp = CommandDispatcher(reg)
    ctx = MagicMock()
    ctx.renderer = None
    ctx.args = ""
    ctx.session_id = ""
    result = disp.dispatch("/unknown-xyz", ctx)
    assert result is False


# ---------------------------------------------------------------------------
# 4. test_skill_appears_in_dropdown_after_registration
# ---------------------------------------------------------------------------


def test_skill_appears_in_dropdown_after_registration():
    """A skill command registered in the unified registry appears in the dropdown."""
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext

    reg = build_builtin_registry()
    reg.register(Command("/deep-research", "Deep research skill", group="Skills"))

    trigger = SlashCommandTrigger(reg)
    ctx = TriggerContext(cwd=Path("."))
    matches = trigger.get_matches("deep", ctx)
    assert any("/deep-research" in m.value for m in matches)


# ---------------------------------------------------------------------------
# 5. test_plugin_command_registered_and_dispatched
# ---------------------------------------------------------------------------


def test_plugin_command_registered_and_dispatched(tmp_path):
    """Plugin file with COMMAND is discovered, registered, and dispatchable."""
    cmds_dir = tmp_path / ".agenthicc" / "commands"
    cmds_dir.mkdir(parents=True)

    handler_calls = []

    # Write the plugin file
    plugin_file = cmds_dir / "test_cmd.py"
    plugin_file.write_text(
        "from agenthicc.commands import Command, CommandContext\n"
        "\n"
        "def _h(ctx: CommandContext) -> bool:\n"
        "    return True\n"
        "\n"
        "COMMAND = Command(\n"
        "    '/test-integration',\n"
        "    'Integration test command',\n"
        "    group='Custom',\n"
        "    handler=_h,\n"
        ")\n"
    )

    plugin_set = discover_command_plugins(project_dir=tmp_path / ".agenthicc")
    reg = build_builtin_registry()
    for cmd in plugin_set.all_commands:
        reg.register(cmd)

    disp = CommandDispatcher(reg)
    ctx = MagicMock()
    ctx.renderer = None
    ctx.args = ""
    ctx.session_id = ""

    result = disp.dispatch("/test-integration", ctx)
    assert result is True


# ---------------------------------------------------------------------------
# 6. test_project_command_overrides_user_global
# ---------------------------------------------------------------------------


def test_project_command_overrides_user_global(tmp_path):
    """Project-local command with the same name as a user-global one wins."""
    user_cmds = tmp_path / "user" / "commands"
    proj_cmds = tmp_path / "proj" / "commands"
    user_cmds.mkdir(parents=True)
    proj_cmds.mkdir(parents=True)

    (user_cmds / "deploy.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/deploy', 'User deploy', group='Custom')\n"
    )
    (proj_cmds / "deploy.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/deploy', 'Project deploy', group='Custom')\n"
    )

    plugin_set = discover_command_plugins(
        project_dir=tmp_path / "proj",
        user_dir=tmp_path / "user",
    )
    reg = build_builtin_registry()
    for cmd in plugin_set.all_commands:
        reg.register(cmd)

    resolved = reg.get("/deploy")
    assert resolved is not None
    assert resolved.description == "Project deploy"


# ---------------------------------------------------------------------------
# 7. test_slash_trigger_returns_all_groups
# ---------------------------------------------------------------------------


def test_slash_trigger_returns_all_groups():
    """SlashCommandTrigger.get_matches('') returns commands from all groups."""
    from agenthicc.commands import UnifiedCommandRegistry
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext

    reg = UnifiedCommandRegistry()
    reg.register(Command("/builtin-cmd", "Built-in", group="Built-in"))
    reg.register(Command("/skill-cmd", "Skill", group="Skills"))
    reg.register(Command("/custom-cmd", "Custom", group="Custom"))

    trigger = SlashCommandTrigger(reg)
    ctx = TriggerContext(cwd=Path("."))
    matches = trigger.get_matches("", ctx)
    values = [m.value for m in matches]
    assert "/builtin-cmd" in values
    assert "/skill-cmd" in values
    assert "/custom-cmd" in values


# ---------------------------------------------------------------------------
# 8. test_discovery_command_has_correct_source_id
# ---------------------------------------------------------------------------


def test_discovery_command_has_correct_source_id(tmp_path):
    """Plugin file COMMAND without explicit source_id gets source_id derived from stem."""
    cmds_dir = tmp_path / "commands"
    cmds_dir.mkdir(parents=True)

    (cmds_dir / "test_cmd.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/test-src', 'Test source id')\n"
    )

    plugin_set = discover_command_plugins(
        project_dir=tmp_path,
        user_dir=tmp_path / "user_nonexistent",
    )
    assert len(plugin_set.all_commands) == 1
    cmd = plugin_set.all_commands[0]
    assert cmd.source_id == "command-plugin:test_cmd"


# ---------------------------------------------------------------------------
# Additional: round-trip through registry with aliases
# ---------------------------------------------------------------------------


def test_builtin_commands_all_retrievable():
    """Every command in BUILTIN_COMMANDS can be retrieved from the built-in registry."""
    from agenthicc.commands import BUILTIN_COMMANDS

    reg = build_builtin_registry()
    for cmd in BUILTIN_COMMANDS:
        assert reg.get(cmd.name) is cmd, f"Could not retrieve built-in {cmd.name}"


def test_dispatch_with_args_uses_menu_factory(monkeypatch):
    """menu_factory always wins (PRD-70): args are passed to the factory via ctx.args."""
    menu_args = []
    handler_called = []

    reg = build_builtin_registry()
    reg.register(Command(
        "/dual",
        "Dual",
        handler=lambda ctx: handler_called.append(ctx.args) or True,
        menu_factory=lambda ctx: menu_args.append(ctx.args) or object(),
    ))

    disp = CommandDispatcher(reg)
    ctx = MagicMock()
    ctx.args = ""
    ctx.session_id = ""

    # menu_factory receives the args; handler is never called
    disp.dispatch("/dual somearg", ctx)
    assert menu_args == ["somearg"]
    assert handler_called == []
