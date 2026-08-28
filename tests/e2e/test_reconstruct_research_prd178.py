"""Offline end-to-end research-first reconstruct_site journey."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.tools.sandbox import WorkspaceScope
from agenthicc.workflows.reconstruct_site import (
    ReconstructSiteParams,
    ReconstructSiteRunner,
    ReconstructState,
)


def _config(tmp_path: Path) -> SimpleNamespace:
    execution = SimpleNamespace(
        effective_model=lambda: "fixture-model",
        effective_usable_budget=lambda: 10_000,
        provider="openai",
        model="fixture-model",
        profile="",
        base_url="",
    )
    return SimpleNamespace(
        app_state=SimpleNamespace(
            active_mode=lambda: SimpleNamespace(blocked_capabilities=frozenset()),
            update_workflow_phase=lambda **_kwargs: None,
        ),
        agent_runner=SimpleNamespace(),
        cfg=SimpleNamespace(execution=execution),
        params=ReconstructSiteParams(
            profile="custom",
            custom_phases=(
                "init",
                "recon",
                "visual_research",
                "interaction_analysis",
                "content_assets",
                "responsive_research",
                "architecture",
                "design_system",
                "research_gate",
                "final_validation",
            ),
        ),
        session_memory=object(),
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


class ResearchFirstRunner(ReconstructSiteRunner):
    """Deterministic phase adapters exercising the real outer loop and gate."""

    async def _init(self, context, _memory):
        context.target_url = "https://fixture.example"
        context.target_directory = "site"
        return ReconstructState.RECON

    async def _recon(self, context, _memory):
        context.route_inventory = [{"route": "/", "purpose": "home"}]
        context.pages_to_implement = ["/"]
        return ReconstructState.VISUAL_RESEARCH

    async def _visual_research(self, context, _memory):
        context.design_tokens = {"font": {"family": "Fixture Sans", "size": 16}}
        context.visual_observations = [{"route": "/", "viewport": "mobile"}]
        return ReconstructState.INTERACTION_ANALYSIS

    async def _interaction_analysis(self, context, _memory):
        context.interaction_inventory = [{"interaction": "nav"}]
        context.interaction_traces = []
        return ReconstructState.CONTENT_ASSETS

    async def _content_assets(self, context, _memory):
        context.asset_inventory = [{"name": "logo", "type": "svg"}]
        return ReconstructState.RESPONSIVE_RESEARCH

    async def _responsive_research(self, context, _memory):
        context.responsive_inventory = [{"route": "/", "viewport": "mobile"}]
        context.responsive_breakpoints = [{"min": 768, "rule": "drawer-to-nav"}]
        return ReconstructState.ARCHITECTURE

    async def _architecture(self, context, _memory):
        context.architecture = "fixture architecture"
        return ReconstructState.DESIGN_SYSTEM

    async def _design_system(self, context, _memory):
        context.component_inventory = [{"pattern": "nav", "primitive": "navigation"}]
        return ReconstructState.RESEARCH_GATE

    async def _research_gate(self, context, memory):
        async def approve_degraded(**kwargs: object) -> None:
            tools = kwargs["tools"]
            await tools[1](
                exception_ids=list(self._active_context.unresolved_research),
                rationale="The deterministic E2E run intentionally has no browser backend.",
                baseline_artifact_id=self._active_context.research_baseline_id,
            )

        self.run_phase = approve_degraded  # type: ignore[method-assign]
        return await super()._research_gate(context, memory)

    async def _final_validation(self, context, _memory):
        context.validation_status["final"] = "approved"
        return ReconstructState.COMPLETE


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_research_first_journey_persists_gate_and_degraded_decision(tmp_path: Path) -> None:
    runner = ResearchFirstRunner(_config(tmp_path), None)
    completed = await runner.run("reconstruct the fixture site")

    assert completed.state is ReconstructState.COMPLETE
    assert completed.research_gate_status == "approved_degraded"
    assert completed.research_baseline_id
    assert "research_gate" in completed.completed_phases
    manifest = json.loads((tmp_path / completed.artifact_manifest_path).read_text(encoding="utf-8"))
    kinds = {item["kind"] for item in manifest["artifacts"]}
    assert {"research_coverage_report", "fidelity_baseline", "research_gate_receipt"} <= kinds
    assert manifest["status"] == "complete"
