"""TUX-07: "No, and tell the agent" reaches the model through the executor and gate."""

from __future__ import annotations

import asyncio

import pytest
from lauren_ai import tool

from agenthicc.tools import AgenthiccToolExecutor, ToolErrorCode
from agenthicc.tools.approval import ApprovalGate, ApprovalService
from agenthicc.tools.capabilities import tool_execute
from agenthicc.tools.capability_gate import ToolCapabilityGate
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry
from agenthicc.tui.workspace.dialog import DialogSettings
from agenthicc.tui.workspace.overlay import OverlayHost
from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay

pytestmark = pytest.mark.integration

ran: list[str] = []


@tool_execute
@tool()
async def run_bash(command: str) -> str:
    """A command probe that records what would have run."""
    ran.append(command)
    return "ran"


async def _pending(app: AppState) -> None:
    for _ in range(200):
        if app.pending_approval() is not None:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no approval was requested")


@pytest.mark.asyncio
async def test_tell_the_agent_text_reaches_the_tool_result() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Safe"))
    service = ApprovalService(app)
    executor = AgenthiccToolExecutor(
        [run_bash],
        global_hooks=[ToolCapabilityGate(app), ApprovalGate(app, service)],
    )
    host = OverlayHost(app)
    ran.clear()

    call = asyncio.create_task(executor.execute("run_bash", {"command": "npm ci"}, "call-1"))
    await _pending(app)
    request = app.pending_approval()
    assert request is not None
    assert request.reason == "Safe mode asks before running commands."
    overlay = ApprovalOverlay(request, service, host.hide, settings=DialogSettings(mode="Safe"))
    host.show(overlay)

    host.handle_key(Key.CHAR, "4")  # No, and tell the agent what to do instead
    for ch in "use pnpm instead":
        host.handle_key(Key.CHAR, ch)
    host.handle_key(Key.ENTER, "")
    composer = overlay._composer
    if composer is not None:
        await composer.settle()

    result = await asyncio.wait_for(call, timeout=5)
    assert not result.ok
    assert result.error_kind == ToolErrorCode.APPROVAL_DENIED
    assert "use pnpm instead" in str(result.error)
    assert ran == []
    assert not host.active


@pytest.mark.asyncio
async def test_two_allows_the_rest_of_the_turn_through_the_gate() -> None:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Safe"))
    service = ApprovalService(app)
    executor = AgenthiccToolExecutor(
        [run_bash],
        global_hooks=[ToolCapabilityGate(app), ApprovalGate(app, service)],
    )
    host = OverlayHost(app)
    ran.clear()

    call = asyncio.create_task(executor.execute("run_bash", {"command": "make"}, "call-2"))
    await _pending(app)
    request = app.pending_approval()
    assert request is not None
    host.show(ApprovalOverlay(request, service, host.hide))
    host.handle_key(Key.CHAR, "2")
    assert (await asyncio.wait_for(call, timeout=5)).ok
    # Remembered for the turn: the next command runs without a dialog.
    second = await asyncio.wait_for(
        executor.execute("run_bash", {"command": "make test"}, "call-3"), timeout=5
    )
    assert second.ok
    assert ran == ["make", "make test"]
