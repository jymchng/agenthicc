"""Concurrent typed subagents for agenthicc (PRD-124)."""
from __future__ import annotations

from agenthicc.subagents.types import (
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

__all__ = [
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
]
