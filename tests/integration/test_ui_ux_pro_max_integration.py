"""Approval and durable event integration for specialist workflow phases."""

from __future__ import annotations

from types import MappingProxyType
from agenthicc.tools.surface import ToolSurface, ToolSurfaceHolder
from agenthicc.runners.reload import SurfaceHolder
import asyncio
from pathlib import Path
from types import SimpleNamespace
from types import MethodType
from unittest.mock import MagicMock

import pytest
from lauren_ai._memory import ShortTermMemory

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState as KernelAppState
from agenthicc.kernel import Event
from agenthicc.kernel import EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tools.approval import ApprovalService
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.ui_ux_pro_max import (
    UiUxProMax,
    UiUxProMaxContext,
    UiUxProMaxRunner,
    UiUxProMaxState,
)
from agenthicc.workflows.ui_ux_pro_max.state import PHASES_BY_PROFILE

pytestmark = pytest.mark.integration


@pytest.fixture
async def processor(tmp_path: Path):
    state = KernelAppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snapshot.json"),
        ),
        policy=SecurityPolicy(),
    )
    instance = EventProcessor(initial_state=state, persist=False)
    task = asyncio.create_task(instance.run())
    yield instance
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _runner(
    tmp_path: Path,
    processor: EventProcessor,
    app: AppState,
    approvals: ApprovalService,
) -> UiUxProMaxRunner:
    cfg = AgenthiccConfig()
    cfg.execution.model = "integration-test-model"  # type: ignore[misc]
    cfg.execution.effective_usable_budget = lambda: 2_000  # type: ignore[method-assign]
    config = WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=processor,
        agent_runner=SimpleNamespace(
            _transport=SimpleNamespace(_config=SimpleNamespace(model="integration-test-model"))
        ),  # type: ignore[arg-type]
        approval_svc=approvals,
        cfg=cfg,
        skills=SurfaceHolder(MappingProxyType({})),
        tool_surface=ToolSurfaceHolder(ToolSurface(0, ())),
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=MagicMock(),
        params=None,
        session_memory=ShortTermMemory(max_tokens=2_000),
        workspace_scope=SimpleNamespace(primary_root=tmp_path),  # type: ignore[arg-type]
    )
    runner = UiUxProMaxRunner(config)
    runner._cfg.app_state.update_workflow_phase = lambda **_kwargs: None  # type: ignore[method-assign]
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_experience_plan_approval_controls_transition_and_emits_kernel_events(
    tmp_path: Path,
    processor: EventProcessor,
    allowed: bool,
) -> None:
    app = AppState.create()
    approvals = ApprovalService(app)
    runner = _runner(tmp_path, processor, app, approvals)
    tools = runner._experience_architecture_tools()
    by_name = {tool.__name__: tool for tool in tools}  # type: ignore[attr-defined]
    request = by_name["request_implementation_approval"]
    submit = by_name["submit_experience_architecture"]
    assert callable(request) and callable(submit)

    async def approve() -> None:
        for _ in range(200):
            if app.pending_approval() is not None:
                approvals.respond(allowed, message="integration approval result")
                return
            await asyncio.sleep(0.005)
        raise AssertionError("the architecture phase did not request approval")

    responder = asyncio.create_task(approve())
    try:
        response = await request("Implement the existing dashboard with a responsive alert list")
        await responder
    finally:
        if not responder.done():
            responder.cancel()
            await asyncio.gather(responder, return_exceptions=True)

    assert response["approved"] is allowed  # type: ignore[index]
    result = await submit(
        summary="Mapped dashboard states and components",
        details={
            "screen_inventory": ["dashboard", "alert detail"],
            "interaction_state_matrix": ["dashboard: loading, empty, success, error"],
            "component_dependency_graph": ["Dashboard → AlertList"],
            "page_component_specs": ["AlertList exposes keyboard-operable filters"],
            "acceptance_mapping": ["keyboard navigation → tab-order test"],
            "implementation_tasks": ["update AlertList", "add narrow viewport test"],
            "artifact_refs": ["docs/design/experience.md"],
        },
    )
    await processor.drain()

    if allowed:
        assert result["ok"] is True  # type: ignore[index]
        assert tools.event.is_set()
        assert tools.data["decision"] == "submit_experience_architecture"
        assert tools.data["approval_feedback"] == "integration approval result"
    else:
        assert result["ok"] is False  # type: ignore[index]
        assert not tools.event.is_set()
        assert app.pending_approval() is None


class _PhaseStop(RuntimeError):
    """Sentinel raised by the stubbed ``_run_phase`` to leave the drive loop.

    The drive loop normally runs every phase; a test only needs the boundary
    between ``product_discovery`` and the phase it routes to, so the stubbed
    phase runner answers the phase under test and raises this for the next one.
    """


def _capture_events(processor: EventProcessor) -> list[Event]:
    """Record every published event in order through a processor sink."""
    captured: list[Event] = []
    processor.add_sink(captured.append)
    return captured


def _payloads(captured: list[Event], event_type: str) -> list[dict[str, object]]:
    return [event.payload for event in captured if event.event_type == event_type]


def _stub_phase_runner(runner: UiUxProMaxRunner, responses: dict[str, dict[str, object]]) -> None:
    """Answer the given phases from *responses* and stop at the first other one."""

    async def run_phase(
        self: UiUxProMaxRunner,
        _context: UiUxProMaxContext,
        phase: str,
        *_args: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        if phase in responses:
            return responses[phase]
        raise _PhaseStop(phase)

    runner._run_phase = MethodType(run_phase, runner)  # type: ignore[method-assign]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "discovery_details",
    [
        {"summary": "Brief recorded.", "classification": "broad"},
        {"summary": "Brief recorded."},
    ],
    ids=["recorded-classification-key", "recorded-without-a-profile-key"],
)
async def test_drive_routes_the_recorded_discovery_payload_to_design_intelligence(
    tmp_path: Path,
    processor: EventProcessor,
    discovery_details: dict[str, object],
) -> None:
    # PRD-265: the two payloads recorded from the failing session both have to
    # reach design_intelligence instead of raising tuple.index for
    # targeted_design_research, which profile 'broad' does not define.
    app = AppState.create()
    runner = _runner(tmp_path, processor, app, ApprovalService(app))
    captured = _capture_events(processor)
    _stub_phase_runner(runner, {"product_discovery": discovery_details})
    context = UiUxProMaxContext(intent="Improve the alert list", run_id="recorded-payload")

    try:
        with pytest.raises(_PhaseStop):
            await runner._drive(context, context.state, resumed=False)
    finally:
        await processor.drain()

    run_started = _payloads(captured, "WorkflowRunStarted")
    assert len(run_started) == 1
    assert run_started[0]["phase_names"] == list(PHASES_BY_PROFILE["broad"])
    assert run_started[0]["resumed"] is False

    assert [payload["phase_name"] for payload in _payloads(captured, "WorkflowPhaseStarted")] == [
        "product_discovery",
        "design_intelligence",
    ]
    completed = _payloads(captured, "WorkflowPhaseCompleted")
    assert completed[0]["phase_name"] == "product_discovery"
    assert completed[0]["metadata"]["route_profile"] == "broad"  # type: ignore[index]

    assert context.route_profile == "broad"
    assert context.state is UiUxProMaxState.DESIGN_INTELLIGENCE
    assert app.workflow_run() is not None
    assert "tuple.index" not in repr([event.payload for event in captured])


@pytest.mark.asyncio
async def test_resuming_a_recorded_empty_profile_checkpoint_continues_at_product_discovery(
    tmp_path: Path,
    processor: EventProcessor,
) -> None:
    # The recorded checkpoints carry route_profile "", which used to make the
    # context unrestorable; the real recovery path has to restore it as broad
    # and resume the drive at product_discovery rather than restart or crash.
    app = AppState.create()
    runner = _runner(tmp_path, processor, app, ApprovalService(app))
    captured = _capture_events(processor)
    _stub_phase_runner(runner, {"product_discovery": {"summary": "Brief recorded."}})

    payload: dict[str, object] = {
        "intent": "Improve the alert list",
        "run_id": "recorded-empty-profile",
        "state": "PRODUCT_DISCOVERY",
        "route_profile": "",
    }
    context = UiUxProMax.checkpoint_context_from_payload(payload, ShortTermMemory(max_tokens=2_000))
    assert context is not None
    assert context.route_profile == "broad"
    assert context.route_profile_fallback_source == "checkpoint"

    try:
        with pytest.raises(_PhaseStop):
            await runner.resume(context)
    finally:
        await processor.drain()

    run_started = _payloads(captured, "WorkflowRunStarted")
    assert len(run_started) == 1
    assert run_started[0]["resumed"] is True
    assert run_started[0]["phase_names"] == list(PHASES_BY_PROFILE["broad"])
    assert [payload_["phase_name"] for payload_ in _payloads(captured, "WorkflowPhaseStarted")] == [
        "product_discovery",
        "design_intelligence",
    ]
    assert context.resume_resolution_source == "checkpoint_cursor"
    assert context.state is UiUxProMaxState.DESIGN_INTELLIGENCE
    # The status view publishes the recorded fallback for the rest of the run.
    run = app.workflow_run()
    assert run is not None
    assert "fell back to broad" in run.diagnostic
    assert "source=checkpoint" in run.diagnostic
