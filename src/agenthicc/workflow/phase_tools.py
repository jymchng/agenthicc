"""Phase-specific workflow tools injected into agent turns (PRD-88).

Tools created here are closures over shared asyncio state so the workflow
runner can observe their side-effects after _run_agent_turn returns.

Usage (inside WorkflowRunner._run_phase for planner phases)::

    plan_event = asyncio.Event()
    plan_data  = {}
    extra = make_planner_tools(approval_svc, plan_event, plan_data)
    filtered   = filtered + extra          # inject into this phase only
    await _run_agent_turn(..., project_plugin_tools=filtered, ...)
    if plan_event.is_set():
        final_plan = plan_data["plan"]     # written by finalize_plan()
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any


def make_planner_tools(
    approval_svc: Any,         # ApprovalService | None
    plan_event:   asyncio.Event,
    plan_data:    dict,
) -> list:
    """Return [request_plan_approval, finalize_plan] as @tool()-decorated callables.

    All three objects close over shared state so the workflow runner can
    observe the outcome without parsing LLM text:

      approval_state  — tracks whether the most recent request_plan_approval
                        call returned approved=True.  finalize_plan refuses
                        to proceed when this is False, forcing the model to
                        seek (and receive) approval before handing off.
      plan_data       — written by finalize_plan when approval is confirmed.
      plan_event      — set by finalize_plan; observed by _run_phase.

    approval_svc=None → request_plan_approval auto-approves (headless/tests).
    """
    # NOTE: no ``from __future__ import annotations`` in this scope —
    # @tool() reads real annotations at decoration time.
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    # Shared approval gate — updated by request_plan_approval, read by finalize_plan.
    approval_state: dict = {"granted": False}

    @_tool()
    async def request_plan_approval(plan: str) -> dict:
        """Request human review of the proposed plan via the Plan Approval overlay.

        Show the plan to the user and wait for their decision.
        Returns whether the plan was approved and any written feedback.

        If the response is not approved, revise the plan and call this tool
        again.  Only call finalize_plan() after receiving approved=True.

        Args:
            plan: The complete plan text to present to the user.
        """
        if approval_svc is None:
            approval_state["granted"] = True
            return {"approved": True, "feedback": ""}

        from agenthicc.tools.approval import ApprovalRequest  # noqa: PLC0415

        req = ApprovalRequest(
            tool_name="Plan Review",
            tool_use_id=uuid.uuid4().hex,
            tool_input={"plan": plan},
            capabilities=frozenset(),   # no caps → passes ToolCapabilityGate
            event=asyncio.Event(),
            kind="plan_review",         # → PlanApprovalOverlay in tui_session.py
        )
        response = await approval_svc.request_approval(req)
        # Always update the gate so each rejection correctly resets it.
        approval_state["granted"] = response.allowed
        return {
            "approved": response.allowed,
            "feedback": response.message or "",
        }

    @_tool()
    async def finalize_plan(plan: str) -> dict:
        """Finalize the approved plan and signal transition to execution.

        Call this ONLY after request_plan_approval has returned approved=True.
        Writes the plan to the workflow context and exits the planning phase.

        This tool will refuse with an error if the plan has not been approved —
        call request_plan_approval() first and ensure it returns approved=True.

        Args:
            plan: The final, approved plan text.
        """
        if not approval_state["granted"]:
            return {
                "ok": False,
                "error": (
                    "The plan has not been approved. "
                    "Call request_plan_approval() first and ensure it returns "
                    "approved=True before calling finalize_plan()."
                ),
            }
        plan_data["plan"] = plan
        plan_event.set()
        return {
            "ok": True,
            "message": (
                "Plan finalized and handed to the execution phase. "
                "Your task is complete for this phase."
            ),
        }

    return [request_plan_approval, finalize_plan]
