"""Unit tests for the performance budget ratchet."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from agenthicc.performance import (
    MetricBudget,
    _collect_weekly_profiles,
    _format_report,
    _read_budgets,
    _write_updated_budgets,
    check_budget_changes,
    check_budgets,
)

pytestmark = pytest.mark.unit


def _budget(
    value: float,
    *,
    tolerance: float = 0.0,
    ratchet: Literal["upper", "two_way"] = "upper",
    scope: Literal["local", "ci", "weekly"] = "local",
) -> MetricBudget:
    return MetricBudget(value=value, tolerance=tolerance, scope=scope, ratchet=ratchet)


def test_budget_check_accepts_metrics_with_headroom() -> None:
    budgets = {"cli.cpu_s": _budget(1.0, tolerance=0.25)}

    assert check_budgets({"cli.cpu_s": 1.1}, budgets, "local") == []


def test_ci_check_does_not_gate_wall_clock_diagnostics() -> None:
    budgets = {
        "tui.first_prompt.wall_s": MetricBudget(
            value=1.0,
            tolerance=0.0,
            scope="local",
            gate_scopes=frozenset({"local"}),
        )
    }

    assert check_budgets({"tui.first_prompt.wall_s": 10.0}, budgets, "ci") == []


def test_weekly_profile_uses_thirty_second_idle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int, float]] = []

    def fake_profile(
        sessions: int, events: int, samples: int, idle_seconds: float
    ) -> dict[str, object]:
        calls.append((sessions, events, samples, idle_seconds))
        return {
            "measurements": {
                "fresh_process_first_page": {"cpu_p95_ms": 1.0},
                "search": {"cpu_p95_ms": 1.0},
                "live_screen": {
                    "idle_cpu_fraction": 0.0,
                    "idle_appended_events": 0,
                    "change_to_visible": {"p95_ms": 1.0},
                },
            }
        }

    monkeypatch.setattr("agenthicc.performance._run_agents_profile", fake_profile)

    _collect_weekly_profiles(samples=1, idle_seconds=2.0)

    assert calls == [(10_000, 1_000_000, 1, 30.0)]


def test_update_cannot_raise_a_two_way_budget(tmp_path: Path) -> None:
    path = tmp_path / "budgets.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "metrics": {
                    "cli.modules": {
                        "budget": 2,
                        "ratchet": "two_way",
                        "scope": "local",
                        "tolerance": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cannot raise a budget"):
        _write_updated_budgets(path, {"cli.modules": 3})

    assert json.loads(path.read_text(encoding="utf-8"))["metrics"]["cli.modules"]["budget"] == 2


def test_budget_check_names_cpu_regressions() -> None:
    budgets = {"cli.cpu_s": _budget(1.0, tolerance=0.25)}

    problems = check_budgets({"cli.cpu_s": 1.3}, budgets, "local")

    assert len(problems) == 1
    assert problems[0].startswith("cli.cpu_s: over budget")


def test_exact_count_ratchet_rejects_unrecorded_improvements() -> None:
    budgets = {"cli.modules": _budget(200, ratchet="two_way")}

    problems = check_budgets({"cli.modules": 199}, budgets, "local")

    assert len(problems) == 1
    assert problems[0].startswith("cli.modules: budget is stale")


def test_report_marks_stale_two_way_budget() -> None:
    budgets = {"cli.modules": _budget(200, ratchet="two_way")}

    report = _format_report({"cli.modules": 199}, budgets, {}, "local")

    assert "STALE  cli.modules: 199 / 200" in report


def test_check_requires_every_metric_for_selected_scope() -> None:
    budgets = {
        "cli.cpu_s": _budget(1.0),
        "weekly.sessions": _budget(10_000, scope="weekly"),
    }

    problems = check_budgets({}, budgets, "local")

    assert problems == ["cli.cpu_s: probe did not report a measurement"]


def test_check_rejects_measurements_without_a_budget() -> None:
    problems = check_budgets({"cli.cpu_s": 0.5}, {}, "local")

    assert problems == ["cli.cpu_s: no local budget is defined"]


def test_budget_file_requires_valid_scope_and_nonnegative_values(tmp_path: Path) -> None:
    path = tmp_path / "budgets.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "metrics": {"bad.cpu_s": {"budget": -1, "tolerance": 0, "scope": "local"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid budget entry"):
        _read_budgets(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("budget", float("nan")),
        ("budget", float("inf")),
        ("budget", 10**1_000),
        ("tolerance", float("nan")),
        ("tolerance", float("inf")),
    ],
)
def test_budget_file_rejects_non_finite_values(tmp_path: Path, field: str, value: float) -> None:
    path = tmp_path / "budgets.json"
    entry: dict[str, int | float | str] = {
        "budget": 1,
        "tolerance": 0,
        "scope": "local",
    }
    entry[field] = value
    path.write_text(json.dumps({"version": 1, "metrics": {"bad.cpu_s": entry}}), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid budget entry"):
        _read_budgets(path)


def test_budget_increase_requires_approval_label() -> None:
    baseline = {"cli.cpu_s": _budget(1.0, tolerance=0.25)}
    current = {"cli.cpu_s": _budget(1.1, tolerance=0.25)}

    assert check_budget_changes(baseline, current, increase_approved=False) == [
        "cli.cpu_s: budget gate was weakened; add the perf-budget-increase label"
    ]
    assert check_budget_changes(baseline, current, increase_approved=True) == []


@pytest.mark.parametrize(
    ("baseline", "current"),
    [
        (_budget(1.0, tolerance=0.25), _budget(1.0, tolerance=0.5)),
        (_budget(1.0, scope="ci"), _budget(1.0, scope="weekly")),
        (
            MetricBudget(1.0, 0.0, "ci", gate_scopes=frozenset({"local", "ci", "weekly"})),
            MetricBudget(1.0, 0.0, "ci", gate_scopes=frozenset({"local", "weekly"})),
        ),
        (
            _budget(1.0, ratchet="two_way"),
            _budget(1.0, ratchet="upper"),
        ),
    ],
)
def test_weakened_budget_gate_requires_approval(
    baseline: MetricBudget, current: MetricBudget
) -> None:
    assert check_budget_changes(
        {"metric": baseline}, {"metric": current}, increase_approved=False
    ) == ["metric: budget gate was weakened; add the perf-budget-increase label"]


def test_removing_budget_cannot_be_approved_by_label() -> None:
    baseline = {"cli.cpu_s": _budget(1.0)}

    assert check_budget_changes(baseline, {}, increase_approved=True) == [
        "cli.cpu_s: budget was removed; restore the metric instead of dropping its gate"
    ]


def test_adding_budget_or_strengthening_gate_needs_no_approval() -> None:
    baseline = {"cli.cpu_s": _budget(1.0)}
    current = {
        "cli.cpu_s": _budget(0.9),
        "cli.modules": _budget(200, ratchet="two_way"),
    }

    assert check_budget_changes(baseline, current, increase_approved=False) == []
