"""Specialized phase, routing, tool-contract, and checkpoint tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.loader import builtin_workflow_descriptors
from agenthicc.workflows.registry import build_workflow_registry
from agenthicc.workflows.ui_ux_pro_max.definition import UiUxProMax, UiUxProMaxParams
from agenthicc.workflows.ui_ux_pro_max.phase_tools import (
    make_design_acceptance_tools,
    make_design_research_tools,
    make_design_system_review_tools,
    make_design_system_synthesis_tools,
    make_experience_architecture_tools,
    make_product_discovery_tools,
    make_stack_adaptation_tools,
    make_visual_audit_tools,
)
from agenthicc.workflows.ui_ux_pro_max.runner import (
    CACHE_CONTRACT,
    PHASE_NAMES,
    UiUxProMaxRunner,
)
from agenthicc.workflows.ui_ux_pro_max.state import (
    PHASES_BY_PROFILE,
    UiUxProMaxContext,
    UiUxProMaxState,
)

pytestmark = pytest.mark.unit


def _tool_map(tools: Sequence[object]) -> dict[str, object]:
    return {tool.__name__: tool for tool in tools}  # type: ignore[attr-defined]


async def _call(tools: Sequence[object], name: str, **kwargs: object) -> object:
    function = _tool_map(tools)[name]
    assert callable(function)
    return await function(**kwargs)


def _discovery_payload(scope: str = "broad") -> dict[str, object]:
    return {
        "summary": "Existing application and user request inspected.",
        "scope": scope,
        "project_name": "sample",
        "project_type": "analytics dashboard",
        "platform": "web",
        "stack": "React and TypeScript",
        "request_kind": "redesign" if scope == "broad" else "accessibility audit",
        "target_users": ["operations staff"],
        "user_journeys": ["inspect current alerts"],
        "required_surfaces": ["dashboard", "alert detail"],
        "functional_requirements": ["keep filtering behavior"],
        "ux_requirements": ["make alert status scannable"],
        "accessibility_expectations": ["keyboard navigation", "visible focus"],
        "technical_constraints": ["reuse existing routes"],
        "existing_visual_conventions": ["blue brand token"],
        "acceptance_criteria": ["keyboard navigation", "alert status is scannable"],
        "assumptions": ["desktop and mobile are in scope"],
    }


def test_builtin_registry_exposes_specialist_workflow_phases() -> None:
    descriptors = builtin_workflow_descriptors()
    descriptor = next(item for item in descriptors if item.name == "ui_ux_pro_max")
    assert descriptor.module == "agenthicc.workflows.ui_ux_pro_max.definition"
    assert build_workflow_registry().get("ui_ux_pro_max") is UiUxProMax
    assert UiUxProMax.phase_names() == list(PHASE_NAMES)
    assert UiUxProMax.mode_bindings == []
    assert {"design_system_synthesis", "visual_ux_audit", "design_acceptance"} <= set(
        UiUxProMax.phase_names()
    )


def test_checkpoint_topology_is_profile_aware_and_preserves_branch_edges() -> None:
    broad = UiUxProMax.resolve_checkpoint_topology({"route_profile": "broad"})
    targeted = UiUxProMax.resolve_checkpoint_topology({"route_profile": "targeted"})
    accessibility = UiUxProMax.resolve_checkpoint_topology({"route_profile": "accessibility_audit"})

    assert broad.profile == "broad"
    assert broad.phase_names == PHASES_BY_PROFILE["broad"]
    assert broad.reject_phases[broad.index_for("design_system_review")] == "design_system_synthesis"
    assert broad.reject_phases[broad.index_for("visual_ux_audit")] == "design_remediation"
    assert "design_system_synthesis" not in targeted.phase_names
    assert "design_remediation" not in accessibility.phase_names
    assert "design_implementation" not in accessibility.phase_names
    assert broad.topology_fingerprint != targeted.topology_fingerprint


def test_checkpoint_topology_normalizes_unknown_profile_to_broad() -> None:
    topology = UiUxProMax.resolve_checkpoint_topology({"route_profile": "unknown"})

    assert topology.profile == "broad"
    assert topology.phase_names == PHASES_BY_PROFILE["broad"]


def test_params_bound_loops_and_expose_phase_model_overrides() -> None:
    params = UiUxProMax.build_params(
        {
            "product_discovery_model": " fast-model ",
            "visual_ux_audit_model": "review-model",
            "max_design_review_attempts": 999,
            "max_remediation_cycles": -3,
            "max_phase_turns": 100,
        }
    )
    assert isinstance(params, UiUxProMaxParams)
    assert params.product_discovery_model == "fast-model"
    assert params.visual_ux_audit_model == "review-model"
    assert params.max_design_review_attempts == 5
    assert params.max_remediation_cycles == 0
    assert params.max_phase_turns == 40
    assert params.model_for_phase("product_discovery", "fallback") == "fast-model"
    assert params.model_for_phase("delivery", "fallback") == "fallback"


def test_context_checkpoint_is_bounded_json_and_reattaches_same_memory() -> None:
    memory = object()
    context = UiUxProMaxContext(
        intent="Create a terminal dashboard",
        run_id="run-1",
        state=UiUxProMaxState.DESIGN_REMEDIATION,
        route_profile="broad",
        shared_memory=memory,  # type: ignore[arg-type]
        project_root="/repo/app",
        platform="terminal",
        stack="Textual",
        acceptance_criteria=["keyboard accessible", "no clipping"],
        design_review_attempts=1,
        remediation_cycles=1,
        phase_iteration=13,
        completed_phases=["product_discovery", "design_intelligence"],
        last_boundary={
            "completed_phase": "visual_ux_audit",
            "next_phase": "design_remediation",
            "outcome": "rejected",
            "durable": True,
        },
    )
    context.add_artifact(
        "design_intelligence", "report", "Search not available", ["notes/research.md"]
    )
    payload = UiUxProMax.checkpoint_context_to_payload(context)
    assert payload is not None
    assert "shared_memory" not in payload
    json.dumps(payload)
    restored = UiUxProMax.checkpoint_context_from_payload(payload, memory)
    assert isinstance(restored, UiUxProMaxContext)
    assert restored.shared_memory is memory
    assert restored.state is UiUxProMaxState.DESIGN_REMEDIATION
    assert restored.route_profile == "broad"
    assert restored.design_review_attempts == 1
    assert restored.remediation_cycles == 1
    assert restored.artifacts["design_intelligence"].references == ["notes/research.md"]

    large = UiUxProMaxContext(intent="x" * 30_000, run_id="run")
    large.research_findings = ["y" * 4_000] * 1_000
    bounded = large.to_checkpoint_payload()
    assert len(json.dumps(bounded)) < 800_000
    assert len(bounded["research_findings"]) == 40  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_state", ["INIT", "ANALYZE", "VISUAL_AUDIT"])
def test_invalid_checkpoint_states_fail_closed(bad_state: str) -> None:
    with pytest.raises(ValueError, match="unknown workflow state"):
        UiUxProMaxContext.from_checkpoint_payload(
            {"intent": "intent", "run_id": "run", "state": bad_state}
        )
    assert (
        UiUxProMax.checkpoint_context_from_payload(
            {"intent": "intent", "run_id": "run", "state": bad_state}
        )
        is None
    )


def test_checkpoint_rejects_invalid_route_profile_and_filters_malformed_counts() -> None:
    with pytest.raises(ValueError, match="unknown route profile"):
        UiUxProMaxContext.from_checkpoint_payload(
            {"intent": "intent", "run_id": "run", "state": "PRODUCT_DISCOVERY", "route_profile": []}
        )
    context = UiUxProMaxContext.from_checkpoint_payload(
        {
            "intent": "intent",
            "run_id": "run",
            "state": "DESIGN_IMPLEMENTATION",
            "phase_attempts": {"design_implementation": 4, "delivery": True, "invalid": "many"},
        }
    )
    assert context.phase_attempts == {"design_implementation": 4}


@pytest.mark.asyncio
async def test_product_discovery_transition_records_without_validating_details() -> None:
    event, data = asyncio.Event(), {}
    tools = make_product_discovery_tools(event, data)
    assert all(ToolCapability.CONTROL in get_tool_capabilities(tool) for tool in tools)

    details = {"scope": "unknown", "acceptance_criteria": []}
    result = await _call(
        tools,
        "complete_product_discovery",
        summary="Discovery summary",
        details=details,
    )

    assert result["ok"] is True  # type: ignore[index]
    assert event.is_set()
    assert data["scope"] == "unknown"
    assert data["acceptance_criteria"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("broad", UiUxProMaxState.DESIGN_INTELLIGENCE),
        ("targeted", UiUxProMaxState.TARGETED_DESIGN_RESEARCH),
        ("accessibility_audit", UiUxProMaxState.TARGETED_DESIGN_RESEARCH),
    ],
)
async def test_discovery_selects_the_adaptive_research_route(
    tmp_path: Path, scope: str, expected: UiUxProMaxState
) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="Improve this interface", run_id="route")

    async def discovery(
        self: UiUxProMaxRunner, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return _discovery_payload(scope)

    runner._run_phase = MethodType(discovery, runner)  # type: ignore[method-assign]
    assert await runner._product_discovery(context) is expected
    assert context.route_profile == scope
    assert context.acceptance_criteria


@pytest.mark.asyncio
async def test_failed_search_and_missing_datasets_are_recorded_as_limitations(
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(
        intent="Fix the alert status contrast", run_id="research", route_profile="targeted"
    )

    async def failed_search(
        self: UiUxProMaxRunner, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {
            "summary": "Skill search failed; no upstream findings are claimed.",
            "queries": ["alert status contrast"],
            "datasets_consulted": [],
            "findings": [],
            "source_provenance": [],
            "applicable_recommendations": [],
            "rejected_recommendations": [],
            "conflicts": [],
            "confidence": "low",
            "limitations": ["search.py exited with an error", "UX dataset unavailable"],
            "artifact_refs": ["docs/design/research.md"],
        }

    runner._run_phase = MethodType(failed_search, runner)  # type: ignore[method-assign]
    next_state = await runner._design_research(context, targeted=True)

    assert next_state is UiUxProMaxState.STACK_ADAPTATION
    assert context.research_datasets == []
    assert context.research_findings == []
    assert context.research_limitations == [
        "search.py exited with an error",
        "UX dataset unavailable",
    ]
    assert context.research_artifact_refs == ["docs/design/research.md"]


@pytest.mark.asyncio
async def test_unknown_stack_is_explicit_and_does_not_block_adaptation(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="Fix a component", run_id="stack", route_profile="targeted")

    async def unknown_stack(
        self: UiUxProMaxRunner, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {
            "summary": "Stack could not be detected from the available project files.",
            "detected_stack": "unknown",
            "stack_evidence": [],
            "limitations": ["No framework manifest was present."],
            "artifact_refs": [],
        }

    runner._run_phase = MethodType(unknown_stack, runner)  # type: ignore[method-assign]
    next_state = await runner._stack_adaptation(context)

    assert next_state is UiUxProMaxState.EXPERIENCE_ARCHITECTURE
    assert context.detected_stack == "unknown"
    assert context.stack_limitations == ["No framework manifest was present."]


@pytest.mark.asyncio
async def test_implementation_provider_failure_keeps_exact_phase_cursor(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="Implement the approved component", run_id="implementation")

    async def provider_failure(self: UiUxProMaxRunner, **_kwargs: object) -> None:
        context.state = UiUxProMaxState.DESIGN_IMPLEMENTATION
        raise RuntimeError("mock provider failure")

    runner.run_phase = MethodType(provider_failure, runner)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="mock provider failure"):
        await runner._run_phase(
            context,
            "design_implementation",
            "Implement the approved changes.",
            runner._implementation_tools(),
            "complete_design_implementation",
        )
    assert context.state is UiUxProMaxState.DESIGN_IMPLEMENTATION
    assert context.phase_attempts["design_implementation"] == 1


@pytest.mark.asyncio
async def test_phase_continues_when_a_turn_ends_without_its_transition_tool(
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="Implement the approved component", run_id="implementation")
    tools = runner._implementation_tools()
    calls: list[str] = []

    async def finish_on_retry(self: UiUxProMaxRunner, **kwargs: object) -> None:
        text = kwargs.get("text")
        calls.append(text if isinstance(text, str) else "")
        if len(calls) == 2:
            result = await _call(
                tools,
                "complete_design_implementation",
                summary="Implemented the approved component and its empty state.",
                details={"modified_files": ["src/component.tsx"]},
            )
            assert result["ok"] is True  # type: ignore[index]

    runner.run_phase = MethodType(finish_on_retry, runner)  # type: ignore[method-assign]
    result = await runner._run_phase(
        context,
        "design_implementation",
        "Implement the approved changes.",
        tools,
        "complete_design_implementation",
    )

    assert len(calls) == 2
    assert calls[0] == "Implement the approved changes."
    assert "Continue from the existing conversation" in calls[1]
    assert result["decision"] == "complete_design_implementation"
    assert result["modified_files"] == ["src/component.tsx"]


@pytest.mark.asyncio
async def test_phase_fails_after_bounded_missing_transition_retries(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="Implement the approved component", run_id="implementation")
    calls: list[str] = []

    async def no_transition(self: UiUxProMaxRunner, **kwargs: object) -> None:
        text = kwargs.get("text")
        calls.append(text if isinstance(text, str) else "")

    runner.run_phase = MethodType(no_transition, runner)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="complete_design_implementation.*3 attempts"):
        await runner._run_phase(
            context,
            "design_implementation",
            "Implement the approved changes.",
            runner._implementation_tools(),
            "complete_design_implementation",
        )

    assert len(calls) == 3
    assert context.state is UiUxProMaxState.DESIGN_IMPLEMENTATION
    assert context.phase_attempts["design_implementation"] == 1


@pytest.mark.asyncio
async def test_design_system_review_uses_explicit_outcomes_and_bounds_revisions() -> None:
    event, data = asyncio.Event(), {}
    tools = make_design_system_review_tools(event, data, attempts=0, max_attempts=1)
    revision = await _call(
        tools,
        "request_design_system_revision",
        summary="Review found an inconsistency",
        details={"actionable_findings": ["Contrast token fails for body text"]},
    )
    assert revision["ok"] is True  # type: ignore[index]
    assert data["decision"] == "request_design_system_revision"

    event.clear()
    blocked_tools = make_design_system_review_tools(event, data, attempts=1, max_attempts=1)
    exhausted = await _call(
        blocked_tools,
        "request_design_system_revision",
        summary="The same issue remains",
        details={"actionable_findings": ["Contrast token still fails"]},
    )
    assert exhausted["ok"] is True  # type: ignore[index]
    assert data["decision"] == "block_design_system"


@pytest.mark.asyncio
async def test_design_system_review_block_routes_to_explicit_unverified_acceptance(
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(
        intent="Create a product UI",
        run_id="blocked-review",
        acceptance_criteria=["contrast is sufficient"],
    )

    async def blocked_review(
        self: UiUxProMaxRunner, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {
            "decision": "block_design_system",
            "summary": "No feasible contrast pairing can be established from available evidence.",
            "findings": ["Need an authoritative brand contrast requirement."],
        }

    runner._run_phase = MethodType(blocked_review, runner)  # type: ignore[method-assign]
    next_state = await runner._design_system_review(context)

    assert next_state is UiUxProMaxState.DESIGN_ACCEPTANCE
    assert context.design_review_status == "blocked"
    assert context.unverified_criteria == ["contrast is sufficient"]


@pytest.mark.asyncio
async def test_research_tool_accepts_explicit_unavailable_dataset_report() -> None:
    event, data = asyncio.Event(), {}
    tools = make_design_research_tools(event, data)
    result = await _call(
        tools,
        "submit_design_research",
        summary="Search failed and no upstream guidance is claimed.",
        details={
            "queries": ["accessible terminal focus", "recorded failed search attempt"],
            "datasets_consulted": [],
            "findings": [],
            "source_provenance": [],
            "applicable_recommendations": [],
            "rejected_recommendations": [],
            "conflicts": [],
            "confidence": "unknown",
            "limitations": ["dataset missing", "search command returned non-zero"],
            "artifact_refs": ["research/findings.md"],
        },
    )

    assert result["ok"] is True  # type: ignore[index]
    assert data["datasets_consulted"] == []
    assert data["limitations"] == ["dataset missing", "search command returned non-zero"]


@pytest.mark.asyncio
async def test_stack_transition_records_unknown_stack_without_validation() -> None:
    event, data = asyncio.Event(), {}
    tools = make_stack_adaptation_tools(event, data)
    accepted = await _call(
        tools,
        "submit_implementation_guide",
        summary="Stack detection had insufficient evidence.",
        details={"detected_stack": "", "stack_evidence": []},
    )

    assert accepted["ok"] is True  # type: ignore[index]
    assert data["detected_stack"] == ""


@pytest.mark.asyncio
async def test_design_system_transition_records_incomplete_details() -> None:
    event, data = asyncio.Event(), {}
    tools = make_design_system_synthesis_tools(event, data)
    details = {"color_tokens": [], "artifact_refs": []}
    result = await _call(
        tools,
        "submit_design_system",
        summary="A vague design direction",
        details=details,
    )

    assert result["ok"] is True  # type: ignore[index]
    assert event.is_set()
    assert data["color_tokens"] == []


@pytest.mark.asyncio
async def test_experience_architecture_requires_approval_not_content_verification() -> None:
    event, data = asyncio.Event(), {}
    tools = make_experience_architecture_tools(None, event, data)
    by_name = _tool_map(tools)
    submission = by_name["submit_experience_architecture"]
    request = by_name["request_implementation_approval"]
    assert callable(submission) and callable(request)

    details = {"implementation_tasks": ["update the dashboard"]}
    denied = await submission(summary="Screens and states mapped", details=details)
    assert denied["ok"] is False  # type: ignore[index]
    assert not event.is_set()

    approved = await request("Implement the dashboard")
    assert approved["approved"] is True  # type: ignore[index]
    result = await submission(summary="Screens and states mapped", details=details)
    assert result["ok"] is True  # type: ignore[index]
    assert data["implementation_tasks"] == ["update the dashboard"]


@pytest.mark.asyncio
async def test_acceptance_transition_records_results_without_verifying_them() -> None:
    event, data = asyncio.Event(), {}
    tools = make_design_acceptance_tools(event, data)
    unverified_results = [
        "PASS | keyboard navigation |",
        "PASS | unknown criterion | no evidence",
        "FAIL | keyboard navigation | duplicate result",
    ]

    result = await _call(
        tools,
        "complete_design_acceptance",
        summary="Acceptance review submitted.",
        details={"criterion_results": unverified_results},
    )

    assert result["ok"] is True  # type: ignore[index]
    assert event.is_set()
    assert data["criterion_results"] == unverified_results


@pytest.mark.asyncio
async def test_acceptance_runner_marks_missing_results_unverified(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    criteria = ["keyboard navigation", "visible focus"]
    context = UiUxProMaxContext(
        intent="Audit the interface", run_id="incomplete-acceptance", acceptance_criteria=criteria
    )

    async def acceptance(
        self: UiUxProMaxRunner, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {"summary": "Acceptance summary", "criterion_results": []}

    runner._run_phase = MethodType(acceptance, runner)  # type: ignore[method-assign]

    next_state = await runner._design_acceptance(context)

    assert next_state is UiUxProMaxState.DELIVERY
    assert context.unverified_criteria == criteria


@pytest.mark.asyncio
async def test_visual_audit_transition_does_not_validate_inspection_status() -> None:
    event, data = asyncio.Event(), {}
    tools = make_visual_audit_tools(event, data)
    result = await _call(
        tools,
        "submit_visual_ux_audit",
        summary="Audit done",
        details={"visual_inspection_status": "looks good"},
    )

    assert result["ok"] is True  # type: ignore[index]
    assert data["visual_inspection_status"] == "looks good"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "expected", "cycles"),
    [
        (0, UiUxProMaxState.DESIGN_ACCEPTANCE, 0),
        (1, UiUxProMaxState.DESIGN_REMEDIATION, 1),
    ],
)
async def test_visual_findings_respect_remediation_cycle_budget(
    tmp_path: Path,
    budget: int,
    expected: UiUxProMaxState,
    cycles: int,
) -> None:
    runner = _make_runner(tmp_path, max_remediation_cycles=budget)
    context = UiUxProMaxContext(
        intent="Improve the interface", run_id="audit", route_profile="targeted"
    )

    async def audit(self: UiUxProMaxRunner, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "summary": "A high-impact focus issue remains.",
            "findings": [
                "High | Dialog | focus escapes | keep focus inside | observed source and test"
            ],
            "visual_inspection_status": "unavailable",
            "visual_evidence_refs": [],
            "command_results": [],
            "acceptance_evidence": [],
            "limitations": ["No terminal capture tool available."],
        }

    runner._run_phase = MethodType(audit, runner)  # type: ignore[method-assign]
    next_state = await runner._visual_ux_audit(context)

    assert next_state is expected
    assert context.remediation_cycles == cycles


@pytest.mark.asyncio
async def test_delivery_blocks_when_any_acceptance_criterion_is_unverified(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(
        intent="Audit visual contrast", run_id="partial", acceptance_criteria=["contrast verified"]
    )
    context.unverified_criteria = ["contrast verified"]

    async def delivery(
        self: UiUxProMaxRunner, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {"summary": "Source checks passed, but contrast measurement is unavailable."}

    runner._run_phase = MethodType(delivery, runner)  # type: ignore[method-assign]
    assert await runner._delivery(context) is UiUxProMaxState.BLOCKED


def _make_runner(tmp_path: Path, *, max_remediation_cycles: int = 2) -> UiUxProMaxRunner:
    app = AppState.create()

    async def emit(_event: object) -> None:
        return None

    cfg = AgenthiccConfig()
    cfg.execution.model = "unit-test-model"  # type: ignore[misc]
    cfg.execution.effective_usable_budget = lambda: 1_000  # type: ignore[method-assign]
    config = WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=SimpleNamespace(emit=emit),  # type: ignore[arg-type]
        agent_runner=SimpleNamespace(
            _transport=SimpleNamespace(_config=SimpleNamespace(model="unit-test-model"))
        ),  # type: ignore[arg-type]
        approval_svc=None,
        cfg=cfg,
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=SimpleNamespace(),  # type: ignore[arg-type]
        agents_registry=SimpleNamespace(),  # type: ignore[arg-type]
        params=UiUxProMaxParams(max_remediation_cycles=max_remediation_cycles),
        session_memory=object(),  # type: ignore[arg-type]
        workspace_scope=SimpleNamespace(primary_root=tmp_path),  # type: ignore[arg-type]
    )
    runner = UiUxProMaxRunner(config)
    runner._cfg.app_state.update_workflow_phase = lambda **_kwargs: None  # type: ignore[method-assign]
    return runner


def test_cache_contract_keeps_changing_artifacts_out_of_stable_prefix() -> None:
    for dynamic_name in (
        "acceptance criteria",
        "research",
        "retry state",
        "artifact references",
    ):
        assert dynamic_name in CACHE_CONTRACT
    assert "do not" in CACHE_CONTRACT.lower()
    assert "same session conversation ID" in CACHE_CONTRACT


@pytest.mark.asyncio
async def test_resume_preserves_branch_cursor_instead_of_restarting_or_following_linear_edge(
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(
        intent="Audit this UI",
        run_id="run-branch",
        state=UiUxProMaxState.DESIGN_ACCEPTANCE,
        route_profile="broad",
        completed_phases=[
            "product_discovery",
            "design_intelligence",
            "design_system_synthesis",
            "design_system_review",
            "stack_adaptation",
            "experience_architecture",
            "design_implementation",
            "visual_ux_audit",
        ],
        last_boundary={
            "completed_phase": "visual_ux_audit",
            "next_phase": "design_acceptance",
            "outcome": "branch",
            "durable": True,
        },
    )
    runner._reconcile_resume_cursor(context)
    assert context.state is UiUxProMaxState.DESIGN_ACCEPTANCE
    assert context.resume_resolution_source == "reentry_cursor"


@pytest.mark.asyncio
async def test_accessibility_scope_skips_generation_and_implementation(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="Audit keyboard navigation", run_id="audit")
    context.route_profile = "accessibility_audit"
    assert runner._active_phases(context) == PHASES_BY_PROFILE["accessibility_audit"]

    async def stack_result(
        self: UiUxProMaxRunner,
        *_args: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "summary": "Detected stack guide",
            "detected_stack": "Textual",
            "stack_evidence": ["pyproject.toml"],
            "limitations": [],
            "artifact_refs": [],
        }

    runner._run_phase = MethodType(stack_result, runner)  # type: ignore[method-assign]
    next_state = await runner._stack_adaptation(context)
    assert next_state is UiUxProMaxState.VISUAL_UX_AUDIT


def test_phase_tools_are_individually_workflow_control_tools() -> None:
    factories = (
        make_product_discovery_tools,
        make_visual_audit_tools,
    )
    for factory in factories:
        tools = factory(asyncio.Event(), {})
        assert tools
        assert all(ToolCapability.CONTROL in get_tool_capabilities(tool) for tool in tools)
