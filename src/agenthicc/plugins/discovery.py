"""Tool plugin discovery and dynamic loading (PRD-24)."""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agenthicc.plugins.deps import (
    PluginDependencyError,
    declared_dependencies,
    missing_requirements,
)
from agenthicc.plugins.trust import current_extension_trust
from agenthicc.tools.base import tool_name

log = logging.getLogger(__name__)

PluginTool = Callable[..., object]

__all__ = [
    "LoadResult",
    "PluginToolSet",
    "_load_plugin_file",
    "_scan_directory",
    "discover_project_tools",
    "discover_agent_tools",
    "warn_conflicts",
]


@dataclass
class LoadResult:
    """Outcome of loading a single plugin file."""

    path: Path
    tools: list[PluginTool] = field(default_factory=list)
    error: str | None = None
    missing_deps: list[str] = field(default_factory=list)
    commands: list[object] = field(default_factory=list)  # Command objects from COMMANDS export
    untrusted: bool = False  # skipped without import: not trusted (agenthicc trust)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing_deps and not self.untrusted


@dataclass
class PluginToolSet:
    """Aggregated result of scanning a tools directory tree."""

    results: list[LoadResult] = field(default_factory=list)

    @property
    def all_tools(self) -> list[PluginTool]:
        """Flat list of every successfully loaded tool callable."""
        return [t for r in self.results for t in r.tools]

    @property
    def all_commands(self) -> list[object]:
        """Flat list of all Command objects from plugin files that exported COMMANDS."""
        return [cmd for r in self.results for cmd in r.commands]

    @property
    def failed(self) -> list[LoadResult]:
        return [r for r in self.results if not r.ok]


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------


def _load_plugin_file(path: Path) -> LoadResult:
    """Check trust and declared dependencies, then import the plugin exactly once."""
    if not current_extension_trust().permits(path, kind="tool"):
        return LoadResult(path=path, untrusted=True)
    try:
        missing = missing_requirements(declared_dependencies(path))
    except PluginDependencyError as exc:
        return LoadResult(path=path, error=str(exc))
    if missing:
        return LoadResult(path=path, missing_deps=missing)

    # Import the plugin, once.
    module_name = f"_agenthicc_plugin_{path.stem}_{abs(hash(str(path)))}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return LoadResult(path=path, error="could not create module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except ImportError as exc:
        inferred = _infer_missing_from_ast(path)
        if inferred:
            return LoadResult(path=path, missing_deps=inferred)
        return LoadResult(path=path, error=f"ImportError: {exc}")
    except Exception as exc:
        return LoadResult(path=path, error=f"{type(exc).__name__}: {exc}")

    tools = getattr(module, "TOOLS", None)
    if tools is None:
        valid: list[PluginTool] = []
    elif not isinstance(tools, (list, tuple)):
        return LoadResult(path=path, error="TOOLS must be a list of callables")
    else:
        valid = []
        for t in tools:
            if callable(t):
                valid.append(t)
            else:
                log.warning("Plugin %s: non-callable item in TOOLS skipped: %r", path, t)

    # Also extract optional COMMANDS list (PRD-45)
    raw_commands = getattr(module, "COMMANDS", None)
    if raw_commands is not None:
        valid_cmds: list[object] = []
        for item in raw_commands if isinstance(raw_commands, (list, tuple)) else []:
            # Validate lazily — just collect non-None items; full validation in plugin_loader
            if item is not None:
                valid_cmds.append(item)
        result_commands = valid_cmds
    else:
        result_commands = []

    # PRD-124 Phase 5: extract optional SUBAGENT_TYPES list.
    # Plugin files may export SUBAGENT_TYPES = [SubagentTypeSpec(...), ...]
    # to register custom subagent types into the global DEFAULT_REGISTRY.
    raw_sa_types = getattr(module, "SUBAGENT_TYPES", None)
    if raw_sa_types is not None:
        from agenthicc.subagents.types import SubagentTypeSpec, DEFAULT_REGISTRY

        for spec in raw_sa_types if isinstance(raw_sa_types, (list, tuple)) else []:
            if isinstance(spec, SubagentTypeSpec):
                DEFAULT_REGISTRY.register(spec)
                log.debug("Plugin %s: registered subagent type %r", path, spec.name)
            else:
                log.warning(
                    "Plugin %s: non-SubagentTypeSpec item in SUBAGENT_TYPES skipped: %r", path, spec
                )

    return LoadResult(path=path, tools=valid, commands=result_commands)


# ---------------------------------------------------------------------------
# Directory scanner
# ---------------------------------------------------------------------------


def _scan_directory(root: Path) -> list[LoadResult]:
    """Recursively load all *.py files under *root*."""
    if not root.is_dir():
        return []
    results: list[LoadResult] = []
    for py_file in sorted(root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        # A plugin is an isolation boundary. An unexpected loader failure
        # must not prevent later files from being attempted; the caller can
        # publish successful results and report this file as failed.
        try:
            result = _load_plugin_file(py_file)
        except Exception as exc:  # noqa: BLE001
            result = LoadResult(path=py_file, error=f"{type(exc).__name__}: {exc}")
            log.error(
                "Tool plugin load failed: %s — %s (continuing scan)",
                py_file,
                result.error,
            )
        if result.missing_deps:
            deps_str = " ".join(result.missing_deps)
            log.warning(
                "Plugin %s skipped — missing dependencies: %s\n  Fix: pip install %s",
                py_file,
                result.missing_deps,
                deps_str,
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

    builtins = frozenset(map(tool_name, _BUILTIN))
    for tool in plugin_set.all_tools:
        name = tool_name(tool)
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
    user_root = (user_dir or Path.home() / ".agenthicc") / "agents" / agent_name / "tools"
    project_root = (project_dir or Path(".agenthicc")) / "agents" / agent_name / "tools"

    results: list[LoadResult] = []
    results.extend(_scan_directory(user_root))
    results.extend(_scan_directory(project_root))
    return PluginToolSet(results=results)
