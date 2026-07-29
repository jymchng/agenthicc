"""Unit coverage for Safe approval, Plan hard blocks, and Yolo bypasses."""

from __future__ import annotations

import pytest
from lauren_ai import ToolCallContext
from dataclasses import replace

from agenthicc.tools.approval import ApprovalGate, ApprovalResponse
from agenthicc.tools.capabilities import CAPABILITIES_KEY, ToolCapability
from agenthicc.tools.capability_gate import ToolCapabilityGate
from agenthicc.tools.capabilities import get_tool_capabilities
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.unit


def _context(metadata: dict[str, object] | None = None) -> ToolCallContext:
    return ToolCallContext(
        agent_context=None,
        tool_use_id="mode-test",
        turn=1,
        tool_name="test_tool",
        tool_input={},
        metadata=metadata or {},
    )


def _aborted(decision: object) -> bool:
    return bool(getattr(decision, "_aborted", False))


@pytest.mark.asyncio
async def test_plan_hard_blocks_write_before_approval_gate() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Plan"))
    gate = ToolCapabilityGate(app)
    decision = await gate.before_tool_call(
        _context({CAPABILITIES_KEY: frozenset({ToolCapability.WRITE})})
    )

    assert _aborted(decision)
    assert "Plan mode" in str(getattr(decision, "value", decision))


@pytest.mark.asyncio
async def test_plan_blocks_unannotated_tools_and_does_not_request_approval() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Plan"))
    capability_gate = ToolCapabilityGate(app)
    decision = await capability_gate.before_tool_call(_context())

    assert _aborted(decision)
    assert "no declared capability metadata" in str(getattr(decision, "value", decision))


@pytest.mark.asyncio
async def test_plan_blocks_malformed_capability_metadata_fail_closed() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Plan"))
    capability_gate = ToolCapabilityGate(app)
    decision = await capability_gate.before_tool_call(
        _context({CAPABILITIES_KEY: frozenset({"not-a-capability"})})
    )

    assert _aborted(decision)
    assert "no declared capability metadata" in str(getattr(decision, "value", decision))


@pytest.mark.asyncio
async def test_yolo_allows_unannotated_tools_without_approval() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Yolo"))
    capability_gate = ToolCapabilityGate(app)
    decision = await capability_gate.before_tool_call(_context())

    assert not _aborted(decision)


class _RecordingApproval:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.requests: list[object] = []

    async def request_approval(self, request: object) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(allowed=self.allowed)


@pytest.mark.asyncio
async def test_safe_requests_approval_for_side_effecting_capability() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Safe"))
    service = _RecordingApproval(True)
    gate = ApprovalGate(app, service)  # type: ignore[arg-type]
    decision = await gate.before_tool_call(
        _context({CAPABILITIES_KEY: frozenset({ToolCapability.WRITE})})
    )

    assert not _aborted(decision)
    assert len(service.requests) == 1
    assert getattr(service.requests[0], "capabilities") == frozenset({ToolCapability.WRITE})


@pytest.mark.asyncio
async def test_safe_requests_approval_for_unannotated_capability() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Safe"))
    service = _RecordingApproval(True)
    gate = ApprovalGate(app, service)  # type: ignore[arg-type]
    decision = await gate.before_tool_call(_context())

    assert not _aborted(decision)
    assert getattr(service.requests[0], "capabilities") == frozenset({ToolCapability.UNDECLARED})


@pytest.mark.asyncio
async def test_safe_denial_is_structured_and_does_not_leave_pending_state() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Safe"))
    service = _RecordingApproval(False)
    gate = ApprovalGate(app, service)  # type: ignore[arg-type]
    decision = await gate.before_tool_call(
        _context({CAPABILITIES_KEY: frozenset({ToolCapability.EXECUTE})})
    )

    assert _aborted(decision)
    assert "denied" in str(getattr(decision, "value", decision)).lower()
    assert app.pending_approval() is None


@pytest.mark.asyncio
async def test_dangerous_flag_bypasses_safe_approval_but_not_plan_block() -> None:
    app = AppState.create()
    app.cli_flags = replace(app.cli_flags, dangerously_skip_permissions=True)
    service = _RecordingApproval(False)
    gate = ApprovalGate(app, service)  # type: ignore[arg-type]

    app.active_mode.set(build_default_registry().get("Safe"))
    safe_decision = await gate.before_tool_call(
        _context({CAPABILITIES_KEY: frozenset({ToolCapability.WRITE})})
    )
    assert not _aborted(safe_decision)
    assert service.requests == []

    app.active_mode.set(build_default_registry().get("Plan"))
    plan_decision = await ToolCapabilityGate(app).before_tool_call(
        _context({CAPABILITIES_KEY: frozenset({ToolCapability.WRITE})})
    )
    assert _aborted(plan_decision)


@pytest.mark.asyncio
async def test_control_capability_is_available_in_plan_without_approval() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Plan"))
    service = _RecordingApproval(False)
    metadata = {CAPABILITIES_KEY: frozenset({ToolCapability.CONTROL})}

    capability = await ToolCapabilityGate(app).before_tool_call(_context(metadata))
    approval = await ApprovalGate(app, service).before_tool_call(_context(metadata))  # type: ignore[arg-type]

    assert not _aborted(capability)
    assert not _aborted(approval)
    assert not service.requests


def test_builtin_memory_and_authoring_tools_declare_non_side_effect_policy() -> None:
    from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
    from agenthicc.workflows.memory_tools import make_memory_tools

    memory = {tool.__name__: get_tool_capabilities(tool) for tool in make_memory_tools(None, None)}
    inspection = make_inspection_tools()

    assert ToolCapability.WRITE in memory["memory_write"]
    assert ToolCapability.READ in memory["memory_read"]
    assert ToolCapability.SEARCH in memory["semantic_search"]
    assert ToolCapability.WRITE in memory["publish_artifact"]
    assert all(ToolCapability.READ in get_tool_capabilities(tool) for tool in inspection)
