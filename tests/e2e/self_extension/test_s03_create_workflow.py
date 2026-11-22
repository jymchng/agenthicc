"""Author a workflow and call it from the first parent turn after publication."""

from __future__ import annotations

import asyncio
from io import StringIO
from typing import TYPE_CHECKING, Literal

import pytest
from rich.console import Console

from agenthicc.commands import CommandContext, CommandDispatcher

from .harness import reply, tool_call

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from .harness import AuthoredSession, ScriptedTransport

pytestmark = pytest.mark.e2e

_SOURCE = """\
from collections.abc import Mapping

from agenthicc.workflows.plugin import PhaseSpec, WorkflowInvocation, WorkflowParams, WorkflowPlugin


class ReportFixture(WorkflowPlugin):
    name = "report_fixture"
    description = "Summarize a fixture."
    invocation = WorkflowInvocation(
        agent_callable=True,
        when_to_use="Summarize fixture evidence.",
        returns="The fixture summary.",
    )
    phases = [PhaseSpec("report", agent_type="planner", system_prompt_override="Report the evidence.")]

    @classmethod
    def build_params(cls, raw: Mapping[str, object]) -> WorkflowParams:
        if raw:
            raise ValueError("This workflow takes no parameters.")
        return WorkflowParams()
"""


def _queue_authoring(
    authored_session: AuthoredSession, script: ScriptedTransport, *, candidate: str = _SOURCE
) -> Path:
    context = authored_session.context
    source = authored_session.project / ".agenthicc/workflows/report_fixture/runner.py"
    marker = source.parent / ".agenthicc-pending.json"

    script.expect(
        "enter the authoring workflow",
        lambda request: "enter_workflow" in request.tools,
        tool_call(
            "enter_workflow",
            {
                "workflow": "create_workflow",
                "intent": "Create an agent-callable workflow that summarizes fixture evidence.",
            },
            use_id="author",
        ),
    )
    script.expect(
        "finish the parent turn",
        lambda request: request.contains("handoff_accepted"),
        reply("I’ll continue after authoring."),
    )
    script.expect(
        "record the design",
        lambda request: "finalize_design" in request.tools,
        tool_call(
            "finalize_design",
            {"workflow_name": "report_fixture", "design": "Summarize a fixture in one phase."},
        ),
    )
    script.expect(
        "finish design", lambda request: request.contains("Design finalized"), reply("Designed.")
    )
    script.expect(
        "write the scoped package",
        lambda request: marker.is_file() and "write_file" in request.tools,
        tool_call("write_file", {"path": "runner.py", "content": candidate}),
    )
    script.expect(
        "complete generation",
        lambda request: source.is_file() and "mark_generation_complete" in request.tools,
        tool_call("mark_generation_complete", {"summary": "Wrote the report workflow."}),
    )
    script.expect("finish generation", lambda request: marker.is_file(), reply("Package written."))
    script.expect(
        "review the verified candidate",
        lambda request: request.contains("DETERMINISTIC VALIDATION REPORT"),
        tool_call("approve_workflow", {"summary": "The report workflow matches the design."}),
    )
    script.expect(
        "finish validation", lambda request: marker.is_file(), reply("Candidate verified.")
    )
    if candidate == _SOURCE:
        script.expect(
            "summarize while the package is still pending",
            lambda request: (
                marker.is_file()
                and context.workflow_registry.get("report_fixture") is None
                and not authored_session.trust.records()
            ),
            reply("The validated report workflow is ready for publication."),
        )
    return source


async def test_new_workflow_is_published_and_called_in_the_same_session(
    authored_session: AuthoredSession, script: ScriptedTransport
) -> None:
    context = authored_session.context
    source = _queue_authoring(authored_session, script)
    marker = source.parent / ".agenthicc-pending.json"
    script.expect(
        "call the newly loaded workflow",
        lambda request: (
            request.contains("[WORKFLOW RETURN]")
            and request.contains("report_fixture")
            and request.contains("loaded")
            and context.workflow_registry.get("report_fixture") is not None
            and authored_session.trust.check(source, kind="workflow") is None
        ),
        tool_call(
            "enter_workflow",
            {"workflow": "report_fixture", "intent": "Summarize this fixture."},
            use_id="run-report",
        ),
    )
    script.expect(
        "finish the parent turn",
        lambda request: request.contains("handoff_accepted"),
        reply("I’ll continue after the report."),
    )
    script.expect(
        "answer in the new workflow",
        lambda request: request.contains("Summarize this fixture."),
        reply("The fixture includes a verified summary."),
    )
    script.expect(
        "deliver the new workflow return",
        lambda request: request.contains("The fixture includes a verified summary."),
        reply("The report is complete."),
    )

    result = await authored_session.run("default", "Create a workflow and run it for this fixture.")

    assert result.status == "complete", result.error
    assert context.workflow_stack.depth == 0
    assert context.workflow_stack.base.workflow_name == "default"
    assert source.is_file()
    assert not marker.exists()


async def test_failed_validation_returns_and_discards_the_candidate(
    authored_session: AuthoredSession, script: ScriptedTransport
) -> None:
    context = authored_session.context
    context.cfg.workflows["authoring"] = {"max_repair_cycles": 0}
    invalid = _SOURCE.replace('when_to_use="Summarize fixture evidence."', 'when_to_use=""')
    source = _queue_authoring(authored_session, script, candidate=invalid)
    script.expect(
        "receive the validation failure",
        lambda request: (
            request.contains("[WORKFLOW RETURN]")
            and request.contains("Status: failed")
            and request.contains("invocation.when_to_use")
        ),
        reply("The invalid candidate was discarded."),
    )

    result = await authored_session.run("default", "Create a reporting workflow.")

    assert result.status == "complete", result.error
    assert context.workflow_stack.depth == 0
    assert context.workflow_registry.get("report_fixture") is None
    assert not source.parent.exists()
    assert not authored_session.trust.records()


@pytest.mark.parametrize("policy", ["stage_only"])
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_operator_decides_whether_to_publish_the_pending_workflow(
    authored_session: AuthoredSession,
    script: ScriptedTransport,
    policy: str,
    decision: Literal["approve", "reject"],
) -> None:
    source = _queue_authoring(authored_session, script)
    marker = source.parent / ".agenthicc-pending.json"
    context = authored_session.context
    script.expect(
        "receive the staged return",
        lambda request: (
            request.contains("[WORKFLOW RETURN]")
            and request.contains("staged")
            and context.workflow_registry.get("report_fixture") is None
        ),
        reply("The workflow is waiting for review."),
    )

    assert policy == "stage_only"
    result = await authored_session.run("default", "Create a workflow for the fixture.")
    assert result.status == "complete", result.error
    assert source.is_file() and marker.is_file()
    assert context.workflow_registry.get("report_fixture") is None
    assert not authored_session.trust.records()
    (staged,) = context.authored.store().staged_runs()

    tasks: list[asyncio.Task[None]] = []
    output = StringIO()

    def start(coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    arguments = f"{decision} {staged.run_id}"
    command = f"/authored {arguments}"
    command_context = CommandContext(
        command,
        arguments,
        context.session_id,
        Console(file=output),
        context.cfg,
        authored=context.authored,
        start_task=start,
    )
    assert CommandDispatcher(context.cmd_registry).dispatch(command, command_context)
    await tasks.pop()

    assert not marker.exists()
    assert not context.authored.store().staged_runs()
    if decision == "approve":
        assert "loaded" in output.getvalue()
        assert context.workflow_registry.get("report_fixture") is not None
        assert authored_session.trust.check(source, kind="workflow") is None
    else:
        assert "rejected" in output.getvalue()
        assert context.workflow_registry.get("report_fixture") is None
        assert not source.exists()
        assert not authored_session.trust.records()
