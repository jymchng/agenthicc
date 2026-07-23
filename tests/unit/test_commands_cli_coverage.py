"""Coverage for built-in slash commands and decorator-based CLI discovery."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.commands.builtins import (
    _cmd_commands,
    _cmd_init,
    _cmd_model,
    _cmd_mode,
    _cmd_replay,
    _cmd_skills,
    _make_skill_handler,
    build_builtin_registry,
)
from agenthicc.commands.command import CommandContext
from agenthicc.config import AgenthiccConfig
from agenthicc.skills.loader import SkillDef, SkillDiscoveryResult
from agenthicc.tui.runtime import ModeManager, ModeRegistry, RuntimeMode

pytestmark = pytest.mark.unit


def _command_context(tmp_path: Path) -> CommandContext:
    modes = ModeRegistry()
    modes.register(RuntimeMode("Auto", badge="A", description="automatic"))
    modes.register(RuntimeMode("Plan", badge="P", description="planning"))
    return CommandContext(
        text="",
        args="",
        model="anthropic/test",
        console=Console(record=True),
        config=AgenthiccConfig(),
        session_id="command-session",
        command_registry=build_builtin_registry(),
        mode_manager=ModeManager(modes),
    )


def test_builtin_commands_cover_status_model_mode_and_simple_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.commands import builtins

    ctx = _command_context(tmp_path)
    assert builtins._cmd_cancel(ctx)
    assert builtins._cmd_expand(ctx)
    assert builtins._cmd_history(ctx)
    assert builtins._cmd_mcp(ctx)
    assert builtins._cmd_status(ctx)
    assert builtins._cmd_clear(ctx)

    ctx.text = "/model unknown"
    ctx.args = "unknown"
    assert _cmd_model(ctx)
    ctx.text = "/model ollama local"
    ctx.args = "ollama local"
    assert _cmd_model(ctx)
    ctx.text = "/model unknown"
    ctx.args = "unknown"
    assert _cmd_model(ctx)
    ctx.text = "/models"
    ctx.args = ""
    assert _cmd_model(ctx)

    assert _cmd_mode(ctx)
    ctx.args = "Plan"
    assert _cmd_mode(ctx)
    ctx.args = "missing"
    assert _cmd_mode(ctx)
    assert _cmd_commands(ctx)
    ctx.command_registry = None
    assert _cmd_commands(ctx)
    ctx.args = "reload"
    ctx.reload_commands = lambda: (True, "reloaded")
    assert _cmd_commands(ctx)
    ctx.reload_commands = lambda: (_ for _ in ()).throw(RuntimeError("reload broke"))
    assert _cmd_commands(ctx)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        builtins,
        "build_bootstrap_plan",
        lambda cwd: SimpleNamespace(changed=False, exists=False, preview=lambda: ""),
        raising=False,
    )
    assert _cmd_init(ctx)


def test_builtin_replay_skills_and_skill_handler_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.skills import loader

    ctx = _command_context(tmp_path)
    pending: list[str] = []
    ctx.set_pending_replay = pending.append
    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log.find_latest_session_for_cwd", lambda: None
    )
    assert _cmd_replay(ctx)

    path = tmp_path / "conversation.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("agenthicc.tui.runtime.session_log.get_session_log_path", lambda sid: path)
    ctx.args = "session-id"
    assert _cmd_replay(ctx)
    assert pending == ["session-id"]

    skill = SkillDef("Coverage", "coverage", tmp_path, description="desc", _body="body")
    ctx.skills = {"coverage": skill}
    assert _cmd_skills(ctx)
    ctx.args = "wrong"
    assert _cmd_skills(ctx)
    ctx.args = "reload"
    ctx.reload_skills = lambda: SkillDiscoveryResult({"coverage": skill})
    assert _cmd_skills(ctx)
    ctx.reload_skills = lambda: (_ for _ in ()).throw(RuntimeError("reload failed"))  # type: ignore[assignment]
    assert _cmd_skills(ctx)

    monkeypatch.setattr(
        loader, "process_skill_body", lambda *args, **kwargs: "instructions", raising=False
    )
    monkeypatch.setattr(
        "agenthicc.skills.runner.process_skill_body", lambda *args, **kwargs: "instructions"
    )
    captured: list[str] = []
    ctx.set_pending_skill = captured.append
    handler = _make_skill_handler("coverage", skill)
    ctx.args = "one two"
    assert handler(ctx)
    assert captured and "instructions" in captured[0]

    denied = SkillDef("Denied", "denied", tmp_path, allowed_agents=("other",), _body="no")
    assert _make_skill_handler("denied", denied)(ctx)


def test_builtin_init_config_menu_and_help_factories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.commands import builtins

    ctx = _command_context(tmp_path)
    plan = SimpleNamespace(changed=True, exists=False, preview=lambda: "preview")
    monkeypatch.setattr("agenthicc.project_bootstrap.build_bootstrap_plan", lambda cwd: plan)
    monkeypatch.chdir(tmp_path)
    ctx.args = ""
    assert _cmd_init(ctx)
    ctx.args = "write"
    monkeypatch.setattr(
        "agenthicc.project_bootstrap.write_bootstrap_plan",
        lambda plan, force: tmp_path / "AGENTS.md",
    )
    assert _cmd_init(ctx)
    plan.exists = True
    ctx.args = "write"
    assert _cmd_init(ctx)
    ctx.args = "write --force"
    assert _cmd_init(ctx)
    from agenthicc.project_bootstrap import BootstrapError

    monkeypatch.setattr(
        "agenthicc.project_bootstrap.build_bootstrap_plan",
        lambda cwd: (_ for _ in ()).throw(BootstrapError("bad")),
    )
    assert _cmd_init(ctx)

    overlay = builtins._menu_config(ctx)
    help_overlay = builtins._help_menu(ctx)
    assert overlay.name and help_overlay.name


def test_cli_registry_decorators_tree_wire_and_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.cli import registry
    from agenthicc.cli.context import CLIContext

    old_registry = registry._REGISTRY.copy()
    old_groups = registry._GROUPS.copy()
    registry._REGISTRY.clear()
    registry._GROUPS.clear()
    calls: list[object] = []
    try:

        @registry.group("demo", help="demo commands")
        def _group() -> None:
            return None

        @registry.command("demo", "run", help="run it")
        def run(ctx: CLIContext, count: int, verbose: bool = False) -> None:
            calls.append((ctx, count, verbose))

        run.__annotations__["ctx"] = CLIContext

        @registry.command("demo", "async")
        async def async_cmd(ctx: CLIContext) -> None:
            """Asynchronous demo command."""
            calls.append("async")

        async_cmd.__annotations__["ctx"] = CLIContext

        tree = registry._as_tree()
        assert tree["demo"]["children"]["run"]["entry"] is not None
        parser = argparse.ArgumentParser()
        registry._wire(parser, tree)
        ns = parser.parse_args(["demo", "run", "4", "--verbose"])
        registry._call(ns._entry, SimpleNamespace(name="ctx"), ns)
        assert calls[0][1:] == ("4", True)
        ns = parser.parse_args(["demo", "async"])
        registry._call(ns._entry, SimpleNamespace(name="ctx"), ns)
        assert "async" in calls
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(old_registry)
        registry._GROUPS.clear()
        registry._GROUPS.update(old_groups)


def test_cli_registry_discovery_toml_and_trust_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.cli import registry

    old = registry._REGISTRY.copy()
    original_registry = registry._REGISTRY
    registry._REGISTRY.clear()
    try:
        user = tmp_path / "user" / "cli"
        user.mkdir(parents=True)
        (user / "plugin.py").write_text(
            "from agenthicc.cli.registry import command\n"
            "@command('user', 'hello')\n"
            'def hello():\n    """User hello."""\n    return None\n',
            encoding="utf-8",
        )
        registry._discover_directory(user, "user")
        assert ("user", "hello") in registry._REGISTRY

        cli_toml = user.parent / "cli.toml"
        cli_toml.write_text(
            "[[command]]\npath=['tool','run']\nrun='echo {value}'\n"
            "[[command.args]]\nname='value'\n",
            encoding="utf-8",
        )
        registry._load_toml_commands(cli_toml, "user")
        assert ("tool", "run") in registry._REGISTRY
        monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls_append(args))
        asyncio.run(registry._REGISTRY[("tool", "run")].handler(value="ok"))

        project = tmp_path / "project" / "cli"
        project.mkdir(parents=True)
        plugin = project / "project.py"
        plugin.write_text(
            "from agenthicc.cli.registry import command\n"
            "@command('project', 'hello')\n"
            'def hello():\n    """Project hello."""\n    return None\n',
            encoding="utf-8",
        )
        digest = "sha256:" + hashlib.sha256(plugin.read_bytes()).hexdigest()
        (project.parent / "trusted_cli.json").write_text(
            json.dumps({"files": {"cli/project.py": digest}}), encoding="utf-8"
        )
        monkeypatch.setattr(
            "agenthicc.cli.registry.load_config",
            lambda: SimpleNamespace(plugins=SimpleNamespace(auto_trust=False)),
            raising=False,
        )
        registry._maybe_load_trusted(project)
        assert ("project", "hello") in registry._REGISTRY

        class DuplicateEntries(dict[tuple[str, ...], object]):
            def items(self):  # type: ignore[no-untyped-def]
                return [
                    (("shadow",), registry._Entry(("shadow",), "u", lambda: None, False, "user")),
                    (
                        ("shadow",),
                        registry._Entry(("shadow",), "p", lambda: None, False, "project"),
                    ),
                ]

        registry._REGISTRY = DuplicateEntries()  # type: ignore[assignment]
        registry._check_shadows()
        with pytest.raises(SystemExit):
            registry._check_shadows(strict=True)
    finally:
        registry._REGISTRY = original_registry
        original_registry.clear()
        original_registry.update(old)


def calls_append(args: object) -> object:
    return SimpleNamespace(args=args)
