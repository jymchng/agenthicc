"""Depth, fan-out, and ancestry policy for nested subagent pools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agenthicc.config import SubagentSettings
from agenthicc.subagents.types import SubagentTypeRegistry

if TYPE_CHECKING:
    from agenthicc.plugins.registry import ToolRegistry


@dataclass(frozen=True)
class ResolvedDepthTier:
    """The most restrictive configured profile for one type at one depth."""

    allowed_tools: frozenset[str] | None = None
    allowed_models: frozenset[str] | None = None


def resolve_depth_tier(
    settings: SubagentSettings,
    depth: int,
    agent_type: str,
) -> ResolvedDepthTier:
    """Resolve the nearest configured type profile, inheriting missing fields."""
    allowed_tools: frozenset[str] | None = None
    allowed_models: frozenset[str] | None = None
    for configured_depth in sorted(key for key in settings.depth_tiers if key <= depth):
        tier = settings.depth_tiers[configured_depth].get(agent_type)
        if tier is None:
            continue
        if tier.allowed_tools is not None:
            allowed_tools = frozenset(tier.allowed_tools)
        if tier.allowed_models is not None:
            allowed_models = frozenset(tier.allowed_models)
    return ResolvedDepthTier(allowed_tools, allowed_models)


def expand_tool_allowlist(
    declared: frozenset[str] | None, registry: ToolRegistry | None
) -> frozenset[str] | None:
    """Expand group patterns against the current tool registry."""
    if declared is None or registry is None:
        return declared
    return frozenset(
        name
        for pattern in declared
        for name in (registry.glob_expand(pattern) if pattern.endswith(".*") else (pattern,))
    )


def validate_depth_tiers(
    settings: SubagentSettings,
    registry: SubagentTypeRegistry,
    allowed_models: frozenset[str] | None,
    tool_registry: ToolRegistry | None = None,
) -> None:
    """Reject depth profiles that widen a type or its inherited model ceiling."""
    configured_types = {type_name for tiers in settings.depth_tiers.values() for type_name in tiers}
    for type_name in sorted(configured_types):
        spec = registry.get(type_name)
        if spec is None:
            raise ValueError(f"subagents.depth_tiers references unknown type {type_name!r}")
        type_models: set[str] | None
        if spec.model and spec.model.casefold() != "inherit":
            type_models = {spec.model}
        elif allowed_models is not None:
            type_models = set(allowed_models)
        else:
            type_models = None
        declared_tools = expand_tool_allowlist(spec.allowed_tools, tool_registry)
        prior_tools = set(declared_tools) if declared_tools is not None else None
        prior_models = type_models
        for depth in sorted(settings.depth_tiers):
            tier = settings.depth_tiers[depth].get(type_name)
            if tier is None:
                continue
            if tier.allowed_tools is not None:
                requested_tools = set(tier.allowed_tools)
                if prior_tools is not None and not requested_tools <= prior_tools:
                    extra = ", ".join(sorted(requested_tools - prior_tools))
                    raise ValueError(
                        f"subagents.depth_tiers.{depth}.{type_name}.allowed_tools grants "
                        f"tools outside the type or inherited profile: {extra}"
                    )
                prior_tools = requested_tools
            if tier.allowed_models is not None:
                requested_models = set(tier.allowed_models)
                if prior_models is None:
                    raise ValueError(
                        f"subagents.depth_tiers.{depth}.{type_name}.allowed_models cannot be "
                        "validated because the type has no declared model ceiling; configure "
                        "subagents.allowed_models or a fixed type model"
                    )
                if not requested_models <= prior_models:
                    extra_models = ", ".join(sorted(requested_models - prior_models))
                    raise ValueError(
                        f"subagents.depth_tiers.{depth}.{type_name}.allowed_models grants "
                        f"models outside the type or inherited profile: {extra_models}"
                    )
                prior_models = requested_models
