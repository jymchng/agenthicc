"""Unit tests for the built-in mode definitions.

Covers:
- build_default_registry() returns exactly 6 modes
- Mode names are: Auto, Plan, Ask, Review, Safe, Debug
- Auto: no tool_filter (all tools pass through)
- Plan: write_file blocked, read_file and git_status allowed
- Plan: system_patch contains "PLAN" and "MUST NOT"
- Ask: no tool_filter, system_patch contains "ASK"
- Review: run_bash blocked, git_diff and read_file allowed
- Safe: write_file blocked, run_bash blocked, read_file allowed
- Debug: no tool_filter, post_hook is not None
- Debug post_hook returns content appended with a string containing "DEBUG"
- Cycle order matches expected list [Auto, Plan, Ask, Review, Safe, Debug]
"""
from __future__ import annotations

import pytest

from agenthicc.modes.builtin import build_default_registry

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

_EXPECTED_ORDER = ["Auto", "Plan", "Ask", "Review", "Safe", "Debug"]


def _reg():
    """Return a fresh default registry for each test."""
    return build_default_registry()


# ---------------------------------------------------------------------------
# Registry count and names
# ---------------------------------------------------------------------------


def test_build_default_registry_returns_six_modes():
    reg = _reg()
    assert len(reg) == 6


def test_build_default_registry_mode_names():
    reg = _reg()
    names = [m.name for m in reg.all_modes()]
    assert names == _EXPECTED_ORDER


def test_all_six_names_present():
    reg = _reg()
    for name in _EXPECTED_ORDER:
        assert reg.get(name) is not None, f"Mode {name!r} missing from registry"


# ---------------------------------------------------------------------------
# Cycle order
# ---------------------------------------------------------------------------


def test_cycle_order_matches_expected():
    reg = _reg()
    current = "Auto"
    visited = []
    for _ in range(6):
        current = reg.next_after(current).name
        visited.append(current)
    assert visited == ["Plan", "Ask", "Review", "Safe", "Debug", "Auto"]


def test_cycle_wraps_from_debug_to_auto():
    reg = _reg()
    assert reg.next_after("Debug").name == "Auto"


# ---------------------------------------------------------------------------
# Auto mode
# ---------------------------------------------------------------------------


def test_auto_no_tool_filter():
    reg = _reg()
    auto = reg.get("Auto")
    assert auto is not None
    assert auto.tool_filter is None


def test_auto_write_file_passes_no_filter():
    """With no tool_filter, write_file is always allowed."""
    reg = _reg()
    auto = reg.get("Auto")
    # No filter means all tools pass
    assert auto.tool_filter is None


def test_auto_colour():
    reg = _reg()
    auto = reg.get("Auto")
    assert auto.colour == "green"


# ---------------------------------------------------------------------------
# Plan mode
# ---------------------------------------------------------------------------


def test_plan_has_tool_filter():
    reg = _reg()
    plan = reg.get("Plan")
    assert plan is not None
    assert plan.tool_filter is not None


def test_plan_blocks_write_file():
    reg = _reg()
    plan = reg.get("Plan")
    assert plan.tool_filter("write_file", {}) is False


def test_plan_allows_read_file():
    reg = _reg()
    plan = reg.get("Plan")
    assert plan.tool_filter("read_file", {}) is True


def test_plan_allows_git_status():
    reg = _reg()
    plan = reg.get("Plan")
    assert plan.tool_filter("git_status", {}) is True


def test_plan_blocks_run_bash():
    reg = _reg()
    plan = reg.get("Plan")
    assert plan.tool_filter("run_bash", {}) is False


def test_plan_system_patch_contains_plan():
    reg = _reg()
    plan = reg.get("Plan")
    assert "PLAN" in plan.system_patch


def test_plan_system_patch_contains_must_not():
    reg = _reg()
    plan = reg.get("Plan")
    assert "MUST NOT" in plan.system_patch


def test_plan_colour():
    reg = _reg()
    plan = reg.get("Plan")
    assert plan.colour == "yellow"


# ---------------------------------------------------------------------------
# Ask mode
# ---------------------------------------------------------------------------


def test_ask_no_tool_filter():
    reg = _reg()
    ask = reg.get("Ask")
    assert ask is not None
    assert ask.tool_filter is None


def test_ask_system_patch_contains_ask():
    reg = _reg()
    ask = reg.get("Ask")
    assert "ASK" in ask.system_patch


def test_ask_system_patch_non_empty():
    reg = _reg()
    ask = reg.get("Ask")
    assert ask.system_patch


def test_ask_colour():
    reg = _reg()
    ask = reg.get("Ask")
    assert ask.colour == "cyan"


# ---------------------------------------------------------------------------
# Review mode
# ---------------------------------------------------------------------------


def test_review_has_tool_filter():
    reg = _reg()
    review = reg.get("Review")
    assert review is not None
    assert review.tool_filter is not None


def test_review_blocks_run_bash():
    reg = _reg()
    review = reg.get("Review")
    assert review.tool_filter("run_bash", {}) is False


def test_review_allows_git_diff():
    reg = _reg()
    review = reg.get("Review")
    assert review.tool_filter("git_diff", {}) is True


def test_review_allows_read_file():
    reg = _reg()
    review = reg.get("Review")
    assert review.tool_filter("read_file", {}) is True


def test_review_blocks_write_file():
    reg = _reg()
    review = reg.get("Review")
    assert review.tool_filter("write_file", {}) is False


def test_review_colour():
    reg = _reg()
    review = reg.get("Review")
    assert review.colour == "blue"


# ---------------------------------------------------------------------------
# Safe mode
# ---------------------------------------------------------------------------


def test_safe_has_tool_filter():
    reg = _reg()
    safe = reg.get("Safe")
    assert safe is not None
    assert safe.tool_filter is not None


def test_safe_blocks_write_file():
    reg = _reg()
    safe = reg.get("Safe")
    assert safe.tool_filter("write_file", {}) is False


def test_safe_blocks_run_bash():
    reg = _reg()
    safe = reg.get("Safe")
    assert safe.tool_filter("run_bash", {}) is False


def test_safe_allows_read_file():
    reg = _reg()
    safe = reg.get("Safe")
    assert safe.tool_filter("read_file", {}) is True


def test_safe_colour():
    reg = _reg()
    safe = reg.get("Safe")
    assert safe.colour == "red"


# ---------------------------------------------------------------------------
# Debug mode
# ---------------------------------------------------------------------------


def test_debug_no_tool_filter():
    reg = _reg()
    debug = reg.get("Debug")
    assert debug is not None
    assert debug.tool_filter is None


def test_debug_post_hook_is_not_none():
    reg = _reg()
    debug = reg.get("Debug")
    assert debug.post_hook is not None


def test_debug_post_hook_returns_content_plus_debug():
    reg = _reg()
    debug = reg.get("Debug")
    result = debug.post_hook("my response", None)
    assert "my response" in result
    assert "DEBUG" in result


def test_debug_post_hook_appends_to_response():
    reg = _reg()
    debug = reg.get("Debug")
    original = "Original agent response."
    result = debug.post_hook(original, None)
    # Original content comes first, then debug info appended
    assert result.startswith(original)
    assert len(result) > len(original)


def test_debug_colour():
    reg = _reg()
    debug = reg.get("Debug")
    assert debug.colour == "magenta"


# ---------------------------------------------------------------------------
# All modes have labels
# ---------------------------------------------------------------------------


def test_all_modes_have_non_empty_labels():
    reg = _reg()
    for mode in reg.all_modes():
        assert mode.label, f"Mode {mode.name!r} has empty label"


def test_all_modes_have_descriptions():
    reg = _reg()
    for mode in reg.all_modes():
        assert mode.description, f"Mode {mode.name!r} has empty description"


def test_all_modes_have_builtin_source_id():
    reg = _reg()
    for mode in reg.all_modes():
        assert mode.source_id == "builtin", (
            f"Mode {mode.name!r} has unexpected source_id={mode.source_id!r}"
        )


def test_all_modes_badges_contain_ansi_reset():
    """Every badge string ends with ANSI reset."""
    reg = _reg()
    for mode in reg.all_modes():
        assert "\x1b[0m" in mode.badge, f"Mode {mode.name!r} badge missing ANSI reset"
