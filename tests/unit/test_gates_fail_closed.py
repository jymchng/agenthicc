"""TOOLR-07: a capability or approval gate that fails blocks the tool."""

from __future__ import annotations

import pytest
from lauren_ai import ToolCallContext
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec
from agenthicc.tools.approval import ApprovalGate
from agenthicc.tools.capabilities import CAPABILITIES_KEY, ToolCapability, tool_write
from agenthicc.tools.capability_gate import (
    FailClosedGate,
    ToolCapabilityGate,
    order_security_gates,
)
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.unit


def _context(capabilities: frozenset[str]) -> ToolCallContext:
    return ToolCallContext(
        agent_context=None,
        tool_use_id="gate-1",
        turn=1,
        tool_name="write_thing",
        tool_input={"path": "x"},
        metadata={CAPABILITIES_KEY: capabilities},
    )


class _BrokenApprovals:
    async def request_approval(self, request: object) -> object:
        raise RuntimeError("approval UI went away")


def _app(mode: str) -> AppState:
    app = AppState.create()
    app.active_mode.set(build_default_registry().get(mode))
    return app


@pytest.mark.asyncio
async def test_a_raising_approval_service_blocks_the_call() -> None:
    gate = ApprovalGate(_app("Safe"), _BrokenApprovals())  # type: ignore[arg-type]
    ctx = _context(frozenset({ToolCapability.WRITE}))

    decision = await gate.before_tool_call(ctx)

    assert decision._aborted is True  # type: ignore[attr-defined]
    result = decision._abort_result  # type: ignore[attr-defined]
    assert result["ok"] is False and result["error_code"] == "internal"
    assert "RuntimeError" in result["error"] and "was not run" in result["error"]
    assert ctx.metadata["gate_failure"]["gate"] == "ApprovalGate"


@pytest.mark.asyncio
async def test_a_raising_capability_lookup_blocks_the_call() -> None:
    class _BrokenMode:
        @property
        def blocked_capabilities(self) -> frozenset[str]:
            raise KeyError("mode registry corrupted")

    app = _app("Plan")
    app.active_mode.set(_BrokenMode())  # type: ignore[arg-type]
    decision = await ToolCapabilityGate(app).before_tool_call(
        _context(frozenset({ToolCapability.WRITE}))
    )

    assert decision._aborted is True  # type: ignore[attr-defined]
    assert decision._abort_result["error_code"] == "internal"  # type: ignore[attr-defined]


def test_security_gates_run_after_every_other_hook() -> None:
    class _Rewriter:
        async def before_tool_call(self, ctx: ToolCallContext) -> object:
            return None

    app = _app("Safe")
    capability = ToolCapabilityGate(app)
    approval = ApprovalGate(app, _BrokenApprovals())  # type: ignore[arg-type]
    first, second = _Rewriter(), _Rewriter()

    ordered = order_security_gates([capability, first, approval, second])

    assert ordered == [first, second, capability, approval]
    assert all(isinstance(hook, FailClosedGate) for hook in ordered[2:])


@pytest.mark.asyncio
async def test_a_worker_never_writes_when_its_approval_gate_fails() -> None:
    """Through the real executor: lauren-ai would otherwise ignore the error and run it."""
    written: list[str] = []

    @tool_write
    @tool()
    async def write_thing(path: str) -> dict[str, object]:
        """Write a thing."""
        written.append(path)
        return {"ok": True}

    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="implementer",
            allowed_tools=frozenset({"write_thing"}),
            max_turns=3,
            system_prompt="Write.",
            max_turn_time_s=10.0,
        )
    )
    transport = MockTransport()
    transport.queue_tool_use("write_thing", {"path": "a.txt"}, tool_use_id="w-1")
    transport.queue_response(
        Completion(
            id="f",
            model="m",
            content="done",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
    )
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        [write_thing],
        registry=registry,
        app_state=_app("Safe"),
        approval_svc=_BrokenApprovals(),  # type: ignore[arg-type]
    )

    result = await spawn(tasks=[{"type": "implementer", "task": "Write a.txt"}], timeout_s=30)  # type: ignore[operator]

    assert written == []
    assert result["ok"] is False
    tool_results = [
        str(block.get("content"))
        for message in transport.calls[-1].messages
        if isinstance(message, dict) and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert any("was not run" in item for item in tool_results)
