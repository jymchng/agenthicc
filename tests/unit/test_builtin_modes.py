"""Clean-slate contract tests for the three legacy-compatible built-in modes.

The runtime policy is tested separately in ``test_three_mode_gates``.  These
tests ensure the older ``agenthicc.modes`` import path exposes the same public
identities and cannot reintroduce the removed six-mode product model.
"""

from __future__ import annotations

import pytest

from agenthicc.modes.builtin import build_default_registry

pytestmark = pytest.mark.unit


def test_builtin_registry_has_exactly_three_selectable_modes() -> None:
    registry = build_default_registry()

    assert [mode.name for mode in registry.all_modes()] == ["Safe", "Plan", "Yolo"]


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("Auto", "Yolo"), ("Guard", "Safe"), ("Ask", "Safe"), ("Review", "Plan")],
)
def test_legacy_aliases_resolve_without_creating_cycle_entries(alias: str, canonical: str) -> None:
    registry = build_default_registry()

    assert registry.get(alias) is registry.get(canonical)
    assert alias not in [mode.name for mode in registry.all_modes()]


def test_legacy_registry_lookup_is_case_insensitive_for_canonical_names() -> None:
    registry = build_default_registry()

    assert registry.get("safe") is registry.get("Safe")


def test_debug_is_not_silently_reintroduced() -> None:
    assert build_default_registry().get("Debug") is None


def test_cycle_is_safe_plan_yolo_and_wraps() -> None:
    registry = build_default_registry()
    current = "Safe"

    visited = []
    for _ in range(4):
        current = registry.next_after(current).name
        visited.append(current)

    assert visited == ["Plan", "Yolo", "Safe", "Plan"]


def test_safe_is_approval_posture_not_legacy_tool_filter() -> None:
    safe = build_default_registry().get("Safe")

    assert safe is not None
    assert safe.tool_filter is None
    assert "approval" in safe.description.lower()


def test_plan_legacy_adapter_remains_read_only_for_old_callers() -> None:
    plan = build_default_registry().get("Plan")

    assert plan is not None and plan.tool_filter is not None
    assert plan.tool_filter("read_file", {}) is True
    assert plan.tool_filter("git_status", {}) is True
    assert plan.tool_filter("write_file", {}) is False
    assert plan.tool_filter("run_bash", {}) is False
    assert "MUST NOT" in plan.system_patch


def test_yolo_is_unrestricted_for_legacy_callers() -> None:
    yolo = build_default_registry().get("Yolo")

    assert yolo is not None
    assert yolo.tool_filter is None
    assert yolo.colour == "green"
