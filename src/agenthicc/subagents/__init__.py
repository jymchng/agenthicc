"""Concurrent typed subagents for agenthicc (PRD-124)."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from agenthicc.subagents.types import (
    DEFAULT_REGISTRY,
    DEFAULT_SUBAGENT_TIMEOUT_S,
    SubagentAggregator,
    SubagentPermissionConstraints,
    SubagentTypeRegistry,
    SubagentTypeSpec,
)

if TYPE_CHECKING:
    from agenthicc.subagents.communication import (
        AgentMessageBroker,
        AgentMessageEnvelope,
        CommunicationError,
        PoolContinuationRegistry,
        PoolHandle,
        make_child_communication_tools,
        make_parent_communication_tools,
    )
    from agenthicc.subagents.policy import SubagentExecutionPolicy
    from agenthicc.subagents.pool import (
        AggregatedResult,
        SubagentPool,
        SubagentResult,
        SubagentTask,
        SubagentWorker,
        run_pool,
    )
    from agenthicc.subagents.tool import make_spawn_subagents_tool

__all__ = [
    "DEFAULT_REGISTRY",
    "DEFAULT_SUBAGENT_TIMEOUT_S",
    "AgentMessageBroker",
    "AgentMessageEnvelope",
    "AggregatedResult",
    "CommunicationError",
    "PoolContinuationRegistry",
    "PoolHandle",
    "SubagentAggregator",
    "SubagentExecutionPolicy",
    "SubagentPermissionConstraints",
    "SubagentPool",
    "SubagentResult",
    "SubagentTask",
    "SubagentTypeRegistry",
    "SubagentTypeSpec",
    "SubagentWorker",
    "make_child_communication_tools",
    "make_parent_communication_tools",
    "make_spawn_subagents_tool",
    "run_pool",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentMessageBroker": ("communication", "AgentMessageBroker"),
    "AgentMessageEnvelope": ("communication", "AgentMessageEnvelope"),
    "CommunicationError": ("communication", "CommunicationError"),
    "PoolContinuationRegistry": ("communication", "PoolContinuationRegistry"),
    "PoolHandle": ("communication", "PoolHandle"),
    "make_child_communication_tools": ("communication", "make_child_communication_tools"),
    "make_parent_communication_tools": ("communication", "make_parent_communication_tools"),
    "SubagentExecutionPolicy": ("policy", "SubagentExecutionPolicy"),
    "AggregatedResult": ("pool", "AggregatedResult"),
    "SubagentPool": ("pool", "SubagentPool"),
    "SubagentResult": ("pool", "SubagentResult"),
    "SubagentTask": ("pool", "SubagentTask"),
    "SubagentWorker": ("pool", "SubagentWorker"),
    "run_pool": ("pool", "run_pool"),
    "make_spawn_subagents_tool": ("tool", "make_spawn_subagents_tool"),
}


def __getattr__(name: str) -> object:
    """Load optional worker runtime modules only when their exports are used."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    module = import_module(f"{__name__}.{module_name}")
    value = object.__getattribute__(module, attribute_name)
    globals()[name] = value
    return value
