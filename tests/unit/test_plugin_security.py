"""Digest-bound extension trust (PRD-250 S-02).

Opening an untrusted repository must import no project code: every loader of
Python extensions consults the trust policy before executing a file, trust is
bound to the file's content, and trust records never live in the repository.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenthicc.plugins.trust import (
    ExtensionTrust,
    current_extension_trust,
    override_extension_trust,
)

pytestmark = pytest.mark.unit

#: Every extension below records its module name here when it is imported.
_MARKER_ENV = "AGENTHICC_TEST_IMPORTED"


def _importing_source(extra: str = "") -> str:
    return (
        "import os\n"
        "from pathlib import Path\n"
        f"marker = Path(os.environ['{_MARKER_ENV}'])\n"
        "marker.write_text(marker.read_text() + __name__ + '\\n')\n" + extra
    )


@pytest.fixture
def strict_trust(tmp_path: Path) -> Iterator[ExtensionTrust]:
    """A production-shaped policy: only a fake home directory is trusted."""
    trust = ExtensionTrust(
        store_path=tmp_path / "home" / ".agenthicc" / "trust.json",
        trusted_roots=(tmp_path / "home" / ".agenthicc",),
    )
    with override_extension_trust(trust):
        yield trust


@pytest.fixture
def marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "imported.log"
    path.write_text("")
    monkeypatch.setenv(_MARKER_ENV, str(path))
    return path


def _repository(tmp_path: Path) -> Path:
    """A repository's ``.agenthicc`` shipping one extension of every kind."""
    root = tmp_path / "repo" / ".agenthicc"
    files = {
        "tools/tool.py": _importing_source("TOOLS = []\n"),
        "commands/command.py": _importing_source(),
        "workflows/flow/runner.py": _importing_source(),
        "workflows/flow/helpers.py": _importing_source(),
        "modes/mode.py": _importing_source(),
        "agents/agent.py": _importing_source(),
        "cli/plugin.py": _importing_source(),
    }
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


# ── trust records ─────────────────────────────────────────────────────────────


def test_trust_is_bound_to_file_content(tmp_path: Path, strict_trust: ExtensionTrust) -> None:
    plugin = tmp_path / "repo" / "tool.py"
    plugin.parent.mkdir()
    plugin.write_text("TOOLS = []\n")

    assert strict_trust.check(plugin, kind="tool") is not None
    record = strict_trust.trust(plugin, kind="tool")
    assert record.path == str(plugin.resolve())
    assert strict_trust.check(plugin, kind="tool") is None

    plugin.write_text("TOOLS = []  # edited\n")
    refusal = strict_trust.check(plugin, kind="tool")
    assert refusal is not None and refusal.reason == "changed since it was trusted"


def test_files_under_a_trusted_root_need_no_record(
    tmp_path: Path, strict_trust: ExtensionTrust
) -> None:
    own = tmp_path / "home" / ".agenthicc" / "tools" / "mine.py"
    own.parent.mkdir(parents=True)
    own.write_text("TOOLS = []\n")

    assert strict_trust.check(own, kind="tool") is None
    assert strict_trust.records() == ()


def test_revoke_and_records(tmp_path: Path, strict_trust: ExtensionTrust) -> None:
    plugin = tmp_path / "repo" / "tool.py"
    plugin.parent.mkdir()
    plugin.write_text("TOOLS = []\n")
    strict_trust.trust(plugin, kind="tool")

    assert [record.kind for record in strict_trust.records()] == ["tool"]
    assert strict_trust.revoke(plugin) is True
    assert strict_trust.revoke(plugin) is False
    assert strict_trust.check(plugin, kind="tool") is not None


def test_the_store_lives_outside_the_repository_and_is_private(
    tmp_path: Path, strict_trust: ExtensionTrust
) -> None:
    plugin = tmp_path / "repo" / "tool.py"
    plugin.parent.mkdir()
    plugin.write_text("TOOLS = []\n")
    strict_trust.trust(plugin, kind="tool")

    store = strict_trust.store_path
    assert not store.is_relative_to(tmp_path / "repo")
    assert store.stat().st_mode & 0o777 == 0o600
    assert json.loads(store.read_text())["version"] == 1


def test_a_repository_cannot_forge_its_own_trust(
    tmp_path: Path, strict_trust: ExtensionTrust
) -> None:
    root = _repository(tmp_path)
    plugin = root / "tools" / "tool.py"
    # A repository may ship the old trust manifests; nothing reads them.
    (root / "trusted_plugins.json").write_text(json.dumps({"trusted": {str(plugin): {}}}))
    (root / "trusted_cli.json").write_text(json.dumps({"files": {}}))

    assert strict_trust.check(plugin, kind="tool") is not None


def test_an_unreadable_store_trusts_nothing(tmp_path: Path, strict_trust: ExtensionTrust) -> None:
    plugin = tmp_path / "repo" / "tool.py"
    plugin.parent.mkdir()
    plugin.write_text("TOOLS = []\n")
    strict_trust.store_path.parent.mkdir(parents=True, exist_ok=True)
    strict_trust.store_path.write_text("{not json")

    assert strict_trust.check(plugin, kind="tool") is not None


def test_without_an_override_only_the_users_home_is_trusted() -> None:
    from agenthicc.plugins import trust as trust_module

    token = trust_module._CURRENT.set(None)
    try:
        policy = current_extension_trust()
    finally:
        trust_module._CURRENT.reset(token)

    assert policy.trusted_roots == ((Path.home() / ".agenthicc").resolve(),)
    assert policy.store_path == Path.home() / ".agenthicc" / "trust.json"


# ── loaders: an untrusted repository imports nothing ─────────────────────────


def test_opening_an_untrusted_repository_imports_no_project_code(
    tmp_path: Path,
    strict_trust: ExtensionTrust,
    marker: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.agents.registry import build_agents_registry
    from agenthicc.cli import registry as cli_registry
    from agenthicc.commands.plugin_loader import discover_command_plugins
    from agenthicc.modes.plugin_loader import discover_mode_plugins
    from agenthicc.plugins.discovery import discover_project_tools
    from agenthicc.workflows.registry import build_workflow_registry

    root = _repository(tmp_path)
    user = tmp_path / "home" / ".agenthicc"
    monkeypatch.setattr(cli_registry, "_PLUGIN_COMMANDS", dict(cli_registry._PLUGIN_COMMANDS))

    tools = discover_project_tools(project_dir=root, user_dir=user)
    commands = discover_command_plugins(project_dir=root, user_dir=user)
    # The mode loader takes the project root and home, not their .agenthicc.
    discover_mode_plugins(project_dir=root.parent, user_dir=user.parent)
    build_agents_registry(project_dir=root, user_dir=user)
    build_workflow_registry(project_dir=root, user_dir=user)
    cli_registry.discover_plugins(project_dir=root, user_dir=user)

    assert marker.read_text() == ""
    assert [result.untrusted for result in tools.results] == [True]
    assert [result.untrusted for result in commands.results] == [True]


def test_a_trusted_extension_loads_and_runs_once(
    tmp_path: Path, strict_trust: ExtensionTrust, marker: Path
) -> None:
    from agenthicc.plugins.discovery import discover_project_tools

    root = _repository(tmp_path)
    strict_trust.trust(root / "tools" / "tool.py", kind="tool")

    tools = discover_project_tools(project_dir=root, user_dir=tmp_path / "home" / ".agenthicc")

    assert [result.ok for result in tools.results] == [True]
    assert len(marker.read_text().splitlines()) == 1


def test_a_workflow_package_needs_every_file_trusted(
    tmp_path: Path, strict_trust: ExtensionTrust, marker: Path
) -> None:
    from agenthicc.workflows.loader import load_python_workflows

    root = _repository(tmp_path)
    package = root / "workflows" / "flow"
    strict_trust.trust(package / "runner.py", kind="workflow")

    assert load_python_workflows(package) == []
    assert marker.read_text() == ""


def test_trust_cli_lists_and_trusts_every_project_extension(
    tmp_path: Path,
    strict_trust: ExtensionTrust,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agenthicc.cli.commands.trust import project_extension_files, trust_add, trust_list

    root = _repository(tmp_path)
    monkeypatch.chdir(root.parent)
    files = project_extension_files(root.parent)
    assert {kind for _path, kind in files} == {
        "tool",
        "command",
        "workflow",
        "mode",
        "agent",
        "cli",
    }

    trust_list(None)  # type: ignore[arg-type]
    assert "not trusted" in capsys.readouterr().out

    trust_add(None)  # type: ignore[arg-type]
    assert all(strict_trust.check(path, kind=kind) is None for path, kind in files)


def test_each_loader_imports_its_extension_once_trusted(
    tmp_path: Path,
    strict_trust: ExtensionTrust,
    marker: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the loaders above really reach every file when trusted."""
    from agenthicc.agents.registry import build_agents_registry
    from agenthicc.cli import registry as cli_registry
    from agenthicc.cli.commands.trust import project_extension_files
    from agenthicc.commands.plugin_loader import discover_command_plugins
    from agenthicc.modes.plugin_loader import discover_mode_plugins
    from agenthicc.plugins.discovery import discover_project_tools
    from agenthicc.workflows.registry import build_workflow_registry

    root = _repository(tmp_path)
    user = tmp_path / "home" / ".agenthicc"
    for path, kind in project_extension_files(root.parent):
        strict_trust.trust(path, kind=kind)
    monkeypatch.setattr(cli_registry, "_PLUGIN_COMMANDS", dict(cli_registry._PLUGIN_COMMANDS))

    discover_project_tools(project_dir=root, user_dir=user)
    discover_command_plugins(project_dir=root, user_dir=user)
    # The mode loader takes the project root and home, not their .agenthicc.
    discover_mode_plugins(project_dir=root.parent, user_dir=user.parent)
    build_agents_registry(project_dir=root, user_dir=user)
    build_workflow_registry(project_dir=root, user_dir=user)
    cli_registry.discover_plugins(project_dir=root, user_dir=user)

    imported = marker.read_text().splitlines()
    assert any("plugin" in name and "workflow" not in name for name in imported), imported
    assert len(imported) >= 6, imported
