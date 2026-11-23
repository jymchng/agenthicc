"""Model-facing workflow admission, roles, and bounded tool results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.runners.workflow_stack import WorkflowStack
from agenthicc.subagents.policy import SubagentExecutionPolicy
from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse
from agenthicc.tools.capabilities import ToolCapability
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import ModeManager
from agenthicc.workflows.plugin import PhaseSpec, WorkflowInvocation, WorkflowParams, WorkflowPlugin
from agenthicc.workflows.registry import WorkflowRegistry
from agenthicc.workflows.stack_tools import (
    WorkflowToolScope,
    bind_workflow_tools,
    enter_workflow,
    list_workflows,
)

pytestmark = pytest.mark.unit


class Inspect(WorkflowPlugin):
    name = "inspect"
    phases = [PhaseSpec("inspect", allowed_capabilities=frozenset({ToolCapability.READ}))]
    invocation = WorkflowInvocation(agent_callable=True, when_to_use="Inspect the project.")


class Write(WorkflowPlugin):
    name = "write_example"
    phases = [PhaseSpec("write", allowed_capabilities=frozenset({ToolCapability.WRITE}))]
    invocation = WorkflowInvocation(agent_callable=True)


class Interactive(WorkflowPlugin):
    name = "interactive"
    invocation = WorkflowInvocation(agent_callable=True, interactive=True)


class Private(WorkflowPlugin):
    name = "private"


@dataclass
class Confirm:
    allowed: bool
    requests: list[ApprovalRequest]

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(self.allowed)

    def reset_turn_memory(self) -> None:
        return None


def _scope(
    mode: str = "Yolo",
    *,
    max_depth: int = 4,
    headless: bool = False,
    approval: Confirm | None = None,
) -> WorkflowToolScope:
    state = AppState.create()
    ModeManager(app_state=state, default_name=mode)
    policy = SubagentExecutionPolicy.from_app_state(state)
    assert policy is not None
    stack = WorkflowStack(policy, max_depth=max_depth)
    registry = WorkflowRegistry()
    for workflow in (Inspect, Write, Interactive, Private):
        registry.register(workflow)
    return WorkflowToolScope(
        stack,
        registry,
        AgenthiccConfig(),
        active_mode=state.active_mode,
        approval=approval,
        headless=headless,
    )


async def test_approval_happens_once_and_receipt_does_not_start_a_frame() -> None:
    approval = Confirm(True, [])
    scope = _scope("Safe", approval=approval)
    with bind_workflow_tools(scope):
        first = await enter_workflow("inspect", "Inspect the fixture.")
        assert first["status"] == "handoff_accepted"
        assert await enter_workflow("inspect", "Inspect the fixture.") == first
        different = await enter_workflow("inspect", "Inspect another fixture.")
        assert different["code"] == "handoff_already_pending"
        assert different["fix"]
    assert len(approval.requests) == 1
    request = approval.requests[0]
    assert request.kind == "workflow_enter"
    assert request.tool_input["workflow"] == "inspect"
    assert request.tool_input["intent"] == "Inspect the fixture."
    assert scope.stack.depth == 0
    pending = scope.take_pending()
    assert pending is not None and pending.frame_id == first["frame_id"]
    assert scope.take_pending() is None


async def test_enter_workflow_returns_typed_refusals_before_creating_a_run() -> None:
    scope = _scope("Plan", headless=True)
    with bind_workflow_tools(scope):
        for name, code in {
            "missing": "unknown_workflow",
            "private": "not_agent_callable",
            "write_example": "ceiling_denied",
            "interactive": "not_allowed_from_frame",
        }.items():
            refused = await enter_workflow(name, "A task.")
            assert refused["code"] == code
            assert refused["fix"]
        assert (await enter_workflow("interactive", "A task."))[
            "reason"
        ] == "no_human_gate_available"
        assert [item["name"] for item in await list_workflows()] == ["inspect"]
    assert scope.stack.depth == 0
    assert scope.take_pending() is None

    limited = _scope(max_depth=0)
    with bind_workflow_tools(limited):
        assert (await enter_workflow("inspect", "A task."))["code"] == "depth_exceeded"
    assert limited.stack.depth == 0


async def test_parameter_validation_precedes_approval_and_acceptance_is_idempotent() -> None:
    approval = Confirm(True, [])
    scope = _scope("Safe", approval=approval)

    @dataclass
    class ReportParams(WorkflowParams):
        format: str = "summary"

    class Report(Inspect):
        name = "report"

        @classmethod
        def build_params(cls, raw: Mapping[str, object]) -> ReportParams:
            if raw != {"format": "summary"}:
                raise ValueError("Expected the summary format.")
            return ReportParams()

    scope.registry.register(Report)
    with bind_workflow_tools(scope):
        refused = await enter_workflow(Report.name, "Report the results.", {"format": "invalid"})
        assert refused["code"] == "invalid_params"
        assert refused["fix"]
        assert scope.pending is None
        assert approval.requests == []

        accepted = await enter_workflow(Report.name, "Report the results.", {"format": "summary"})
        assert accepted["status"] == "handoff_accepted"
        assert (
            await enter_workflow(Report.name, "Report the results.", {"format": "invalid"})
            == accepted
        )
    assert len(approval.requests) == 1
    assert approval.requests[0].tool_input["params"] == {"format": "summary"}
    assert approval.requests[0].tool_input["interactive"] is False
    assert approval.requests[0].tool_input["capability_ceiling"]
    pending = scope.take_pending()
    assert pending is not None and pending.params == ReportParams()
    assert scope.stack.depth == 0


async def test_a_frame_must_opt_in_before_it_can_call_another_workflow() -> None:
    scope = _scope()
    scope.stack.set_base(Inspect.name)
    with bind_workflow_tools(scope):
        assert await list_workflows() == []
        assert (await enter_workflow("write_example", "A task."))[
            "code"
        ] == "not_allowed_from_frame"

    class Parent(Inspect):
        name = "parent"
        invocation = WorkflowInvocation(agent_callable=True, allows_child_workflows=True)

    scope.registry.register(Parent)
    scope.stack.set_base(Parent.name)
    with bind_workflow_tools(scope):
        assert (await enter_workflow(Parent.name, "A task."))["code"] == "cycle"
    assert scope.stack.depth == 0


async def test_denied_approval_leaves_the_turn_available_for_other_tools() -> None:
    approval = Confirm(False, [])
    scope = _scope("Safe", approval=approval)
    with bind_workflow_tools(scope):
        assert (await enter_workflow("inspect", "Inspect the fixture."))["code"] == "denied"
        assert [item["name"] for item in await list_workflows()]
    assert scope.take_pending() is None
    assert scope.stack.depth == 0


async def test_workflow_listing_is_ordered_and_bounded() -> None:
    scope = _scope()
    for index in range(40):
        plugin = type(
            f"Workflow{index}",
            (Inspect,),
            {
                "name": f"workflow_{index:02}",
                "invocation": WorkflowInvocation(
                    agent_callable=True, when_to_use="Inspect " * 45, returns="Results " * 45
                ),
            },
        )
        scope.registry.register(plugin)
    with bind_workflow_tools(scope):
        listing = await list_workflows()
    names = [item["name"] for item in listing]
    assert names == sorted(names)
    assert len(json.dumps(listing)) <= 6_000
