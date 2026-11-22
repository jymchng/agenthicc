"""The provider, operator approval, and session-owned workflow handoff."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, CompletionChunk
from lauren_ai._transport._mock import MockTransport

from agenthicc.runners.headless import execute_workflow
from agenthicc.subagents.types import SubagentTypeSpec
from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse
from agenthicc.workflows.plugin import WorkflowInvocation
from agenthicc.workflows.registry import WorkflowRegistry

from .test_workflow_stack_session import ChildRunner, ChildWorkflow, _reply, _settle
from .test_workflow_stack_session import stack_session as stack_session

if TYPE_CHECKING:
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.runners.tui_session import TUISession
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.base_runner import BaseWorkflowRunner
    from agenthicc.workflows.config import WorkflowConfig

pytestmark = pytest.mark.integration


class _Approval:
    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(allowed=True)

    def reset_turn_memory(self) -> None:
        return None


def _submit(session: TUISession, context: SessionContext, text: str) -> None:
    context.app_state.conversation.queue.append(text)
    session.advance()


async def test_parent_turn_closes_before_model_requested_child_starts(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
) -> None:
    session, context, transport = stack_session
    approvals = _Approval()
    context.approval_svc = approvals
    child_started: list[int] = []

    class Assemble(ChildWorkflow):
        name = "create_tools"
        invocation = WorkflowInvocation(agent_callable=True, publishes=("tool",))

        @classmethod
        def build_runner(
            cls, config: WorkflowConfig, mode_manager: ModeManager | None
        ) -> BaseWorkflowRunner:
            child_started.append(len(transport.calls))
            return ChildRunner(config)

    context.workflow_registry.register(Assemble)
    transport.queue_tool_use("list_workflows", {})
    transport.queue_tool_use(
        "enter_workflow", {"workflow": "create_tools", "intent": "Prepare a read-only helper."}
    )
    _reply(transport, "I'll continue after the workflow.")
    _reply(transport, "I received the workflow result.")
    _submit(session, context, "Prepare the helper.")
    await _settle(session)

    assert child_started == [3]
    assert context.workflow_stack.depth == 0
    assert len(transport.calls) == 4
    assert len(approvals.requests) == 1
    assert approvals.requests[0].kind == "workflow_enter"
    assert "create_tools" in repr(transport.calls[1].messages)
    assert "handoff_accepted" in repr(transport.calls[2].messages)
    assert repr(transport.calls[-1].messages).count("[WORKFLOW RETURN]") == 1


@pytest.mark.parametrize("timeout", [False, True])
async def test_interrupted_turn_after_accepted_receipt_starts_no_child(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
    timeout: bool,
) -> None:
    session, context, _transport = stack_session
    accepted = asyncio.Event()
    context.approval_svc = _Approval()
    if timeout:
        context.cfg.execution.turn_timeout_s = 2.0

    class Review(ChildWorkflow):
        name = "review_fixture"
        invocation = WorkflowInvocation(agent_callable=True)

    class CancelAfterReceipt(MockTransport):
        async def complete(
            self, *args: object, **kwargs: object
        ) -> Completion | AsyncIterator[CompletionChunk]:
            if self.calls:
                accepted.set()
                await asyncio.Event().wait()
            return await super().complete(*args, **kwargs)

    context.workflow_registry.register(Review)
    transport = CancelAfterReceipt()
    context.agent_runner = AgentRunnerBase(transport=transport)
    transport.queue_tool_use(
        "enter_workflow", {"workflow": Review.name, "intent": "Review the sample."}
    )
    _submit(session, context, "Please review.")
    task = session._agent_task
    assert task is not None
    async with asyncio.timeout(5):
        await accepted.wait()
    assert context.workflow_stack.depth == 0
    if not timeout:
        task.cancel()
    await task

    assert context.workflow_stack.depth == 0
    assert session._workflow_handle is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize("driver", ["tui", "headless"])
async def test_opted_in_parent_resumes_the_same_checkpoint_after_child(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
    driver: str,
) -> None:
    from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

    session, context, transport = stack_session
    context.approval_svc = _Approval()
    parent_run_ids: list[str] = []

    class InspectChild(ChildWorkflow):
        name = "handoff_child"
        invocation = WorkflowInvocation(agent_callable=True)

        @classmethod
        def build_runner(
            cls, config: WorkflowConfig, mode_manager: ModeManager | None
        ) -> BaseWorkflowRunner:
            base = context.workflow_stack.base
            assert base.status == "suspended"
            parent_run_ids.append(base.run_id)
            refreshed = WorkflowRegistry()
            refreshed.register(cls)
            context.workflow_registry.replace_with(refreshed)
            return ChildRunner(config)

    class Parent(WorkflowPlugin):
        name = "handoff_parent"
        invocation = WorkflowInvocation(allows_child_workflows=True)
        phases = [PhaseSpec("review", agent_type="auto")]

    context.workflow_registry.register(Parent)
    context.workflow_registry.register(InspectChild)
    session._select_base_workflow(Parent.name)
    transport.queue_tool_use(
        "enter_workflow", {"workflow": InspectChild.name, "intent": "Check the fixture."}
    )
    _reply(transport, "I am ready to resume after the child.")
    _reply(transport, "I received the check and completed the review.")
    if driver == "headless":
        result = await execute_workflow(context, Parent.name, "Review and check the fixture.")
        assert result.status == "complete", result
    else:
        _submit(session, context, "Review and check the fixture.")
        await _settle(session)

    assert len(parent_run_ids) == 1 and parent_run_ids[0]
    assert context.workflow_stack.depth == 0
    assert context.workflow_stack.base.run_id == parent_run_ids[0]
    assert len(transport.calls) == 3
    assert "[WORKFLOW RETURN]" in repr(transport.calls[-1].messages)


async def test_worker_inherits_workflow_status_without_the_entry_tool(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
) -> None:
    session, context, transport = stack_session
    context.subagent_registry.register(
        SubagentTypeSpec("inspector", None, "Inspect your workflow status.")
    )
    transport.queue_tool_use(
        "spawn_subagents", {"tasks": [{"type": "inspector", "task": "Check your workflow status."}]}
    )
    transport.queue_tool_use("workflow_status", {})
    _reply(transport, "The worker inspected its frame.")
    _reply(transport, "The worker completed.")
    _submit(session, context, "Ask a worker to inspect its frame.")
    await _settle(session)

    assert len(transport.calls) == 4
    assert "enter_workflow" in {item["name"] for item in transport.calls[0].tools or []}
    names = {item["name"] for item in transport.calls[1].tools or []}
    assert {"workflow_status", "list_workflows"} <= names
    assert "enter_workflow" not in names
    assert "inspector" in repr(transport.calls[2].messages)
