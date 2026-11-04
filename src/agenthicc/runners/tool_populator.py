"""populate_agent_tools — fill an ``@agent()`` class's tool map (PRD-95, TOOLR-04).

``_build_runner_for_agent`` was imported from lauren-ai's testing module solely
for its side effect of populating ``meta.tools`` from ``meta.tool_classes``.
This module exposes that logic without touching testing infrastructure.

A tool that cannot be registered is never dropped silently: every failure is
logged and returned, and a name registered twice names both sources.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from types import FunctionType

__all__ = ["ToolRegistrationFailure", "populate_agent_tools"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolRegistrationFailure:
    """One tool that is missing from the agent, and why."""

    tool: str
    source: str
    error: str

    def describe(self) -> str:
        return f"{self.tool} ({self.source}): {self.error}"


def _source(tool_ref: object) -> str:
    """Where *tool_ref* is defined: its function or class, else its instance's class."""
    target = tool_ref if isinstance(tool_ref, (type, FunctionType)) else type(tool_ref)
    return f"{target.__module__}.{target.__qualname__}"


def populate_agent_tools(
    agent_instance: object, tools: Sequence[object]
) -> tuple[ToolRegistrationFailure, ...]:
    """Populate ``meta.tools`` on *agent_instance* from *tools*.

    *tools* is the list of tool callables or classes previously passed to
    ``@use_tools()``. Returns the tools that could not be registered: an
    object without ``@tool()`` metadata, a name already taken by another
    source, or a registration lauren-ai rejected. Each is also logged.
    """
    from lauren_ai._agents import AGENT_META
    from lauren_ai._tools import TOOL_META, _add_to_tool_map

    meta = getattr(type(agent_instance), AGENT_META, None)
    if meta is None:
        return ()

    tool_map: dict[str, object] = {}
    sources: dict[str, str] = {}
    failures: list[ToolRegistrationFailure] = []
    for tool_ref in tools:
        if tool_ref is None:
            continue
        source = _source(tool_ref)
        tool_meta = getattr(tool_ref, TOOL_META, None)
        if tool_meta is None:
            failures.append(ToolRegistrationFailure(repr(tool_ref), source, "no @tool() metadata"))
            continue
        name = str(tool_meta.name)
        if name in sources:
            if sources[name] != source:
                failures.append(
                    ToolRegistrationFailure(
                        name, source, f"name already registered by {sources[name]}"
                    )
                )
            continue
        try:
            _add_to_tool_map(tool_map, tool_ref)
        except Exception as exc:  # noqa: BLE001 - reported, never dropped
            failures.append(ToolRegistrationFailure(name, source, f"{type(exc).__name__}: {exc}"))
            continue
        sources[name] = source
    meta.tools = tool_map
    for failure in failures:
        logger.warning("tool not registered: %s", failure.describe())
    return tuple(failures)
