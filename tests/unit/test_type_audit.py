"""Tests for the source typing-debt ratchet command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "type_audit.py"

_CLEAN_BASELINE: dict[str, int] = {
    "functions_with_missing_annotations": 0,
    "missing_parameter_annotations": 0,
    "missing_return_annotations": 0,
    "explicit_any_annotations": 0,
    "bare_list_annotations": 0,
    "bare_dict_annotations": 0,
    "getattr_calls": 0,
    "hasattr_calls": 0,
    "type_ignore_comments": 0,
    "mypy_module_suppressions": 0,
}


def _run_audit(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    # The audit is a repository tool, not package code: keep coverage's
    # subprocess tracer out of it so a whole-tree scan stays fast.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_PROCESS")}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _source_tree(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "src"
    root.mkdir()
    (root / "module.py").write_text(source, encoding="utf-8")
    return root


def _baseline(tmp_path: Path, values: dict[str, int]) -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_audit_emits_machine_readable_metrics() -> None:
    result = _run_audit("--root", "src/agenthicc")

    assert result.returncode == 0
    metrics = json.loads(result.stdout)
    assert metrics["source_files"] > 0
    assert metrics["functions"] > 0


def test_audit_accepts_current_baseline() -> None:
    result = _run_audit("--check", "docs/reference/type-safety-baseline.json")

    assert result.returncode == 0, result.stderr
    assert "Type audit OK" in result.stdout


def test_audit_rejects_a_debt_regression(tmp_path: Path) -> None:
    root = _source_tree(tmp_path, "def untyped(value):\n    return value\n\nitems: list = []\n")

    result = _run_audit("--root", str(root), "--check", str(_baseline(tmp_path, _CLEAN_BASELINE)))

    assert result.returncode == 1
    assert "bare_list_annotations: regressed" in result.stderr


def test_audit_rejects_a_stale_baseline(tmp_path: Path) -> None:
    root = _source_tree(tmp_path, "def typed(value: int) -> int:\n    return value\n")
    loose = {**_CLEAN_BASELINE, "getattr_calls": 3}

    result = _run_audit("--root", str(root), "--check", str(_baseline(tmp_path, loose)))

    assert result.returncode == 1
    assert "getattr_calls: baseline is stale (baseline=3, current=0)" in result.stderr


def test_update_writes_only_debt_metrics(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path, "def typed(value: object) -> bool:\n    return hasattr(value, 'x')\n"
    )
    baseline = tmp_path / "baseline.json"

    assert _run_audit("--root", str(root), "--update", str(baseline)).returncode == 0

    written = json.loads(baseline.read_text(encoding="utf-8"))
    assert written == {**_CLEAN_BASELINE, "hasattr_calls": 1}
    assert _run_audit("--root", str(root), "--check", str(baseline)).returncode == 0


def test_audit_rejects_a_baseline_raised_against_a_git_revision(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path, "def typed(value: object) -> bool:\n    return hasattr(value, 'x')\n"
    )
    baseline = _baseline(tmp_path, {**_CLEAN_BASELINE, "hasattr_calls": 1})
    git = ["git", "-c", "user.name=audit", "-c", "user.email=audit@example.invalid"]
    subprocess.run([*git, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "baseline.json"], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "baseline"], cwd=tmp_path, check=True)
    (root / "module.py").write_text(
        "def typed(value: object) -> bool:\n"
        "    return hasattr(value, 'x') and hasattr(value, 'y')\n",
        encoding="utf-8",
    )
    baseline.write_text(json.dumps({**_CLEAN_BASELINE, "hasattr_calls": 2}), encoding="utf-8")

    unguarded = _run_audit("--root", str(root), "--check", str(baseline), cwd=tmp_path)
    guarded = _run_audit(
        "--root", str(root), "--check", str(baseline), "--baseline-ref", "HEAD", cwd=tmp_path
    )

    assert unguarded.returncode == 0, unguarded.stderr
    assert guarded.returncode == 1
    assert "hasattr_calls: the baseline was raised from 1 to 2" in guarded.stderr


def test_an_unknown_baseline_ref_fails_instead_of_skipping_the_guard(tmp_path: Path) -> None:
    root = _source_tree(tmp_path, "def typed(value: int) -> int:\n    return value\n")
    baseline = _baseline(tmp_path, _CLEAN_BASELINE)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    result = _run_audit(
        "--root", str(root), "--check", str(baseline), "--baseline-ref", "missing", cwd=tmp_path
    )

    assert result.returncode == 1
    assert "'missing' is not a commit" in result.stderr


@pytest.mark.parametrize(
    ("marker", "today", "expected"),
    [
        ("", "2026-10-22", "has no time box"),
        ("# type-debt: owner=WS-4.2; expires=2026-10-21\n", "2026-10-22", "expired on 2026-10-21"),
        ("# type-debt: owner=WS-4.2; expires=2026-10-22\n", "2026-10-22", None),
    ],
)
def test_module_suppressions_need_an_unexpired_time_box(
    tmp_path: Path, marker: str, today: str, expected: str | None
) -> None:
    root = _source_tree(tmp_path, f'"""Doc."""\n\n{marker}# mypy: disable-error-code=override\n')
    baseline = _baseline(tmp_path, {**_CLEAN_BASELINE, "mypy_module_suppressions": 1})

    result = _run_audit("--root", str(root), "--check", str(baseline), "--today", today)

    if expected is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert "'# mypy: disable-error-code=override'" in result.stderr
        assert expected in result.stderr
