"""Coverage for atomic live reload of user-defined slash commands."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.commands import Command, CommandContext, CommandDispatcher, UnifiedCommandRegistry
from agenthicc.commands.builtins import build_builtin_registry
from agenthicc.commands.plugin_loader import CommandLoadResult, CommandPluginSet
from agenthicc.config import AgenthiccConfig
from agenthicc.runners.tui_session import TUISession
from agenthicc.skills.loader import SkillDef

pytestmark = pytest.mark.unit


def _command_context(
    *,
    output: StringIO,
    reload_commands=None,
    args: str = "",
) -> CommandContext:
    return CommandContext(
        text=f"/commands{(' ' + args) if args else ''}",
        args=args,
        model="test-model",
        console=Console(file=output, force_terminal=False),
        config=AgenthiccConfig(),
        command_registry=build_builtin_registry(),
        reload_commands=reload_commands,
    )


def test_registry_replace_with_preserves_identity_and_removes_stale_aliases() -> None:
    current = UnifiedCommandRegistry()
    current.register(Command("/old", "Old", aliases=("/legacy",)))

    replacement = UnifiedCommandRegistry()
    replacement.register(Command("/new", "New", aliases=("/fresh",)))

    current.replace_with(replacement)

    assert current.get("/new") is not None
    assert current.get("/fresh") is current.get("/new")
    assert current.get("/old") is None
    assert current.get("/legacy") is None


def test_commands_reload_dispatches_callback_and_reports_result() -> None:
    output = StringIO()
    called: list[bool] = []
    context = _command_context(
        output=output,
        args="reload",
        reload_commands=lambda: (called.append(True) or True, "Commands reloaded — added: /hello"),
    )

    handled = CommandDispatcher(context.command_registry).dispatch("/commands reload", context)

    assert handled is True
    assert called == [True]
    assert "Commands reloaded" in output.getvalue()


def test_commands_reload_without_interactive_callback_is_handled() -> None:
    output = StringIO()
    context = _command_context(output=output, args="reload")

    handled = CommandDispatcher(context.command_registry).dispatch("/commands reload", context)

    assert handled is True
    assert "only available in an interactive session" in output.getvalue()


def _reload_session(registry: UnifiedCommandRegistry, *, skills=None, plugin_names=None):
    context = SimpleNamespace(
        cmd_registry=registry,
        skills=skills or {},
        command_plugin_names=set(plugin_names or set()),
    )
    session = object.__new__(TUISession)
    session._ctx = context
    return session, context


def test_reload_replaces_plugins_preserves_builtin_skill_and_external_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_builtin_registry()
    registry.register(Command("/old", "Old version", aliases=("/legacy",), source_id="plugin:old"))
    registry.register(Command("/removed", "Remove me", source_id="plugin:removed"))
    registry.register(Command("/mcp-status", "MCP status", group="MCP", source_id="mcp:demo"))
    skill = SkillDef(name="Review", slug="review", path=tmp_path / "review")
    registry.register(Command("/review", "Review", group="Skills", source_id="skill:review"))
    session, context = _reload_session(
        registry,
        skills={"review": skill},
        plugin_names={"/old", "/removed"},
    )

    replacement = Command(
        "/old",
        "New version",
        aliases=("/current",),
        group="Plugins",
        source_id="command-plugin:old",
    )
    discovered = CommandPluginSet(
        results=[CommandLoadResult(path=tmp_path / "commands" / "old.py", commands=[replacement])]
    )
    monkeypatch.setattr(
        "agenthicc.commands.plugin_loader.discover_command_plugins",
        lambda **_: discovered,
    )
    original_registry = context.cmd_registry

    ok, message = session._reload_commands()

    assert ok is True
    assert "updated: /old" in message
    assert "removed: /removed" in message
    assert context.cmd_registry is original_registry
    assert context.cmd_registry.get("/old").description == "New version"  # type: ignore[union-attr]
    assert context.cmd_registry.get("/current") is context.cmd_registry.get("/old")
    assert context.cmd_registry.get("/legacy") is None
    assert context.cmd_registry.get("/removed") is None
    assert context.cmd_registry.get("/help") is not None
    assert context.cmd_registry.get("/mcp-status") is not None
    assert context.cmd_registry.get("/review") is not None
    assert context.command_plugin_names == {"/old"}


def test_reload_reads_disk_changes_and_rolls_back_malformed_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    commands_dir = tmp_path / ".agenthicc" / "commands"
    commands_dir.mkdir(parents=True)

    live_file = commands_dir / "live.py"
    live_file.write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/live', 'Version one', aliases=('/old-live',))\n"
    )
    removed_file = commands_dir / "removed.py"
    removed_file.write_text(
        "from agenthicc.commands import Command\nCOMMAND = Command('/removed', 'Removed later')\n"
    )

    from agenthicc.commands.plugin_loader import discover_command_plugins

    initial = discover_command_plugins(
        project_dir=Path(".agenthicc"), user_dir=Path.home() / ".agenthicc"
    )
    registry = build_builtin_registry()
    registry.register_many(initial.all_commands)
    session, context = _reload_session(
        registry,
        plugin_names={command.name for command in initial.all_commands},
    )
    original_registry = context.cmd_registry

    live_file.write_text(
        "from agenthicc.commands import Command\n"
        "COMMANDS = [\n"
        "    Command('/live', 'Version two', aliases=('/new-live',)),\n"
        "    Command('/added', 'Added now'),\n"
        "]\n"
    )
    removed_file.unlink()

    ok, message = session._reload_commands()

    assert ok is True
    assert "added: /added" in message
    assert "updated: /live" in message
    assert "removed: /removed" in message
    assert context.cmd_registry is original_registry
    assert context.cmd_registry.get("/live").description == "Version two"  # type: ignore[union-attr]
    assert context.cmd_registry.get("/new-live") is context.cmd_registry.get("/live")
    assert context.cmd_registry.get("/old-live") is None
    assert context.cmd_registry.get("/added") is not None
    assert context.cmd_registry.get("/removed") is None

    live_file.write_text("from agenthicc.commands import Command\nCOMMANDS = 'malformed'\n")

    ok, message = session._reload_commands()

    assert ok is False
    assert "existing commands kept" in message
    assert "COMMANDS must be a list" in message
    assert context.cmd_registry is original_registry
    assert context.cmd_registry.get("/live").description == "Version two"  # type: ignore[union-attr]
    assert context.cmd_registry.get("/new-live") is context.cmd_registry.get("/live")
    assert context.command_plugin_names == {"/live", "/added"}


def test_reload_failure_keeps_registry_aliases_and_plugin_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_builtin_registry()
    old = Command("/old", "Old", aliases=("/legacy",), source_id="plugin:old")
    registry.register(old)
    session, context = _reload_session(registry, plugin_names={"/old"})
    discovered = CommandPluginSet(
        results=[
            CommandLoadResult(
                path=tmp_path / "commands" / "broken.py",
                error="SyntaxError: invalid syntax",
            )
        ]
    )
    monkeypatch.setattr(
        "agenthicc.commands.plugin_loader.discover_command_plugins",
        lambda **_: discovered,
    )
    original_registry = context.cmd_registry

    ok, message = session._reload_commands()

    assert ok is False
    assert "existing commands kept" in message
    assert "broken.py" in message
    assert context.cmd_registry is original_registry
    assert context.cmd_registry.get("/old") is old
    assert context.cmd_registry.get("/legacy") is old
    assert context.command_plugin_names == {"/old"}


def test_reload_exception_keeps_registry_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_builtin_registry()
    old = Command("/old", "Old", source_id="plugin:old")
    registry.register(old)
    session, context = _reload_session(registry, plugin_names={"/old"})

    def fail(**_kwargs):
        raise OSError("commands directory unavailable")

    monkeypatch.setattr("agenthicc.commands.plugin_loader.discover_command_plugins", fail)
    original_registry = context.cmd_registry

    ok, message = session._reload_commands()

    assert ok is False
    assert "commands directory unavailable" in message
    assert context.cmd_registry is original_registry
    assert context.cmd_registry.get("/old") is old
    assert context.command_plugin_names == {"/old"}
