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
`lauren_ai._tools`.

It **should** also declare a `DEPENDENCIES` list of PEP-508 requirement
strings for any third-party packages it needs.  agenthicc reads `DEPENDENCIES`
**before** importing the file, checks which packages are missing, and either
installs them automatically or prints a clear fix hint — keeping
`ImportError` out of the load pipeline entirely.

```python
# .agenthicc/tools/weather_tools.py

from __future__ import annotations
from lauren_ai._tools import tool

# Declare third-party deps so agenthicc can check/install them before loading.
DEPENDENCIES = ["httpx>=0.27"]


@tool()
async def get_current_weather(city: str, units: str = "metric") -> dict:
    """Get the current weather for a city.

    Args:
        city: City name (e.g. "London").
        units: Unit system — "metric" or "imperial".
    """
    import httpx  # deferred import: only runs after deps are confirmed present
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

**Conventions:**
- `DEPENDENCIES` is optional but strongly recommended for any plugin that uses
  third-party packages.
- Third-party imports inside tool functions (deferred `import`) are
  preferred over top-level imports; they are skipped until the tool is called,
  which avoids `ImportError` at load time even without `DEPENDENCIES`.
- Files that do not export `TOOLS` are silently skipped.
- A sidecar `<stem>.requirements.txt` next to the `.py` file is treated as an
  implicit `DEPENDENCIES` list (one requirement per line) when `DEPENDENCIES`
  is absent.

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
    missing_deps: list[str] = field(default_factory=list)  # unprovided requirements

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing_deps


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


## Dependency Checking

Three signals are tried in order to determine a plugin's requirements:

1. **`DEPENDENCIES` list** in the plugin file (preferred — explicit, version-aware).
2. **Sidecar `<stem>.requirements.txt`** in the same directory (for multi-file
   plugins that keep deps in a separate file).
3. **AST import scan** (last resort, best-effort — only catches top-level
   `import` / `from … import` statements; misses dynamic imports).

```python
import ast
import importlib.metadata
import importlib.util
import re
import subprocess
import sys


def _requirements_from_sidecar(path: Path) -> list[str]:
    """Read <stem>.requirements.txt next to *path* if it exists."""
    req_file = path.with_suffix("").with_suffix(".requirements.txt")
    # e.g. weather_tools.requirements.txt
    req_file = path.parent / (path.stem + ".requirements.txt")
    if req_file.exists():
        return [
            line.strip()
            for line in req_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    return []


def _ast_scan_imports(path: Path) -> list[str]:
    """Return top-level imported package names from *path* via AST."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module.split(".")[0])
    return list(dict.fromkeys(names))  # deduplicated, order-preserving


def _check_missing(requirements: list[str]) -> list[str]:
    """Return the subset of *requirements* that are not currently satisfied."""
    missing = []
    for req in requirements:
        try:
            importlib.metadata.requires(req)  # raises if not installed/wrong version
            pkg = re.split(r"[>=<!~\[]", req)[0].strip()
            importlib.metadata.version(pkg)   # raises PackageNotFoundError if absent
        except Exception:
            missing.append(req)
    return missing


def _infer_missing_from_ast(path: Path) -> list[str]:
    """AST-scan fallback: return import roots not found on sys.path."""
    names = _ast_scan_imports(path)
    stdlib = set(sys.stdlib_module_names)
    missing = []
    for name in names:
        if name in stdlib:
            continue
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    return missing


def _install_deps(requirements: list[str], target: str = "user") -> None:
    """Install *requirements* via pip into the current environment."""
    flags = ["--user"] if target == "user" else []
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *flags, *requirements]
    )
```

### Dependency resolution in `_load_plugin_file()`

```python
def _load_plugin_file(path: Path, auto_install: bool = False) -> LoadResult:
    """Import a single plugin file; check/install deps first, then extract TOOLS."""

    # ── Step 1: read DEPENDENCIES without full exec ───────────────────────
    # Probe-import the file to read DEPENDENCIES cheaply; if it fails at this
    # stage the real exec below will also fail and capture the error properly.
    declared_deps: list[str] = []
    try:
        probe_spec = importlib.util.spec_from_file_location("_dep_probe", path)
        if probe_spec and probe_spec.loader:
            probe_mod = importlib.util.module_from_spec(probe_spec)
            probe_spec.loader.exec_module(probe_mod)  # type: ignore[union-attr]
            declared_deps = list(getattr(probe_mod, "DEPENDENCIES", []))
    except Exception:
        pass  # will be caught properly in Step 3

    if not declared_deps:
        declared_deps = _requirements_from_sidecar(path)

    # ── Step 2: check / install missing deps ─────────────────────────────
    missing = _check_missing(declared_deps)
    if missing:
        if auto_install:
            log.info("Auto-installing missing deps for %s: %s", path, missing)
            try:
                _install_deps(missing)
                missing = _check_missing(missing)   # verify install succeeded
            except Exception as exc:
                log.error("Auto-install failed for %s: %s", path, exc)
        if missing:
            # Surface clear hint; do NOT attempt to exec the file.
            return LoadResult(path=path, missing_deps=missing)

    # ── Step 3: full import ───────────────────────────────────────────────
    module_name = f"_agenthicc_plugin_{path.stem}_{abs(hash(str(path)))}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return LoadResult(path=path, error="could not create module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except ImportError as exc:
        # No DEPENDENCIES declared; fall back to AST scan for a better hint.
        inferred = _infer_missing_from_ast(path)
        if inferred:
            return LoadResult(path=path, missing_deps=inferred)
        return LoadResult(path=path, error=f"ImportError: {exc}")
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


def _scan_directory(
    root: Path,
    auto_install: bool = False,
) -> list[LoadResult]:
    """Recursively load all *.py files under *root*."""
    if not root.is_dir():
        return []
    results: list[LoadResult] = []
    for py_file in sorted(root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue   # skip __init__.py, private helpers
        result = _load_plugin_file(py_file, auto_install=auto_install)
        if result.missing_deps:
            deps_str = " ".join(result.missing_deps)
            log.warning(
                "Plugin %s skipped — missing dependencies: %s\n"
                "  Fix: pip install %s\n"
                "  Or set [plugins] auto_install = true in agenthicc.toml",
                py_file, result.missing_deps, deps_str,
            )
        elif result.error:
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


def test_load_plugin_missing_dep_no_auto_install(tmp_path):
    f = tmp_path / "needs_dep.py"
    f.write_text(
        "DEPENDENCIES = ['this-package-does-not-exist-xyz']\n"
        "from lauren_ai._tools import tool\n"
        "@tool()\nasync def t() -> None: pass\nTOOLS = [t]\n"
    )
    result = _load_plugin_file(f, auto_install=False)
    assert not result.ok
    assert result.missing_deps  # surfaced, not a generic error


def test_load_plugin_importerror_triggers_ast_scan(tmp_path):
    f = tmp_path / "undeclared.py"
    f.write_text(
        "import this_package_does_not_exist_xyz\n"
        "from lauren_ai._tools import tool\n"
        "@tool()\nasync def t() -> None: pass\nTOOLS = [t]\n"
    )
    result = _load_plugin_file(f, auto_install=False)
    assert not result.ok
    # AST scan should have caught "this_package_does_not_exist_xyz"
    assert "this_package_does_not_exist_xyz" in result.missing_deps


def test_sidecar_requirements_txt(tmp_path):
    req = tmp_path / "my_tools.requirements.txt"
    req.write_text("this-package-does-not-exist-xyz\n")
    f = tmp_path / "my_tools.py"
    f.write_text("TOOLS = []\n")
    result = _load_plugin_file(f, auto_install=False)
    assert not result.ok
    assert result.missing_deps
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
