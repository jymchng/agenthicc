"""Integration coverage for the real executor → capability → approval pipeline."""

from __future__ import annotations

import pytest
from lauren_ai import tool

from agenthicc.tools import AgenthiccToolExecutor, ToolErrorKind
from agenthicc.tools.approval import ApprovalGate, ApprovalResponse
from agenthicc.tools.capability_gate import ToolCapabilityGate
from agenthicc.tools.capabilities import ToolCapability, tool_read, tool_write
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.integration


class ApprovalScript:
    def __init__(self, *responses: bool) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []

    async def request_approval(self, request: object) -> ApprovalResponse:
        self.requests.append(request)
        allowed = self.responses.pop(0) if self.responses else False
        return ApprovalResponse(allowed=allowed)


@tool_read
@tool()
async def read_probe() -> str:
    """A read-only probe."""
    return "read"


@tool_write
@tool()
async def write_probe() -> str:
    """A side-effect probe."""
    return "write"


@tool()
async def unknown_probe() -> str:
    """A deliberately unannotated plugin-like probe."""
    return "unknown"


def _executor(app: AppState, approval: ApprovalScript) -> AgenthiccToolExecutor:
    return AgenthiccToolExecutor(
        [read_probe, write_probe, unknown_probe],
        global_hooks=[ToolCapabilityGate(app), ApprovalGate(app, approval)],
    )


@pytest.mark.asyncio
async def test_safe_read_write_and_retry_flow() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Safe"))
    approval = ApprovalScript(False, True)
    executor = _executor(app, approval)

    read = await executor.execute("read_probe", {}, "read-1")
    denied = await executor.execute("write_probe", {}, "write-1")
    allowed = await executor.execute("write_probe", {}, "write-2")

    assert read.ok and read.value == "read"
    assert not denied.ok and denied.error_kind == ToolErrorKind.denied.value
    assert allowed.ok and allowed.value == "write"
    assert [getattr(request, "tool_name") for request in approval.requests] == [
        "write_probe",
        "write_probe",
    ]
    assert app.pending_approval() is None


@pytest.mark.asyncio
async def test_safe_unannotated_plugin_tool_is_approval_gated() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Safe"))
    approval = ApprovalScript(True)

    result = await _executor(app, approval).execute("unknown_probe", {}, "unknown-1")

    assert result.ok is True
    assert len(approval.requests) == 1
    assert ToolCapability.UNDECLARED in approval.requests[0].capabilities


@pytest.mark.asyncio
async def test_safe_malformed_metadata_is_approval_gated() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Safe"))
    approval = ApprovalScript(True)
    setattr(
        unknown_probe,
        "__lauren_ai_tool_metadata__",
        {"capabilities": frozenset({"unknown"})},
    )
    executor = AgenthiccToolExecutor(
        [unknown_probe],
        global_hooks=[ToolCapabilityGate(app), ApprovalGate(app, approval)],
    )

    # This exercises the executor context boundary rather than only the hook.
    try:
        result = await executor.execute("unknown_probe", {}, "malformed-1")
    finally:
        delattr(unknown_probe, "__lauren_ai_tool_metadata__")

    assert result.ok is True
    assert approval.requests[0].capabilities == frozenset({ToolCapability.UNDECLARED})


@pytest.mark.asyncio
async def test_plan_hard_block_never_calls_approval_or_tool() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Plan"))
    approval = ApprovalScript(True)

    write = await _executor(app, approval).execute("write_probe", {}, "plan-write")
    unknown = await _executor(app, approval).execute("unknown_probe", {}, "plan-unknown")

    assert not write.ok and write.error_kind == ToolErrorKind.denied.value
    assert not unknown.ok and unknown.error_kind == ToolErrorKind.denied.value
    assert approval.requests == []


@pytest.mark.asyncio
async def test_yolo_preserves_auto_unrestricted_behavior() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Yolo"))
    approval = ApprovalScript(False)
    executor = _executor(app, approval)

    write = await executor.execute("write_probe", {}, "yolo-write")
    unknown = await executor.execute("unknown_probe", {}, "yolo-unknown")

    assert write.ok and unknown.ok
    assert approval.requests == []
