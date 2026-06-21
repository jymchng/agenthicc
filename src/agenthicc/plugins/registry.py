"""Tool plugin registry — merges built-in and plugin tools (PRD-25)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PluginTool = Callable[..., object]

__all__ = ["ToolRegistry", "build_registry"]


@dataclass
class ToolRegistry:
    """Ordered, deduplicated registry of tool callables for one agent turn.

    Build order:
      1. Built-in tools (always present, lowest precedence for dedup)
      2. User-global project tools
      3. Project-local project tools
      4. Agent-specific tools (highest precedence — may shadow all above)

    Deduplication is by tool function ``__name__``.  Later entries win.
    """

    _by_name: dict[str, PluginTool] = field(default_factory=dict)

    # ── mutation ──────────────────────────────────────────────────────────

    def register(self, tool: PluginTool, *, source: str = "unknown") -> None:
        """Add (or replace) a tool by name."""
        name = getattr(tool, "__name__", repr(tool))
        if name in self._by_name:
            log.debug("Tool %r overridden by %s", name, source)
        self._by_name[name] = tool

    def register_many(self, tools: list[PluginTool], *, source: str = "unknown") -> None:
        for t in tools:
            self.register(t, source=source)

    # ── read ──────────────────────────────────────────────────────────────

    @property
    def tools(self) -> list[PluginTool]:
        """Ordered list (insertion order preserved, last-writer-wins per name)."""
        return list(self._by_name.values())

    @property
    def names(self) -> list[str]:
        return list(self._by_name.keys())

    def describe(self) -> str:
        """Markdown summary for the agent system prompt."""
        if not self._by_name:
            return ""
        lines = ["### Available Tools\n"]
        for name, tool in self._by_name.items():
            doc = (tool.__doc__ or "").strip().splitlines()[0] if tool.__doc__ else ""
            lines.append(f"- **{name}**: {doc}")
        return "\n".join(lines)

    def summary_log(self) -> dict[str, object]:
        """Serialisable tool count summary for session log."""
        return {"total_tools": len(self._by_name), "names": self.names}


def build_registry(
    agent_name: str | None = None,
    project_plugin_tools: list[PluginTool] | None = None,
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> ToolRegistry:
    """Construct a fully merged ToolRegistry for one agent turn.

    Args:
        agent_name: If provided, agent-specific tools are loaded and appended.
        project_plugin_tools: Pre-discovered project-wide plugins (from session
            startup cache); if None they are discovered on the fly.
        project_dir: Override for the project's .agenthicc/ root.
        user_dir: Override for the user's ~/.agenthicc/ root.
    """
    registry = ToolRegistry()

    # 1. Built-ins (always first)
    from agenthicc.agent_tools import AGENT_TOOLS  # noqa: PLC0415

    registry.register_many(AGENT_TOOLS, source="builtin")

    # 2. Project-wide plugins (cached at session start via renderer._project_plugin_tools)
    if project_plugin_tools is None:
        from agenthicc.plugins.discovery import discover_project_tools  # noqa: PLC0415

        project_plugin_tools = discover_project_tools(
            project_dir=project_dir,
            user_dir=user_dir,
        ).all_tools
    registry.register_many(project_plugin_tools, source="project-plugin")

    # 3. Agent-specific plugins (highest precedence, loaded per-turn)
    if agent_name:
        from agenthicc.plugins.discovery import discover_agent_tools  # noqa: PLC0415

        agent_set = discover_agent_tools(
            agent_name=agent_name,
            project_dir=project_dir,
            user_dir=user_dir,
        )
        registry.register_many(agent_set.all_tools, source=f"agent:{agent_name}")

    return registry
