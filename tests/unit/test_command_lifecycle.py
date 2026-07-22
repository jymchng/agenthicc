"""Unit tests for Command Lifecycle and Extension (PRD-45).

Tests cover source namespacing, bulk unregistration, plugin COMMANDS export,
skill handler factory, completions_factory field, and config menu dispatch.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Source namespacing
# ---------------------------------------------------------------------------


def test_source_id_on_command():
    """Command dataclass has a source_id field."""
    from agenthicc.commands import Command

    cmd = Command("/x", "X", source_id="skill:foo")
    assert cmd.source_id == "skill:foo"


def test_source_namespacing():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "a", source_id="skill:foo"))
    reg.register(Command("/b", "b", source_id="skill:foo"))
    reg.register(Command("/c", "c", source_id="builtin"))
    foo_cmds = reg.commands_for_source("skill:foo")
    assert len(foo_cmds) == 2


def test_commands_for_source_builtin():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "a", source_id="builtin"))
    reg.register(Command("/b", "b", source_id="plugin:x"))
    builtins = reg.commands_for_source("builtin")
    assert len(builtins) == 1
    assert builtins[0].name == "/a"


def test_unregister_source_count():
    """unregister_source returns the correct count of removed commands."""
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "a", source_id="plugin:x"))
    reg.register(Command("/b", "b", source_id="plugin:x"))
    reg.register(Command("/c", "c", source_id="builtin"))
    removed = reg.unregister_source("plugin:x")
    assert removed == 2


def test_unregister_source():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "a", source_id="plugin:x"))
    reg.register(Command("/b", "b", source_id="plugin:x"))
    removed = reg.unregister_source("plugin:x")
    assert removed == 2
    assert reg.get("/a") is None
    assert reg.get("/b") is None


def test_unregister_source_leaves_others():
    from agenthicc.commands import UnifiedCommandRegistry, Command

    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "a", source_id="plugin:x"))
    reg.register(Command("/c", "c", source_id="builtin"))
    reg.unregister_source("plugin:x")
    assert reg.get("/c") is not None


def test_unregister_nonexistent_source_returns_zero():
    from agenthicc.commands import UnifiedCommandRegistry

    reg = UnifiedCommandRegistry()
    removed = reg.unregister_source("nonexistent:xyz")
    assert removed == 0


# ---------------------------------------------------------------------------
# Completions factory field
# ---------------------------------------------------------------------------


def test_completions_factory_field():
    """Command accepts a completions_factory callable."""
    from agenthicc.commands import Command

    def _completions(fragment: str) -> list[str]:
        return ["anthropic", "openai"]

    cmd = Command("/model", "Switch model", completions_factory=_completions)
    assert cmd.completions_factory is _completions
    assert cmd.completions_factory("ant") == ["anthropic", "openai"]


def test_completions_factory_default_none():
    from agenthicc.commands import Command

    cmd = Command("/x", "X")
    assert cmd.completions_factory is None


# ---------------------------------------------------------------------------
# Plugin tool files exporting COMMANDS (via plugins.discovery._load_plugin_file)
# ---------------------------------------------------------------------------


def test_plugin_commands_export(tmp_path):
    """A plugin file with COMMANDS contributes them via LoadResult.commands."""
    plugin = tmp_path / "my_plugin.py"
    plugin.write_text(
        "from agenthicc.commands import Command\n"
        "TOOLS = []\n"
        "COMMANDS = [Command('/my-cmd', 'My command', source_id='plugin:my_plugin')]\n"
    )
    from agenthicc.plugins.discovery import _load_plugin_file

    result = _load_plugin_file(plugin)
    assert result.ok
    cmds = getattr(result, "commands", [])
    assert any(c.name == "/my-cmd" for c in cmds)


def test_command_plugin_commands_export(tmp_path):
    """LoadResult from plugin discovery has a .commands attribute."""
    plugin = tmp_path / "deployer.py"
    plugin.write_text(
        "from agenthicc.commands import Command\n"
        "TOOLS = []\n"
        "COMMANDS = [\n"
        "    Command('/deploy', 'Deploy', source_id='plugin:deployer'),\n"
        "]\n"
    )
    from agenthicc.plugins.discovery import _load_plugin_file

    result = _load_plugin_file(plugin)
    assert result.ok
    assert hasattr(result, "commands")
    assert len(result.commands) == 1
    assert result.commands[0].name == "/deploy"


# ---------------------------------------------------------------------------
# Skill handler factory
# ---------------------------------------------------------------------------


def test_skill_handler_sets_pending_skill():
    from agenthicc.commands.builtins import _make_skill_handler

    received = []
    skill = MagicMock()
    skill.path = MagicMock()
    ctx = MagicMock()
    ctx.args = ""
    ctx.session_id = ""
    ctx.console = MagicMock()
    ctx.set_pending_skill = received.append
    with patch("agenthicc.skills.runner.process_skill_body", return_value="body"):
        handler = _make_skill_handler("test-skill", skill)
        handler(ctx)
    assert len(received) == 1
    assert "body" in received[0]


def test_skill_handler_returns_true():
    from agenthicc.commands.builtins import _make_skill_handler

    skill = MagicMock()
    skill.path = MagicMock()
    ctx = MagicMock()
    ctx.args = "arg1 arg2"
    ctx.session_id = ""
    ctx.console = MagicMock()
    ctx.set_pending_skill = lambda _: None
    with patch("agenthicc.skills.runner.process_skill_body", return_value="processed"):
        handler = _make_skill_handler("my-skill", skill)
        result = handler(ctx)
    assert result is True


def test_skill_handler_passes_args():
    from agenthicc.commands.builtins import _make_skill_handler

    skill = MagicMock()
    skill.path = MagicMock()
    ctx = MagicMock()
    ctx.args = "foo bar"
    ctx.session_id = ""
    ctx.console = MagicMock()
    ctx.set_pending_skill = lambda _: None

    captured_args = []

    def _mock_process(s, args, cwd, session_id="", effort="medium"):
        captured_args.extend(args)
        return "result"

    with patch("agenthicc.skills.runner.process_skill_body", side_effect=_mock_process):
        handler = _make_skill_handler("my-skill", skill)
        handler(ctx)
    assert captured_args == ["foo", "bar"]


# ---------------------------------------------------------------------------
# Config menu dispatch
# ---------------------------------------------------------------------------


def test_config_command_sets_pending_menu():
    from agenthicc.commands import build_builtin_registry, CommandDispatcher
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
    disp.dispatch("/config", ctx)
    assert renderer._pending_menu is not None
