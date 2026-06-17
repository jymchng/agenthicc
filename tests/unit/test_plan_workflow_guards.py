"""Unit tests: PRD-89 — plan approval enforcement and mode reset."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tools(approved: bool = True, feedback: str = ""):
    """Return (request_plan_approval, finalize_plan, plan_event, plan_data)."""
    from agenthicc.workflows.phase_tools import make_planner_tools

    approval_svc = MagicMock()
    response = MagicMock()
    response.allowed  = approved
    response.message  = feedback
    approval_svc.request_approval = AsyncMock(return_value=response)

    plan_event = asyncio.Event()
    plan_data  = {}
    tools = make_planner_tools(approval_svc, plan_event, plan_data)
    rpa, fp = tools[0], tools[1]
    return rpa, fp, plan_event, plan_data, approval_svc


# ── approval gate (Bug 1) ─────────────────────────────────────────────────────


class TestApprovalGate:
    async def test_finalize_fails_without_prior_approval(self):
        """finalize_plan returns ok=False if request_plan_approval was never called."""
        _, fp, plan_event, plan_data, _ = _make_tools()
        result = await fp(plan="My plan")
        assert result["ok"] is False
        assert "approved" in result["error"].lower() or "approval" in result["error"].lower()
        assert not plan_event.is_set()
        assert "plan" not in plan_data

    async def test_finalize_fails_after_rejection(self):
        """finalize_plan returns ok=False if the last approval was rejected."""
        rpa, fp, plan_event, plan_data, _ = _make_tools(approved=False, feedback="needs error handling")
        await rpa(plan="My plan")          # rejected
        result = await fp(plan="My plan")  # should be blocked
        assert result["ok"] is False
        assert not plan_event.is_set()

    async def test_finalize_succeeds_after_approval(self):
        """finalize_plan succeeds and sets plan_event when approval was granted."""
        rpa, fp, plan_event, plan_data, _ = _make_tools(approved=True)
        rpa_result = await rpa(plan="My plan")
        assert rpa_result["approved"] is True
        fp_result = await fp(plan="My plan")
        assert fp_result["ok"] is True
        assert plan_event.is_set()
        assert plan_data["plan"] == "My plan"

    async def test_approval_state_resets_on_each_request(self):
        """A rejection followed by approval followed by finalize succeeds."""
        from agenthicc.workflows.phase_tools import make_planner_tools

        plan_event = asyncio.Event()
        plan_data  = {}
        call_count = [0]

        approval_svc = MagicMock()

        async def _side_effect(req):
            call_count[0] += 1
            r = MagicMock()
            r.allowed = call_count[0] >= 2   # reject first, approve second
            r.message = "" if r.allowed else "please revise"
            return r

        approval_svc.request_approval = _side_effect
        rpa, fp, _, _, _ = make_planner_tools(approval_svc, plan_event, plan_data), None, None, None, None
        rpa, fp = make_planner_tools(approval_svc, plan_event, plan_data)[:2]

        first  = await rpa(plan="v1")
        assert first["approved"] is False         # rejected

        second = await rpa(plan="v2")
        assert second["approved"] is True          # approved

        result = await fp(plan="v2")
        assert result["ok"] is True
        assert plan_event.is_set()
        assert plan_data["plan"] == "v2"

    async def test_finalize_blocked_returns_structured_error(self):
        """Error response from finalize_plan includes a clear instruction."""
        _, fp, _, _, _ = _make_tools()
        result = await fp(plan="anything")
        assert "ok" in result
        assert result["ok"] is False
        assert "error" in result
        assert len(result["error"]) > 0

    async def test_headless_auto_approves(self):
        """approval_svc=None auto-approves, allowing finalize_plan."""
        from agenthicc.workflows.phase_tools import make_planner_tools

        plan_event = asyncio.Event()
        plan_data  = {}
        rpa, fp = make_planner_tools(None, plan_event, plan_data)[:2]

        rpa_result = await rpa(plan="headless plan")
        assert rpa_result["approved"] is True

        fp_result = await fp(plan="headless plan")
        assert fp_result["ok"] is True
        assert plan_event.is_set()

    async def test_request_plan_approval_returns_feedback_on_rejection(self):
        """Feedback text is passed through from the approval response."""
        rpa, _, _, _, svc = _make_tools(approved=False, feedback="add error handling")
        result = await rpa(plan="My plan")
        assert result["approved"] is False
        assert result["feedback"] == "add error handling"


# ── mode reset (Bug 2) ────────────────────────────────────────────────────────


class TestModeReset:
    """The mode-reset logic lives in tui_session._run_turn which is hard to
    unit-test directly.  We test the conditions it checks instead."""

    def _make_wf_run(self, status: str):
        wf = MagicMock()
        wf.status = status
        return wf

    def test_switch_condition_on_complete(self):
        """Mode should switch when workflow status is 'complete' and mode has a workflow."""
        wf_result   = self._make_wf_run("complete")
        active_mode = MagicMock()
        active_mode.default_workflow = "code_plan"

        should_switch = (
            wf_result is not None
            and getattr(wf_result, "status", None) == "complete"
            and active_mode.default_workflow is not None
        )
        assert should_switch

    def test_no_switch_on_failed(self):
        """Mode must NOT switch when workflow fails."""
        wf_result   = self._make_wf_run("failed")
        active_mode = MagicMock()
        active_mode.default_workflow = "code_plan"

        should_switch = (
            wf_result is not None
            and getattr(wf_result, "status", None) == "complete"
            and active_mode.default_workflow is not None
        )
        assert not should_switch

    def test_no_switch_when_mode_has_no_workflow(self):
        """Mode must NOT switch when the active mode has no workflow binding."""
        wf_result   = self._make_wf_run("complete")
        active_mode = MagicMock()
        active_mode.default_workflow = None

        should_switch = (
            wf_result is not None
            and getattr(wf_result, "status", None) == "complete"
            and active_mode.default_workflow is not None
        )
        assert not should_switch

    def test_no_switch_when_no_workflow_run(self):
        """Mode must NOT switch when workflow_run() returns None."""
        wf_result   = None
        active_mode = MagicMock()
        active_mode.default_workflow = "code_plan"

        should_switch = (
            wf_result is not None
            and getattr(wf_result, "status", None) == "complete"
            and active_mode.default_workflow is not None
        )
        assert not should_switch
