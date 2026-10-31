"""Agents system — named, reusable agent definitions (PRD-87, AGX-09)."""

from __future__ import annotations

from agenthicc.agents.definitions import (
    AgentSpec,
    DefinitionError,
    parse_definition,
    render_definition,
)
from agenthicc.agents.plugin import (
    AgentPlugin,
    READ_CAPS,
    WRITE_CAPS,
    ROLE_DEFAULT_ALLOWED,
)
from agenthicc.agents.registry import AgentsRegistry, CatalogEntry, build_agents_registry

__all__ = [
    "AgentPlugin",
    "AgentSpec",
    "AgentsRegistry",
    "CatalogEntry",
    "DefinitionError",
    "READ_CAPS",
    "ROLE_DEFAULT_ALLOWED",
    "WRITE_CAPS",
    "build_agents_registry",
    "parse_definition",
    "render_definition",
]
