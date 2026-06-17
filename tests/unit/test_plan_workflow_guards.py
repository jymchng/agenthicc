"""Unit tests: plan approval enforcement and mode reset (PRD-89, PRD-101).

The plan approval gate is implemented via make_transition_tools with an
EdgeGate(kind="plan_review") on the plan→execute "approve" edge.
Each edge creates one named @tool(); the agent calls approve({...}) directly.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tools(approved: bool = True, feedback: str = ""):
    """Return (tools_list, transition_event, transition_data, approval_svc)."""
    from agenthicc.workflow.phase_tools import make_transition_tools
    from agenthicc.workflow.plugin import DataBus, EdgeGate, EdgeSpec, PhaseNode

    approval_svc = MagicMock()
    response = MagicMock()
    response.allowed = approved
    response.message = feedback
    approval_svc.request_approval = AsyncMock(return_value=response)

    node = PhaseNode(
        name  = "plan",
        edges = (
            EdgeSpec("execute", "approve", gate=EdgeGate(kind="plan_review")),
            EdgeSpec("plan",    "revise"),
        ),
    )
    data_bus         = DataBus(intent="x", run_id="r")
    transition_event = asyncio.Event()
    transition_data: dict = {}
    tools = make_transition_tools(node, data_bus, transition_event, transition_data, approval_svc)
    return tools, transition_event, transition_data, approval_svc


def _get(tools, name):
    for t in tools:
        if getattr(t, "__name__", None) == name:
            return t
    raise KeyError(name)


# ── approval gate ─────────────────────────────────────────────────────────────

class TestApprovalGate:
    async def test_approve_tool_commits_when_allowed(self):
        tools, ev, td, _ = _make_tools(approved=True)
        result = await _get(tools, "approve")(output={"plan": "step 1"})
        assert result["ok"] is True
        assert ev.is_set()
        assert td["edge_label"] == "approve"

    async def test_approve_tool_blocked_when_denied(self):
        tools, ev, td, _ = _make_tools(approved=False, feedback="needs error handling")
        result = await _get(tools, "approve")(output={"plan": "draft"})
        assert result.get("approved") is False
        assert "needs error handling" in result.get("feedback", "")
        assert not ev.is_set()

    async def test_revise_tool_needs_no_gate(self):
        """The revise edge has no gate — commits immediately."""
        tools, ev, td, _ = _make_tools()
        result = await _get(tools, "revise")(output={"notes": "added details"})
        assert result["ok"] is True
        assert ev.is_set()
        assert td["edge_label"] == "revise"

    async def test_two_tools_created_for_two_edges(self):
        tools, _, _, _ = _make_tools()
        names = {t.__name__ for t in tools}
        assert names == {"approve", "revise"}

    async def test_headless_approval_svc_none_approves_immediately(self):
        from agenthicc.workflow.phase_tools import make_transition_tools
        from agenthicc.workflow.plugin import DataBus, EdgeGate, EdgeSpec, PhaseNode
        node = PhaseNode(
            name  = "plan",
            edges = (EdgeSpec("execute", "approve", gate=EdgeGate(kind="plan_review")),),
        )
        bus  = DataBus(intent="x", run_id="r")
        ev   = asyncio.Event()
        td: dict = {}
        tools  = make_transition_tools(node, bus, ev, td, approval_svc=None)
        result = await _get(tools, "approve")(output={"plan": "draft"})
        assert result["ok"] is True
        assert ev.is_set()

    async def test_approval_feedback_injected_into_output(self):
        tools, ev, td, _ = _make_tools(approved=True, feedback="add tests")
        await _get(tools, "approve")(output={"plan": "draft"})
        assert td["output"].get("_user_instructions") == "add tests"

    async def test_rejection_next_call_can_approve(self):
        """Approval state is independent per call — second approve succeeds."""
        from agenthicc.workflow.phase_tools import make_transition_tools
        from agenthicc.workflow.plugin import DataBus, EdgeGate, EdgeSpec, PhaseNode

        call_count = [0]
        approval_svc = MagicMock()

        async def _side(req):
            call_count[0] += 1
            r = MagicMock()
            r.allowed = call_count[0] >= 2
            r.message = "" if r.allowed else "revise first"
            return r

        approval_svc.request_approval = _side

        node = PhaseNode(
            name  = "plan",
            edges = (EdgeSpec("execute", "approve", gate=EdgeGate(kind="plan_review")),),
        )
        bus = DataBus(intent="x", run_id="r")
        ev  = asyncio.Event()
        td: dict = {}
        tools = make_transition_tools(node, bus, ev, td, approval_svc)

        r1 = await _get(tools, "approve")(output={})
        assert r1.get("approved") is False
        assert not ev.is_set()

        r2 = await _get(tools, "approve")(output={})
        assert r2["ok"] is True
        assert ev.is_set()


# ── mode reset ────────────────────────────────────────────────────────────────

class TestModeReset:
    def _wf_run(self, status: str):
        wf = MagicMock()
        wf.status = status
        return wf

    def test_switch_on_complete(self):
        wf = self._wf_run("complete")
        active_mode = MagicMock()
        active_mode.default_workflow = "code_plan"
        assert wf.status == "complete" and active_mode.default_workflow is not None

    def test_no_switch_on_failed(self):
        assert self._wf_run("failed").status != "complete"

    def test_no_switch_when_mode_has_no_workflow(self):
        active_mode = MagicMock()
        active_mode.default_workflow = None
        assert active_mode.default_workflow is None
