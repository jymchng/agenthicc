"""The declarative built-in CLI manifest and on-demand wiring (AGX-01)."""

from __future__ import annotations

import argparse
import importlib
import inspect
import pkgutil
import subprocess
import sys

import pytest

from agenthicc.cli import registry
from agenthicc.cli.manifest import BUILTIN_COMMANDS, BUILTIN_GROUPS
from agenthicc.cli.parser import parse_cli
from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout

pytestmark = pytest.mark.unit


def test_paths_are_unique_and_every_command_has_help() -> None:
    paths = [ref.path for ref in BUILTIN_COMMANDS]
    assert len(paths) == len(set(paths))
    assert all(ref.help.strip() for ref in BUILTIN_COMMANDS)
    assert all(help_text.strip() for help_text in BUILTIN_GROUPS.values())


def test_every_command_module_is_referenced_by_the_manifest() -> None:
    package = importlib.import_module("agenthicc.cli.commands")
    modules = {name for _, name, _ in pkgutil.iter_modules(package.__path__)}
    referenced = {ref.target.partition(":")[0].rsplit(".", 1)[1] for ref in BUILTIN_COMMANDS}
    assert referenced == modules


def test_every_target_is_a_callable_whose_signature_can_be_wired() -> None:
    for ref in BUILTIN_COMMANDS:
        entry = registry.load_entry(ref)
        assert callable(entry.handler), ref.target
        assert entry.is_async == inspect.iscoroutinefunction(entry.handler)
        registry._add_params(argparse.ArgumentParser(), entry.handler)


@cli_test_timeout(1)
def test_each_command_module_imports_on_its_own() -> None:
    """A module must not rely on another command module having been imported."""
    modules = sorted({ref.target.partition(":")[0] for ref in BUILTIN_COMMANDS})
    code = (
        "import importlib, sys\n"
        "for name in sys.argv[1:]:\n"
        "    for loaded in [m for m in sys.modules if m.startswith('agenthicc.cli.commands.')]:\n"
        "        del sys.modules[loaded]\n"
        "    importlib.import_module(name)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, *modules],
        capture_output=True,
        text=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_resolution_prefers_the_longest_known_path() -> None:
    index = registry.command_index()
    assert index.resolve(["agents", "status", "abc"]) == ("agents", "status")
    assert index.resolve(["agents"]) == ("agents",)
    assert index.resolve(["jobs"]) is None  # moved under agents (AGX-05)
    assert index.resolve(["config"]) == ("config",)
    assert index.resolve(["no-such-command"]) is None
    assert index.resolve([]) is None
    assert registry.leading_tokens(["agents", "--json", "x"]) == ["agents"]


def test_only_the_selected_command_is_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    index = registry.command_index()
    loaded: list[tuple[str, ...]] = []
    original = registry.load_entry

    def recording(source: object) -> registry._Entry:
        entry = original(source)  # type: ignore[arg-type]
        loaded.append(entry.path)
        return entry

    monkeypatch.setattr(registry, "load_entry", recording)
    parser = argparse.ArgumentParser()
    entry = registry.wire(parser, index, ("agents", "status"))
    assert entry is not None and entry.path == ("agents", "status")
    assert loaded == [("agents", "status")]
    namespace = parser.parse_args(["agents", "status", "abc", "--json"])
    assert namespace.session_id == "abc"
    assert namespace.json is True


def test_agents_takes_an_optional_session_beside_its_subcommands() -> None:
    index = registry.command_index()
    parser = argparse.ArgumentParser()
    entry = registry.wire(parser, index, ("agents",))
    assert entry is not None and entry.path == ("agents",)
    namespace = parser.parse_args(["agents", "abc123", "--tab", "history"])
    assert (namespace.session_id, namespace.tab) == ("abc123", "history")
    namespace = parser.parse_args(["agents"])
    assert namespace.session_id == ""


def test_group_help_lists_subcommands_without_importing_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(source: object) -> registry._Entry:
        raise AssertionError(f"group help imported {source!r}")

    monkeypatch.setattr(registry, "load_entry", refuse)
    monkeypatch.setattr(sys, "argv", ["agenthicc", "config", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        parse_cli()
    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "show" in output and "validate" in output


def test_a_group_without_a_subcommand_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["agenthicc", "config"])
    with pytest.raises(SystemExit) as exc_info:
        parse_cli()
    assert exc_info.value.code == 2
    assert "required" in capsys.readouterr().err


def test_an_unknown_command_lists_the_valid_choices(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["agenthicc", "no-such-command"])
    with pytest.raises(SystemExit) as exc_info:
        parse_cli()
    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "invalid choice" in error and "agents" in error


def test_a_plugin_command_overrides_the_builtin_at_the_same_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry, "_PLUGIN_COMMANDS", {})

    @registry.command("agents", help="custom agents")
    def custom_agents() -> None:
        return None

    index = registry.command_index()
    assert index.source_for(("agents",)) == "plugin"
    entry = registry.wire(argparse.ArgumentParser(), index, ("agents",))
    assert entry is not None and entry.handler is custom_agents
