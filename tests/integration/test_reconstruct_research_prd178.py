"""Integration coverage for the PRD-178 evidence gate and checkpoint boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.tools.sandbox import WorkspaceScope
from agenthicc.workflows.reconstruct_site import (
    CoverageMatrix,
    ReconstructContext,
    ReconstructSiteParams,
    ReconstructSiteRunner,
    ReconstructState,
)
from agenthicc.workflows.reconstruct_site.evidence import ReconstructEvidenceStore
from agenthicc.workflows.reconstruct_site.evidence_plan import (
    LEGACY_PHASE_PLAN_VERSION,
    RECONSTRUCT_PHASE_PLAN,
)


def _config(
    tmp_path: Path, params: ReconstructSiteParams, *, browser: object | None = None
) -> SimpleNamespace:
    execution = SimpleNamespace(
        effective_model=lambda: "fake-model",
        effective_usable_budget=lambda: 10_000,
        provider="openai",
        model="fake-model",
        profile="",
        base_url="",
    )
    app_state = SimpleNamespace(
        active_mode=lambda: SimpleNamespace(blocked_capabilities=frozenset()),
        update_workflow_phase=lambda **_kwargs: None,
    )
    return SimpleNamespace(
        app_state=app_state,
        agent_runner=SimpleNamespace(),
        cfg=SimpleNamespace(execution=execution),
        params=params,
        session_memory=object(),
        workflow_handle=None,
        workspace_scope=WorkspaceScope.create(tmp_path),
        browser_manager=browser,
        browser_tools=(),
        plugin_tools=[],
        mcp_registry=None,
        memory_router=None,
        semantic_index=None,
        approval_svc=None,
        terminal_wait_policies={},
    )


def _context(tmp_path: Path, *, complete: bool) -> tuple[ReconstructSiteRunner, ReconstructContext]:
    runner = ReconstructSiteRunner(
        _config(tmp_path, ReconstructSiteParams(profile="static"), browser=object()), None
    )
    context = ReconstructContext(
        intent="reconstruct the fixture",
        run_id="gate-run",
        state=ReconstructState.RESEARCH_GATE,
        profile="static",
        target_url="https://reference.example",
        target_directory="site",
        route_inventory=[{"route": "/", "purpose": "home"}],
    )
    matrix = runner._coverage(context)
    if complete:
        for cell_id in tuple(matrix.cells):
            matrix.record(
                cell_id,
                artifact_ids=(
                    "screenshot:fixture",
                    "measurement:fixture",
                    "responsive_observation:fixture",
                ),
            )
        context.coverage_matrix = matrix.to_dict()
    runner._active_context = context
    runner._active_plan = RECONSTRUCT_PHASE_PLAN.active("static")
    runner._active_phase_name = "research_gate"
    return runner, context


@pytest.mark.asyncio
async def test_gate_validates_current_baseline_and_allows_bootstrap_only_after_approval(
    tmp_path: Path,
) -> None:
    runner, context = _context(tmp_path, complete=True)

    async def approve(**kwargs: object) -> None:
        tools = kwargs["tools"]
        await tools[0](
            summary="All required fixture cells are complete.",
            baseline_artifact_id=runner._active_context.research_baseline_id,
        )

    runner.run_phase = approve  # type: ignore[method-assign]
    result = await runner._research_gate(context, object())

    assert result is ReconstructState.BOOTSTRAP
    assert context.research_gate_status == "approved"
    assert context.research_baseline_id
    assert context.unresolved_research == []
    manifest = ReconstructEvidenceStore(
        runner._workspace(),
        context.run_id,
        plan_version=context.plan_version,
        profile=context.profile,
    ).manifest
    assert any(item.kind == "fidelity_baseline" for item in manifest.artifacts)
    assert any(item.kind == "research_coverage_report" for item in manifest.artifacts)


@pytest.mark.asyncio
async def test_gate_rejects_prose_or_approval_when_cells_are_missing(tmp_path: Path) -> None:
    runner, context = _context(tmp_path, complete=False)
    calls = 0

    async def approve(**kwargs: object) -> None:
        nonlocal calls
        calls += 1
        tools = kwargs["tools"]
        await tools[0](
            summary="I am done.",
            baseline_artifact_id=runner._active_context.research_baseline_id,
        )

    runner.run_phase = approve  # type: ignore[method-assign]
    result = await runner._research_gate(context, object())

    assert result is ReconstructState.FAILED
    assert calls == 5
    assert context.research_gate_status == "pending"
    assert context.unresolved_research


def test_checkpoint_contains_compact_coverage_and_rehydrates_full_baseline(tmp_path: Path) -> None:
    runner, context = _context(tmp_path, complete=True)
    store = runner._ensure_evidence(context)
    runner._publish_baseline(context, store, 1)
    # Use the public plugin class rather than a runner implementation detail.
    from agenthicc.workflows.reconstruct_site import ReconstructSiteWorkflow

    encoded = ReconstructSiteWorkflow.checkpoint_context_to_payload(context)
    assert "cells" not in encoded["coverage_matrix"]
    assert encoded["coverage_matrix"]["blocking_cell_ids"] == []
    restored = ReconstructSiteWorkflow.checkpoint_context_from_payload(encoded)
    assert restored.research_baseline["artifact_id"] == context.research_baseline_id
    assert restored.coverage_matrix["total"] == len(context.coverage_matrix["cells"])
    assert isinstance(CoverageMatrix.from_dict(context.coverage_matrix), CoverageMatrix)
    runner._rehydrate_evidence(restored)
    assert len(restored.coverage_matrix["cells"]) == len(context.coverage_matrix["cells"])


def test_corrupt_evidence_is_marked_stale_and_rewinds_to_research(tmp_path: Path) -> None:
    runner, context = _context(tmp_path, complete=True)
    store = runner._ensure_evidence(context)
    runner._publish_baseline(context, store, 1)
    context.state = ReconstructState.COMPLETE
    record = next(
        item for item in store.manifest.artifacts if item.kind == "research_coverage_report"
    )
    runner._workspace().resolve(record.relative_path).write_text("changed", encoding="utf-8")

    resumed = ReconstructSiteRunner(
        _config(tmp_path, ReconstructSiteParams(profile="static"), browser=object()), None
    )
    resumed._rehydrate_evidence(context)

    assert context.state is ReconstructState.VISUAL_RESEARCH
    assert record.artifact_id in context.stale_artifact_ids
    assert context.research_gate_status == "pending"
    refreshed = ReconstructEvidenceStore(
        resumed._workspace(),
        context.run_id,
        plan_version=context.plan_version,
        profile=context.profile,
    )
    assert any(
        item.artifact_id == record.artifact_id and item.status == "stale"
        for item in refreshed.manifest.artifacts
    )


def test_resume_recovers_the_persisted_prd177_plan_version(tmp_path: Path) -> None:
    runner, context = _context(tmp_path, complete=False)
    context.plan_version = LEGACY_PHASE_PLAN_VERSION
    context.profile = "production"
    store = runner._ensure_evidence(context)
    assert store.manifest.plan_version == LEGACY_PHASE_PLAN_VERSION
    context.artifact_manifest_path = store.manifest_relative_path

    resumed = ReconstructSiteRunner(
        _config(tmp_path, ReconstructSiteParams(profile="static"), browser=object()), None
    )
    selected = resumed._select_profile(context)

    assert selected.version == LEGACY_PHASE_PLAN_VERSION
    assert selected.next_name("design_system") == "bootstrap"
