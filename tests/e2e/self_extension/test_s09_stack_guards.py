"""Exercise depth and cycle refusals through the workflow tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.workflows.plugin import PhaseSpec, WorkflowInvocation, WorkflowPlugin

from .harness import reply, tool_call

if TYPE_CHECKING:
    from lauren_ai._transport import Completion

    from .harness import AuthoredSession, ScriptedTransport


@pytest.mark.e2e
async def test_depth_and_cycle_refusals_do_not_start_extra_runs(
    authored_session: AuthoredSession, script: ScriptedTransport
) -> None:
    context = authored_session.context
    stack = context.workflow_stack
    names = tuple(f"guard_{index}" for index in range(1, 6))
    for name in names:
        definition = type(
            name,
            (WorkflowPlugin,),
            {
                "name": name,
                "invocation": WorkflowInvocation(agent_callable=True, allows_child_workflows=True),
                "phases": [PhaseSpec("check", agent_type="planner")],
            },
        )
        context.workflow_registry.register(definition)

    def enter(name: str, use_id: str) -> Completion:
        return tool_call(
            "enter_workflow", {"workflow": name, "intent": f"Check {name}."}, use_id=use_id
        )

    script.expect(
        "enter the first child",
        lambda request: "enter_workflow" in request.tools,
        enter(names[0], "enter-first"),
    )
    script.expect(
        "yield the root",
        lambda request: request.contains("handoff_accepted"),
        reply("Waiting for the child."),
    )
    script.expect(
        "test re-entry", lambda request: stack.depth == 1, enter(names[0], "repeat-first")
    )
    script.expect(
        "reject the cycle and enter the second child",
        lambda request: request.contains('"cycle"') and request.contains("already active"),
        enter(names[1], "enter-second"),
    )
    script.expect(
        "yield the first child",
        lambda request: request.contains("handoff_accepted"),
        reply("Waiting for the next child."),
    )
    for name in names[2:4]:
        script.expect(
            f"enter {name}",
            lambda request: "enter_workflow" in request.tools,
            enter(name, f"enter-{name}"),
        )
        script.expect(
            f"yield to {name}",
            lambda request: request.contains("handoff_accepted"),
            reply("Waiting for the next child."),
        )
    script.expect(
        "test the depth limit", lambda request: stack.depth == 4, enter(names[-1], "too-deep")
    )
    script.expect(
        "reject the fifth frame",
        lambda request: request.contains("depth_exceeded"),
        reply("The final permitted frame is complete."),
    )
    for depth in range(3, -1, -1):
        script.expect(
            f"return to depth {depth}",
            lambda request, depth=depth: (
                stack.depth == depth and request.contains("[WORKFLOW RETURN]")
            ),
            reply("The result is received."),
        )

    result = await authored_session.run("default", "Check that the nested workflow guards hold.")
    assert result.status == "complete" and stack.depth == 0
    store = WorkflowCheckpointStore(context.session_id)
    completed = [store.load(run_id) for run_id in store.list_run_ids()]
    assert {record.workflow_name for record in completed if record is not None} == set(names[:4])
