---
title: "PRD-24: Tool Plugin Discovery — Filesystem Conventions and Dynamic Loading"
status: draft
version: 0.1.0
created: 2026-06-12
---

# PRD-24: Tool Plugin Discovery

## Executive Summary

Clients of agenthicc need to extend the default tool set without modifying the
library source.  This PRD defines the **filesystem conventions** for placing
custom Python tool files in a project's `.agenthicc/` directory and the
**dynamic import pipeline** that loads them safely at session startup.

Two placement conventions:

| Path pattern | Scope |
|---|---|
| `.agenthicc/tools/<name>.py` | Available to **every** agent in this session |
| `.agenthicc/agents/<agent-name>/tools/<name>.py` | Available only to the named agent |

Personal (user-global) equivalents mirror these paths under `~/.agenthicc/`.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `.agenthicc/tools/*.py` files are auto-discovered and loaded at session startup |
| G2 | `~/.agenthicc/tools/*.py` provides user-global tools loaded before project tools |
| G3 | Each `.py` file must expose `TOOLS: list` of `@tool()`-decorated callables |
| G4 | Files that fail to load (syntax error, import error) are logged and skipped — session continues |
| G5 | Discovery is recursive inside `tools/` subdirectories (e.g. `tools/weather/api.py`) |
| G6 | Loaded tool names must not conflict with built-in tools; conflicts log a warning and the project tool wins |
| G7 | The loader returns a `PluginToolSet` with a `.all_tools` property for use at call sites |

## Non-Goals
- Watching for file changes and hot-reloading mid-session (v2)
- Sandboxed execution of plugin code (plugins run with full user permissions)
- Remote / URL-based plugins

---

## Filesystem Layout

```
~/.agenthicc/
└── tools/
    ├── personal_utils.py        # user-global project-independent tools
    └── ai_helpers/
        └── summarise.py

.agenthicc/
├── tools/
│   ├── weather_tools.py         # project-wide custom tools
│   ├── database_tools.py
│   └── internal/
│       └── crm_api.py
└── agents/
    ├── researcher/
    │   └── tools/
    │       ├── web_scraper.py   # only the "researcher" agent sees these
    │       └── arxiv_search.py
    └── writer/
        └── tools/
            └── style_checker.py
```

---

## Tool File Contract

Every plugin Python file **must** expose a module-level list named `TOOLS`
containing zero or more callables decorated with `@tool()` from
`lauren_ai._tools`:

```python
# .agenthicc/tools/weather_tools.py

from __future__ import annotations
import httpx
from lauren_ai._tools import tool


@tool()
async def get_current_weather(city: str, units: str = "metric") -> dict:
    """Get the current weather for a city.

    Args:
        city: City name (e.g. "London").
        units: Unit system — "metric" or "imperial".
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"q": city, "appid": "demo"},
            timeout=10,
        )
    return resp.json()


@tool()
async def list_supported_cities() -> list[str]:
    """List cities that the weather API covers."""
    return ["London", "Paris", "New York", "Tokyo"]


# Required export
TOOLS = [get_current_weather, list_supported_cities]
```

Files that do not export `TOOLS` are silently skipped.

---

## Data Structures

```python
# src/agenthicc/plugins/discovery.py

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Every discovered and successfully loaded tool callable.
PluginTool = Callable[..., Any]


@dataclass
class LoadResult:
    """Outcome of loading a single plugin file."""
    path: Path
    tools: list[PluginTool] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class PluginToolSet:
    """Aggregated result of scanning a tools directory tree."""
    results: list[LoadResult] = field(default_factory=list)

    @property
    def all_tools(self) -> list[PluginTool]:
        """Flat list of every successfully loaded tool callable."""
        out: list[PluginTool] = []
        for r in self.results:
            out.extend(r.tools)
        return out

    @property
    def failed(self) -> list[LoadResult]:
        return [r for r in self.results if not r.ok]
```

---

## Loading Algorithm

```python
# src/agenthicc/plugins/discovery.py  (continued)


def _load_plugin_file(path: Path) -> LoadResult:
    """Import a single plugin file; extract its TOOLS list."""
    module_name = f"_agenthicc_plugin_{path.stem}_{abs(hash(str(path)))}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return LoadResult(path=path, error="could not create module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        return LoadResult(path=path, error=f"{type(exc).__name__}: {exc}")

    tools = getattr(module, "TOOLS", None)
    if tools is None:
        return LoadResult(path=path, tools=[])   # no TOOLS export — skip silently
    if not isinstance(tools, (list, tuple)):
        return LoadResult(path=path, error="TOOLS must be a list of callables")

    valid: list[PluginTool] = []
    for t in tools:
        if callable(t):
            valid.append(t)
        else:
            log.warning("Plugin %s: non-callable item in TOOLS skipped: %r", path, t)

    return LoadResult(path=path, tools=valid)


def _scan_directory(root: Path) -> list[LoadResult]:
    """Recursively load all *.py files under *root*."""
    if not root.is_dir():
        return []
    results: list[LoadResult] = []
    for py_file in sorted(root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue   # skip __init__.py, private helpers
        result = _load_plugin_file(py_file)
        if result.error:
            log.error(
                "Tool plugin load failed: %s — %s (skipping)",
                py_file,
                result.error,
            )
        elif result.tools:
            log.debug("Loaded %d tool(s) from %s", len(result.tools), py_file)
        results.append(result)
    return results


def discover_project_tools(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> PluginToolSet:
    """Discover all project-wide tools (both user-global and project-local).

    User-global tools are loaded first; project tools are appended and may
    shadow user-global tools with the same name.
    """
    user_root    = (user_dir    or Path.home() / ".agenthicc") / "tools"
    project_root = (project_dir or Path(".agenthicc"))         / "tools"

    results: list[LoadResult] = []
    results.extend(_scan_directory(user_root))
    results.extend(_scan_directory(project_root))
    return PluginToolSet(results=results)


def discover_agent_tools(
    agent_name: str,
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> PluginToolSet:
    """Discover tools scoped to a specific named agent.

    Loads from:
      ~/.agenthicc/agents/<agent_name>/tools/
      .agenthicc/agents/<agent_name>/tools/
    """
    user_root    = (user_dir    or Path.home() / ".agenthicc") / "agents" / agent_name / "tools"
    project_root = (project_dir or Path(".agenthicc"))         / "agents" / agent_name / "tools"

    results: list[LoadResult] = []
    results.extend(_scan_directory(user_root))
    results.extend(_scan_directory(project_root))
    return PluginToolSet(results=results)
```

---

## Conflict Detection

```python
# src/agenthicc/plugins/discovery.py  (continued)

from agenthicc.agent_tools import AGENT_TOOLS as _BUILTIN


def _builtin_names() -> frozenset[str]:
    return frozenset(getattr(t, "__name__", "") for t in _BUILTIN)


def warn_conflicts(plugin_set: PluginToolSet) -> None:
    builtins = _builtin_names()
    for tool in plugin_set.all_tools:
        name = getattr(tool, "__name__", "")
        if name in builtins:
            log.warning(
                "Plugin tool %r shadows a built-in tool with the same name. "
                "The plugin version will be used.",
                name,
            )
```

---

## Session Startup Integration

In `_run_tui_session()` in `__main__.py`:

```python
from agenthicc.plugins.discovery import (
    discover_project_tools,
    warn_conflicts,
    PluginToolSet,
)

_project_plugins: PluginToolSet = discover_project_tools()
warn_conflicts(_project_plugins)

# Attach to renderer so _run_agent_turn() can read it
renderer._project_plugin_tools = _project_plugins.all_tools
```

---

## Tests

```python
# tests/unit/test_plugin_discovery.py

import pytest
from pathlib import Path
from agenthicc.plugins.discovery import _load_plugin_file, _scan_directory

pytestmark = pytest.mark.unit


def test_load_valid_plugin(tmp_path):
    f = tmp_path / "my_tools.py"
    f.write_text(
        "from lauren_ai._tools import tool\n"
        "@tool()\nasync def ping() -> str:\n    return 'pong'\n"
        "TOOLS = [ping]\n"
    )
    result = _load_plugin_file(f)
    assert result.ok
    assert len(result.tools) == 1
    assert result.tools[0].__name__ == "ping"


def test_load_plugin_syntax_error(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def bad syntax!!!")
    result = _load_plugin_file(f)
    assert not result.ok
    assert "SyntaxError" in result.error


def test_load_plugin_no_tools_export(tmp_path):
    f = tmp_path / "no_export.py"
    f.write_text("x = 42\n")
    result = _load_plugin_file(f)
    assert result.ok
    assert result.tools == []


def test_scan_directory_skips_private(tmp_path):
    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "_helper.py").write_text("TOOLS = []\n")
    (tmp_path / "tools.py").write_text(
        "from lauren_ai._tools import tool\n"
        "@tool()\nasync def t() -> None: pass\nTOOLS = [t]\n"
    )
    results = _scan_directory(tmp_path)
    loaded_names = [r.path.name for r in results if r.tools]
    assert "tools.py" in loaded_names
    assert "__init__.py" not in loaded_names
    assert "_helper.py" not in loaded_names


def test_scan_missing_directory_returns_empty(tmp_path):
    results = _scan_directory(tmp_path / "nonexistent")
    assert results == []
```

---

## Verification

```bash
# Create a minimal plugin
mkdir -p .agenthicc/tools
cat > .agenthicc/tools/hello_tools.py << 'EOF'
from lauren_ai._tools import tool

@tool()
async def say_hello(name: str = "World") -> str:
    """Say hello to someone."""
    return f"Hello, {name}!"

TOOLS = [say_hello]
EOF

uv run agenthicc
# Session should log: "Loaded 1 tool(s) from .agenthicc/tools/hello_tools.py"
# Ask the agent: "use the say_hello tool" → agent calls say_hello("World")
```
