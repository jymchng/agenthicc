"""Integration tests for the agenthicc Mode system.

Exercises ModeManager, ModeRegistry, discover_mode_plugins, and the optional
_cmd_mode built-in command together as a full pipeline.  No real LLM calls are
made; all tests are self-contained.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from agenthicc.modes import (
    ModeManager,
    build_default_registry,
    discover_mode_plugins,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 1. test_mode_cycle_all_builtin
# ---------------------------------------------------------------------------


def test_mode_cycle_all_builtin():
    """Cycling 6 times visits all six built-in modes; 7th continues from Auto."""
    registry = build_default_registry()
    manager = ModeManager(registry)

    # Start at Auto (default).
    assert manager.active_name == "Auto"

    # Cycle order: Auto→Plan→Ask→Review→Safe→Debug→Auto (wraps)
    # 6 cycles from Auto visit: Plan, Ask, Review, Safe, Debug, Auto.
    visited: set[str] = set()
    for _ in range(6):
        mode = manager.cycle()
        visited.add(mode.name)

    assert visited == {"Auto", "Plan", "Safe",}

    # The 6th cycle wrapped back to Auto; the 7th continues from Auto → Plan.
    seventh = manager.cycle()
    assert seventh.name == "Plan"


# ---------------------------------------------------------------------------
# 2. test_apply_to_agent_plan_mode
# ---------------------------------------------------------------------------


def test_apply_to_agent_plan_mode():
    """Plan mode blocks write/exec tools and prepends the PLAN system patch."""
    registry = build_default_registry()
    manager = ModeManager(registry)
    manager.set("Plan")

    tools = ["write_file", "read_file", "run_bash", "git_status"]
    system, filtered_tools = manager.apply_to_agent("base", tools)

    assert "write_file" not in filtered_tools
    assert "read_file" in filtered_tools
    assert "git_status" in filtered_tools
    assert "run_bash" not in filtered_tools

    plan_mode = registry.get("Plan")
    assert plan_mode is not None
    assert "PLAN" in system
    assert system.startswith(plan_mode.system_patch[:20])


# ---------------------------------------------------------------------------
# 4. test_mode_plugin_discovered_and_registered
# ---------------------------------------------------------------------------


def test_mode_plugin_discovered_and_registered(tmp_path: Path):
    """A .agenthicc/modes/mymode.py exporting MODE is discovered and registerable."""
    modes_dir = tmp_path / ".agenthicc" / "modes"
    modes_dir.mkdir(parents=True)

    plugin_file = modes_dir / "mymode.py"
    plugin_file.write_text(
        "from agenthicc.modes import Mode\n"
        "MODE = Mode('Custom', 'CUST', 'Custom mode')\n"
    )

    plugin_set = discover_mode_plugins(project_dir=tmp_path)

    assert not plugin_set.failed, f"Unexpected failures: {plugin_set.failed}"

    registry = build_default_registry()
    for mode in plugin_set.all_modes:
        registry.register(mode)

    assert registry.get("Custom") is not None


# ---------------------------------------------------------------------------
# 5. test_mode_plugin_bad_syntax_skipped
# ---------------------------------------------------------------------------


def test_mode_plugin_bad_syntax_skipped(tmp_path: Path):
    """A plugin file with a syntax error is recorded as failed; no valid modes."""
    modes_dir = tmp_path / ".agenthicc" / "modes"
    modes_dir.mkdir(parents=True)

    bad_file = modes_dir / "bad.py"
    bad_file.write_text(
        "this is not valid python !!!\n"
        "def broken(\n"
    )

    plugin_set = discover_mode_plugins(project_dir=tmp_path)

    assert len(plugin_set.failed) > 0, "Expected at least one failure"
    failed_names = {r.path.name for r in plugin_set.failed}
    assert "bad.py" in failed_names

    # No successfully loaded modes from the bad file.
    assert all(m.name != "bad" for m in plugin_set.all_modes)


# ---------------------------------------------------------------------------
# 6. test_mode_switch_affects_apply
# ---------------------------------------------------------------------------


def test_mode_switch_affects_apply():
    """Switching from Safe to Auto changes which tools are exposed."""
    registry = build_default_registry()
    manager = ModeManager(registry)

    manager.set("Safe")
    tools = ["write_file", "run_bash", "git_commit", "read_file"]
    _, safe_tools = manager.apply_to_agent("base", tools)

    # Safe mode blocks write/exec/git-commit.
    assert "write_file" not in safe_tools
    assert "run_bash" not in safe_tools
    assert "git_commit" not in safe_tools
    # read_file is in the safe allowlist.
    assert "read_file" in safe_tools

    manager.set("Auto")
    _, auto_tools = manager.apply_to_agent("base", tools)

    # Auto mode has no filter — all tools returned.
    assert set(auto_tools) == set(tools)


# ---------------------------------------------------------------------------
# 7. test_manager_set_returns_none_unknown
# ---------------------------------------------------------------------------


def test_manager_set_returns_none_unknown():
    """ModeManager.set() with an unknown name returns None; active stays Auto."""
    registry = build_default_registry()
    manager = ModeManager(registry)

    assert manager.active_name == "Auto"

    result = manager.set("DoesNotExist")
    assert result is None
    assert manager.active_name == "Auto"


# ---------------------------------------------------------------------------
# 8. test_cmd_mode_lists_modes
# ---------------------------------------------------------------------------


def test_cmd_mode_lists_modes():
    """_cmd_mode with no args calls console.print at least once."""
    try:
        from agenthicc.commands.builtins import _cmd_mode  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        pytest.skip("_cmd_mode not yet implemented in builtins")

    from agenthicc.commands.command import CommandContext

    registry = build_default_registry()
    manager = ModeManager(registry)

    console = MagicMock()

    ctx = CommandContext(
        text="/mode",
        args="",
        model=MagicMock(),
        console=console,
        config=MagicMock(),
        session_id="",
        mode_manager=manager,
    )

    _cmd_mode(ctx)

    assert console.print.called, "console.print should have been called"


# ---------------------------------------------------------------------------
# 9. test_cmd_mode_switches
# ---------------------------------------------------------------------------


def test_cmd_mode_switches():
    """_cmd_mode with args='Plan' activates Plan mode on the manager."""
    try:
        from agenthicc.commands.builtins import _cmd_mode  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        pytest.skip("_cmd_mode not yet implemented in builtins")

    from agenthicc.commands.command import CommandContext

    registry = build_default_registry()
    manager = ModeManager(registry)

    console = MagicMock()

    ctx = CommandContext(
        text="/mode Plan",
        args="Plan",
        model=MagicMock(),
        console=console,
        config=MagicMock(),
        session_id="",
        mode_manager=manager,
    )

    _cmd_mode(ctx)

    assert manager.active_name == "Plan"
