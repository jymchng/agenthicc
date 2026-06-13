"""Tool plugin discovery and dynamic loading (PRD-24)."""
from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

PluginTool = Callable[..., Any]

__all__ = [
    "LoadResult", "PluginToolSet",
    "_load_plugin_file", "_scan_directory",
    "discover_project_tools", "discover_agent_tools",
    "warn_conflicts",
]


@dataclass
class LoadResult:
    """Outcome of loading a single plugin file."""

    path: Path
    tools: list[PluginTool] = field(default_factory=list)
    error: str | None = None
    missing_deps: list[str] = field(default_factory=list)

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
        return [t for r in self.results for t in r.tools]

    @property
    def failed(self) -> list[LoadResult]:
        return [r for r in self.results if not r.ok]


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _requirements_from_sidecar(path: Path) -> list[str]:
    """Read <stem>.requirements.txt next to *path* if it exists."""
    req_file = path.parent / (path.stem + ".requirements.txt")
    if req_file.exists():
        return [
            line.strip()
            for line in req_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    return []


def _check_missing(requirements: list[str]) -> list[str]:
    """Return the subset of *requirements* that are not currently satisfied."""
    missing: list[str] = []
    for req in requirements:
        try:
            pkg = re.split(r"[>=<!~\[]", req)[0].strip()
            importlib.metadata.version(pkg)
        except Exception:
            missing.append(req)
    return missing


def _infer_missing_from_ast(path: Path) -> list[str]:
    """AST-scan fallback: return import roots not found on sys.path."""
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
    # deduplicate, order-preserving
    seen: dict[str, None] = {}
    for n in names:
        seen[n] = None
    deduped = list(seen.keys())

    stdlib = set(sys.stdlib_module_names)
    missing: list[str] = []
    for name in deduped:
        if name in stdlib:
            continue
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    return missing


def _install_deps(requirements: list[str], target: str = "venv") -> None:
    """Install *requirements* via pip into the current environment."""
    flags = ["--user"] if target == "user" else []
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *flags, *requirements]
    )


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------


def _load_plugin_file(path: Path, auto_install: bool = False) -> LoadResult:
    """Import a single plugin file; check/install deps first, then extract TOOLS."""

    # ── Step 1: probe-import to read DEPENDENCIES ─────────────────────────
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
                missing = _check_missing(missing)  # verify install succeeded
            except Exception as exc:
                log.error("Auto-install failed for %s: %s", path, exc)
        if missing:
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
        inferred = _infer_missing_from_ast(path)
        if inferred:
            return LoadResult(path=path, missing_deps=inferred)
        return LoadResult(path=path, error=f"ImportError: {exc}")
    except Exception as exc:
        return LoadResult(path=path, error=f"{type(exc).__name__}: {exc}")

    tools = getattr(module, "TOOLS", None)
    if tools is None:
        return LoadResult(path=path, tools=[])
    if not isinstance(tools, (list, tuple)):
        return LoadResult(path=path, error="TOOLS must be a list of callables")

    valid: list[PluginTool] = []
    for t in tools:
        if callable(t):
            valid.append(t)
        else:
            log.warning("Plugin %s: non-callable item in TOOLS skipped: %r", path, t)

    return LoadResult(path=path, tools=valid)


# ---------------------------------------------------------------------------
# Directory scanner
# ---------------------------------------------------------------------------


def _scan_directory(root: Path, auto_install: bool = False) -> list[LoadResult]:
    """Recursively load all *.py files under *root*."""
    if not root.is_dir():
        return []
    results: list[LoadResult] = []
    for py_file in sorted(root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
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


# ---------------------------------------------------------------------------
# Public discovery API
# ---------------------------------------------------------------------------


def discover_project_tools(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> PluginToolSet:
    """Discover all project-wide tools (both user-global and project-local).

    User-global tools are loaded first; project tools are appended and may
    shadow user-global tools with the same name.
    """
    user_root = (user_dir or Path.home() / ".agenthicc") / "tools"
    project_root = (project_dir or Path(".agenthicc")) / "tools"

    results: list[LoadResult] = []
    results.extend(_scan_directory(user_root))
    results.extend(_scan_directory(project_root))
    return PluginToolSet(results=results)


def warn_conflicts(plugin_set: PluginToolSet) -> None:
    """Warn if any plugin tool shadows a built-in tool name."""
    from agenthicc.agent_tools import AGENT_TOOLS as _BUILTIN
    builtins = frozenset(getattr(t, "__name__", "") for t in _BUILTIN)
    for tool in plugin_set.all_tools:
        name = getattr(tool, "__name__", "")
        if name in builtins:
            log.warning("Plugin tool %r shadows built-in; plugin version used.", name)


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
    user_root = (
        (user_dir or Path.home() / ".agenthicc") / "agents" / agent_name / "tools"
    )
    project_root = (
        (project_dir or Path(".agenthicc")) / "agents" / agent_name / "tools"
    )

    results: list[LoadResult] = []
    results.extend(_scan_directory(user_root))
    results.extend(_scan_directory(project_root))
    return PluginToolSet(results=results)
