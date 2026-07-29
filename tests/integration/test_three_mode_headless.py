"""Headless mode uses the same policy gates without waiting for a UI."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from lauren_ai import ToolCallContext

from agenthicc.runners.headless import _HeadlessApprovalService
from agenthicc.tools.approval import ApprovalGate
from agenthicc.tools.capabilities import CAPABILITIES_KEY, ToolCapability
from agenthicc.tools.capability_gate import ToolCapabilityGate
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.integration


def _ctx(capability: ToolCapability) -> ToolCallContext:
    return ToolCallContext(
        agent_context=None,
        tool_use_id="headless-mode-test",
        turn=1,
        tool_name="write_probe",
        tool_input={},
        metadata={CAPABILITIES_KEY: frozenset({capability})},
    )


def _aborted(decision: object) -> bool:
    return bool(getattr(decision, "_aborted", False))


@pytest.mark.asyncio
async def test_headless_safe_denies_without_waiting_for_an_overlay() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Safe"))
    gate = ApprovalGate(app, _HeadlessApprovalService(False))  # type: ignore[arg-type]

    decision = await asyncio.wait_for(gate.before_tool_call(_ctx(ToolCapability.WRITE)), 0.2)

    assert _aborted(decision)
    assert app.pending_approval() is None


@pytest.mark.asyncio
async def test_headless_dangerous_flag_allows_safe_but_not_plan() -> None:
    app = AppState.create()
    app.cli_flags = replace(app.cli_flags, dangerously_skip_permissions=True)
    safe_gate = ApprovalGate(app, _HeadlessApprovalService(False))  # type: ignore[arg-type]
    app.active_mode.set(build_default_registry().get("Safe"))

    safe_decision = await safe_gate.before_tool_call(_ctx(ToolCapability.WRITE))
    assert not _aborted(safe_decision)

    app.active_mode.set(build_default_registry().get("Plan"))
    plan_decision = await ToolCapabilityGate(app).before_tool_call(_ctx(ToolCapability.WRITE))
    assert _aborted(plan_decision)
