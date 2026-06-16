"""Unit tests for the mode plugin loader (plugin_loader.py).

Covers:
- Load single MODE export from a .py file
- Load MODES list export (multiple modes from one file)
- source_id auto-set from file stem when left as "builtin"
- SyntaxError captured in error field, ok=False
- Private files (_foo.py) skipped by discover
- Missing DEPENDENCIES produces missing_deps list
- File with no MODE/MODES silently skipped (ok=True, modes=[])
- Plugin appears in cycle after registering to registry
- Project-dir overrides user-dir: same name, project wins
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agenthicc.modes.plugin_loader import (
    load_mode_file,
    discover_modes,
    _scan_mode_directory,
)
from agenthicc.modes.registry import ModeRegistry
from agenthicc.modes.mode import Mode

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODE_HEADER = "from agenthicc.modes.mode import Mode\n"


def _write_mode_file(path: Path, content: str) -> Path:
    path.write_text(_MODE_HEADER + content)
    return path


# ---------------------------------------------------------------------------
# Load single MODE export
# ---------------------------------------------------------------------------


def test_load_single_mode_export(tmp_path):
    f = tmp_path / "mymode.py"
    _write_mode_file(
        f,
        "MODE = Mode(name='Alpha', label='ALPHA', description='Alpha mode')\n",
    )
    result = load_mode_file(f)
    assert result.ok
    assert len(result.modes) == 1
    assert result.modes[0].name == "Alpha"


def test_load_single_mode_label(tmp_path):
    f = tmp_path / "mymode.py"
    _write_mode_file(
        f,
        "MODE = Mode(name='Gamma', label='GAMMA', description='Gamma mode')\n",
    )
    result = load_mode_file(f)
    assert result.ok
    assert result.modes[0].label == "GAMMA"


def test_load_single_mode_is_mode_instance(tmp_path):
    f = tmp_path / "mymode.py"
    _write_mode_file(
        f,
        "MODE = Mode(name='Epsilon', label='EPS', description='Epsilon mode')\n",
    )
    result = load_mode_file(f)
    assert result.ok
    assert isinstance(result.modes[0], Mode)


# ---------------------------------------------------------------------------
# Load MODES list export (multiple modes)
# ---------------------------------------------------------------------------


def test_load_modes_list_export(tmp_path):
    f = tmp_path / "two_modes.py"
    _write_mode_file(
        f,
        "MODES = [\n"
        "    Mode(name='First', label='FIRST', description='First mode'),\n"
        "    Mode(name='Second', label='SECOND', description='Second mode'),\n"
        "]\n",
    )
    result = load_mode_file(f)
    assert result.ok
    assert len(result.modes) == 2
    names = [m.name for m in result.modes]
    assert "First" in names
    assert "Second" in names


def test_load_modes_list_preserves_order(tmp_path):
    f = tmp_path / "ordered.py"
    _write_mode_file(
        f,
        "MODES = [\n"
        "    Mode(name='A', label='A', description='A'),\n"
        "    Mode(name='B', label='B', description='B'),\n"
        "    Mode(name='C', label='C', description='C'),\n"
        "]\n",
    )
    result = load_mode_file(f)
    assert result.ok
    assert [m.name for m in result.modes] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# source_id auto-set from file stem
# ---------------------------------------------------------------------------


def test_source_id_set_from_file_stem_when_builtin(tmp_path):
    """When the plugin Mode has source_id='builtin', it is replaced with
    'mode-plugin:<stem>'."""
    f = tmp_path / "custom_ops.py"
    _write_mode_file(
        f,
        # source_id defaults to 'builtin' when not specified
        "MODE = Mode(name='CustomOps', label='COPS', description='Custom ops')\n",
    )
    result = load_mode_file(f)
    assert result.ok
    mode = result.modes[0]
    assert mode.source_id == "mode-plugin:custom_ops"


def test_source_id_preserved_when_explicitly_set(tmp_path):
    """When source_id is explicitly set to something other than 'builtin',
    it is kept as-is."""
    f = tmp_path / "external.py"
    _write_mode_file(
        f,
        "MODE = Mode(name='Ext', label='EXT', description='External', "
        "source_id='my-company:v1')\n",
    )
    result = load_mode_file(f)
    assert result.ok
    assert result.modes[0].source_id == "my-company:v1"


def test_source_id_uses_file_stem_not_full_name(tmp_path):
    """source_id uses the file stem (no .py extension)."""
    f = tmp_path / "enterprise_mode.py"
    _write_mode_file(
        f,
        "MODE = Mode(name='Ent', label='ENT', description='Enterprise')\n",
    )
    result = load_mode_file(f)
    assert result.ok
    assert result.modes[0].source_id == "mode-plugin:enterprise_mode"


# ---------------------------------------------------------------------------
# SyntaxError captured in error field, ok=False
# ---------------------------------------------------------------------------


def test_syntax_error_sets_error_field(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def bad syntax!!!!\n")
    result = load_mode_file(f)
    assert not result.ok
    assert result.error is not None
    assert "SyntaxError" in result.error


def test_syntax_error_ok_is_false(tmp_path):
    f = tmp_path / "broken2.py"
    f.write_text("x = (1 +\n")
    result = load_mode_file(f)
    assert result.ok is False


def test_syntax_error_modes_is_empty(tmp_path):
    f = tmp_path / "broken3.py"
    f.write_text("import (\n")
    result = load_mode_file(f)
    assert result.modes == []


# ---------------------------------------------------------------------------
# Private files skipped by discover
# ---------------------------------------------------------------------------


def test_scan_skips_private_files(tmp_path):
    """Files starting with _ are silently skipped."""
    (tmp_path / "_private.py").write_text(
        _MODE_HEADER
        + "MODE = Mode(name='Private', label='PRIV', description='Private')\n"
    )
    (tmp_path / "public.py").write_text(
        _MODE_HEADER
        + "MODE = Mode(name='Public', label='PUB', description='Public')\n"
    )
    results = _scan_mode_directory(tmp_path)
    loaded_names = [m.name for r in results for m in r.modes]
    assert "Public" in loaded_names
    assert "Private" not in loaded_names


def test_scan_skips_dunder_files(tmp_path):
    """__init__.py and other dunder files are skipped."""
    (tmp_path / "__init__.py").write_text(
        _MODE_HEADER
        + "MODE = Mode(name='Init', label='INIT', description='Init')\n"
    )
    (tmp_path / "valid.py").write_text(
        _MODE_HEADER
        + "MODE = Mode(name='Valid', label='VALID', description='Valid')\n"
    )
    results = _scan_mode_directory(tmp_path)
    loaded_names = [m.name for r in results for m in r.modes]
    assert "Valid" in loaded_names
    assert "Init" not in loaded_names


# ---------------------------------------------------------------------------
# Missing DEPENDENCIES produces missing_deps list
# ---------------------------------------------------------------------------


def test_missing_dependency_sets_missing_deps(tmp_path):
    f = tmp_path / "needs_dep.py"
    f.write_text(
        "DEPENDENCIES = ['this-package-does-not-exist-xyz-mode-test']\n"
        + _MODE_HEADER
        + "MODE = Mode(name='Guarded', label='GUARD', description='Needs dep')\n"
    )
    result = load_mode_file(f)
    assert not result.ok
    assert len(result.missing_deps) >= 1
    assert "this-package-does-not-exist-xyz-mode-test" in result.missing_deps


def test_missing_dependency_ok_is_false(tmp_path):
    f = tmp_path / "missing.py"
    f.write_text(
        "DEPENDENCIES = ['no-such-package-agenthicc-test-xyz']\n"
        + _MODE_HEADER
        + "MODE = Mode(name='M', label='M', description='M')\n"
    )
    result = load_mode_file(f)
    assert result.ok is False


def test_missing_dependency_modes_is_empty(tmp_path):
    """When deps are missing, no modes are returned even if MODE is valid."""
    f = tmp_path / "no_dep.py"
    f.write_text(
        "DEPENDENCIES = ['no-such-package-agenthicc-test-xyz']\n"
        + _MODE_HEADER
        + "MODE = Mode(name='M', label='M', description='M')\n"
    )
    result = load_mode_file(f)
    assert result.modes == []


def test_missing_dependency_using_mock(tmp_path):
    """Patch importlib.metadata.version to raise for testing purposes."""
    f = tmp_path / "mock_dep.py"
    f.write_text(
        "DEPENDENCIES = ['some-package']\n"
        + _MODE_HEADER
        + "MODE = Mode(name='Mocked', label='MOCK', description='Mocked dep')\n"
    )
    import importlib.metadata

    with patch.object(
        importlib.metadata,
        "version",
        side_effect=importlib.metadata.PackageNotFoundError("some-package"),
    ):
        result = load_mode_file(f)
    assert not result.ok
    assert result.missing_deps


# ---------------------------------------------------------------------------
# File with no MODE/MODES silently skipped (ok=True, modes=[])
# ---------------------------------------------------------------------------


def test_no_mode_export_ok_true(tmp_path):
    f = tmp_path / "no_export.py"
    f.write_text("x = 42\n")
    result = load_mode_file(f)
    assert result.ok is True


def test_no_mode_export_modes_empty(tmp_path):
    f = tmp_path / "no_export2.py"
    f.write_text("UNRELATED = 'value'\n")
    result = load_mode_file(f)
    assert result.modes == []


def test_no_mode_export_no_error(tmp_path):
    f = tmp_path / "no_export3.py"
    f.write_text("import os\nx = os.getcwd()\n")
    result = load_mode_file(f)
    assert result.error is None


# ---------------------------------------------------------------------------
# Plugin appears in cycle after registering to registry
# ---------------------------------------------------------------------------


def test_plugin_appears_in_cycle(tmp_path):
    f = tmp_path / "cycle_mode.py"
    _write_mode_file(
        f,
        "MODE = Mode(name='Custom', label='CUSTOM', description='Custom mode')\n",
    )
    result = load_mode_file(f)
    assert result.ok

    from agenthicc.modes.builtin import build_default_registry
    reg = build_default_registry()
    for mode in result.modes:
        reg.register(mode)

    # Custom should now be reachable in the cycle
    all_names = [m.name for m in reg.all_modes()]
    assert "Custom" in all_names


def test_plugin_in_cycle_reachable_from_debug(tmp_path):
    """After Debug, the next mode should be Custom (appended at end)."""
    f = tmp_path / "extra.py"
    _write_mode_file(
        f,
        "MODE = Mode(name='Extra', label='EXTRA', description='Extra mode')\n",
    )
    result = load_mode_file(f)
    assert result.ok

    from agenthicc.modes.builtin import build_default_registry
    reg = build_default_registry()
    for mode in result.modes:
        reg.register(mode)

    # Extra is appended after Debug; cycling from Debug should reach Extra
    assert reg.next_after("Debug").name == "Extra"
    # And Extra wraps back to Auto
    assert reg.next_after("Extra").name == "Auto"


# ---------------------------------------------------------------------------
# Project-dir overrides user-dir: same name, project wins
# ---------------------------------------------------------------------------


def test_project_overrides_user_same_name(tmp_path):
    """When both user and project dirs contain a mode with the same name,
    the project version (loaded last) wins when registered."""
    user_base = tmp_path / "user"
    project_base = tmp_path / "project"
    (user_base / "modes").mkdir(parents=True)
    (project_base / "modes").mkdir(parents=True)

    # User version
    (user_base / "modes" / "shared.py").write_text(
        _MODE_HEADER
        + "MODE = Mode(name='Shared', label='USER', description='User shared',"
        " source_id='user:v1')\n"
    )
    # Project version — should win
    (project_base / "modes" / "shared.py").write_text(
        _MODE_HEADER
        + "MODE = Mode(name='Shared', label='PROJ', description='Project shared',"
        " source_id='proj:v1')\n"
    )

    results = discover_modes(project_dir=project_base, user_dir=user_base)
    all_modes = [m for r in results for m in r.modes]

    # Both versions are discovered
    assert len(all_modes) == 2

    # Register in order (user first, project last) — project wins
    reg = ModeRegistry()
    for m in all_modes:
        reg.register(m)

    winner = reg.get("Shared")
    assert winner is not None
    assert winner.label == "PROJ"
    assert winner.source_id == "proj:v1"


def test_discover_loads_user_first_project_second(tmp_path):
    """discover_modes returns user results before project results."""
    user_base = tmp_path / "user"
    project_base = tmp_path / "project"
    (user_base / "modes").mkdir(parents=True)
    (project_base / "modes").mkdir(parents=True)

    (user_base / "modes" / "ua.py").write_text(
        _MODE_HEADER
        + "MODE = Mode(name='UserMode', label='U', description='User mode')\n"
    )
    (project_base / "modes" / "pb.py").write_text(
        _MODE_HEADER
        + "MODE = Mode(name='ProjMode', label='P', description='Project mode')\n"
    )

    results = discover_modes(project_dir=project_base, user_dir=user_base)
    names_in_order = [m.name for r in results for m in r.modes]
    assert names_in_order.index("UserMode") < names_in_order.index("ProjMode")


def test_project_only_no_user_dir(tmp_path):
    """discover_modes works when only project directory has plugins."""
    project_base = tmp_path / "proj"
    (project_base / "modes").mkdir(parents=True)
    (project_base / "modes" / "solo.py").write_text(
        _MODE_HEADER
        + "MODE = Mode(name='Solo', label='SOLO', description='Solo mode')\n"
    )
    results = discover_modes(project_dir=project_base, user_dir=tmp_path / "nonexistent")
    all_modes = [m for r in results for m in r.modes]
    assert len(all_modes) == 1
    assert all_modes[0].name == "Solo"
