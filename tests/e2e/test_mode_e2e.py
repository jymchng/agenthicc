"""E2E tests for the agenthicc Mode system.

Covers keyboard constants, cycling, TUI rendering, notification clearing,
tool restriction, session persistence, and full plugin flow.  No real LLM
calls are made.
"""
from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agenthicc.modes import (
    Mode,
    ModeManager,
    build_default_registry,
    discover_mode_plugins,
)
from agenthicc.tui.mention_input import Key

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# 1. test_shift_tab_key_constant
# ---------------------------------------------------------------------------


def test_shift_tab_key_constant():
    """Key.SHIFT_TAB has the string value 'SHIFT_TAB'."""
    assert Key.SHIFT_TAB == "SHIFT_TAB"


# ---------------------------------------------------------------------------
# 2. test_shift_tab_cycles_six_builtin_modes
# ---------------------------------------------------------------------------


def test_shift_tab_cycles_six_builtin_modes():
    """Cycling the manager 6 times visits all built-in modes; cycle wraps at 6."""
    registry = build_default_registry()
    manager = ModeManager(registry)

    assert manager.active_name == "Auto"

    # Cycle order: Auto→Plan→Ask→Review→Safe→Debug→Auto (wraps)
    # 6 cycles from Auto visit: Plan, Ask, Review, Safe, Debug, Auto.
    names: list[str] = []
    for _ in range(6):
        mode = manager.cycle()
        names.append(mode.name)

    assert set(names) == {"Auto", "Plan", "Ask", "Review", "Safe", "Debug"}

    # The 6th cycle reached Auto (wrap); the 7th continues Auto → Plan.
    seventh = manager.cycle()
    assert seventh.name == "Plan"


# ---------------------------------------------------------------------------
# 3. test_mode_footer_rendered_always
# ---------------------------------------------------------------------------


def test_mode_footer_rendered_always(capsys):
    """_redraw with mode_line always writes one footer line and returns n >= 1."""
    from agenthicc.tui.mention_input import _redraw
    from agenthicc.tui.trigger import MatchItem

    n = _redraw(
        "prompt> ",
        [],
        "",
        [],
        0,
        0,
        False,
        mode_line="⏵⏵ Auto  (shift+tab to cycle)",
    )

    assert n == 1

    captured = capsys.readouterr()
    assert "Auto" in captured.out


# ---------------------------------------------------------------------------
# 4. test_mode_footer_with_dropdown
# ---------------------------------------------------------------------------


def test_mode_footer_with_dropdown(capsys):
    """_redraw with mode_line + 3 dropdown items returns 1 + 3 = 4 lines."""
    from agenthicc.tui.mention_input import _redraw
    from agenthicc.tui.trigger import MatchItem

    items = [
        MatchItem(display=f"f{i}.py", value=f"f{i}.py", hint="")
        for i in range(3)
    ]

    n = _redraw(
        "prompt> ",
        [],
        "",
        items,
        0,
        0,
        True,
        mode_line="⏵⏵ Auto  (shift+tab to cycle)",
    )

    assert n == 1 + 3


# ---------------------------------------------------------------------------
# 5. test_mode_notification_clears
# ---------------------------------------------------------------------------


def test_mode_notification_clears():
    """Mode-switch notification is consumed on first call; subsequent calls return footer."""
    registry = build_default_registry()
    manager = ModeManager(registry)

    # Simulate the _get_mode_line closure from mention_input.py inline.
    _mode_notification: list[object] = [None]

    def _get_mode_line() -> str:
        notif = _mode_notification[0]
        if notif is not None:
            _mode_notification[0] = None
            return f"❖ Switched to {notif.name} mode"  # type: ignore[union-attr]
        if manager.active is None:
            return "⏵⏵ Auto  (shift+tab to cycle)"
        m = manager.active
        if m.name == "Auto":
            return "⏵⏵ Auto  (shift+tab to cycle)"
        return f"⏵⏵ {m.name}  (shift+tab to cycle)"

    # Switch to Plan and set the notification.
    plan_mode = manager.set("Plan")
    assert plan_mode is not None
    _mode_notification[0] = plan_mode

    # First call: returns the "Switched to Plan mode" notification.
    first = _get_mode_line()
    assert "Switched to Plan mode" in first

    # Second call: notification is cleared; returns normal footer.
    second = _get_mode_line()
    assert "Switched to Plan mode" not in second
    assert "Plan" in second or "shift+tab" in second


# ---------------------------------------------------------------------------
# 6. test_plan_mode_tool_restriction_e2e
# ---------------------------------------------------------------------------


def test_plan_mode_tool_restriction_e2e():
    """Plan mode blocks write_file and run_bash; allows read_file and git_diff."""
    registry = build_default_registry()
    manager = ModeManager(registry)
    manager.set("Plan")

    tools = ["write_file", "run_bash", "read_file", "git_diff"]
    _, filtered = manager.apply_to_agent("sys", tools)

    assert "write_file" not in filtered
    assert "run_bash" not in filtered
    assert "read_file" in filtered
    assert "git_diff" in filtered


# ---------------------------------------------------------------------------
# 7. test_mode_persists_across_two_turns
# ---------------------------------------------------------------------------


def test_mode_persists_across_two_turns():
    """Active mode (Review) persists across two apply_to_agent calls."""
    registry = build_default_registry()
    manager = ModeManager(registry)
    manager.set("Review")

    tools = ["read_file", "git_diff", "run_bash", "write_file"]

    _, turn1_tools = manager.apply_to_agent("base", tools)
    assert "run_bash" not in turn1_tools
    assert "read_file" in turn1_tools

    # Mode must remain Review between turns.
    assert manager.active_name == "Review"

    _, turn2_tools = manager.apply_to_agent("base", tools)
    assert "run_bash" not in turn2_tools
    assert "read_file" in turn2_tools

    assert manager.active_name == "Review"


# ---------------------------------------------------------------------------
# 8. test_mode_plugin_full_flow
# ---------------------------------------------------------------------------


def test_mode_plugin_full_flow(tmp_path: Path):
    """Full flow: write plugin, discover, register, apply — system patch present."""
    modes_dir = tmp_path / ".agenthicc" / "modes"
    modes_dir.mkdir(parents=True)

    plugin_file = modes_dir / "strict.py"
    plugin_file.write_text(
        "from agenthicc.modes import Mode\n"
        "MODE = Mode(\n"
        "    'Strict',\n"
        "    'STRICT',\n"
        "    'Enforce rules',\n"
        "    system_patch='[STRICT] be strict\\n',\n"
        ")\n"
    )

    plugin_set = discover_mode_plugins(project_dir=tmp_path)
    assert not plugin_set.failed, f"Plugin load errors: {plugin_set.failed}"

    registry = build_default_registry()
    for mode in plugin_set.all_modes:
        registry.register(mode)

    manager = ModeManager(registry)
    result = manager.set("Strict")
    assert result is not None, "Strict mode should be registered"

    system, _ = manager.apply_to_agent("base", ["write_file", "read_file"])
    assert "[STRICT]" in system
