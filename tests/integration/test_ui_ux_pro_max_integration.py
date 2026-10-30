"""Approval and durable event integration for specialist workflow phases."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from lauren_ai._memory import ShortTermMemory

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState as KernelAppState
from agenthicc.kernel import EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tools.approval import ApprovalService
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.ui_ux_pro_max import UiUxProMaxRunner

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
        skills={},
        plugin_tools=[],
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
