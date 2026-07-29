"""End-to-end session policy scenarios using the real tool executor."""

from __future__ import annotations

import pytest
from lauren_ai import tool

from agenthicc.tools import AgenthiccToolExecutor, ToolErrorKind
from agenthicc.tools.approval import ApprovalGate, ApprovalResponse
from agenthicc.tools.capabilities import tool_read, tool_write
from agenthicc.tools.capability_gate import ToolCapabilityGate
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import ModeManager

pytestmark = pytest.mark.e2e


class _SessionApprovals:
    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.requests: list[str] = []

    async def request_approval(self, request: object) -> ApprovalResponse:
        self.requests.append(str(getattr(request, "tool_name", "")))
        return ApprovalResponse(allowed=self.answers.pop(0) if self.answers else False)


@tool_read
@tool()
async def _e2e_read() -> str:
    return "read-ok"


@tool_write
@tool()
async def _e2e_write() -> str:
    return "write-ok"


def _executor(app: AppState, approvals: _SessionApprovals) -> AgenthiccToolExecutor:
    return AgenthiccToolExecutor(
        [_e2e_read, _e2e_write],
        global_hooks=[ToolCapabilityGate(app), ApprovalGate(app, approvals)],
    )


@pytest.mark.asyncio
async def test_one_session_moves_safe_plan_yolo_with_distinct_enforcement() -> None:
    app = AppState.create()
    approvals = _SessionApprovals(False, True)
    manager = ModeManager(app_state=app)
    executor = _executor(app, approvals)

    read = await executor.execute("_e2e_read", {}, "read-1")
    denied = await executor.execute("_e2e_write", {}, "write-denied")
    retried = await executor.execute("_e2e_write", {}, "write-approved")
    manager.set_by_name("Plan")
    plan = await executor.execute("_e2e_write", {}, "plan-write")
    manager.set_by_name("Yolo")
    yolo = await executor.execute("_e2e_write", {}, "yolo-write")

    assert read.ok and read.value == "read-ok"
    assert not denied.ok and denied.error_kind == ToolErrorKind.denied.value
    assert retried.ok and retried.value == "write-ok"
    assert not plan.ok and plan.error_kind == ToolErrorKind.denied.value
    assert yolo.ok and yolo.value == "write-ok"
    assert approvals.requests == ["_e2e_write", "_e2e_write"]
    assert app.active_mode().name == "Yolo"
