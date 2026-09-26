"""Concurrent typed subagents for agenthicc (PRD-124)."""

from __future__ import annotations

from agenthicc.subagents.types import (
    DEFAULT_SUBAGENT_TIMEOUT_S,
    SubagentTypeSpec,
    SubagentAggregator,
    SubagentTypeRegistry,
    DEFAULT_REGISTRY,
)
from agenthicc.subagents.pool import (
    SubagentTask,
    SubagentResult,
    AggregatedResult,
    SubagentWorker,
    SubagentPool,
    run_pool,
)
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.policy import SubagentExecutionPolicy
from agenthicc.subagents.communication import (
    AgentMessageBroker,
    AgentMessageEnvelope,
    CommunicationError,
    PoolContinuationRegistry,
    PoolHandle,
    make_child_communication_tools,
    make_parent_communication_tools,
)

__all__ = [
    "DEFAULT_SUBAGENT_TIMEOUT_S",
    "SubagentTypeSpec",
    "SubagentAggregator",
    "SubagentTypeRegistry",
    "DEFAULT_REGISTRY",
    "SubagentTask",
    "SubagentResult",
    "AggregatedResult",
    "SubagentWorker",
    "SubagentPool",
    "run_pool",
    "make_spawn_subagents_tool",
    "SubagentExecutionPolicy",
    "AgentMessageBroker",
    "AgentMessageEnvelope",
    "CommunicationError",
    "PoolContinuationRegistry",
    "PoolHandle",
    "make_child_communication_tools",
    "make_parent_communication_tools",
]
