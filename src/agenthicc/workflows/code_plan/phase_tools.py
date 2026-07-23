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
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tools.approval import ApprovalService


def make_planner_tools(
    approval_svc: ApprovalService | None,
    plan_event: asyncio.Event,
    plan_data: dict[str, object],
    exit_event: asyncio.Event | None = None,
) -> list[Callable[..., object]]:
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
    approval_state: dict[str, bool] = {"granted": False}

    @_tool()
    async def request_plan_approval(plan: str) -> dict[str, object]:
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
            capabilities=frozenset(),  # no caps → passes ToolCapabilityGate
            event=asyncio.Event(),
            kind="plan_review",  # → PlanApprovalOverlay in tui_session.py
        )
        response = await approval_svc.request_approval(req)
        # Always update the gate so each rejection correctly resets it.
        approval_state["granted"] = response.allowed
        feedback = response.message or ""
        if response.allowed:
            suffix = (
                "The plan is approved. Call finalize_plan() now to hand off to the execution phase."
            )
            feedback = f"{feedback}\n\n{suffix}" if feedback else suffix
        return {
            "approved": response.allowed,
            "feedback": feedback,
        }

    @_tool()
    async def finalize_plan(plan: str) -> dict[str, object]:
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

    tools: list[Callable[..., object]] = [request_plan_approval, finalize_plan]

    if exit_event is not None:
        _exit_event = exit_event

        @_tool()
        async def exit_code_plan() -> dict[str, object]:
            """Exit the code_plan workflow immediately without producing a plan.

            Call this when the user's request does not require planning,
            execution, or review — for example: a question, an explanation
            request, a read-only query, or an intent too vague to turn into
            concrete implementation steps.

            After calling this tool, write a short conversational reply to the
            user explaining what you can help with instead.

            Do NOT call this if the task is executable but complex or
            unfamiliar — that is exactly what planning is for.
            """
            _exit_event.set()
            return {"accepted": True}

        tools.append(exit_code_plan)  # noqa: F821

    return tools


def make_executor_tools(
    execute_event: asyncio.Event,
    execute_data: dict[str, object],
) -> list[Callable[..., object]]:
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
    async def mark_execute_complete(summary: str) -> dict[str, object]:
        """Signal that all implementation tasks are finished.

        Call this ONLY when every file has been written, every function
        implemented, and the code is ready for the review phase.

        Do NOT call this if you still have outstanding implementation tasks.
        The system will automatically start the review phase after you call
        this tool.  Do NOT call it more than once.

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


def make_reviewer_tools(
    review_event: asyncio.Event,
    review_data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return [approve_review, reject_review] as @tool()-decorated callables.

    Replaces XML-tag output parsing for the review phase.  The agent calls
    one of these tools to signal its decision; the transition is unambiguous
    regardless of how the agent phrases its reasoning.

      review_event — set by either tool; checked by _run_phase after the turn.
      review_data  — {"action": "approve"|"reject", "summary": str, "reason": str}.
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    @_tool()
    async def approve_review(summary: str) -> dict[str, object]:
        """Signal that the implementation passes review and is ready to summarize.

        Call this when all tests pass and the code is correct.  The workflow
        will move to the summary phase automatically.

        Do NOT call this if you found any issues — call reject_review instead.

        Args:
            summary: One or two sentences describing what was verified.
        """
        review_data["action"] = "approve"
        review_data["summary"] = summary
        review_event.set()
        return {
            "ok": True,
            "message": (
                "Review approved.  Transitioning to the summary phase.  "
                "Write one sentence confirming the approval and stop."
            ),
        }

    @_tool()
    async def reject_review(reason: str) -> dict[str, object]:
        """Signal that the implementation has issues that must be fixed.

        Call this when tests fail or the code is incorrect.  The workflow
        will return to the execution phase automatically.

        Do NOT call this if the implementation is correct — call approve_review instead.

        Args:
            reason: One or two sentences describing exactly what needs to be fixed.
        """
        review_data["action"] = "reject"
        review_data["reason"] = reason
        review_event.set()
        return {
            "ok": True,
            "message": (
                "Review rejected.  Transitioning back to the execution phase.  "
                "Write one sentence summarising the issue and stop."
            ),
        }

    return [approve_review, reject_review]


def _validate_questions(questions: object) -> list[str]:
    """Return a list of human-readable problem strings, empty when valid."""
    if not isinstance(questions, list) or not questions:
        return ["questions must be a non-empty list"]
    problems: list[str] = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            problems.append(f"question[{i}] must be a dict, got {type(q).__name__}")
            continue
        if not q.get("id"):
            problems.append(f"question[{i}] missing required key 'id'")
        if not q.get("text"):
            problems.append(f"question[{i}] missing required key 'text'")
        opts = q.get("options")
        if not opts:
            problems.append(f"question[{i}] missing required key 'options'")
        elif not isinstance(opts, list) or len(opts) == 0:
            problems.append(f"question[{i}].options must be a non-empty list of strings")
    return problems


def make_questions_tool(
    approval_svc: ApprovalService | None,
) -> list[Callable[..., object]]:
    """Return [ask_user] as a @tool()-decorated callable.

    ask_user() presents a QuestionsOverlay to the user and blocks until all
    questions are answered.  Returns a dict mapping question id → answer string.

    approval_svc=None → returns {"cancelled": True} immediately (headless/tests).
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    @_tool()
    async def ask_user(questions: list[dict[str, object]]) -> dict[str, object]:
        """Present the user with a set of questions and collect their answers.

        Each question must be a dict with:
          - "id"      (str)  — key in the returned answer dict
          - "text"    (str)  — question shown to the user
          - "options" (list) — selectable choices as plain strings

        A free-text fallback ("Other — type your answer") is always added
        automatically — do not include it in options.

        Returns a dict mapping each question id to the answer string, or
        {"cancelled": True} if the user cancels.

        On invalid input returns {"error": ..., "problems": [...]} so you
        can correct the format and retry.

        Example:
            ask_user([
                {
                    "id": "language",
                    "text": "Which language should we use?",
                    "options": ["Python", "TypeScript", "Go"]
                },
                {
                    "id": "framework",
                    "text": "Which framework?",
                    "options": ["FastAPI", "Django", "Flask"]
                }
            ])

        Args:
            questions: list of {id, text, options} dicts.
        """
        if approval_svc is None:
            return {"cancelled": True}

        problems = _validate_questions(questions)
        if problems:
            return {
                "error": "invalid questions format — fix the problems below and retry",
                "problems": problems,
                "expected_format": {
                    "id": "str",
                    "text": "str",
                    "options": ["str", "...at least one option"],
                },
            }

        import asyncio as _asyncio  # noqa: PLC0415
        import uuid as _uuid  # noqa: PLC0415
        import json as _json  # noqa: PLC0415
        from agenthicc.tools.approval import ApprovalRequest  # noqa: PLC0415

        req = ApprovalRequest(
            tool_name="Questions",
            tool_use_id=_uuid.uuid4().hex,
            tool_input={"questions": questions},
            capabilities=frozenset(),
            event=_asyncio.Event(),
            kind="questions",
        )
        response = await approval_svc.request_approval(req)
        if not response.allowed:
            return {"cancelled": True}
        try:
            decoded = _json.loads(response.message)
            if isinstance(decoded, dict) and all(isinstance(key, str) for key in decoded):
                return {key: value for key, value in decoded.items()}
            return {"error": "answers must be a JSON object"}
        except Exception:  # noqa: BLE001
            return {"error": "failed to parse answers"}

    return [ask_user]
