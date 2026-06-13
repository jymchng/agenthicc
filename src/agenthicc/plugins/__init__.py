"""Tool plugin system (PRD-24 through PRD-27)."""
from __future__ import annotations

from agenthicc.plugins.agent_config import (
    AgentDef,
    discover_agents,
    load_agent_system_prompt,
    validate_agent_name,
)

__all__ = [
    "AgentDef",
    "discover_agents",
    "load_agent_system_prompt",
    "validate_agent_name",
]
