"""Lightweight transition tools for UI/UX Pro Max specialist phases.

These tools record the agent's phase summary and optional structured details.
They do not inspect work, validate evidence, or decide whether a result is correct.
Full reports and design artifacts are created through ordinary workspace tools.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from agenthicc.tools.approval import ApprovalService
from agenthicc.workflows.ui_ux_pro_max.state import bounded_text


def _record(
    event: asyncio.Event,
    data: dict[str, object],
    *,
    decision: str,
    summary: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    if event.is_set():
        return {
            "ok": False,
            "error": "A successful transition is already recorded for this phase.",
            "fix": "Do not submit another transition; the runner will apply the recorded result.",
        }
    data.clear()
    data.update(details or {})
    data.update({"decision": decision, "summary": bounded_text(summary)})
    event.set()
    return {"ok": True, "message": f"Recorded {decision}; the runner will apply the transition."}


def make_product_discovery_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_product_discovery(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the product-discovery summary and any structured details."""
        return _record(
            event, data, decision="complete_product_discovery", summary=summary, details=details
        )

    return [complete_product_discovery]


def make_design_research_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def submit_design_research(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the design-research summary and any structured details."""
        return _record(
            event, data, decision="submit_design_research", summary=summary, details=details
        )

    return [submit_design_research]


def make_design_system_synthesis_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def submit_design_system(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the design-system summary and any structured details."""
        return _record(
            event, data, decision="submit_design_system", summary=summary, details=details
        )

    return [submit_design_system]


def make_design_system_review_tools(
    event: asyncio.Event,
    data: dict[str, object],
    *,
    attempts: int,
    max_attempts: int,
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def approve_design_system(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the review phase's approval decision and optional details."""
        return _record(
            event, data, decision="approve_design_system", summary=summary, details=details
        )

    @tool_control
    @_tool()
    async def request_design_system_revision(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record a revision decision, subject to the workflow's retry limit."""
        decision = (
            "request_design_system_revision" if attempts < max_attempts else "block_design_system"
        )
        return _record(event, data, decision=decision, summary=summary, details=details)

    @tool_control
    @_tool()
    async def block_design_system(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record a blocked review decision and optional details."""
        return _record(
            event, data, decision="block_design_system", summary=summary, details=details
        )

    return [approve_design_system, request_design_system_revision, block_design_system]


def make_stack_adaptation_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def submit_implementation_guide(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the implementation-guide summary and any structured details."""
        return _record(
            event, data, decision="submit_implementation_guide", summary=summary, details=details
        )

    return [submit_implementation_guide]


def make_experience_architecture_tools(
    approval_service: ApprovalService | None,
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    approval: dict[str, object] = {"granted": False, "feedback": ""}

    @_tool()
    async def request_implementation_approval(plan: str) -> dict[str, object]:
        """Request optional approval for the concrete implementation plan."""
        clean_plan = bounded_text(plan)
        if not clean_plan:
            return {"ok": False, "error": "The implementation plan must not be empty."}
        if approval_service is None:
            approval["granted"] = True
            approval["feedback"] = "No interactive approval service is configured for this run."
            return {"ok": True, "approved": True, "feedback": approval["feedback"]}
        from agenthicc.tools.approval import ApprovalRequest

        request = ApprovalRequest(
            tool_name="UI/UX Pro Max Implementation Plan",
            tool_use_id=uuid.uuid4().hex,
            tool_input={"plan": clean_plan},
            capabilities=frozenset(),
            event=asyncio.Event(),
            kind="plan_review",
        )
        try:
            response = await approval_service.request_approval(request)
        except Exception as exc:  # noqa: BLE001
            approval["granted"] = False
            return {"ok": False, "error": f"approval failed: {type(exc).__name__}: {exc}"}
        approval["granted"] = bool(response.allowed)
        approval["feedback"] = bounded_text(response.message)
        return {
            "ok": bool(response.allowed),
            "approved": bool(response.allowed),
            "feedback": approval["feedback"],
        }

    @tool_control
    @_tool()
    async def submit_experience_architecture(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the experience-architecture summary and any structured details."""
        if approval["granted"] is not True:
            return {
                "ok": False,
                "error": "Request and receive implementation-plan approval before submitting this phase.",
            }
        values = dict(details or {})
        if approval.get("feedback"):
            values.setdefault("approval_feedback", approval["feedback"])
        return _record(
            event,
            data,
            decision="submit_experience_architecture",
            summary=summary,
            details=values,
        )

    return [request_implementation_approval, submit_experience_architecture]


def make_implementation_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_design_implementation(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the implementation summary and any structured details."""
        return _record(
            event, data, decision="complete_design_implementation", summary=summary, details=details
        )

    return [complete_design_implementation]


def make_visual_audit_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def submit_visual_ux_audit(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the audit summary and any structured details without evaluating them."""
        return _record(
            event, data, decision="submit_visual_ux_audit", summary=summary, details=details
        )

    return [submit_visual_ux_audit]


def make_remediation_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_design_remediation(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the remediation summary and any structured details."""
        return _record(
            event, data, decision="complete_design_remediation", summary=summary, details=details
        )

    return [complete_design_remediation]


def make_design_acceptance_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_design_acceptance(
        summary: str, details: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Record the acceptance summary and any supplied details without verification."""
        return _record(
            event, data, decision="complete_design_acceptance", summary=summary, details=details
        )

    return [complete_design_acceptance]


def make_delivery_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_design_delivery(summary: str) -> dict[str, object]:
        """Record the final delivery summary."""
        return _record(event, data, decision="complete_design_delivery", summary=summary)

    return [complete_design_delivery]
