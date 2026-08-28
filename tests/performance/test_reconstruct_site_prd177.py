"""Offline performance and regression checks for PRD-177 contracts."""

from __future__ import annotations

import time
from pathlib import Path

from agenthicc.tools.sandbox import WorkspaceView
from agenthicc.workflows.reconstruct_site.evidence import ReconstructEvidenceStore
from agenthicc.workflows.reconstruct_site.evidence_plan import RECONSTRUCT_PHASE_PLAN
from agenthicc.workflows.reconstruct_site.research import build_coverage_matrix


def test_phase_plan_selection_p95_is_below_twenty_milliseconds() -> None:
    samples: list[float] = []
    for _ in range(100):
        started = time.perf_counter()
        assert RECONSTRUCT_PHASE_PLAN.active("production").total_phases == 41
        samples.append(time.perf_counter() - started)
    samples.sort()
    assert samples[94] < 0.020


def test_manifest_updates_support_one_thousand_records(tmp_path: Path) -> None:
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path),
        "performance-run",
        plan_version="reconstruct-site.v2",
        profile="static",
    )
    samples: list[float] = []
    for index in range(1_000):
        started = time.perf_counter()
        store.put(
            "research",
            f"deterministic research record {index}",
            phase="recon",
            attempt=index + 1,
            suffix=".txt",
        )
        samples.append(time.perf_counter() - started)
    samples.sort()
    assert len(store.manifest.artifacts) == 1_000
    assert samples[949] < 0.100
    assert store.verify() == []


def test_coverage_expansion_and_digest_scale_without_evidence_bodies() -> None:
    surfaces = [{"route": f"/page-{index}", "interactions": ["navigation"]} for index in range(10)]
    started = time.perf_counter()
    matrix = build_coverage_matrix("application", surfaces)
    elapsed = time.perf_counter() - started

    assert matrix.total == 30
    assert len(str(matrix.compact_digest())) < 5_000
    assert elapsed < 0.100
