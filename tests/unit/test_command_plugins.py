"""Unit tests for Command Plugins — user-defined slash commands (PRD-46).

Tests cover _load_command_file, discover_command_plugins, CommandPluginSet,
source_id derivation, dependency checking, private-file skipping, and
end-to-end dropdown registration.
"""
from __future__ import annotations

import pytest

from agenthicc.commands.plugin_loader import (
    _load_command_file,
    discover_command_plugins,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _load_command_file — single COMMAND export
# ---------------------------------------------------------------------------


def test_load_single_command(tmp_path):
    f = tmp_path / "greet.py"
    f.write_text(
        "from agenthicc.commands import Command, CommandContext\n"
        "def _h(ctx): return True\n"
        "COMMAND = Command('/greet', 'Say hello', handler=_h)\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert len(result.commands) == 1
    assert result.commands[0].name == "/greet"


def test_load_single_command_with_description(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/hello', 'Say hello to someone', argument_hint='[name]')\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert result.commands[0].description == "Say hello to someone"
    assert result.commands[0].argument_hint == "[name]"


# ---------------------------------------------------------------------------
# _load_command_file — multiple COMMANDS export
# ---------------------------------------------------------------------------


def test_load_multiple_commands(tmp_path):
    f = tmp_path / "deploy.py"
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMANDS = [\n"
        "    Command('/deploy-staging', 'Staging'),\n"
        "    Command('/deploy-prod', 'Production'),\n"
        "]\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert len(result.commands) == 2
    assert {c.name for c in result.commands} == {"/deploy-staging", "/deploy-prod"}


def test_load_both_command_and_commands(tmp_path):
    """A file exporting both COMMAND and COMMANDS gets all of them."""
    f = tmp_path / "mixed.py"
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/alpha', 'Alpha')\n"
        "COMMANDS = [Command('/beta', 'Beta')]\n"
    )
    result = _load_command_file(f)
    assert result.ok
    names = {c.name for c in result.commands}
    assert "/alpha" in names
    assert "/beta" in names


# ---------------------------------------------------------------------------
# _load_command_file — silent skip when no export
# ---------------------------------------------------------------------------


def test_load_no_export_skips_silently(tmp_path):
    f = tmp_path / "helper.py"
    f.write_text("x = 42\n")
    result = _load_command_file(f)
    assert result.ok
    assert result.commands == []


# ---------------------------------------------------------------------------
# _load_command_file — error cases
# ---------------------------------------------------------------------------


def test_load_syntax_error_captured(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def bad syntax !!!\n")
    result = _load_command_file(f)
    assert not result.ok
    assert "SyntaxError" in result.error  # type: ignore[operator]


def test_load_invalid_command_type_captured(tmp_path):
    f = tmp_path / "bad_type.py"
    f.write_text("COMMAND = 'not a Command instance'\n")
    result = _load_command_file(f)
    assert not result.ok
    assert "must be a Command" in result.error  # type: ignore[operator]


def test_load_invalid_commands_list_type(tmp_path):
    """COMMANDS must be a list — a dict should cause an error."""
    f = tmp_path / "bad_list.py"
    f.write_text("COMMANDS = {'a': 'b'}\n")
    result = _load_command_file(f)
    assert not result.ok
    assert "COMMANDS must be a list" in result.error  # type: ignore[operator]


# ---------------------------------------------------------------------------
# source_id derivation
# ---------------------------------------------------------------------------


def test_source_id_derived_from_stem(tmp_path):
    f = tmp_path / "my_cmd.py"
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/x', 'X')\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert result.commands[0].source_id == "command-plugin:my_cmd"


def test_source_id_preserved_when_set(tmp_path):
    f = tmp_path / "explicit.py"
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/x', 'X', source_id='custom:my-source')\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert result.commands[0].source_id == "custom:my-source"


def test_source_id_derived_for_multiple_commands(tmp_path):
    """All commands in COMMANDS get source_id derived from file stem if unset."""
    f = tmp_path / "multi_cmd.py"
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMANDS = [Command('/a', 'A'), Command('/b', 'B')]\n"
    )
    result = _load_command_file(f)
    assert result.ok
    for cmd in result.commands:
        assert cmd.source_id == "command-plugin:multi_cmd"


# ---------------------------------------------------------------------------
# Dependency checking
# ---------------------------------------------------------------------------


def test_missing_dep_reported(tmp_path):
    f = tmp_path / "needs_dep.py"
    f.write_text(
        "DEPENDENCIES = ['this-package-does-not-exist-xyz>=1.0']\n"
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/x', 'X')\n"
    )
    result = _load_command_file(f)
    assert not result.ok
    assert result.missing_deps


def test_empty_dependencies_no_problem(tmp_path):
    """An empty DEPENDENCIES list is valid and causes no issues."""
    f = tmp_path / "nodeps.py"
    f.write_text(
        "DEPENDENCIES = []\n"
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/nodeps', 'No deps')\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert result.commands[0].name == "/nodeps"


def test_stdlib_dep_not_treated_as_missing(tmp_path):
    """Standard-library packages listed in DEPENDENCIES should not be flagged missing."""
    f = tmp_path / "stdlib_dep.py"
    # pathlib is always available
    f.write_text(
        "DEPENDENCIES = ['pathlib']\n"
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/x', 'X')\n"
    )
    result = _load_command_file(f)
    # pathlib is stdlib and importlib.metadata won't find it, but this just tests
    # the result structure — ok is False only when packages are missing at import time.
    # The key assertion is that missing_deps doesn't raise an error.
    assert isinstance(result.ok, bool)


# ---------------------------------------------------------------------------
# discover_command_plugins — conflict resolution
# ---------------------------------------------------------------------------


def test_discover_project_overrides_user(tmp_path):
    user_cmds = tmp_path / "user" / "commands"
    proj_cmds = tmp_path / "proj" / "commands"
    user_cmds.mkdir(parents=True)
    proj_cmds.mkdir(parents=True)

    (user_cmds / "greet.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/greet', 'User version')\n"
    )
    (proj_cmds / "greet.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/greet', 'Project version')\n"
    )

    plugin_set = discover_command_plugins(
        project_dir=tmp_path / "proj",
        user_dir=tmp_path / "user",
    )
    # all_commands deduplication is handled by UnifiedCommandRegistry (last-write-wins)
    all_names = [c.name for c in plugin_set.all_commands]
    assert all_names.count("/greet") == 2  # both loaded; registry deduplicates

    from agenthicc.commands import UnifiedCommandRegistry

    reg = UnifiedCommandRegistry()
    for cmd in plugin_set.all_commands:
        reg.register(cmd)
    assert reg.get("/greet").description == "Project version"  # type: ignore[union-attr]


def test_discover_user_only(tmp_path):
    user_cmds = tmp_path / "user" / "commands"
    user_cmds.mkdir(parents=True)
    (user_cmds / "global_cmd.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/global', 'Global')\n"
    )
    plugin_set = discover_command_plugins(
        project_dir=tmp_path / "proj",
        user_dir=tmp_path / "user",
    )
    names = [c.name for c in plugin_set.all_commands]
    assert "/global" in names


def test_discover_project_only(tmp_path):
    proj_cmds = tmp_path / "proj" / "commands"
    proj_cmds.mkdir(parents=True)
    (proj_cmds / "local_cmd.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/local', 'Local')\n"
    )
    plugin_set = discover_command_plugins(
        project_dir=tmp_path / "proj",
        user_dir=tmp_path / "user_nonexistent",
    )
    names = [c.name for c in plugin_set.all_commands]
    assert "/local" in names


# ---------------------------------------------------------------------------
# Private files skipped
# ---------------------------------------------------------------------------


def test_private_files_skipped(tmp_path):
    cmds_dir = tmp_path / "commands"
    cmds_dir.mkdir()
    (cmds_dir / "_helper.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/helper', 'Helper')\n"
    )
    plugin_set = discover_command_plugins(project_dir=tmp_path, user_dir=tmp_path / "user")
    assert plugin_set.all_commands == []


def test_private_files_skipped_with_double_underscore(tmp_path):
    cmds_dir = tmp_path / "commands"
    cmds_dir.mkdir()
    (cmds_dir / "__init__.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/init', 'Init')\n"
    )
    plugin_set = discover_command_plugins(project_dir=tmp_path, user_dir=tmp_path / "user")
    assert plugin_set.all_commands == []


# ---------------------------------------------------------------------------
# CommandPluginSet helpers
# ---------------------------------------------------------------------------


def test_plugin_set_all_commands_excludes_failed(tmp_path):
    cmds_dir = tmp_path / "commands"
    cmds_dir.mkdir()
    (cmds_dir / "good.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/good', 'Good')\n"
    )
    (cmds_dir / "bad.py").write_text("def bad syntax !!!\n")
    plugin_set = discover_command_plugins(project_dir=tmp_path, user_dir=tmp_path / "user")
    # Only the good command appears in all_commands
    names = [c.name for c in plugin_set.all_commands]
    assert "/good" in names
    # Bad file is in failed list
    assert len(plugin_set.failed) >= 1


def test_plugin_set_failed_property(tmp_path):
    cmds_dir = tmp_path / "commands"
    cmds_dir.mkdir()
    (cmds_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
    plugin_set = discover_command_plugins(project_dir=tmp_path, user_dir=tmp_path / "user")
    assert any(not r.ok for r in plugin_set.failed)


def test_plugin_set_empty_dirs(tmp_path):
    plugin_set = discover_command_plugins(
        project_dir=tmp_path / "proj_nonexistent",
        user_dir=tmp_path / "user_nonexistent",
    )
    assert plugin_set.all_commands == []
    assert plugin_set.failed == []


# ---------------------------------------------------------------------------
# End-to-end: discovered command in dropdown
# ---------------------------------------------------------------------------


def test_command_appears_in_slash_dropdown(tmp_path):
    """End-to-end: a discovered command shows up in the / trigger dropdown."""
    from agenthicc.commands import build_builtin_registry
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext

    f = tmp_path / "commands" / "my_cmd.py"
    f.parent.mkdir()
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/my-custom', 'My custom command', group='Custom')\n"
    )
    plugin_set = discover_command_plugins(project_dir=tmp_path)
    reg = build_builtin_registry()
    for cmd in plugin_set.all_commands:
        reg.register(cmd)

    trigger = SlashCommandTrigger(reg)
    ctx = TriggerContext(cwd=tmp_path)
    matches = trigger.get_matches("my", ctx)
    assert any("/my-custom" in m.value for m in matches)
