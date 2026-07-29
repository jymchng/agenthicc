"""Phase-transition tools injected into create_workflow agent turns.

Every create_workflow phase advances **only** when the agent calls one of these
``@tool()``-decorated closures; the runner observes the shared ``asyncio.Event``
and data dict *after* the turn returns, so a transition can never be inferred
from the agent's prose.  This mirrors the tool-driven handoffs in
``code_plan.phase_tools``.

Usage (inside ``CreateWorkflowRunner._design`` and friends)::

    design_event = asyncio.Event()
    design_data: dict[str, object] = {}
    tools += make_design_tools(approval_svc, design_event, design_data, exit_event)
    await self._run_turn(..., tools=tools, ...)
    if design_event.is_set():
        approved_design = design_data["design"]
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tools.approval import ApprovalService

# A workflow name must be a python-module-safe slug so the generated file can
# live at ``.agenthicc/workflows/<name>.py`` and be importable by the loader.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _transition_failure(error: str, fix: str, **extra: object) -> dict[str, object]:
    """Return an actionable failure result for a rejected phase handoff."""
    return {
        "ok": False,
        "error": error,
        "fix": fix,
        "message": f"{error} Fix: {fix}",
        **extra,
    }


def validate_workflow_name(name: object) -> str | None:
    """Return an error string when *name* is not a valid workflow slug, else ``None``."""
    if not isinstance(name, str) or not name.strip():
        return "workflow_name must be a non-empty string"
    if not _NAME_RE.match(name.strip()):
        return (
            "workflow_name must be a lower_snake_case identifier "
            "(letters, digits and underscores, starting with a letter)"
        )
    return None


def make_design_tools(
    approval_svc: ApprovalService | None,
    design_event: asyncio.Event,
    design_data: dict[str, object],
    exit_event: asyncio.Event | None = None,
) -> list[Callable[..., object]]:
    """Return ``[request_design_approval, finalize_design, (exit_create_workflow)]``.

    Mirrors ``make_planner_tools``: ``request_design_approval`` shows the plan-review
    overlay and records the decision in a shared gate; ``finalize_design`` refuses
    to hand off until the gate is granted.  The gate is reset on every approval
    decision so a later rejection correctly blocks finalization again.

    ``approval_svc=None`` → ``request_design_approval`` auto-approves (headless/tests).
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_control  # noqa: PLC0415

    # Shared gate — written by request_design_approval, read by finalize_design.
    approval_state: dict[str, bool] = {"granted": False}

    @tool_control
    @_tool()
    async def request_design_approval(design: str, workflow_name: str) -> dict[str, object]:
        """Present the proposed workflow design to the user for approval.

        Show the complete design: the workflow's name and purpose, the ordered
        phases, each phase's objective / tools / mode, and the transition graph
        (next / on_reject).  Returns whether the design was approved plus any
        written feedback.  If it is not approved, revise the design and call this
        tool again.  Only call finalize_design() after receiving approved=True.

        Args:
            design: The complete, human-readable design of the workflow.
            workflow_name: The lower_snake_case name for the new workflow.
        """
        if not isinstance(design, str) or not design.strip():
            return _transition_failure(
                "The design transition was rejected: design must be a non-empty string.",
                "Provide the complete workflow design, then call "
                "request_design_approval(design, workflow_name) again.",
                approved=False,
            )
        name_error = validate_workflow_name(workflow_name)
        if name_error is not None:
            return _transition_failure(
                f"The design transition was rejected: {name_error}.",
                "Choose a valid lower_snake_case workflow_name and call "
                "request_design_approval(design, workflow_name) again.",
                approved=False,
            )

        if approval_svc is None:
            approval_state["granted"] = True
            return {
                "ok": True,
                "approved": True,
                "feedback": (
                    "The design is approved. Call finalize_design() now to hand off "
                    "to the generation phase."
                ),
            }

        from agenthicc.tools.approval import ApprovalRequest  # noqa: PLC0415

        req = ApprovalRequest(
            tool_name="Workflow Design Review",
            tool_use_id=uuid.uuid4().hex,
            tool_input={"plan": design, "workflow_name": workflow_name},
            capabilities=frozenset(),  # human approval request; no tool side effect
            event=asyncio.Event(),
            kind="plan_review",  # → PlanApprovalOverlay in tui_session.py
        )
        try:
            response = await approval_svc.request_approval(req)
        except Exception as exc:  # noqa: BLE001
            approval_state["granted"] = False
            return _transition_failure(
                f"Design approval could not be requested: {type(exc).__name__}: {exc}",
                "Resolve the approval-service error, then call "
                "request_design_approval(design, workflow_name) again.",
                approved=False,
            )

        approval_state["granted"] = response.allowed
        feedback = response.message or ""
        if response.allowed:
            suffix = (
                "The design is approved. Call finalize_design() now to hand off "
                "to the generation phase."
            )
            feedback = f"{feedback}\n\n{suffix}" if feedback else suffix
            return {"ok": True, "approved": True, "feedback": feedback}
        return _transition_failure(
            "The design approval request was rejected; the design cannot advance yet.",
            "Revise the design using the feedback, then call "
            "request_design_approval(design, workflow_name) again. "
            "Call finalize_design() only after approved=True.",
            approved=False,
            feedback=feedback,
        )

    @tool_control
    @_tool()
    async def finalize_design(design: str, workflow_name: str) -> dict[str, object]:
        """Finalize the approved design and hand off to the generation phase.

        Call this ONLY after request_design_approval returned approved=True.
        Writes the design to the workflow context and exits the design phase.
        Refuses with an error if the design has not been approved.

        Args:
            design: The final, approved workflow design.
            workflow_name: The lower_snake_case name for the new workflow.
        """
        if not isinstance(design, str) or not design.strip():
            return _transition_failure(
                "The design transition was rejected: finalized design must be non-empty.",
                "Provide the complete approved design, then call "
                "finalize_design(design, workflow_name) again.",
            )
        name_error = validate_workflow_name(workflow_name)
        if name_error is not None:
            return _transition_failure(
                f"The design transition was rejected: {name_error}.",
                "Provide a valid lower_snake_case workflow_name and call "
                "finalize_design(design, workflow_name) again.",
            )
        if not approval_state["granted"]:
            return _transition_failure(
                "The design has not been approved, so the phase cannot advance.",
                "Call request_design_approval(design, workflow_name) first and call "
                "finalize_design() only after it returns approved=True.",
            )
        design_data["design"] = design.strip()
        design_data["workflow_name"] = workflow_name.strip()
        design_event.set()
        return {
            "ok": True,
            "message": (
                "Design finalized and handed to the generation phase. Your role in the "
                "design phase is now complete — do not call any more tools and do not "
                "begin writing files. Write a single short acknowledgment (one or two "
                "sentences), then stop. The system will automatically start the "
                "generation phase."
            ),
        }

    tools: list[Callable[..., object]] = [request_design_approval, finalize_design]

    if exit_event is not None:
        _exit_event = exit_event

        @tool_control
        @_tool()
        async def exit_create_workflow(suggestion: str) -> dict[str, object]:
            """Exit the create_workflow workflow without authoring anything.

            Call this when the user's request is not actually about creating a new
            workflow — for example a question, an explanation request, or a task an
            existing workflow already covers.  Provide a short suggestion telling
            the user what to do instead, then write a brief conversational reply.

            Do NOT call this if the user genuinely wants a new custom workflow —
            that is exactly what this workflow is for.

            Args:
                suggestion: A brief message telling the user what to do instead.
            """
            _exit_event.set()
            design_data["suggestion"] = suggestion.strip() if isinstance(suggestion, str) else ""
            return {"accepted": True}

        tools.append(exit_create_workflow)  # noqa: F821

    return tools


def make_generation_tools(
    generate_event: asyncio.Event,
    generate_data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return ``[mark_generation_complete]``.

    Set once the agent has written the workflow file to disk.  ``generate_data``
    captures ``{"summary": str, "path": str}``; the runner validates the path
    deterministically after the turn returns.  If the turn ends without this tool
    firing, the generate phase retries.
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_control  # noqa: PLC0415

    @tool_control
    @_tool()
    async def mark_generation_complete(summary: str, path: str) -> dict[str, object]:
        """Signal that the workflow file has been fully written to disk.

        Call this ONLY after you have written the complete WorkflowPlugin source
        to .agenthicc/workflows/<name>.py using the write tools.  The validation
        phase will import and check the file automatically.  Do not call it more
        than once, and do not call it before the file is written.

        Args:
            summary: One or two sentences describing the generated workflow.
            path: The path the workflow file was written to.
        """
        if not isinstance(summary, str) or not summary.strip():
            return _transition_failure(
                "The generation transition was rejected: completion summary must be non-empty.",
                "Finish writing the workflow file and call "
                "mark_generation_complete(summary, path) with a concise summary of the file.",
            )
        if not isinstance(path, str) or not path.strip():
            return _transition_failure(
                "The generation transition was rejected: the written file path must be provided.",
                "Write the workflow file, then call mark_generation_complete(summary, path) "
                "with the exact path you wrote to.",
            )
        generate_data["summary"] = summary.strip()
        generate_data["path"] = path.strip()
        generate_event.set()
        return {
            "ok": True,
            "message": (
                "Generation marked complete and handed to the validation phase. Write a "
                "single short confirmation (one sentence) and stop — do not keep editing. "
                "The validation phase will start automatically."
            ),
        }

    return [mark_generation_complete]


def make_validation_tools(
    validate_event: asyncio.Event,
    validate_data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return ``[approve_workflow, reject_workflow]``.

    The agent calls one of these to signal its decision after reading the
    deterministic validation report.  ``validate_data`` captures
    ``{"action": "approve"|"reject", "summary": str, "reason": str}``.  The runner
    additionally overrides an ``approve`` when deterministic validation failed, so
    a broken workflow can never be accepted.
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.tools.capabilities import tool_control  # noqa: PLC0415

    @tool_control
    @_tool()
    async def approve_workflow(summary: str) -> dict[str, object]:
        """Signal that the generated workflow is correct and ready to finish.

        Call this only when the deterministic validation report shows no errors
        and the workflow's phases and prompts match the approved design.

        Do NOT call this if the report shows any error — call reject_workflow instead.

        Args:
            summary: One or two sentences describing what you verified.
        """
        if not isinstance(summary, str) or not summary.strip():
            return _transition_failure(
                "The validation transition was rejected: approval summary must be non-empty.",
                "Describe the verification evidence and call approve_workflow(summary) again.",
            )
        validate_data["action"] = "approve"
        validate_data["summary"] = summary.strip()
        validate_event.set()
        return {
            "ok": True,
            "message": (
                "Workflow approved. Transitioning to the summary phase. Write one sentence "
                "confirming the approval and stop."
            ),
        }

    @tool_control
    @_tool()
    async def reject_workflow(reason: str) -> dict[str, object]:
        """Signal that the generated workflow has problems that must be fixed.

        Call this when the validation report shows errors, or the file does not
        match the approved design.  The generation phase runs again to fix it.

        Do NOT call this if the workflow is correct — call approve_workflow instead.

        Args:
            reason: One or two sentences describing exactly what must be fixed.
        """
        if not isinstance(reason, str) or not reason.strip():
            return _transition_failure(
                "The validation rejection was rejected: a non-empty reason is required.",
                "Describe the concrete issue that must be fixed and call "
                "reject_workflow(reason) again.",
            )
        validate_data["action"] = "reject"
        validate_data["reason"] = reason.strip()
        validate_event.set()
        return {
            "ok": True,
            "message": (
                "Workflow rejected. Transitioning back to the generation phase. Write one "
                "sentence summarising the issue and stop."
            ),
        }

    return [approve_workflow, reject_workflow]
