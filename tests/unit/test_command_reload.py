"""Coverage for atomic live reload of user-defined slash commands."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from rich.console import Console

from agenthicc.commands import Command, CommandContext, CommandDispatcher, UnifiedCommandRegistry
from agenthicc.commands.builtins import build_builtin_registry
from agenthicc.commands.plugin_loader import CommandLoadResult, CommandPluginSet
from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.runners.reload import ReloadChange, ReloadReport, ReloadService
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.workflows.registry import WorkflowRegistry
from agenthicc.skills.loader import SkillDef

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping
    from agenthicc.runners.reload import ReloadSurface

pytestmark = pytest.mark.unit


def _command_context(
    *,
    output: StringIO,
    args: str = "",
) -> CommandContext:
    return CommandContext(
        text=f"/commands{(' ' + args) if args else ''}",
        args=args,
        model="test-model",
        console=Console(file=output, force_terminal=False),
        config=AgenthiccConfig(),
        command_registry=build_builtin_registry(),
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/commands reload", frozenset({"commands"})),
        ("/tools reload", frozenset({"tools"})),
        ("/workflows reload", frozenset({"workflows"})),
        ("/skills reload", frozenset({"skills"})),
        ("/reload tools skills", frozenset({"tools", "skills"})),
        ("/reload", frozenset({"tools", "skills", "commands", "workflows"})),
    ],
)
async def test_submitting_each_reload_form_calls_the_async_service(
    text: str, expected: frozenset[ReloadSurface]
) -> None:
    output = StringIO()
    registry = build_builtin_registry()
    context = _command_context(output=output)
    called: list[frozenset[ReloadSurface]] = []
    scheduled: list[Coroutine[object, object, None]] = []

    async def reload(surfaces: frozenset[ReloadSurface]) -> ReloadReport:
        called.append(surfaces)
        return ReloadReport(
            1,
            "command",
            surfaces,
            surfaces,
            tuple(ReloadChange(surface, "new", "added") for surface in surfaces),
        )

    context.reload = reload
    context.start_task = scheduled.append
    assert CommandDispatcher(registry).dispatch(text, context)
    assert len(scheduled) == 1
    await scheduled[0]
    assert called == [expected]
    assert "+1" in output.getvalue()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("/commands reload", "Reload is unavailable"),
        ("/reload nonsense", "Usage: /reload"),
    ],
)
def test_reload_reports_unavailable_context_or_invalid_surface(text: str, message: str) -> None:
    output = StringIO()
    context = _command_context(output=output)
    assert CommandDispatcher(build_builtin_registry()).dispatch(text, context)
    assert message in output.getvalue()


def _command_reloader(
    registry: UnifiedCommandRegistry,
    tmp_path: Path,
    *,
    skills: Mapping[str, SkillDef] | None = None,
    plugin_names: tuple[str, ...] = (),
) -> ReloadService:
    return ReloadService(
        processor=EventProcessor(initial_state=AppState.create(), persist=False),
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "user",
        workflow_registry=WorkflowRegistry(),
        command_registry=registry,
        subagent_registry=DEFAULT_REGISTRY.copy(),
        skills=skills,
        command_plugin_names=plugin_names,
    )


async def test_reload_replaces_plugins_preserves_builtin_skill_and_external_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_builtin_registry()
    registry.register(Command("/old", "Old version", aliases=("/legacy",), source_id="plugin:old"))
    registry.register(Command("/removed", "Remove me", source_id="plugin:removed"))
    registry.register(Command("/mcp-status", "MCP status", group="MCP", source_id="mcp:demo"))
    skill = SkillDef(name="Review", slug="review", path=tmp_path / "review")
    registry.register(Command("$review", "Review", group="Skills", source_id="skill:review"))
    reloader = _command_reloader(
        registry,
        tmp_path,
        skills={"review": skill},
        plugin_names=("/old", "/removed"),
    )

    replacement = Command(
        "/old",
        "New version",
        aliases=("/current",),
        group="Plugins",
        source_id="command-plugin:old",
    )
    source = tmp_path / "old.py"
    source.write_text("# updated command", encoding="utf-8")
    discovered = CommandPluginSet(results=[CommandLoadResult(path=source, commands=[replacement])])
    monkeypatch.setattr(
        "agenthicc.commands.plugin_loader.discover_command_plugins",
        lambda **_: discovered,
    )

    report = await reloader.reload(frozenset({"commands"}), reason="command")

    assert report.ok
    assert ReloadChange("commands", "/old", "updated") in report.changes
    assert ReloadChange("commands", "/removed", "removed") in report.changes
    assert registry.get("/old").description == "New version"  # type: ignore[union-attr]
    assert registry.get("/current") is registry.get("/old")
    assert registry.get("/legacy") is None
    assert registry.get("/removed") is None
    assert registry.get("/help") is not None
    assert registry.get("/mcp-status") is not None
    assert registry.get("$review") is not None
    await reloader.close()


async def test_reload_reads_disk_changes_and_rolls_back_malformed_plugin(
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
    reloader = _command_reloader(
        registry,
        tmp_path,
        plugin_names=tuple(command.name for command in initial.all_commands),
    )

    live_file.write_text(
        "from agenthicc.commands import Command\n"
        "COMMANDS = [\n"
        "    Command('/live', 'Version two', aliases=('/new-live',)),\n"
        "    Command('/added', 'Added now'),\n"
        "]\n"
    )
    removed_file.unlink()

    report = await reloader.reload(frozenset({"commands"}), reason="command")

    assert report.ok
    assert ReloadChange("commands", "/added", "added") in report.changes
    assert ReloadChange("commands", "/live", "updated") in report.changes
    assert ReloadChange("commands", "/removed", "removed") in report.changes
    assert registry.get("/live").description == "Version two"  # type: ignore[union-attr]
    assert registry.get("/new-live") is registry.get("/live")
    assert registry.get("/old-live") is None
    assert registry.get("/added") is not None
    assert registry.get("/removed") is None

    live_file.write_text("from agenthicc.commands import Command\nCOMMANDS = 'malformed'\n")

    report = await reloader.reload(frozenset({"commands"}), reason="command")

    assert not report.ok
    assert [item.code for item in report.diagnostics] == ["import_error"]
    assert registry.get("/live").description == "Version two"  # type: ignore[union-attr]
    assert registry.get("/new-live") is registry.get("/live")
    await reloader.close()


async def test_reload_failure_keeps_registry_aliases_and_plugin_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_builtin_registry()
    old = Command("/old", "Old", aliases=("/legacy",), source_id="plugin:old")
    registry.register(old)
    reloader = _command_reloader(registry, tmp_path, plugin_names=("/old",))
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

    report = await reloader.reload(frozenset({"commands"}), reason="command")

    assert not report.ok
    assert Path(report.diagnostics[0].path).name == "broken.py"
    assert registry.get("/old") is old
    assert registry.get("/legacy") is old
    await reloader.close()


async def test_reload_exception_keeps_registry_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_builtin_registry()
    old = Command("/old", "Old", source_id="plugin:old")
    registry.register(old)
    reloader = _command_reloader(registry, tmp_path, plugin_names=("/old",))

    def fail(**_kwargs: object) -> CommandPluginSet:
        raise OSError("commands directory unavailable")

    monkeypatch.setattr("agenthicc.commands.plugin_loader.discover_command_plugins", fail)

    report = await reloader.reload(frozenset({"commands"}), reason="command")

    assert not report.ok
    assert report.diagnostics[0].code == "import_error"
    assert registry.get("/old") is old
    await reloader.close()


def test_picker_offers_the_reload_command_and_registry_reload_arguments(tmp_path: Path) -> None:
    from agenthicc.tui.trigger import TriggerContext
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger

    registry = build_builtin_registry()
    trigger = SlashCommandTrigger(registry, WorkflowRegistry())
    context = TriggerContext(cwd=tmp_path)
    assert "/reload" in {item.value for item in trigger.get_matches("rel", context)}
    for name in ("skills", "commands", "tools", "workflow", "workflows"):
        matches = trigger.get_matches(f"{name} rel", context)
        assert [item.value for item in matches] == [f"/{name} reload"]


async def test_reload_command_reports_a_service_failure() -> None:
    output = StringIO()
    context = _command_context(output=output)
    scheduled: list[Coroutine[object, object, None]] = []

    async def unavailable(_surfaces: frozenset[ReloadSurface]) -> ReloadReport:
        raise RuntimeError("the reload worker stopped")

    context.reload = unavailable
    context.start_task = scheduled.append
    assert CommandDispatcher(build_builtin_registry()).dispatch("/tools reload", context)
    await scheduled[0]
    assert "Reload failed" in output.getvalue()
