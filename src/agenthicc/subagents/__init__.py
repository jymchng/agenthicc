"""Concurrent typed subagents for agenthicc (PRD-124)."""

from __future__ import annotations

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
from agenthicc.subagents.types import (
    DEFAULT_REGISTRY,
    DEFAULT_SUBAGENT_TIMEOUT_S,
    SubagentAggregator,
    SubagentPermissionConstraints,
    SubagentTypeRegistry,
    SubagentTypeSpec,
)

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
