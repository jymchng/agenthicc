"""Tool plugin registry — merges built-in and plugin tools (PRD-25, PRD-125)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PluginTool = Callable[..., object]

__all__ = ["ToolGroup", "ToolRegistry", "build_registry"]


@dataclass
class ToolGroup:
    """Named collection of tools belonging to one domain (PRD-125).

    Groups drive the structured system-prompt section produced by
    ``ToolRegistry.describe()`` and enable glob patterns in subagent
    ``allowed_tools`` frozensets (e.g. ``"fs.*"`` expands to every tool
    whose ``__name__`` is registered under the ``"fs"`` group).

    Attributes
    ----------
    name:
        Machine key used in glob patterns and collision warnings.
        Convention: lowercase with no spaces (``"fs"``, ``"git"``,
        ``"mcp:github"``).
    label:
        Human-readable display name shown in the system-prompt header.
    description:
        One-line summary rendered as an italic sub-heading.
    tools:
        The callables belonging to this group.
    priority:
        Display order in ``describe()``; higher value → rendered first.
    """

    name:        str
    label:       str
    description: str
    tools:       list[PluginTool]
    priority:    int = 0


@dataclass
class ToolRegistry:
    """Ordered, deduplicated registry of tool callables for one agent turn.

    Build order:
      1. Built-in tools (always present, lowest precedence for dedup)
      2. User-global project tools
      3. Project-local project tools
      4. Agent-specific tools (highest precedence — may shadow all above)

    Deduplication is by tool function ``__name__``.  Later entries win.
    Cross-group shadowing (a plugin tool replacing a built-in from a different
    domain) is logged at WARNING level (PRD-125).
    """

    _by_name:     dict[str, PluginTool] = field(default_factory=dict)
    _tool_groups: dict[str, str]        = field(default_factory=dict)  # name → group.name
    _groups:      list[ToolGroup]       = field(default_factory=list)

    # ── mutation ──────────────────────────────────────────────────────────

    def register(
        self,
        tool: PluginTool,
        *,
        source: str = "unknown",
        group: str = "",
    ) -> None:
        """Add (or replace) a tool by name.

        Emits WARNING when a tool from *group* shadows an existing tool that
        belongs to a *different* group (cross-domain shadowing).  Same-group
        override stays at DEBUG.
        """
        name = getattr(tool, "__name__", repr(tool))
        if name in self._by_name:
            existing_group = self._tool_groups.get(name, "")
            if existing_group and group and existing_group != group:
                log.warning(
                    "Tool %r from group %r shadows built-in from group %r "
                    "(source: %s).  This may be intentional but could indicate "
                    "a name collision between a plugin and a core tool.",
                    name, group, existing_group, source,
                )
            else:
                log.debug("Tool %r overridden by %s", name, source)
        self._by_name[name] = tool
        if group:
            self._tool_groups[name] = group

    def register_many(
        self,
        tools: list[PluginTool],
        *,
        source: str = "unknown",
        group: str = "",
    ) -> None:
        for t in tools:
            self.register(t, source=source, group=group)

    def register_group(self, group: ToolGroup, *, source: str = "builtin") -> None:
        """Register all tools in *group* and track their group membership.

        Idempotent with respect to the group list — registering the same
        ``ToolGroup`` object twice is prevented by identity check.
        """
        if any(g is group for g in self._groups):
            return
        self._groups.append(group)
        for tool in group.tools:
            self.register(tool, source=source, group=group.name)

    # ── read ──────────────────────────────────────────────────────────────

    @property
    def tools(self) -> list[PluginTool]:
        """Ordered list (insertion order preserved, last-writer-wins per name)."""
        return list(self._by_name.values())

    @property
    def names(self) -> list[str]:
        return list(self._by_name.keys())

    def glob_expand(self, pattern: str) -> frozenset[str]:
        """Expand a glob pattern to a frozenset of matching tool names.

        ``"fs.*"``  → all tool names registered under group ``"fs"``.
        ``"git.*"`` → all tool names registered under group ``"git"``.
        Literal names (no ``.*`` suffix) pass through unchanged if they exist
        in the registry; an unknown literal returns an empty frozenset.

        Examples
        --------
        >>> registry.glob_expand("fs.*")
        frozenset({"read_file", "write_file", ...})
        >>> registry.glob_expand("git_status")
        frozenset({"git_status"})
        >>> registry.glob_expand("nonexistent")
        frozenset()
        """
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            return frozenset(
                name for name, grp in self._tool_groups.items() if grp == prefix
            )
        if pattern in self._by_name:
            return frozenset({pattern})
        return frozenset()

    def describe(self) -> str:
        """Grouped Markdown summary for the agent system prompt (PRD-125).

        Produces labelled sections for each registered ``ToolGroup`` (sorted
        by descending priority), then an "Additional Tools" section for any
        tools not belonging to a group.  Returns an empty string when the
        registry is empty.
        """
        if not self._by_name:
            return ""

        lines: list[str] = []
        seen: set[str] = set()

        for grp in sorted(self._groups, key=lambda g: -g.priority):
            group_names = [
                getattr(t, "__name__", "")
                for t in grp.tools
                if getattr(t, "__name__", "") in self._by_name
            ]
            if not group_names:
                continue
            lines.append(f"### {grp.label} ({len(group_names)} tools)")
            lines.append(f"_{grp.description}_")
            for name in group_names:
                tool = self._by_name[name]
                doc = (tool.__doc__ or "").strip().splitlines()[0] if tool.__doc__ else ""
                lines.append(f"- **{name}**: {doc}")
            lines.append("")
            seen.update(group_names)

        ungrouped = [n for n in self._by_name if n not in seen]
        if ungrouped:
            lines.append("### Additional Tools")
            for name in ungrouped:
                tool = self._by_name[name]
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

    Built-in tools are registered via their ``ToolGroup`` so that
    ``describe()`` can group them into structured sections and
    ``glob_expand()`` can resolve patterns like ``"fs.*"``.

    Args:
        agent_name: If provided, agent-specific tools are loaded and appended.
        project_plugin_tools: Pre-discovered project-wide plugins (from session
            startup cache); if None they are discovered on the fly.
        project_dir: Override for the project's .agenthicc/ root.
        user_dir: Override for the user's ~/.agenthicc/ root.
    """
    registry = ToolRegistry()

    # 1. Built-ins — registered via ToolGroup so describe() can group them.
    from agenthicc.agent_tools import BUILTIN_GROUPS  # noqa: PLC0415

    for grp in BUILTIN_GROUPS:
        registry.register_group(grp, source="builtin")

    # 2. Project-wide plugins (cached at session start via session context)
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
