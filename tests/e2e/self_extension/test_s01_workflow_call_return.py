"""Follow a child through two model-driven phases and back to the parent."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agenthicc.runners.workflow_stack import WorkflowCall
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

from .harness import reply

if TYPE_CHECKING:
    from .harness import AuthoredSession, ModelRequest, ScriptedTransport

pytestmark = pytest.mark.e2e


async def test_two_child_phases_return_once_to_the_parent(
    authored_session: AuthoredSession, script: ScriptedTransport, request_log: list[ModelRequest]
) -> None:
    class InspectAndReport(WorkflowPlugin):
        name = "inspect_and_report"
        phases = [
            PhaseSpec(
                "inspect",
                agent_type="planner",
                next="report",
                system_prompt_override="Inspect the example.",
            ),
            PhaseSpec(
                "report", agent_type="planner", system_prompt_override="Summarize the inspection."
            ),
        ]

    context = authored_session.context
    context.workflow_registry.register(InspectAndReport)
    stack = context.workflow_stack
    parent = stack.base
    child = stack.push(WorkflowCall(InspectAndReport, "Inspect the sample.", "user"))
    script.expect(
        "child inspection",
        lambda request: request.contains("Inspect the sample."),
        reply("Inspection evidence."),
    )
    script.expect(
        "child report",
        lambda request: request.contains("Inspection evidence."),
        reply("Final report."),
    )
    script.expect(
        "parent return",
        lambda request: request.contains("[WORKFLOW RETURN]"),
        reply("I received the report."),
    )

    result = await authored_session.run(child.workflow_name, child.intent)

    assert result.status == "complete", result.error
    assert result.phases == ("inspect", "report")
    assert stack.depth == 0 and stack.top.frame_id == parent.frame_id
    assert len(request_log) == 3
    assert request_log[2].contains("Final report.")
    assert repr(request_log[2].messages).count("[WORKFLOW RETURN]") == 1
