"""Opt-in large-profile latency gate for PRD-209."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(
    os.environ.get("AGENTHICC_RUN_LARGE_MANAGER_BENCHMARK") != "1",
    reason="set AGENTHICC_RUN_LARGE_MANAGER_BENCHMARK=1 for the 10k/1M load gate",
)
@pytest.mark.timeout(300)
def test_agents_manager_meets_large_registry_latency_budgets(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_agents_manager.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--sessions",
            "10000",
            "--events",
            "1000000",
            "--active-workers",
            "32",
            "--samples",
            "100",
            "--directory",
            str(tmp_path / "manager-benchmark"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    report = json.loads(completed.stdout)
    measurements = report["measurements"]
    assert report["sessions"] == 10_000
    assert report["events"] == 1_000_000
    assert measurements["first_frame_ms"] <= 500
    assert measurements["key_to_render"]["p95_ms"] <= 50
    assert measurements["key_to_render"]["p99_ms"] <= 100
    assert measurements["incremental_refresh"]["p95_ms"] <= 750
