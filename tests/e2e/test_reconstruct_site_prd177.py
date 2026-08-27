"""Offline end-to-end journeys for the optimized reconstruct workflow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.tools.sandbox import WorkspaceScope, WorkspaceView
from agenthicc.workflows.reconstruct_site import (
    ReconstructSiteParams,
    ReconstructSiteRunner,
    ReconstructState,
    ReconstructSiteWorkflow,
)
from agenthicc.workflows.reconstruct_site.evidence import ReconstructEvidenceStore
from agenthicc.workflows.reconstruct_site.evidence_plan import RECONSTRUCT_PHASE_PLAN


def _config(tmp_path: Path, params: ReconstructSiteParams, memory: object) -> SimpleNamespace:
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
        session_memory=memory,
        workflow_handle=None,
        workspace_scope=WorkspaceScope.create(tmp_path),
        browser_manager=None,
        browser_tools=(),
        plugin_tools=[],
        mcp_registry=None,
        memory_router=None,
        semantic_index=None,
        approval_svc=None,
        terminal_wait_policies={},
    )


class DeterministicRunner(ReconstructSiteRunner):
    """Provider-free runner that exercises the real outer-loop boundary."""

    async def _init(self, context, _memory):
        self._active_plan = RECONSTRUCT_PHASE_PLAN.active("static")
        context.profile = "static"
        context.skipped_reasons = {item.name: item.reason for item in self._active_plan.skipped}
        context.skipped_phases = list(context.skipped_reasons)
        self._ensure_evidence(context).set_skipped(
            (item.name, item.reason) for item in self._active_plan.skipped
        )
        context.target_url = "https://reference.example"
        context.pages_to_implement = ["/"]
        context.route_inventory = [{"route": "/", "purpose": "home"}]
        context.artifacts["initial_state"] = "static scope selected"
        return ReconstructState.RECON

    async def _recon(self, context, _memory):
        context.last_transition = "submit_route_inventory"
        return ReconstructState.FINAL_VALIDATION

    async def _final_validation(self, context, _memory):
        context.validation_status["final"] = "approved"
        context.last_transition = "final_approved"
        return ReconstructState.COMPLETE


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_static_reconstruction_resume_rehydrates_final_evidence(tmp_path: Path) -> None:
    memory = object()
    params = ReconstructSiteParams(profile="static")
    runner = DeterministicRunner(_config(tmp_path, params, memory), None)

    completed = await runner.run("reconstruct https://reference.example")

    assert completed.state is ReconstructState.COMPLETE
    assert completed.shared_memory is memory
    assert completed.page_progress == {"completed": 0, "total": 1, "current": 1}
    assert completed.artifact_manifest_path.endswith("manifest.json")
    manifest_path = tmp_path / completed.artifact_manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert {item["phase"] for item in manifest["skipped_phases"]} >= {"sqlite_db", "docker"}
    assert any(item["status"] == "degraded" for item in manifest["screenshots"])

    restored_payload = ReconstructSiteWorkflow.checkpoint_context_to_payload(completed)
    assert restored_payload["route_inventory"] == []
    restored = ReconstructSiteWorkflow.checkpoint_context_from_payload(
        restored_payload, memory=memory
    )
    resumed = await DeterministicRunner(_config(tmp_path, params, memory), None).resume(restored)

    assert resumed.state is ReconstructState.COMPLETE
    assert resumed.profile == "static"
    assert resumed.shared_memory is memory
    assert (
        ReconstructEvidenceStore(
            WorkspaceView(tmp_path),
            resumed.run_id,
            plan_version=resumed.plan_version,
            profile=resumed.profile,
        ).verify()
        == []
    )
