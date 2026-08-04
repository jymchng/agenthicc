"""End-to-end mode transitions through the real tool executor and adapter."""

from __future__ import annotations

from pathlib import Path

import pytest
import asyncio

from agenthicc.tools import AgenthiccToolExecutor, ToolErrorKind
from agenthicc.tools.approval import ApprovalGate, ApprovalResponse
from agenthicc.tools.capability_gate import ToolCapabilityGate
from agenthicc.tools.fs.agent_tools import read_file
from agenthicc.tools.workspace_access import (
    WorkspaceAccessPolicy,
    WorkspaceScope,
    reset_current_workspace_access,
    set_current_workspace_access,
)
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import ModeManager

pytestmark = pytest.mark.e2e


class _Approval:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def request_approval(self, request: object) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(allowed=True, scope_grant="target_once")


@pytest.mark.asyncio
async def test_safe_plan_yolo_parent_workspace_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _inline_to_thread(func: object, *args: object, **kwargs: object) -> object:
        return func(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "parent-data"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("parent data", encoding="utf-8")

    app = AppState.create()
    manager = ModeManager(app_state=app)
    approvals = _Approval()
    policy = WorkspaceAccessPolicy(
        WorkspaceScope.create(workspace),
        mode_provider=app.active_mode,
        approval_service=approvals,  # type: ignore[arg-type]
    )
    executor = AgenthiccToolExecutor(
        [read_file],
        global_hooks=[ToolCapabilityGate(app), ApprovalGate(app, approvals, policy)],
    )
    token = set_current_workspace_access(policy)
    try:
        safe = await executor.execute("read_file", {"path": str(target)}, "safe")
        manager.set_by_name("Plan")
        plan = await executor.execute("read_file", {"path": str(target)}, "plan")
        manager.set_by_name("Yolo")
        yolo = await executor.execute("read_file", {"path": str(target)}, "yolo")
    finally:
        reset_current_workspace_access(token)

    assert safe.ok is True
    assert safe.value["content"] == "parent data"
    assert plan.ok is False
    assert plan.error_kind == ToolErrorKind.denied.value
    assert yolo.ok is True
    assert yolo.value["content"] == "parent data"
    assert len(approvals.requests) == 1
    assert app.active_mode().name == "Yolo"
