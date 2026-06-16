"""Agents system — named, reusable @agent(...)-decorated classes (PRD-87)."""
from __future__ import annotations

from agenthicc.agents.plugin import (
    AgentDefinition,
    AgentPlugin,
    READ_CAPS,
    WRITE_CAPS,
    ROLE_DEFAULT_ALLOWED,
)
from agenthicc.agents.registry import AgentsRegistry, build_agents_registry

__all__ = [
    "AgentDefinition",
    "AgentPlugin",
    "READ_CAPS",
    "WRITE_CAPS",
    "ROLE_DEFAULT_ALLOWED",
    "AgentsRegistry",
    "build_agents_registry",
]
