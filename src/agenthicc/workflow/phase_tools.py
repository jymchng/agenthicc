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
            return {
                "approved": True,
                "feedback": "The plan is approved. Call finalize_plan() now to hand off to the execution phase.",
            }

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
        feedback = response.message or ""
        if response.allowed:
            suffix = "The plan is approved. Call finalize_plan() now to hand off to the execution phase."
            feedback = f"{feedback}\n\n{suffix}" if feedback else suffix
        return {
            "approved": response.allowed,
            "feedback": feedback,
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
                "Your role in the planning phase is now complete — do not call any more tools "
                "and do not begin implementing. "
                "Write a single short acknowledgment (one or two sentences) confirming the plan "
                "is ready, then stop. The system will automatically start the execution phase."
            ),
        }

    return [request_plan_approval, finalize_plan]


# ── PRD-101: unified completion tool ─────────────────────────────────────────


def make_completion_tool(
    node:             Any,           # PhaseNode
    data_bus:         Any,           # DataBus
    transition_event: asyncio.Event,
    transition_data:  dict,
    approval_svc:     Any,           # ApprovalService | None
) -> Any:
    """Return a single @tool()-decorated ``complete_phase`` callable for *node*.

    The tool docstring enumerates the node's valid edge labels so the LLM
    receives them in the tool schema.  When an edge has ``gate`` set, the tool
    suspends via ApprovalService and shows the configured overlay before
    committing the transition.

    Closing state:
      transition_event — set when the agent commits; checked by _run_node.
      transition_data  — {"edge_label": str | None, "output": dict} written here.
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    edges_by_label: dict[str, Any] = {e.label: e for e in (node.edges or ())}
    is_terminal = not edges_by_label
    labels_str  = ", ".join(f'"{l}"' for l in edges_by_label)

    if is_terminal:
        @_tool()
        async def complete_phase(output: dict) -> dict:
            """Signal that this final phase is complete.

            Call this when all tasks are done.  The workflow ends after this.

            Args:
                output: Structured summary of what was accomplished.
            """
            data_bus.set(node.name, output)
            transition_event.set()
            transition_data["edge_label"] = None
            transition_data["output"]     = output
            return {
                "ok":     True,
                "message": (
                    "Phase complete — this is the final phase.  Write a single "
                    "short confirmation and stop."
                ),
            }
    else:
        # Build a docstring with edge labels embedded so the LLM sees them.
        _doc = (
            f"Signal completion of this phase and choose the next transition.\n\n"
            f"Available edges: {labels_str}\n\n"
            f"Args:\n"
            f"    output: Structured data for downstream phases.\n"
            f"    next:   Edge label to follow ({labels_str})."
        )

        @_tool()
        async def complete_phase(output: dict, next: str) -> dict:
            """Signal completion and choose which edge to follow.

            Args:
                output: Structured data for downstream phases.
                next:   Edge label to follow.
            """
            edge = edges_by_label.get(next)
            if edge is None:
                avail = labels_str or "(none)"
                return {
                    "ok":    False,
                    "error": (
                        f"Unknown edge '{next}'.  "
                        f"Available: {avail}"
                    ),
                }

            # Gate: show overlay before committing.
            if edge.gate is not None and approval_svc is not None:
                from agenthicc.tools.approval import ApprovalRequest  # noqa: PLC0415
                req = ApprovalRequest(
                    tool_name=edge.gate.title or f"Review: {node.name}",
                    tool_use_id=uuid.uuid4().hex,
                    tool_input=output,
                    capabilities=frozenset(),
                    event=asyncio.Event(),
                    kind=edge.gate.kind,
                )
                response = await approval_svc.request_approval(req)
                if not response.allowed:
                    fb = response.message or ""
                    return {
                        "approved": False,
                        "feedback": (
                            fb
                            or "Transition not approved.  Revise and call "
                               "complete_phase() again."
                        ),
                    }
                # Incorporate any user instructions into the output
                if response.message:
                    output = {**output, "_user_instructions": response.message}

            data_bus.set(node.name, output)
            data_bus.record_edge(node.name, next)
            transition_event.set()
            transition_data["edge_label"] = next
            transition_data["output"]     = output
            return {
                "ok":     True,
                "message": (
                    f"Phase complete.  Transitioning to '{next}'.  "
                    "Write a single short confirmation and stop."
                ),
            }

        # Patch the docstring so the LLM sees the real edge labels.
        complete_phase.__doc__ = _doc

    return complete_phase


def make_executor_tools(
    execute_event: asyncio.Event,
    execute_data:  dict,
) -> list:
    """Return [mark_execute_complete] as a @tool()-decorated callable.

    Closes over shared asyncio state so WorkflowRunner._run_phase can detect
    whether the agent explicitly finished execution:

      execute_event — set by mark_execute_complete; checked by _run_phase.
      execute_data  — {"summary": str} written by mark_execute_complete.

    If the agent turn ends without calling mark_execute_complete (abrupt end,
    max_turns exhausted, tool error), execute_event remains unset and _run_phase
    returns approved=False, which retries the execute phase via on_reject="execute".
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    @_tool()
    async def mark_execute_complete(summary: str) -> dict:
        """Signal that all implementation tasks are finished.

        Call this ONLY when every file has been written, every function
        implemented, and the code is ready for the review phase.

        Do NOT call this if you still have outstanding implementation tasks.
        The system will automatically start the review phase after you call
        this tool.

        Args:
            summary: One or two sentences describing what was implemented.
        """
        execute_data["summary"] = summary
        execute_event.set()
        return {
            "ok": True,
            "message": (
                "Implementation marked complete and handed to the review phase. "
                "Write a single short confirmation (one sentence) and stop — "
                "do not continue implementing. The review phase will start automatically."
            ),
        }

    return [mark_execute_complete]
