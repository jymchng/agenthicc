"""Unit tests for the core Mode system: Mode, ModeRegistry, ModeManager.

Covers:
- Mode.badge ANSI colour codes for all six supported colours
- ModeRegistry.register, get, all_modes basic operations
- ModeRegistry.next_after cycling and wrapping
- ModeRegistry.register replaces by name while preserving cycle order
- ModeRegistry.unregister_source removes only matching source_id entries
- ModeManager default is Auto (or first mode when Auto absent)
- ModeManager.cycle() advances through modes and wraps back
- ModeManager.set() switches to a named mode; returns None for unknown names
- ModeManager.apply_to_agent() prepends system_patch to the system prompt
- ModeManager.apply_to_agent() applies tool_filter to the tool list
- ModeManager.apply_to_agent() with no tool_filter returns all tools unchanged
"""
from __future__ import annotations

import pytest

from agenthicc.modes.mode import Mode
from agenthicc.modes.registry import ModeRegistry
from agenthicc.modes import ModeManager

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RESET = "\x1b[0m"


def _make_mode(
    name: str,
    label: str | None = None,
    colour: str = "white",
    source_id: str = "builtin",
    system_patch: str = "",
    tool_filter=None,
) -> Mode:
    return Mode(
        name=name,
        label=label or name.upper(),
        description=f"{name} mode",
        colour=colour,
        system_patch=system_patch,
        tool_filter=tool_filter,
        source_id=source_id,
    )


def _make_tool(name: str):
    """Return a plain function with __name__ set to *name*."""
    def fn(): ...
    fn.__name__ = name
    return fn


def _make_registry(*names_and_colours) -> ModeRegistry:
    """Build a ModeRegistry from (name, colour) pairs."""
    reg = ModeRegistry()
    for name, colour in names_and_colours:
        reg.register(_make_mode(name, colour=colour))
    return reg


# ---------------------------------------------------------------------------
# Mode.badge — colour codes
# ---------------------------------------------------------------------------

_COLOUR_ANSI = {
    "green":   "\x1b[32m",
    "yellow":  "\x1b[33m",
    "cyan":    "\x1b[36m",
    "blue":    "\x1b[34m",
    "red":     "\x1b[31m",
    "magenta": "\x1b[35m",
}


def test_badge_contains_label():
    mode = _make_mode("Auto", label="AUTO", colour="green")
    assert "AUTO" in mode.badge


def test_badge_green_colour():
    mode = _make_mode("Auto", colour="green")
    assert _COLOUR_ANSI["green"] in mode.badge
    assert _RESET in mode.badge


def test_badge_yellow_colour():
    mode = _make_mode("Plan", colour="yellow")
    assert _COLOUR_ANSI["yellow"] in mode.badge
    assert _RESET in mode.badge


def test_badge_cyan_colour():
    mode = _make_mode("Ask", colour="cyan")
    assert _COLOUR_ANSI["cyan"] in mode.badge
    assert _RESET in mode.badge


def test_badge_blue_colour():
    mode = _make_mode("Review", colour="blue")
    assert _COLOUR_ANSI["blue"] in mode.badge
    assert _RESET in mode.badge


def test_badge_red_colour():
    mode = _make_mode("Safe", colour="red")
    assert _COLOUR_ANSI["red"] in mode.badge
    assert _RESET in mode.badge


def test_badge_magenta_colour():
    mode = _make_mode("Debug", colour="magenta")
    assert _COLOUR_ANSI["magenta"] in mode.badge
    assert _RESET in mode.badge


def test_badge_has_square_brackets():
    mode = _make_mode("Foo", label="FOO", colour="cyan")
    assert "[FOO]" in mode.badge


# ---------------------------------------------------------------------------
# ModeRegistry — basic operations
# ---------------------------------------------------------------------------


def test_registry_register_and_get():
    reg = ModeRegistry()
    m = _make_mode("Auto")
    reg.register(m)
    assert reg.get("Auto") is m


def test_registry_get_unknown_returns_none():
    reg = ModeRegistry()
    assert reg.get("Nonexistent") is None


def test_registry_all_modes_empty():
    reg = ModeRegistry()
    assert reg.all_modes() == []


def test_registry_all_modes_returns_snapshot():
    reg = ModeRegistry()
    a = _make_mode("A")
    b = _make_mode("B")
    reg.register(a)
    reg.register(b)
    result = reg.all_modes()
    assert result == [a, b]


def test_registry_len():
    reg = ModeRegistry()
    reg.register(_make_mode("A"))
    reg.register(_make_mode("B"))
    assert len(reg) == 2


def test_registry_iter_order():
    reg = ModeRegistry()
    for name in ("X", "Y", "Z"):
        reg.register(_make_mode(name))
    names = [m.name for m in reg]
    assert names == ["X", "Y", "Z"]


# ---------------------------------------------------------------------------
# ModeRegistry.next_after — cycling
# ---------------------------------------------------------------------------


def test_next_after_advances_to_next():
    reg = _make_registry(("Auto", "green"), ("Plan", "yellow"), ("Ask", "cyan"))
    assert reg.next_after("Auto").name == "Plan"
    assert reg.next_after("Plan").name == "Ask"


def test_next_after_wraps_to_first():
    reg = _make_registry(("Auto", "green"), ("Plan", "yellow"), ("Ask", "cyan"))
    assert reg.next_after("Ask").name == "Auto"


def test_next_after_single_mode_wraps_to_itself():
    reg = ModeRegistry()
    reg.register(_make_mode("Solo"))
    assert reg.next_after("Solo").name == "Solo"


def test_next_after_unknown_name_returns_first():
    reg = _make_registry(("Auto", "green"), ("Plan", "yellow"))
    assert reg.next_after("DoesNotExist").name == "Auto"


def test_next_after_empty_raises():
    reg = ModeRegistry()
    with pytest.raises(ValueError, match="empty"):
        reg.next_after("Anything")


def test_next_after_six_modes_full_cycle():
    names = ["Auto", "Plan", "Ask", "Review", "Safe", "Debug"]
    reg = ModeRegistry()
    for name in names:
        reg.register(_make_mode(name))
    current = "Auto"
    visited = []
    for _ in range(6):
        current = reg.next_after(current).name
        visited.append(current)
    assert visited == ["Plan", "Ask", "Review", "Safe", "Debug", "Auto"]


# ---------------------------------------------------------------------------
# ModeRegistry.register — replaces by name, preserving position
# ---------------------------------------------------------------------------


def test_register_replaces_by_name():
    reg = ModeRegistry()
    original = _make_mode("Auto", colour="green")
    replacement = _make_mode("Auto", colour="yellow")
    reg.register(original)
    reg.register(replacement)
    assert len(reg) == 1
    assert reg.get("Auto") is replacement


def test_register_replace_preserves_position():
    reg = ModeRegistry()
    reg.register(_make_mode("A"))
    reg.register(_make_mode("B"))
    reg.register(_make_mode("C"))
    # Replace B — it should still be at index 1
    new_b = Mode(name="B", label="BB", description="replaced B", colour="red")
    reg.register(new_b)
    names = [m.name for m in reg]
    assert names == ["A", "B", "C"]
    assert reg.get("B") is new_b


def test_register_replace_cycle_order_preserved():
    reg = ModeRegistry()
    reg.register(_make_mode("X"))
    reg.register(_make_mode("Y"))
    reg.register(_make_mode("Z"))
    # Replace Y
    reg.register(_make_mode("Y", colour="blue"))
    assert reg.next_after("X").name == "Y"
    assert reg.next_after("Y").name == "Z"
    assert reg.next_after("Z").name == "X"


# ---------------------------------------------------------------------------
# ModeRegistry.unregister_source
# ---------------------------------------------------------------------------


def test_unregister_source_removes_matching():
    reg = ModeRegistry()
    reg.register(_make_mode("A", source_id="builtin"))
    reg.register(_make_mode("B", source_id="plugin:foo"))
    reg.register(_make_mode("C", source_id="plugin:foo"))
    removed = reg.unregister_source("plugin:foo")
    assert removed == 2
    assert reg.get("A") is not None
    assert reg.get("B") is None
    assert reg.get("C") is None


def test_unregister_source_keeps_non_matching():
    reg = ModeRegistry()
    reg.register(_make_mode("A", source_id="builtin"))
    reg.register(_make_mode("B", source_id="plugin:bar"))
    removed = reg.unregister_source("plugin:foo")
    assert removed == 0
    assert len(reg) == 2


def test_unregister_source_returns_count_zero_when_none_match():
    reg = ModeRegistry()
    reg.register(_make_mode("A", source_id="builtin"))
    assert reg.unregister_source("nonexistent") == 0


def test_unregister_source_removes_only_own_source():
    reg = ModeRegistry()
    reg.register(_make_mode("A", source_id="src1"))
    reg.register(_make_mode("B", source_id="src2"))
    reg.register(_make_mode("C", source_id="src1"))
    reg.unregister_source("src1")
    names = [m.name for m in reg]
    assert names == ["B"]


# ---------------------------------------------------------------------------
# ModeManager — default mode
# ---------------------------------------------------------------------------


def test_manager_default_is_auto():
    from agenthicc.modes.builtin import build_default_registry
    reg = build_default_registry()
    mgr = ModeManager(reg)
    assert mgr.active is not None
    assert mgr.active.name == "Auto"


def test_manager_default_is_first_when_auto_absent():
    reg = ModeRegistry()
    reg.register(_make_mode("Plan"))
    reg.register(_make_mode("Ask"))
    mgr = ModeManager(reg, default_name="Plan")
    assert mgr.active is not None
    assert mgr.active.name == "Plan"


def test_manager_active_none_when_registry_empty():
    mgr = ModeManager(ModeRegistry())
    assert mgr.active is None


def test_manager_falls_back_to_first_when_default_not_found():
    reg = ModeRegistry()
    reg.register(_make_mode("Alpha"))
    reg.register(_make_mode("Beta"))
    mgr = ModeManager(reg, default_name="Nonexistent")
    # default_name not found → first mode
    assert mgr.active is not None
    assert mgr.active.name == "Alpha"


# ---------------------------------------------------------------------------
# ModeManager.cycle()
# ---------------------------------------------------------------------------


def test_manager_cycle_advances():
    reg = _make_registry(("Auto", "green"), ("Plan", "yellow"), ("Ask", "cyan"))
    mgr = ModeManager(reg, default_name="Auto")
    result = mgr.cycle()
    assert result is not None
    assert result.name == "Plan"
    assert mgr.active.name == "Plan"


def test_manager_cycle_wraps_back():
    reg = _make_registry(("Auto", "green"), ("Plan", "yellow"))
    mgr = ModeManager(reg, default_name="Plan")
    result = mgr.cycle()
    assert result.name == "Auto"


def test_manager_cycle_six_modes_full_loop():
    from agenthicc.modes.builtin import build_default_registry
    reg = build_default_registry()
    mgr = ModeManager(reg, default_name="Auto")
    names = []
    for _ in range(6):
        names.append(mgr.cycle().name)
    assert names == ["Plan", "Ask", "Review", "Safe", "Debug", "Auto"]


def test_manager_cycle_raises_on_empty_registry():
    mgr = ModeManager(ModeRegistry())
    with pytest.raises(ValueError):
        mgr.cycle()


# ---------------------------------------------------------------------------
# ModeManager.set()
# ---------------------------------------------------------------------------


def test_manager_set_switches_mode():
    reg = _make_registry(("Auto", "green"), ("Plan", "yellow"), ("Ask", "cyan"))
    mgr = ModeManager(reg, default_name="Auto")
    result = mgr.set("Plan")
    assert result is not None
    assert result.name == "Plan"
    assert mgr.active.name == "Plan"


def test_manager_set_unknown_returns_none():
    reg = _make_registry(("Auto", "green"), ("Plan", "yellow"))
    mgr = ModeManager(reg, default_name="Auto")
    result = mgr.set("DoesNotExist")
    assert result is None
    # Current mode unchanged
    assert mgr.active.name == "Auto"


def test_manager_set_preserves_current_on_unknown():
    reg = _make_registry(("Auto", "green"), ("Ask", "cyan"))
    mgr = ModeManager(reg, default_name="Ask")
    mgr.set("NotReal")
    assert mgr.active.name == "Ask"


# ---------------------------------------------------------------------------
# ModeManager.apply_to_agent() — system_patch
# ---------------------------------------------------------------------------


def test_apply_to_agent_prepends_system_patch():
    patch_text = "## PLAN MODE\nDo not write files."
    reg = ModeRegistry()
    reg.register(Mode(
        name="Plan",
        label="PLAN",
        description="Plan mode",
        system_patch=patch_text,
    ))
    mgr = ModeManager(reg, default_name="Plan")
    base = "You are a helpful assistant."
    new_prompt, _ = mgr.apply_to_agent(base, [])
    assert new_prompt.startswith(patch_text)
    assert base in new_prompt


def test_apply_to_agent_empty_patch_leaves_prompt_unchanged():
    reg = ModeRegistry()
    reg.register(Mode(name="Auto", label="AUTO", description="Auto", system_patch=""))
    mgr = ModeManager(reg, default_name="Auto")
    base = "Base system prompt."
    new_prompt, _ = mgr.apply_to_agent(base, [])
    assert new_prompt == base


def test_apply_to_agent_no_mode_leaves_prompt_unchanged():
    mgr = ModeManager(ModeRegistry())
    base = "Unchanged prompt."
    new_prompt, _ = mgr.apply_to_agent(base, [])
    assert new_prompt == base


# ---------------------------------------------------------------------------
# ModeManager.apply_to_agent() — tool_filter
# ---------------------------------------------------------------------------


def test_apply_to_agent_tool_filter_blocks_write_file():
    write_file = _make_tool("write_file")
    read_file = _make_tool("read_file")

    def only_reads(name, kwargs):
        return name != "write_file"

    reg = ModeRegistry()
    reg.register(Mode(
        name="Safe",
        label="SAFE",
        description="Safe mode",
        tool_filter=only_reads,
    ))
    mgr = ModeManager(reg, default_name="Safe")
    _, filtered = mgr.apply_to_agent("prompt", [write_file, read_file])
    assert write_file not in filtered
    assert read_file in filtered


def test_apply_to_agent_tool_filter_allows_all_when_none():
    tools = [_make_tool("write_file"), _make_tool("run_bash"), _make_tool("read_file")]
    reg = ModeRegistry()
    reg.register(Mode(name="Auto", label="AUTO", description="Auto", tool_filter=None))
    mgr = ModeManager(reg, default_name="Auto")
    _, filtered = mgr.apply_to_agent("prompt", tools)
    assert filtered == tools


def test_apply_to_agent_no_filter_returns_all_tools_unchanged():
    tools = [_make_tool("a"), _make_tool("b"), _make_tool("c")]
    reg = ModeRegistry()
    reg.register(Mode(name="Ask", label="ASK", description="Ask", tool_filter=None))
    mgr = ModeManager(reg, default_name="Ask")
    _, filtered = mgr.apply_to_agent("prompt", tools)
    assert filtered == tools


def test_apply_to_agent_filter_receives_tool_name():
    """tool_filter is called with the correct tool __name__."""
    seen_names: list[str] = []

    def capturing_filter(name, kwargs):
        seen_names.append(name)
        return True

    tools = [_make_tool("alpha"), _make_tool("beta")]
    reg = ModeRegistry()
    reg.register(Mode(
        name="Capture",
        label="CAP",
        description="Capture filter calls",
        tool_filter=capturing_filter,
    ))
    mgr = ModeManager(reg, default_name="Capture")
    mgr.apply_to_agent("prompt", tools)
    assert set(seen_names) == {"alpha", "beta"}


def test_apply_to_agent_empty_tools_list():
    reg = ModeRegistry()
    reg.register(Mode(
        name="Plan",
        label="PLAN",
        description="Plan",
        tool_filter=lambda n, k: False,
    ))
    mgr = ModeManager(reg, default_name="Plan")
    _, filtered = mgr.apply_to_agent("prompt", [])
    assert filtered == []


def test_apply_to_agent_named_tool_passes_filter():
    """A named tool whose name matches the filter is kept."""
    allowed = _make_tool("allowed")
    blocked = _make_tool("blocked")

    reg = ModeRegistry()
    reg.register(Mode(
        name="Strict",
        label="STRICT",
        description="Strict mode",
        tool_filter=lambda n, k: n == "allowed",
    ))
    mgr = ModeManager(reg, default_name="Strict")
    _, filtered = mgr.apply_to_agent("prompt", [allowed, blocked])
    assert allowed in filtered
    assert blocked not in filtered
