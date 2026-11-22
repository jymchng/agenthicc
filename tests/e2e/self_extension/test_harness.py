"""Exercise the acceptance harness before attaching the scenario workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

from .harness import ProcessStopped, reply

if TYPE_CHECKING:
    from .harness import AuthoredSession, CrashAt, ModelRequest, ScriptedTransport

pytestmark = pytest.mark.e2e


async def test_harness_captures_the_real_headless_workflow_request(
    authored_session: AuthoredSession, script: ScriptedTransport, request_log: list[ModelRequest]
) -> None:
    class InspectWorkflow(WorkflowPlugin):
        name = "inspect_workflow"
        phases = [PhaseSpec(name="inspect", system_prompt_override="Inspect the fixture.")]

    context = authored_session.context
    context.workflow_registry.register(InspectWorkflow)
    script.expect(
        "inspect the project",
        lambda request: request.contains("Inspect the local fixture"),
        reply("Fixture inspected."),
    )
    outcome = await authored_session.run("inspect_workflow", "Inspect the local fixture")
    assert outcome.status == "complete", outcome.error
    assert outcome.phases == ("inspect",)
    assert len(request_log) == 1
    assert request_log[0].contains("Inspect the local fixture")
    assert context.reload.skills.current().value == {}
    assert authored_session.trust.records() == ()


def test_harness_only_stops_at_the_armed_boundary(crash_at: CrashAt) -> None:
    crash_at.seam = "before_delivery"
    crash_at.check("after_publication")
    with pytest.raises(ProcessStopped, match="before_delivery"):
        crash_at.check("before_delivery")


async def test_script_refuses_an_unexpected_request(script: ScriptedTransport) -> None:
    from lauren_ai._transport import Message

    script.expect(
        "expected intent", lambda request: request.contains("expected intent"), reply("done")
    )

    with pytest.raises(AssertionError, match="did not match"):
        await script.complete([Message.user("different intent")], model="offline")
    with pytest.raises(AssertionError, match="did not match"):
        script.assert_finished()
    assert script.requests[0].contains("different intent")
