"""Resolve workflow tasks against the live registry and their inherited ceiling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agenthicc.tools.capabilities import ToolCapability

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agenthicc.config import AgenthiccConfig
    from agenthicc.workflows.plugin import WorkflowParams, WorkflowPlugin
    from agenthicc.workflows.registry import WorkflowRegistry

WorkflowTaskError = Literal[
    "unknown_workflow",
    "workflow_not_eligible",
    "workflow_requires_human_gate",
    "ceiling_denied",
    "invalid_params",
    "workflow_runtime_unavailable",
    "workflow_workspace_unavailable",
]

_WRITE_CAPABILITIES = frozenset(
    {ToolCapability.WRITE, ToolCapability.EXECUTE, ToolCapability.GIT_WRITE}
)


@dataclass(frozen=True, slots=True)
class WorkflowTaskRefusal:
    """A refusal assigned to this task alone."""

    workflow_name: str
    code: WorkflowTaskError
    reason: str


@dataclass(frozen=True, slots=True)
class WorkflowTaskSelection:
    """The definition and parameters pinned at spawn."""

    definition: type[WorkflowPlugin]
    params: WorkflowParams
    arguments: tuple[tuple[str, str], ...]
    requires_worktree: bool


WorkflowTaskAdmission = WorkflowTaskSelection | WorkflowTaskRefusal


def workflow_task_admission(
    name: str,
    arguments: object,
    registry: WorkflowRegistry,
    config: AgenthiccConfig,
    blocked_capabilities: frozenset[str],
    *,
    can_write: bool,
) -> WorkflowTaskAdmission:
    """Resolve one workflow call without starting a worker or prompting a human."""
    definition = registry.get(name)
    if definition is None:
        return WorkflowTaskRefusal(name, "unknown_workflow", f"Workflow {name!r} is unavailable.")
    if definition.invocation.interactive or any(
        phase.agent_type == "human" for phase in definition.phases
    ):
        return WorkflowTaskRefusal(
            name, "workflow_requires_human_gate", "This workflow needs a human interaction."
        )
    if not definition.invocation.subagent_eligible:
        return WorkflowTaskRefusal(
            name, "workflow_not_eligible", "This workflow does not admit worker execution."
        )
    required = frozenset(
        capability
        for phase in definition.phases
        for capability in (phase.resolved_allowed_caps or ())
    )
    if definition.invocation.publishes:
        required |= {ToolCapability.WRITE}
    denied = required & blocked_capabilities
    requires_worktree = bool(required & _WRITE_CAPABILITIES)
    if denied or (ToolCapability.WRITE in required and not can_write):
        reason = ", ".join(sorted(denied)) or "write"
        return WorkflowTaskRefusal(name, "ceiling_denied", f"The worker scope excludes {reason}.")
    raw: dict[str, str] = {}
    if arguments is not None:
        if not isinstance(arguments, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in arguments.items()
        ):
            return WorkflowTaskRefusal(
                name, "invalid_params", "Workflow params must contain string keys and values."
            )
        raw = dict(arguments)
    try:
        base: Mapping[str, object] = config.workflows.get(name, {})
        params = definition.build_params({**base, **raw})
    except (TypeError, ValueError):
        return WorkflowTaskRefusal(name, "invalid_params", "The workflow refused these parameters.")
    return WorkflowTaskSelection(definition, params, tuple(sorted(raw.items())), requires_worktree)
