---
title: "PRD-25: Tool Plugin Registration — Wiring Plugins into the Agent Runtime"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-24-tool-plugin-discovery.md
---

# PRD-25: Tool Plugin Registration

## Executive Summary

Once plugins are discovered (PRD-24), they must be wired into the agent
runtime so the LLM can see and call them.  The existing agent construction
in `_run_agent_turn()` uses:

```python
@use_tools(*AGENT_TOOLS)
class _AgenthiccAgent: ...
```

This PRD extends that pattern: plugin tools are appended to `AGENT_TOOLS`
at runtime, the merged list is passed to `@use_tools`, and the agent system
prompt is updated to describe the new capabilities.  A `ToolRegistry` class
provides a consistent API for building, caching, and inspecting the merged
tool list.

---

## Goals

| ID | Goal |
|----|------|
| G1 | Project-wide plugin tools are merged with built-in tools before agent construction |
| G2 | Agent-specific plugin tools are merged last and can shadow project-wide tools with the same name |
| G3 | The merged tool list is built once per agent turn (not per LLM call) |
| G4 | A `ToolRegistry.describe()` method returns a markdown summary of all registered tools for the system prompt |
| G5 | Plugin tools appear in `/status` and tool-call spinner output exactly like built-in tools |
| G6 | If a plugin tool raises at call time, the error is caught and returned as a normal tool error envelope — session continues |
| G7 | `ToolRegistry` is serialisable enough to log which tools are active for a given session |

## Non-Goals
- Tool hot-swapping mid-turn
- Tool versioning / dependency resolution
- Auto-generating tool schemas from type hints (must use `@tool()` decorator)

---

## ToolRegistry

```python
# src/agenthicc/plugins/registry.py

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

PluginTool = Callable[..., Any]


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

    _tools: list[PluginTool] = field(default_factory=list)
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

    def summary_log(self) -> dict[str, int]:
        """Serialisable tool count summary for session log."""
        return {"total_tools": len(self._by_name), "names": self.names}
```

---

## Building the Registry per Agent Turn

```python
# src/agenthicc/plugins/registry.py  (continued)

from pathlib import Path
from agenthicc.agent_tools import AGENT_TOOLS as _BUILTIN_TOOLS
from agenthicc.plugins.discovery import (
    discover_project_tools,
    discover_agent_tools,
    PluginToolSet,
)


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
    registry.register_many(_BUILTIN_TOOLS, source="builtin")

    # 2. Project-wide plugins (cached at session start via renderer._project_plugin_tools)
    project_tools = project_plugin_tools or discover_project_tools(
        project_dir=project_dir,
        user_dir=user_dir,
    ).all_tools
    registry.register_many(project_tools, source="project-plugin")

    # 3. Agent-specific plugins (highest precedence, loaded per-turn)
    if agent_name:
        agent_set: PluginToolSet = discover_agent_tools(
            agent_name=agent_name,
            project_dir=project_dir,
            user_dir=user_dir,
        )
        registry.register_many(agent_set.all_tools, source=f"agent:{agent_name}")

    return registry
```

---

## Integration into `_run_agent_turn()`

The relevant section of `src/agenthicc/__main__.py` currently:

```python
from agenthicc.agent_tools import AGENT_TOOLS  # noqa: PLC0415

# ...

@agent_decorator(model=model_id, system=SYSTEM_PROMPT)
@use_tools(*AGENT_TOOLS)
class _AgenthiccAgent: ...
```

Replace with:

```python
from agenthicc.plugins.registry import build_registry  # noqa: PLC0415

# agent_name: slugified form of model_short for now ("default" if unknown)
_agent_name = getattr(renderer, "_agent_name", None) or "default"

_registry = build_registry(
    agent_name=_agent_name,
    project_plugin_tools=getattr(renderer, "_project_plugin_tools", None),
)

_tool_description_suffix = _registry.describe()

@agent_decorator(
    model=model_id,
    system=(
        "You are a capable AI assistant with access to filesystem, shell, "
        "and git tools. Use them directly to complete tasks. "
        "Give concise responses. Show command output when relevant. "
        "Never invent file contents — always read them first."
        + (f"\n\n{_tool_description_suffix}" if _tool_description_suffix else "")
    ),
)
@use_tools(*_registry.tools)
class _AgenthiccAgent: ...
```

---

## Tool-Call Error Wrapping

Plugin tools are called via lauren-ai's `ToolExecutor._dispatch()`.  Any
exception propagates into `AgenthiccToolExecutor.execute()` which already
wraps it into a `ToolResultEnvelope(ok=False, error=...)`.  No additional
error handling is required — plugin failures look identical to built-in tool
failures in the transcript.

---

## System Prompt Description

When plugin tools are present, `_registry.describe()` produces:

```markdown
### Available Tools

- **get_current_weather**: Get the current weather for a city.
- **list_supported_cities**: List cities that the weather API covers.
- **say_hello**: Say hello to someone.
```

This is appended to the base system prompt so the LLM knows the extra
capabilities exist without needing to discover them through tool schemas alone.

---

## `/status` and Spinner Integration

The tool-call spinner (`_live_calls` in `_run_agent_turn`) displays any called
tool by name, including plugin tools.  No changes needed — plugins use the same
`ToolCallStarted` / `ToolCallComplete` signal path as built-ins.

`SlashCommandHandler._status()` shows the active turn's tool calls from
`model.turns[-1].tool_calls`.  Plugin tool calls appear identically.

---

## Session Log Entry

At session startup, log the registry summary:

```python
import logging
log = logging.getLogger(__name__)

# After building the registry:
log.info("Tool registry: %s", _registry.summary_log())
# → {"total_tools": 14, "names": ["list_files", "read_file", ..., "get_current_weather"]}
```

---

## Tests

```python
# tests/unit/test_plugin_registry.py

import pytest
from unittest.mock import AsyncMock
from agenthicc.plugins.registry import ToolRegistry, build_registry

pytestmark = pytest.mark.unit


def _make_tool(name: str):
    async def fn(): pass
    fn.__name__ = name
    fn.__doc__ = f"Does {name} things."
    return fn


def test_register_dedup_last_wins():
    reg = ToolRegistry()
    t1 = _make_tool("ping")
    t2 = _make_tool("ping")
    reg.register(t1, source="builtin")
    reg.register(t2, source="plugin")
    assert reg.tools == [t2]


def test_register_many_preserves_order():
    reg = ToolRegistry()
    tools = [_make_tool(n) for n in ("a", "b", "c")]
    reg.register_many(tools, source="test")
    assert reg.names == ["a", "b", "c"]


def test_describe_produces_markdown():
    reg = ToolRegistry()
    reg.register(_make_tool("ping"), source="test")
    md = reg.describe()
    assert "**ping**" in md
    assert "Does ping things" in md


def test_describe_empty_registry():
    assert ToolRegistry().describe() == ""


def test_build_registry_includes_builtins():
    reg = build_registry(project_plugin_tools=[], agent_name=None)
    # Built-in tools must always be present
    assert "read_file" in reg.names
    assert "git_status" in reg.names


def test_build_registry_plugin_shadows_builtin():
    shadow = _make_tool("read_file")   # same name as built-in
    reg = build_registry(project_plugin_tools=[shadow], agent_name=None)
    # Plugin wins
    assert reg._by_name["read_file"] is shadow


def test_summary_log():
    reg = ToolRegistry()
    reg.register(_make_tool("foo"), source="test")
    summary = reg.summary_log()
    assert summary["total_tools"] == 1
    assert "foo" in summary["names"]
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_plugin_registry.py -v

# Manual: add a plugin, start session, ask agent to use it
mkdir -p .agenthicc/tools
cat > .agenthicc/tools/calc.py << 'EOF'
from lauren_ai._tools import tool

@tool()
async def add(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b

TOOLS = [add]
EOF

uv run agenthicc
# "what is 3.14 plus 2.71?" → agent calls add(3.14, 2.71) → 5.85
```
