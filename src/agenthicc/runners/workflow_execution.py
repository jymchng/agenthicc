"""Run a selected workflow under a session or worker execution binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agenthicc.runners.workflow_execution_context import bind_workflow_execution

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from agenthicc.runners.authored import AuthoredPublisher
    from agenthicc.runners.run_coordinator import ResumePlan
    from agenthicc.subagents.policy import SubagentExecutionPolicy
    from agenthicc.tools.base import ToolLike
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.plugin import WorkflowPlugin


@dataclass(frozen=True, slots=True)
class WorkflowWorkerRuntime:
    """Session-owned services a workflow worker may use through its own scope."""

    config: WorkflowConfig
    publisher: AuthoredPublisher


async def run_workflow_core(
    definition: type[WorkflowPlugin],
    config: WorkflowConfig,
    mode_manager: ModeManager | None,
    intent: str,
    *,
    resume: bool = False,
    resume_plan: ResumePlan | None = None,
    policy: SubagentExecutionPolicy | None = None,
    model_request: str = "",
    observe_tool: Callable[[str, Mapping[str, object], object], None] | None = None,
    worker_tools: tuple[ToolLike, ...] = (),
) -> object:
    """Invoke the plugin with the handle, tool policy, and saved conversation."""
    runner = definition.build_runner(config, mode_manager)
    handle = config.workflow_handle
    if handle is None:
        return await runner.run(intent)
    with bind_workflow_execution(
        handle.run_id,
        resume_plan=resume_plan,
        policy=policy,
        model_request=model_request,
        observe_tool=observe_tool,
        worker_tools=worker_tools,
    ):
        if resume:
            return await runner.resume(handle.context)
        return await runner.run(intent)
