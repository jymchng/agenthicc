"""Tests for the source typing-debt ratchet command."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "type_audit.py"


def _run_audit(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_audit_emits_machine_readable_metrics() -> None:
    result = _run_audit("--root", "src/agenthicc")

    assert result.returncode == 0
    metrics = json.loads(result.stdout)
    assert metrics["source_files"] > 0
    assert metrics["functions"] > 0


def test_audit_accepts_current_baseline() -> None:
    result = _run_audit(
        "--check",
        "docs/reference/type-safety-baseline.json",
    )

    assert result.returncode == 0
    assert "Type audit OK" in result.stdout


def test_audit_rejects_a_debt_regression(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "bad.py").write_text(
        "def untyped(value):\n    return value\n\nitems: list = []\n",
        encoding="utf-8",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "source_files": 0,
                "functions": 0,
                "functions_with_missing_annotations": 0,
                "missing_parameter_annotations": 0,
                "missing_return_annotations": 0,
                "explicit_any_annotations": 0,
                "bare_list_annotations": 0,
                "bare_dict_annotations": 0,
                "getattr_calls": 0,
                "hasattr_calls": 0,
                "type_ignore_comments": 0,
            }
        ),
        encoding="utf-8",
    )

    result = _run_audit("--root", str(source_root), "--check", str(baseline_path))

    assert result.returncode == 1
    assert "Type audit regression" in result.stderr
    assert "bare_list_annotations" in result.stderr
