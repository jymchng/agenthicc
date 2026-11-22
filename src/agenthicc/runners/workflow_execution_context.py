"""Context-local workflow identity for journaled agent turns.

Workflow runners call the shared agent-turn runner at several nested layers.
This context carries the durable workflow run ID across those calls without
adding a run-ID parameter to every workflow tool/phase API. It can also bind a
single interrupted-turn plan when an explicit workflow resume owns that turn.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from agenthicc.runners.run_coordinator import ResumePlan
    from agenthicc.subagents.policy import SubagentExecutionPolicy
    from agenthicc.tui.runtime.mode_manager import RuntimeMode


@dataclass
class _WorkflowExecution:
    run_id: str
    resume_plan: "ResumePlan | None" = None
    resume_plan_consumed: bool = False
    policy: SubagentExecutionPolicy | None = None
    model_request: str = ""


_current_execution: ContextVar[_WorkflowExecution | None] = ContextVar(
    "agenthicc_workflow_execution", default=None
)


@contextmanager
def bind_workflow_execution(
    run_id: str,
    *,
    resume_plan: "ResumePlan | None" = None,
    policy: SubagentExecutionPolicy | None = None,
    model_request: str = "",
) -> Iterator[None]:
    """Bind a workflow ID and optionally one plan for its first agent turn."""

    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("workflow run_id must be a non-empty string")
    execution = _WorkflowExecution(
        run_id=run_id, resume_plan=resume_plan, policy=policy, model_request=model_request
    )
    token = _current_execution.set(execution)
    try:
        yield
    finally:
        _current_execution.reset(token)


def current_workflow_policy() -> SubagentExecutionPolicy | None:
    execution = _current_execution.get()
    return execution.policy if execution is not None else None


def current_workflow_request() -> str:
    execution = _current_execution.get()
    return execution.model_request if execution is not None else ""


def effective_workflow_mode(mode: RuntimeMode) -> RuntimeMode:
    """Apply the active call's inherited policy to a runtime mode read."""
    policy = current_workflow_policy()
    if policy is None:
        return mode
    from agenthicc.runners.workflow_stack import limit_workflow_mode

    return limit_workflow_mode(mode, policy)


def effective_workflow_mode_name(name: str) -> str:
    """Preserve the workspace policy implied by the calling frame's mode."""
    policy = current_workflow_policy()
    if policy is None:
        return name
    from agenthicc.runners.workflow_stack import limit_workflow_mode_name

    return limit_workflow_mode_name(name, policy.mode_name)


def current_workflow_run_id() -> str | None:
    """Return the current workflow run ID, if the turn belongs to a workflow."""

    execution = _current_execution.get()
    return execution.run_id if execution is not None else None


def consume_workflow_resume_plan() -> "ResumePlan | None":
    """Return the bound interrupted-turn plan at most once in this execution."""

    execution = _current_execution.get()
    if execution is None or execution.resume_plan is None or execution.resume_plan_consumed:
        return None
    execution.resume_plan_consumed = True
    return execution.resume_plan
