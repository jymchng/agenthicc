"""Clean-room unit coverage for PRD-178 research contracts."""

from __future__ import annotations

import dataclasses

import pytest

from agenthicc.workflows.reconstruct_site import (
    DEFAULT_VIEWPORTS,
    CoverageMatrix,
    CoverageStatus,
    FidelityBaseline,
    InteractionTrace,
    ResearchGate,
    ResearchValidationError,
    build_coverage_matrix,
)
from agenthicc.workflows.reconstruct_site.runner import _parse_viewports
from agenthicc.workflows.reconstruct_site.evidence_plan import (
    LEGACY_PHASE_PLAN_VERSION,
    LEGACY_RECONSTRUCT_PHASE_PLAN,
    RECONSTRUCT_PHASE_PLAN,
    PhasePlanError,
)


def _static_matrix() -> CoverageMatrix:
    return build_coverage_matrix(
        "static",
        [{"route": "/", "visual_states": ["loaded"], "interactions": ["nav"]}],
    )


def _complete_static_matrix() -> CoverageMatrix:
    matrix = _static_matrix()
    for cell_id in tuple(matrix.cells):
        matrix.record(
            cell_id,
            artifact_ids=("screenshot:shot", "measurement:measure"),
        )
        matrix.record(cell_id, artifact_ids=("responsive_observation:responsive",))
    return matrix


def test_default_matrix_covers_each_surface_at_three_viewports() -> None:
    matrix = build_coverage_matrix(
        "static",
        [{"route": "/", "visual_states": ["loaded", "empty"]}],
    )

    assert tuple(item.viewport_id for item in DEFAULT_VIEWPORTS) == (
        "mobile",
        "tablet",
        "desktop",
    )
    assert matrix.total == 6
    assert matrix.counts()[CoverageStatus.PENDING.value] == 6
    assert all("screenshot" in cell.required_artifacts for cell in matrix.cells.values())


def test_custom_viewport_matrix_is_parsed_and_expands_deterministically() -> None:
    viewports = _parse_viewports('[{"viewport_id":"watch","width":320,"height":568,"touch":true}]')
    matrix = build_coverage_matrix("static", [{"route": "/"}], viewports)

    assert viewports[0].viewport_id == "watch"
    assert matrix.total == 1
    assert next(iter(matrix.cells.values())).viewport_id == "watch"


def test_route_statuses_are_validated_before_cells_are_created() -> None:
    with pytest.raises(ResearchValidationError, match="coverage_status"):
        build_coverage_matrix("static", [{"route": "/", "coverage_status": "invented"}])


def test_visual_and_responsive_evidence_is_required_before_completion() -> None:
    matrix = _static_matrix()
    for cell_id in tuple(matrix.cells):
        matrix.record(cell_id, artifact_ids=("screenshot:shot", "measurement:measure"))
        assert matrix.cells[cell_id].status == CoverageStatus.PENDING.value
        matrix.record(cell_id, artifact_ids=("responsive_observation:responsive",))
        assert matrix.cells[cell_id].status == CoverageStatus.COMPLETE.value
    assert matrix.blocking_cells() == ()


def test_gate_rejects_incomplete_research_and_approves_complete_research() -> None:
    gate = ResearchGate("static")
    with pytest.raises(ResearchValidationError, match="incomplete research"):
        gate.approve(_static_matrix(), baseline_artifact_id="baseline", summary="not ready")

    decision = gate.approve(
        _complete_static_matrix(),
        baseline_artifact_id="baseline",
        summary="Every required cell has evidence.",
    )
    assert decision.approved
    assert decision.status == "approved"
    assert decision.blocking_cell_ids == ()


def test_degraded_approval_only_accepts_unavailable_cells() -> None:
    gate = ResearchGate("static")
    matrix = _static_matrix()
    for cell_id in tuple(matrix.cells):
        matrix.mark_unavailable(cell_id, "reference requires unauthorized login")

    decision = gate.approve_degraded(
        matrix,
        exception_ids=tuple(matrix.cells),
        rationale="The user did not authorize credentials for this protected surface.",
    )
    assert decision.status == "approved_degraded"
    assert decision.blocking_cell_ids == tuple(sorted(matrix.cells))

    matrix = _static_matrix()
    first = next(iter(matrix.cells))
    matrix.mark_unavailable(first, "browser timeout")
    with pytest.raises(ResearchValidationError, match="pending, stale"):
        gate.approve_degraded(
            matrix,
            exception_ids=(first,),
            rationale="Only the timeout was accepted.",
        )


def test_matrix_round_trip_preserves_revision_and_stale_state() -> None:
    matrix = _complete_static_matrix()
    first = next(iter(matrix.cells))
    matrix.mark_stale((first,), "visual validation found a mismatch")
    restored = type(matrix).from_dict(matrix.to_dict())

    assert restored.revision == matrix.revision
    assert restored.cells[first].status == CoverageStatus.STALE.value
    assert restored.cells[first].limitations == ("visual validation found a mismatch",)
    assert restored.cells[first].observed_at > 0


def test_not_applicable_is_explicitly_reasoned_and_not_blocking() -> None:
    matrix = _static_matrix()
    cell_id = next(iter(matrix.cells))

    matrix.mark_not_applicable(cell_id, "The reference surface has no form control.")

    assert matrix.cells[cell_id].status == CoverageStatus.NOT_APPLICABLE.value
    assert matrix.cells[cell_id].limitations == ("The reference surface has no form control.",)
    assert cell_id not in matrix.blocking_cells()


def test_gate_rejects_malformed_exception_rows() -> None:
    gate = ResearchGate("static")
    with pytest.raises(ResearchValidationError, match="exceptions must be objects"):
        gate.evaluate(_static_matrix(), exceptions=("not-an-object",))  # type: ignore[arg-type]


def test_baseline_hash_ignores_observation_timestamps_but_round_trip_keeps_them() -> None:
    matrix = _complete_static_matrix()
    original_observed_at = matrix.cells[next(iter(matrix.cells))].observed_at
    baseline_matrix = type(matrix).from_dict(matrix.to_dict())
    first = next(iter(matrix.cells))
    baseline = FidelityBaseline(
        profile="static",
        scope={"reference_url": "https://reference.example"},
        route_inventory=({"route": "/"},),
        viewports=DEFAULT_VIEWPORTS,
        coverage=baseline_matrix,
    )
    changed = dataclasses.replace(
        matrix.cells[first], observed_at=matrix.cells[first].observed_at + 1000
    )
    matrix.upsert(changed)
    changed_baseline = FidelityBaseline(
        profile="static",
        scope={"reference_url": "https://reference.example"},
        route_inventory=({"route": "/"},),
        viewports=DEFAULT_VIEWPORTS,
        coverage=matrix,
    )

    assert changed_baseline.content_hash == baseline.content_hash
    assert matrix.cells[first].observed_at > original_observed_at


def test_baseline_hash_is_deterministic_and_tamper_evident() -> None:
    matrix = _complete_static_matrix()
    baseline = FidelityBaseline(
        profile="static",
        scope={"reference_url": "https://reference.example"},
        route_inventory=({"route": "/"},),
        viewports=DEFAULT_VIEWPORTS,
        coverage=matrix,
        artifact_ids=("route-artifact",),
    )
    payload = baseline.to_dict()
    restored = FidelityBaseline.from_dict(payload)

    assert restored.content_hash == baseline.content_hash
    tampered = dict(payload)
    tampered["scope"] = {"reference_url": "https://changed.example"}
    with pytest.raises(ResearchValidationError, match="baseline hash"):
        FidelityBaseline.from_dict(tampered)


def test_interaction_trace_requires_observable_result() -> None:
    trace = InteractionTrace(
        trace_id="trace-1",
        cell_id="/|mobile|loaded|nav|reference",
        precondition="drawer is closed",
        action={"kind": "click", "target": "menu"},
        sequence=("click menu",),
        visible_outcome="drawer opens and receives focus",
        focus_effect="first drawer link receives focus",
    )
    assert InteractionTrace.from_dict(trace.to_dict()) == trace
    with pytest.raises(ResearchValidationError, match="visible_outcome"):
        dataclasses.replace(trace, visible_outcome="")


def test_custom_graph_requires_research_gate_and_rejects_unknown_targets() -> None:
    selected = RECONSTRUCT_PHASE_PLAN.active(
        "custom", ("init", "recon", "responsive_research", "research_gate", "final_validation")
    )
    assert selected.names == (
        "init",
        "recon",
        "responsive_research",
        "research_gate",
        "final_validation",
    )
    with pytest.raises(PhasePlanError, match="research_gate"):
        RECONSTRUCT_PHASE_PLAN.active("custom", ("init", "recon", "final_validation"))
    with pytest.raises(PhasePlanError, match="unknown phase"):
        selected.resolve_reentry("does_not_exist")


def test_legacy_plan_keeps_the_pre_research_gate_topology() -> None:
    selected = LEGACY_RECONSTRUCT_PHASE_PLAN.active("production", require_research_gate=False)

    assert LEGACY_PHASE_PLAN_VERSION == "reconstruct-site.v2"
    assert "responsive_research" not in selected.names
    assert "research_gate" not in selected.names
    assert selected.next_name("design_system") == "bootstrap"
