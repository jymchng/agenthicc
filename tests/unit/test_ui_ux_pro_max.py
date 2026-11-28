"""Specialized phase, routing, tool-contract, and checkpoint tests."""

from __future__ import annotations

from types import MappingProxyType
from agenthicc.tools.surface import ToolSurface, ToolSurfaceHolder
from agenthicc.runners.reload import SurfaceHolder
import asyncio
import ast
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
from agenthicc.workflows.plugin import WorkflowRun
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
    active_phases_for,
    ensure_phase_is_active,
    next_state_after_design_research,
    next_state_after_product_discovery,
    next_state_after_stack_adaptation,
    next_state_after_visual_ux_audit,
    phase_index_in,
    route_profile_from_details,
)
from agenthicc.workflows.ui_ux_pro_max.state import (
    PHASES_BY_PROFILE,
    PhaseTopologyError,
    UiUxProMaxContext,
    UiUxProMaxState,
    normalise_route_profile,
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


@pytest.mark.parametrize(
    ("supplied", "canonical", "fallback"),
    [
        ("", "broad", True),
        ("Broad", "broad", False),
        ("broad ", "broad", False),
        ("broad", "broad", False),
        ("BROAD", "broad", False),
        (" broad-design ", "broad", False),
        ("targeted", "targeted", False),
        ("Targeted Design Research", "targeted", False),
        ("accessibility_audit", "accessibility_audit", False),
        ("A11Y", "accessibility_audit", False),
        ("unknown", "broad", True),
        (None, "broad", True),
        (17, "broad", True),
        ([], "broad", True),
    ],
)
def test_route_profile_normaliser(
    supplied: object,
    canonical: str,
    fallback: bool,
) -> None:
    assert normalise_route_profile(supplied) == (canonical, fallback)
    # The canonical value is always a real topology, so a consumer can index
    # PHASES_BY_PROFILE with it without a second guard.
    assert canonical in PHASES_BY_PROFILE


@pytest.mark.parametrize("profile", sorted(PHASES_BY_PROFILE))
def test_route_profile_normaliser_is_idempotent_for_canonical_values(profile: str) -> None:
    assert normalise_route_profile(profile) == (profile, False)


def test_phase_topology_error_names_the_workflow_phase_profile_and_phases() -> None:
    error = PhaseTopologyError(
        "ui_ux_pro_max",
        "targeted_design_research",
        "broad",
        PHASES_BY_PROFILE["broad"],
    )

    message = str(error)
    assert (error.workflow, error.phase, error.profile) == (
        "ui_ux_pro_max",
        "targeted_design_research",
        "broad",
    )
    assert error.phases == PHASES_BY_PROFILE["broad"]
    assert "ui_ux_pro_max" in message
    assert "targeted_design_research" in message
    assert "'broad'" in message
    assert "design_intelligence" in message
    assert isinstance(error, RuntimeError)


def test_topology_mismatch_is_a_configuration_failure_with_an_actionable_row() -> None:
    # FR-6: the transcript row has to say what is wrong; it must never be the
    # bare `ValueError: tuple.index(x): x not in tuple` that started all this.
    from agenthicc.runners.tui_session import _fmt_exc, _workflow_failure_kind

    error = PhaseTopologyError(
        "ui_ux_pro_max", "targeted_design_research", "broad", PHASES_BY_PROFILE["broad"]
    )

    assert _workflow_failure_kind(error) == "configuration"

    row = _fmt_exc(error)
    assert row.startswith("PhaseTopologyError: ")
    assert "tuple.index" not in row
    assert "tuple.index" not in str(error)
    # Names the workflow, the rejected phase, the active profile and the phases
    # that profile really schedules (so the reader can see the mismatch).
    for expected in ("ui_ux_pro_max", "targeted_design_research", "'broad'", "design_intelligence"):
        assert expected in row
    assert "scheduled phases:" in row
    assert len(row) < 1_000


def test_failure_kind_is_declared_by_type_not_by_wording() -> None:
    from agenthicc.runners.tui_session import _workflow_failure_kind

    # PhaseTopologyError's own message mentions the profile, so the wording
    # heuristic would reach "configuration" anyway; that makes its message a
    # useless witness. The declaration has to be what decides, which means a
    # subtype that strips every wording marker must still classify the same way.
    assert PhaseTopologyError.failure_kind == "configuration"
    assert (
        _workflow_failure_kind(PhaseTopologyError("wf", "phase", "profile", ("other",)))
        == "configuration"
    )

    class DeclaredTopologyError(PhaseTopologyError):
        """The same failure with no wording markers left in its message."""

        def __init__(self) -> None:
            RuntimeError.__init__(self, "the plan and the cursor disagree")

    stripped = DeclaredTopologyError()
    assert "profile" not in str(stripped).lower()
    assert "config" not in str(stripped).lower()
    assert _workflow_failure_kind(stripped) == "configuration"

    class Mislabelled(DeclaredTopologyError):
        failure_kind = "not_a_real_kind"

    # An unknown declaration is not trusted; classification falls back.
    assert _workflow_failure_kind(Mislabelled()) == "workflow_error"


def test_topology_error_message_is_bounded_for_a_pathological_phase_list() -> None:
    phases = tuple(f"phase_{index}" for index in range(500))
    message = str(PhaseTopologyError("workflow", "missing", "profile", phases))
    assert "phase_0" in message
    assert "... (+476 more)" in message
    assert len(message) < 2_000


@pytest.mark.parametrize(
    ("route_profile", "expected"),
    [
        ("broad", UiUxProMaxState.DESIGN_INTELLIGENCE),
        ("targeted", UiUxProMaxState.TARGETED_DESIGN_RESEARCH),
        ("accessibility_audit", UiUxProMaxState.TARGETED_DESIGN_RESEARCH),
        # Empty/unknown used to fall through to TARGETED_DESIGN_RESEARCH while
        # the phase tuple said broad -- the original mismatch.
        ("", UiUxProMaxState.DESIGN_INTELLIGENCE),
        ("unknown", UiUxProMaxState.DESIGN_INTELLIGENCE),
        ("Broad", UiUxProMaxState.DESIGN_INTELLIGENCE),
    ],
)
def test_product_discovery_route_is_driven_by_the_normalised_profile(
    route_profile: str,
    expected: UiUxProMaxState,
) -> None:
    context = UiUxProMaxContext(intent="x", run_id="run", route_profile=route_profile)

    next_state = next_state_after_product_discovery(context)

    assert next_state is expected
    assert next_state.name.lower() in active_phases_for(context.route_profile)


@pytest.mark.parametrize("route_profile", ["broad", "targeted", "accessibility_audit"])
def test_research_route_is_driven_by_the_normalised_profile(route_profile: str) -> None:
    context = UiUxProMaxContext(intent="x", run_id="run", route_profile=route_profile)

    next_state = next_state_after_design_research(context)

    assert next_state.name.lower() in active_phases_for(route_profile)


@pytest.mark.parametrize(
    ("route_profile", "expected"),
    [
        ("broad", UiUxProMaxState.EXPERIENCE_ARCHITECTURE),
        ("targeted", UiUxProMaxState.EXPERIENCE_ARCHITECTURE),
        # The accessibility route does not schedule experience_architecture.
        ("accessibility_audit", UiUxProMaxState.VISUAL_UX_AUDIT),
        ("", UiUxProMaxState.EXPERIENCE_ARCHITECTURE),
    ],
)
def test_stack_adaptation_route_reads_the_phase_tuple(
    route_profile: str, expected: UiUxProMaxState
) -> None:
    context = UiUxProMaxContext(intent="x", run_id="run", route_profile=route_profile)

    assert next_state_after_stack_adaptation(context) is expected


@pytest.mark.parametrize(
    ("route_profile", "requested", "expected"),
    [
        ("broad", True, UiUxProMaxState.DESIGN_REMEDIATION),
        ("broad", False, UiUxProMaxState.DESIGN_ACCEPTANCE),
        ("targeted", True, UiUxProMaxState.DESIGN_REMEDIATION),
        # accessibility_audit does not schedule design_remediation at all.
        ("accessibility_audit", True, UiUxProMaxState.DESIGN_ACCEPTANCE),
        ("accessibility_audit", False, UiUxProMaxState.DESIGN_ACCEPTANCE),
    ],
)
def test_visual_audit_route_reads_the_phase_tuple(
    route_profile: str, requested: bool, expected: UiUxProMaxState
) -> None:
    context = UiUxProMaxContext(intent="x", run_id="run", route_profile=route_profile)

    assert next_state_after_visual_ux_audit(context, remediation_requested=requested) is expected


def test_phase_index_in_raises_a_typed_error_not_a_bare_value_error() -> None:
    # The exact PRD reproduction: route_profile '' used to yield the *broad*
    # phase tuple while the transition picked TARGETED_DESIGN_RESEARCH, and
    # indexing it raised `ValueError: tuple.index(x): x not in tuple`.
    assert phase_index_in("", "product_discovery") == 0
    with pytest.raises(PhaseTopologyError) as excinfo:
        phase_index_in("", "targeted_design_research")

    error = excinfo.value
    assert not isinstance(error, ValueError)
    assert (error.workflow, error.phase, error.profile) == (
        "ui_ux_pro_max",
        "targeted_design_research",
        "broad",
    )
    message = str(error)
    assert "ui_ux_pro_max" in message
    assert "targeted_design_research" in message
    assert "'broad'" in message
    assert "design_intelligence" in message  # the profile's actual phases
    assert "tuple.index" not in message


def test_checkpoint_boundary_reproduction_raises_a_typed_error(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="intent", run_id="run", route_profile="")

    with pytest.raises(PhaseTopologyError) as excinfo:
        runner._checkpoint_boundary(
            context, "product_discovery", UiUxProMaxState.TARGETED_DESIGN_RESEARCH
        )

    assert excinfo.value.phase == "targeted_design_research"
    assert excinfo.value.profile == "broad"


def test_no_unguarded_phase_index_lookup_remains_in_the_runner() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "agenthicc"
        / "workflows"
        / "ui_ux_pro_max"
        / "runner.py"
    ).read_text()
    tree = ast.parse(source)

    guarded_functions: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        indexes_phases = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "index"
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "phases"
            for child in ast.walk(node)
        )
        if not indexes_phases:
            continue
        guarded_functions.add(node.name)
        has_membership_guard = any(
            isinstance(child, ast.Compare)
            and any(isinstance(op, (ast.In, ast.NotIn)) for op in child.ops)
            and any(
                isinstance(comparator, ast.Name) and comparator.id == "phases"
                for comparator in child.comparators
            )
            for child in ast.walk(node)
        )
        assert has_membership_guard, f"{node.name} indexes phases without a membership guard"

    # Every phases.index() call must live in one of these two guarded helpers.
    assert guarded_functions == {"phase_index_in", "_phase_index"}


@pytest.mark.parametrize(
    ("payload", "expected", "fallback"),
    [
        # The observed production payload: no "scope" key at all.
        ({"classification": "broad"}, "broad", False),
        ({"scope": "broad"}, "broad", False),
        ({"route_profile": "broad"}, "broad", False),
        ({"profile": "broad"}, "broad", False),
        # Case/whitespace variants of the canonical spellings route too.
        ({"scope": "Broad"}, "broad", False),
        ({"classification": "BROAD"}, "broad", False),
        ({"scope": "targeted"}, "targeted", False),
        ({"scope": "accessibility_audit"}, "accessibility_audit", False),
        # Unusable values fall back to the canonical broad route.
        ({"scope": ""}, "broad", True),
        ({"scope": "   "}, "broad", True),
        ({"scope": "nonsense"}, "broad", True),
        ({"scope": 17}, "broad", True),
        ({}, "broad", True),
    ],
)
def test_route_profile_from_details_accepts_every_documented_key(
    payload: dict[str, object], expected: str, fallback: bool
) -> None:
    assert route_profile_from_details(payload) == (expected, fallback)
    # Whatever comes out is a real topology, so no caller needs a second guard.
    assert expected in PHASES_BY_PROFILE


def test_explicit_scope_beats_an_alias() -> None:
    assert route_profile_from_details({"scope": "targeted", "classification": "broad"}) == (
        "targeted",
        False,
    )
    assert route_profile_from_details({"classification": "accessibility_audit", "profile": ""}) == (
        "accessibility_audit",
        False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_details",
    [
        {"classification": "broad"},
        {"route_profile": "broad"},
        {"profile": "broad"},
        {"scope": "Broad"},
        {"scope": ""},
        {},
    ],
)
async def test_product_discovery_records_the_canonical_profile(
    tmp_path: Path, extra_details: dict[str, object]
) -> None:
    # The observed payload {"classification": "broad"} used to leave
    # route_profile == "", which made the checkpoint unrestorable and the phase
    # tuple disagree with the transition. The context must only ever hold a
    # canonical profile.
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="Improve this interface", run_id="route")

    async def discovery(
        self: UiUxProMaxRunner, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {"summary": "Brief recorded.", **extra_details}

    runner._run_phase = MethodType(discovery, runner)  # type: ignore[method-assign]

    next_state = await runner._product_discovery(context)

    assert context.route_profile == "broad"
    assert next_state is UiUxProMaxState.DESIGN_INTELLIGENCE


@pytest.mark.parametrize(
    "stored_profile",
    ["", "   ", None, "unknown", "Broad", " broad ", "broad-design", "targeted", "A11Y"],
)
def test_checkpoint_restore_normalises_an_unusable_route_profile(
    stored_profile: object,
) -> None:
    # The recorded sessions persisted route_profile "", which used to raise here
    # and make checkpoint_context_from_payload return None -- the run resumed
    # with no context at all.
    context = UiUxProMaxContext.from_checkpoint_payload(
        {
            "intent": "intent",
            "run_id": "run",
            "state": "PRODUCT_DISCOVERY",
            "route_profile": stored_profile,
        }
    )
    expected, fallback = normalise_route_profile(stored_profile)
    assert context.route_profile == expected
    assert context.route_profile in PHASES_BY_PROFILE
    assert bool(context.route_profile_fallback_source) is fallback
    # `UiUxProMax.checkpoint_context_from_payload` must stop swallowing it.
    assert (
        UiUxProMax.checkpoint_context_from_payload(
            {
                "intent": "intent",
                "run_id": "run",
                "state": "PRODUCT_DISCOVERY",
                "route_profile": stored_profile,
            }
        )
        is not None
    )


def test_checkpoint_restore_treats_a_missing_profile_key_as_the_default() -> None:
    context = UiUxProMaxContext.from_checkpoint_payload(
        {"intent": "intent", "run_id": "run", "state": "PRODUCT_DISCOVERY"}
    )
    assert context.route_profile == "broad"
    assert context.route_profile_fallback_source == "checkpoint"
    assert "fell back to broad" in context.route_profile_fallback_diagnostic


@pytest.mark.parametrize("bad_profile", [[], {}, 17, True])
def test_checkpoint_restore_rejects_only_a_non_string_route_profile(
    bad_profile: object,
) -> None:
    with pytest.raises(ValueError, match="non-string route profile"):
        UiUxProMaxContext.from_checkpoint_payload(
            {
                "intent": "intent",
                "run_id": "run",
                "state": "PRODUCT_DISCOVERY",
                "route_profile": bad_profile,
            }
        )
    assert (
        UiUxProMax.checkpoint_context_from_payload(
            {
                "intent": "intent",
                "run_id": "run",
                "state": "PRODUCT_DISCOVERY",
                "route_profile": bad_profile,
            }
        )
        is None
    )


def test_canonical_route_profile_records_no_fallback() -> None:
    context = UiUxProMaxContext.from_checkpoint_payload(
        {
            "intent": "intent",
            "run_id": "run",
            "state": "PRODUCT_DISCOVERY",
            "route_profile": "accessibility_audit",
        }
    )
    assert context.route_profile == "accessibility_audit"
    assert context.route_profile_fallback_source == ""
    assert context.route_profile_fallback_diagnostic == ""


def test_fallback_source_survives_a_resume_that_already_repaired_the_profile() -> None:
    repaired = UiUxProMaxContext.from_checkpoint_payload(
        {"intent": "intent", "run_id": "run", "state": "PRODUCT_DISCOVERY", "route_profile": ""}
    )
    payload = repaired.to_checkpoint_payload()
    restored = UiUxProMaxContext.from_checkpoint_payload(payload)
    # The boundary already wrote the canonical value, so this restore is clean;
    # the origin of the original fallback is still reported.
    assert payload["route_profile"] == "broad"
    assert restored.route_profile == "broad"
    assert restored.route_profile_fallback_source == "checkpoint"
    assert "'" in restored.route_profile_fallback_diagnostic


@pytest.mark.parametrize("raw_profile", ["", "unknown", "Broad", 17])
def test_checkpoint_write_always_stores_a_canonical_profile(raw_profile: object) -> None:
    # Even a context that somehow still holds a raw value cannot write one.
    context = UiUxProMaxContext(intent="intent", run_id="run")
    context.route_profile = raw_profile  # type: ignore[assignment]
    assert (
        context.to_checkpoint_payload()["route_profile"] == normalise_route_profile(raw_profile)[0]
    )


def test_recorded_session_checkpoints_resume_at_product_discovery(tmp_path: Path) -> None:
    # The exact shape the two failed sessions persisted: an empty route profile,
    # PRODUCT_DISCOVERY, no completed phases.  Restoring must now succeed and the
    # resume cursor must reconcile to the phase that was running.
    recorded_fields: dict[str, object] = {
        "intent": "Work in a new worktree and do this uiux pro max workflow to enhance the UI of pyapps",
        "run_id": "run",
        "state": "PRODUCT_DISCOVERY",
        "route_profile": "",
        "current_phase": "product_discovery",
        "phase_iteration": 1,
        "phase_attempts": {"product_discovery": 1},
        "completed_phases": [],
        "phase_history": [],
    }
    context = UiUxProMax.checkpoint_context_from_payload(recorded_fields)
    assert isinstance(context, UiUxProMaxContext)
    assert context.route_profile == "broad"
    assert context.route_profile_fallback_source == "checkpoint"

    runner = _make_runner(tmp_path)
    runner._reconcile_resume_cursor(context)

    assert context.state is UiUxProMaxState.PRODUCT_DISCOVERY
    assert context.resume_resolution_source == "checkpoint_cursor"
    assert "product_discovery" in active_phases_for(context.route_profile)


@pytest.mark.parametrize(
    ("extra_details", "expected_profile", "expected_source"),
    [
        # An unusable value is what a fallback has to be recorded for.
        ({"scope": "unknown"}, "broad", "product_discovery"),
        ({"scope": ""}, "broad", "product_discovery"),
        ({}, "broad", "product_discovery"),
        # A canonical or alias spelling is not a fallback.
        ({"scope": "targeted"}, "targeted", ""),
        ({"scope": "Broad"}, "broad", ""),
        # No profile key at all also fell back, even if the result is correct.
        ({"audit_focus": "contrast"}, "broad", "product_discovery"),
    ],
)
async def test_product_discovery_records_a_profile_fallback_only_when_one_happened(
    tmp_path: Path,
    extra_details: dict[str, object],
    expected_profile: str,
    expected_source: str,
) -> None:
    # FR-7: the run itself (not just a restored checkpoint) has to carry the
    # fallback, together with a bounded diagnostic the status view can show.
    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="Improve this interface", run_id="fallback")

    async def discovery(
        self: UiUxProMaxRunner, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {"summary": "Brief recorded.", **extra_details}

    runner._run_phase = MethodType(discovery, runner)  # type: ignore[method-assign]

    await runner._product_discovery(context)

    assert context.route_profile == expected_profile
    assert context.route_profile_fallback_source == expected_source
    if expected_source:
        assert "fell back to broad" in context.route_profile_fallback_diagnostic
        assert context.route_profile_fallback_detail
    else:
        assert context.route_profile_fallback_diagnostic == ""
        assert context.route_profile_fallback_detail == ""


async def test_the_status_view_reports_a_recorded_profile_fallback(tmp_path: Path) -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.statusline import status_report

    runner = _make_runner(tmp_path)
    context = UiUxProMaxContext(intent="Improve this interface", run_id="status-view")

    async def discovery(
        self: UiUxProMaxRunner, *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        return {"summary": "Brief recorded.", "scope": "nonsense"}

    runner._run_phase = MethodType(discovery, runner)  # type: ignore[method-assign]
    await runner._product_discovery(context)
    assert context.route_profile_fallback_source == "product_discovery"

    # What _drive publishes after the phase, using the same bounded diagnostic.
    state = AppState.create()
    run = WorkflowRun(
        run_id=context.run_id,
        workflow_name="ui_ux_pro_max",
        intent=context.intent,
        current_phase="design_intelligence",
        diagnostic=context.route_profile_fallback_diagnostic,
    )
    state.workflow_run.set(run)

    rows = dict(status_report(state))
    assert "Workflow" in rows
    assert "ui_ux_pro_max" in rows["Workflow"]
    assert "fell back to broad" in rows["Workflow"]
    assert "source=product_discovery" in rows["Workflow"]


def test_the_status_view_stays_quiet_without_a_fallback() -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.statusline import status_report

    state = AppState.create()
    assert "Workflow" not in dict(status_report(state))

    state.workflow_run.set(
        WorkflowRun(
            run_id="run",
            workflow_name="ui_ux_pro_max",
            intent="intent",
            current_phase="design_intelligence",
        )
    )
    # A healthy run adds no row at all, so /status does not grow noise.
    assert "Workflow" not in dict(status_report(state))


@pytest.mark.parametrize(
    "route_profile", ["broad", "targeted", "accessibility_audit", "", "unknown", "Broad"]
)
def test_transition_is_always_member_of_active_phases(route_profile: str) -> None:
    context = UiUxProMaxContext(intent="x", run_id="run", route_profile=route_profile)
    phases = active_phases_for(route_profile)
    assert phases == PHASES_BY_PROFILE[normalise_route_profile(route_profile)[0]]

    for helper in (next_state_after_product_discovery, next_state_after_design_research):
        next_state = helper(context)
        assert next_state.name.lower() in phases

    # Every phase a profile schedules must be a real workflow state, so the
    # reverse lookup used by _phase_index can never miss.
    for phase in phases:
        assert phase.upper() in UiUxProMaxState.__members__


@pytest.mark.parametrize("route_profile", sorted(PHASES_BY_PROFILE))
def test_profile_phase_lists_preserve_workflow_order(route_profile: str) -> None:
    phases = active_phases_for(route_profile)
    assert phases == tuple(name for name in PHASE_NAMES if name in phases)


def test_ensure_phase_is_active_raises_a_typed_error_for_a_real_mismatch() -> None:
    # design_system_synthesis is a broad-only phase; the accessibility route
    # never runs it, so handing it over must be a named topology failure.
    with pytest.raises(PhaseTopologyError) as excinfo:
        ensure_phase_is_active("accessibility_audit", UiUxProMaxState.DESIGN_SYSTEM_SYNTHESIS)

    error = excinfo.value
    assert error.workflow == "ui_ux_pro_max"
    assert error.phase == "design_system_synthesis"
    assert error.profile == "accessibility_audit"
    assert "product_discovery" in error.phases
    assert (
        ensure_phase_is_active("accessibility_audit", UiUxProMaxState.VISUAL_UX_AUDIT)
        is UiUxProMaxState.VISUAL_UX_AUDIT
    )
    # Terminal states are never looked up in a profile tuple.
    for terminal in (
        UiUxProMaxState.COMPLETE,
        UiUxProMaxState.BLOCKED,
        UiUxProMaxState.FAILED,
    ):
        assert ensure_phase_is_active("", terminal) is terminal


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
    # Only a wrong *type* is a malformed checkpoint now; an empty or unknown
    # string is repaired on restore (FR-5) instead of making the whole context
    # unrestorable.
    with pytest.raises(ValueError, match="non-string route profile"):
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
        skills=SurfaceHolder(MappingProxyType({})),
        tool_surface=ToolSurfaceHolder(ToolSurface(0, ())),
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
