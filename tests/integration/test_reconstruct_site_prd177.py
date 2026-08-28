"""Integration coverage for the active in-directory reconstruct workflow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.tools.sandbox import WorkspaceScope
from agenthicc.workflows.loader import builtin_workflow_descriptors, load_builtin_workflow
from agenthicc.workflows.reconstruct_site import (
    ReconstructSiteParams,
    ReconstructSiteRunner,
    ReconstructSiteWorkflow,
    ReconstructState,
)
from agenthicc.workflows.reconstruct_site.evidence_plan import RECONSTRUCT_PHASE_PLAN


def _config(tmp_path: Path, params: ReconstructSiteParams) -> SimpleNamespace:
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
        all_plugin_tools=lambda: [],
        params=params,
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


def test_registry_loads_the_in_directory_runner() -> None:
    descriptor = next(
        item for item in builtin_workflow_descriptors() if item.name == "reconstruct_site"
    )
    plugin = load_builtin_workflow(descriptor)
    assert plugin is ReconstructSiteWorkflow
    assert plugin.__module__ == "agenthicc.workflows.reconstruct_site.runner"
    assert ReconstructSiteRunner.total_phases == len(RECONSTRUCT_PHASE_PLAN.names)


@pytest.mark.asyncio
async def test_fresh_and_resume_dispatch_use_the_same_authoritative_plan(tmp_path: Path) -> None:
    params = ReconstructSiteParams(
        profile="custom",
        custom_phases=("init", "research_gate", "final_validation"),
    )
    config = _config(tmp_path, params)

    class FakeRunner(ReconstructSiteRunner):
        async def _init(self, context, _memory):
            self._active_plan = RECONSTRUCT_PHASE_PLAN.active("custom", params.custom_phases)
            context.profile = "custom"
            context.artifacts["initial_state"] = "scope selected"
            return ReconstructState.FINAL_VALIDATION

        async def _final_validation(self, _context, _memory):
            return ReconstructState.COMPLETE

    runner = FakeRunner(config, None)
    fresh = await runner.run("reconstruct a static site")
    assert fresh.state is ReconstructState.COMPLETE
    assert fresh.profile == "custom"
    assert fresh.skipped_phases
    assert fresh.artifact_manifest_path.endswith("manifest.json")
    assert fresh.artifact_manifest_revision > 0

    resumed = await runner.resume(fresh)
    assert resumed.state is ReconstructState.COMPLETE
    assert resumed.profile == "custom"
    assert resumed.artifact_manifest_revision >= fresh.artifact_manifest_revision


def test_params_route_infra_model_overrides_without_changing_schema() -> None:
    params = ReconstructSiteWorkflow.build_params(
        {
            "profile": "production",
            "sqlite_db_model": "cheap-model",
            "phase_models": {"docs": "docs-model"},
        }
    )
    assert isinstance(params, ReconstructSiteParams)
    assert params.model_for_phase("sqlite_db", "global") == "cheap-model"
    assert params.model_for_phase("docs", "global") == "docs-model"
    assert params.model_for_phase("recon", "global") == "global"


def test_stable_tool_bundle_is_compiled_once_until_explicit_epoch_change(
    tmp_path: Path,
) -> None:
    runner = ReconstructSiteRunner(_config(tmp_path, ReconstructSiteParams()), None)
    first = runner._base_tools()
    second = runner._base_tools()
    assert first == second
    assert runner._tool_cache_epoch == 0

    runner.invalidate_tool_bundle_cache(reason="mcp_reload")
    third = runner._base_tools()
    assert runner._tool_cache_epoch == 1
    assert [getattr(item, "__name__", "") for item in third] == [
        getattr(item, "__name__", "") for item in first
    ]


@pytest.mark.asyncio
async def test_phase_model_override_reaches_public_phase_turn(tmp_path: Path) -> None:
    params = ReconstructSiteParams(phase_models={"recon": "research-model"})
    runner = ReconstructSiteRunner(_config(tmp_path, params), None)
    runner._run_id = "model-run"
    runner._active_phase_name = "recon"
    captured: dict[str, object] = {}

    async def fake_run_turn(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    runner._run_turn = fake_run_turn  # type: ignore[method-assign]
    await runner.run_phase(
        intent="reconstruct",
        text="inspect",
        system_prompt="phase",
        stable_system_prompt="stable",
        shared_memory=object(),
        tools=[],
    )
    assert captured["phase_name"] == "recon"
    assert captured["model_override"] == "research-model"
