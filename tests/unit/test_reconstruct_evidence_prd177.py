"""Clean-room unit coverage for PRD-177's plan and evidence contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.tools.sandbox import WorkspaceView
from agenthicc.workflows.plugin import PhaseSpec
from agenthicc.workflows.reconstruct_site import (
    ReconstructContext,
    ReconstructSiteWorkflow,
    ReconstructState,
)
from agenthicc.workflows.reconstruct_site.evidence import (
    EvidenceError,
    EvidenceIntegrityError,
    ReconstructEvidenceStore,
)
from agenthicc.workflows.reconstruct_site.evidence_plan import (
    PHASE_PLAN_VERSION,
    PhasePlanError,
    RECONSTRUCT_PHASE_PLAN,
    ReconstructPhaseDefinition,
    ReconstructPhasePlan,
)


def test_plan_has_one_authoritative_order_and_real_profile_counts() -> None:
    assert len(RECONSTRUCT_PHASE_PLAN.names) == 41
    assert RECONSTRUCT_PHASE_PLAN.names[0] == "init"
    assert RECONSTRUCT_PHASE_PLAN.names[-1] == "final_validation"
    assert RECONSTRUCT_PHASE_PLAN.active("static").total_phases == 20
    assert RECONSTRUCT_PHASE_PLAN.active("application").total_phases == 21
    assert RECONSTRUCT_PHASE_PLAN.active("production").total_phases == 41


def test_custom_profile_is_explicit_and_validated() -> None:
    selected = RECONSTRUCT_PHASE_PLAN.active(
        "custom", ("init", "recon", "research_gate", "bootstrap", "final_validation")
    )
    assert selected.names == ("init", "recon", "research_gate", "bootstrap", "final_validation")
    assert any(item.name == "visual_research" for item in selected.skipped)

    with pytest.raises(PhasePlanError, match="unknown phases"):
        RECONSTRUCT_PHASE_PLAN.active("custom", ("init", "nope", "final_validation"))
    with pytest.raises(PhasePlanError, match="boundaries"):
        RECONSTRUCT_PHASE_PLAN.active("custom", ("recon", "final_validation"))


def test_active_definition_exposes_edges_retry_policy_and_capabilities() -> None:
    plan = RECONSTRUCT_PHASE_PLAN.active("static")
    init = plan.definition("init")
    assert init.next_phase == "recon"
    assert init.retry_phase == "init"
    assert init.agent_type == "auto"
    assert init.mode_override == "Yolo"
    assert init.required_capabilities
    assert "final_validation" in init.allowed_reentry_targets


def test_plan_rejects_unknown_explicit_graph_edges() -> None:
    with pytest.raises(PhasePlanError, match="unknown next phase"):
        ReconstructPhasePlan(
            (
                ReconstructPhaseDefinition("init", "_init", 1, "init", next_phase="missing"),
                ReconstructPhaseDefinition("final_validation", "_final_validation", 1, "final"),
            )
        )


def test_registry_metadata_is_mechanically_checked_against_the_plan() -> None:
    specs = list(ReconstructSiteWorkflow.phases)
    original = specs[0]
    specs[0] = PhaseSpec(
        name=original.name,
        max_turns=original.max_turns + 1,
        next=original.next,
        on_reject=original.on_reject,
    )
    with pytest.raises(PhasePlanError, match="max_turns"):
        RECONSTRUCT_PHASE_PLAN.validate_phase_specs(specs)


def test_reentry_never_falls_back_and_invalidation_is_downstream() -> None:
    plan = RECONSTRUCT_PHASE_PLAN.active("static")
    with pytest.raises(PhasePlanError, match="unknown phase"):
        plan.resolve_reentry("typo")
    kinds = plan.invalidated_kinds("design_system")
    assert "design_system" in kinds
    assert "visual_validation" in kinds
    assert "route_inventory" not in kinds


def test_atomic_artifact_write_is_hash_addressed_and_idempotent(tmp_path: Path) -> None:
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path), "run-177", plan_version=PHASE_PLAN_VERSION, profile="static"
    )
    first = store.put_json("route_inventory", [{"route": "/"}], phase="recon", attempt=1)
    second = store.put_json("route_inventory", [{"route": "/"}], phase="recon", attempt=1)
    assert first == second
    assert store.manifest.revision == 1
    assert Path(tmp_path / first.relative_path).read_bytes()
    compact = store.checkpoint_digest()
    assert '"route": "/"' not in json.dumps(compact)
    assert compact["artifact_refs"][0]["sha256"] == first.sha256


def test_large_artifact_is_externalized_without_an_artificial_limit(tmp_path: Path) -> None:
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path), "large-run", plan_version=PHASE_PLAN_VERSION, profile="static"
    )
    record = store.put("research", b"x" * 1_100_000, phase="recon", suffix=".bin")
    assert record.byte_count == 1_100_000
    assert store.verify() == []
    assert len(json.dumps(store.checkpoint_digest())) < 10_000


def test_artifact_provenance_links_are_validated_and_round_trip(tmp_path: Path) -> None:
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path), "provenance-run", plan_version=PHASE_PLAN_VERSION, profile="static"
    )
    record = store.put_json(
        "visual_measurements",
        {"heading": {"font_size": 32}},
        phase="visual_research",
        source_cells=("/|desktop|loaded|page|reference",),
    )

    assert record.source_cells == ("/|desktop|loaded|page|reference",)
    restored = ReconstructEvidenceStore(
        WorkspaceView(tmp_path), "provenance-run", plan_version=PHASE_PLAN_VERSION, profile="static"
    )
    assert restored.manifest.artifacts[0].source_cells == record.source_cells
    with pytest.raises(EvidenceError, match="source_cells"):
        store.put_json(
            "visual_measurements",
            {"heading": {"font_size": 32}},
            phase="visual_research",
            source_cells="not-a-cell-list",  # type: ignore[arg-type]
        )
    with pytest.raises(EvidenceError, match="source_cells"):
        store.put_json(
            "visual_measurements",
            {"heading": {"font_size": 32}},
            phase="visual_research",
            source_cells=[1],  # type: ignore[list-item]
        )


def test_integrity_check_detects_tampering_and_missing_files(tmp_path: Path) -> None:
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path), "integrity-run", plan_version=PHASE_PLAN_VERSION, profile="static"
    )
    record = store.put("visual_spec", "tokens", phase="visual_research", suffix=".txt")
    target = tmp_path / record.relative_path
    target.write_text("changed", encoding="utf-8")
    errors = store.verify()
    assert errors[0]["error"] == "content_hash_mismatch"
    with pytest.raises(EvidenceIntegrityError):
        store.read_kind("visual_spec")


def test_manifest_rejects_empty_profile_and_duplicate_records(tmp_path: Path) -> None:
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path), "manifest-run", plan_version=PHASE_PLAN_VERSION, profile="static"
    )
    record = store.put("route_inventory", "routes", phase="recon")
    payload = store.manifest.to_dict()
    payload["profile"] = ""
    with pytest.raises(EvidenceIntegrityError, match="identity"):
        store.manifest.from_dict(payload)

    payload = store.manifest.to_dict()
    payload["artifacts"] = [record.to_dict(), record.to_dict()]
    with pytest.raises(EvidenceIntegrityError, match="duplicate artifact"):
        store.manifest.from_dict(payload)


def test_screenshot_identity_deduplicates_and_redacts_url_credentials(tmp_path: Path) -> None:
    browser_path = tmp_path / ".agenthicc" / "browser-artifacts" / "session" / "shot.png"
    browser_path.parent.mkdir(parents=True)
    browser_path.write_bytes(b"png")
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path), "screen-run", plan_version=PHASE_PLAN_VERSION, profile="static"
    )
    payload = {"artifact_id": "browser-id", "path": str(browser_path)}
    first = store.record_screenshot(
        payload,
        role="reference",
        route="/home",
        url="https://user:password@example.test/home?token=secret#fragment",
        viewport="desktop",
        width=1440,
        height=900,
        backend="Playwright",
    )
    second = store.record_screenshot(
        payload,
        role="reference",
        route="/home",
        url="https://user:password@example.test/home?token=secret#fragment",
        viewport="desktop",
        width=1440,
        height=900,
        backend="Playwright",
    )
    assert first == second
    assert store.manifest.screenshots[0].url == "https://example.test/home"
    assert "password" not in json.dumps(store.manifest.to_dict())

    third = store.record_screenshot(
        {"artifact_id": "different-browser-id", "path": str(browser_path)},
        role="reference",
        route="/home",
        url="https://example.test/home",
        viewport="desktop",
        width=1440,
        height=900,
        backend="Playwright",
        source_cells=("/home|desktop|loaded|nav|reference",),
    )
    assert third == first

    assert store.attach_artifact_source_cells(
        "browser-id", ("/home|desktop|loaded|page|reference",)
    )
    linked = next(item for item in store.manifest.artifacts if item.artifact_id == "browser-id")
    assert linked.source_cells == (
        "/home|desktop|loaded|nav|reference",
        "/home|desktop|loaded|page|reference",
    )
    assert not store.attach_artifact_source_cells(
        "browser-id", ("/home|desktop|loaded|page|reference",)
    )


def test_screenshot_capture_rejects_invalid_identity(tmp_path: Path) -> None:
    browser_path = tmp_path / "shot.png"
    browser_path.write_bytes(b"png")
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path),
        "screen-validation-run",
        plan_version=PHASE_PLAN_VERSION,
        profile="static",
    )
    with pytest.raises(EvidenceError, match="role"):
        store.record_screenshot(
            {"artifact_id": "browser-id", "path": str(browser_path)},
            role="unknown",
            route="/",
            url="https://example.test/",
            viewport="mobile",
            width=390,
            height=844,
        )
    with pytest.raises(EvidenceError, match="dimensions"):
        store.record_screenshot(
            {"artifact_id": "browser-id", "path": str(browser_path)},
            role="reference",
            route="/",
            url="https://example.test/",
            viewport="mobile",
            width=-1,
            height=844,
        )


def test_degraded_screenshot_is_explicit_and_has_no_fake_artifact(tmp_path: Path) -> None:
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path), "degraded-run", plan_version=PHASE_PLAN_VERSION, profile="static"
    )
    result = store.record_degraded_screenshot(
        role="reference",
        route="/",
        viewport="mobile",
        backend="missing",
        reason="Playwright is unavailable",
    )
    assert result.status == "degraded"
    assert result.artifact_id is None
    assert store.manifest.artifacts == ()


def test_artifact_path_and_suffix_are_boundary_checked(tmp_path: Path) -> None:
    store = ReconstructEvidenceStore(
        WorkspaceView(tmp_path), "safe-run", plan_version=PHASE_PLAN_VERSION, profile="static"
    )
    with pytest.raises(EvidenceError):
        store.put("../../outside", "x", phase="recon")
    with pytest.raises(EvidenceError):
        store.put("safe", "x", phase="recon", suffix="../escape")


def test_checkpoint_codec_preserves_refs_and_drops_externalized_bodies() -> None:
    context = ReconstructContext(
        intent="rebuild",
        run_id="codec-run",
        state=ReconstructState.RECON,
        route_inventory=[{"route": "/", "body": "large"}],
        artifact_manifest_path=".agenthicc/reconstruct_site/codec-run/manifest.json",
        required_artifact_ids=["digest"],
        plan_version=PHASE_PLAN_VERSION,
        profile="static",
    )
    payload = ReconstructSiteWorkflow.checkpoint_context_to_payload(context)
    assert payload["route_inventory"] == []
    assert payload["artifact_manifest_path"].endswith("manifest.json")
    restored = ReconstructSiteWorkflow.checkpoint_context_from_payload(payload)
    assert restored.plan_version == PHASE_PLAN_VERSION
    assert restored.required_artifact_ids == ["digest"]
    assert restored.route_inventory == []
