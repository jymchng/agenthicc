"""Command plugin discovery and dynamic loading (PRD-46).

Loads user-defined slash commands recursively from:
  ~/.agenthicc/commands/**/*.py   (user-global)
  .agenthicc/commands/**/*.py     (project-local, shadows user-global)

Each file must export ``COMMAND: Command`` and/or ``COMMANDS: list[Command]``.
It may also export ``DEPENDENCIES: list[str]`` or provide a matching
``<stem>.requirements.txt`` sidecar (PEP-508 requirements checked before
import, same pattern as PRD-24 tool plugins).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .command import Command

log = logging.getLogger(__name__)

__all__ = ["CommandLoadResult", "CommandPluginSet", "discover_command_plugins"]


@dataclass
class CommandLoadResult:
    """Outcome of loading a single command plugin file."""

    path: Path
    commands: list[Command] = field(default_factory=list)
    error: str | None = None
    missing_deps: list[str] = field(default_factory=list)
    untrusted: bool = False  # skipped without import: not trusted (agenthicc trust)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing_deps and not self.untrusted


@dataclass
class CommandPluginSet:
    """Aggregated result of scanning one or more command directories."""

    results: list[CommandLoadResult] = field(default_factory=list)

    @property
    def all_commands(self) -> list[Command]:
        """Flat list of Command objects from every successfully loaded file."""
        return [cmd for r in self.results for cmd in r.commands if r.ok]

    @property
    def failed(self) -> list[CommandLoadResult]:
        return [r for r in self.results if not r.ok]


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------


def _load_command_file(path: Path) -> CommandLoadResult:
    """Import a single command plugin file and extract COMMAND / COMMANDS.

    Steps:
    1. Skip an untrusted file without importing it.
    2. Read the declared dependencies statically and bail if any are missing.
    3. Import the file, once.
    4. Extract COMMAND (single) and/or COMMANDS (list).
    5. Validate types; set source_id when the author left it at the default.
    """
    from agenthicc.plugins.deps import (
        PluginDependencyError,
        declared_dependencies,
        missing_requirements,
    )
    from agenthicc.plugins.trust import current_extension_trust

    if not current_extension_trust().permits(path, kind="command"):
        return CommandLoadResult(path=path, untrusted=True)

    # ── Step 2: check declared dependencies without executing the plugin ──────
    try:
        missing = missing_requirements(declared_dependencies(path))
    except PluginDependencyError as exc:
        return CommandLoadResult(path=path, error=str(exc))
    if missing:
        return CommandLoadResult(path=path, missing_deps=missing)

    # ── Step 3: import, once ──────────────────────────────────────────────────
    # Imports from agenthicc.commands.command are done lazily below to avoid
    # circular imports: a loaded plugin imports Command from agenthicc.commands,
    # not from this loader module itself.
    module_name = f"_agenthicc_cmd_{path.stem}_{abs(hash(str(path)))}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return CommandLoadResult(path=path, error="could not create module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        return CommandLoadResult(path=path, error=f"{type(exc).__name__}: {exc}")

    # ── Step 4: extract exported commands ─────────────────────────────────────
    # Lazy import to avoid circular dependency (plugin files import Command too).
    from agenthicc.commands.command import Command

    single = getattr(module, "COMMAND", None)
    multi = getattr(module, "COMMANDS", None)

    # A file with neither export is silently skipped (e.g. a helper module).
    if single is None and multi is None:
        return CommandLoadResult(path=path, commands=[])

    commands: list[Command] = []

    if single is not None:
        if not isinstance(single, Command):
            return CommandLoadResult(path=path, error="COMMAND must be a Command instance")
        commands.append(single)

    if multi is not None:
        if not isinstance(multi, (list, tuple)):
            return CommandLoadResult(path=path, error="COMMANDS must be a list")
        for item in multi:
            if isinstance(item, Command):
                commands.append(item)
            else:
                log.warning(
                    "Command plugin %s: non-Command item in COMMANDS skipped: %r",
                    path,
                    item,
                )

    # ── Step 5: ensure source_id identifies this plugin ───────────────────────
    # If the author left source_id at the default ("builtin"), derive it from
    # the file stem so the registry can namespace-unregister by source.
    # object.__setattr__ is used defensively in case Command is frozen.
    for cmd in commands:
        if getattr(cmd, "source_id", "builtin") == "builtin":
            object.__setattr__(cmd, "source_id", f"command-plugin:{path.stem}")

    return CommandLoadResult(path=path, commands=commands)


# ---------------------------------------------------------------------------
# Directory scanner
# ---------------------------------------------------------------------------


def _scan_commands_dir(root: Path) -> list[CommandLoadResult]:
    """Load all non-private ``*.py`` files below *root* recursively."""
    if not root.is_dir():
        return []

    results: list[CommandLoadResult] = []
    for py_file in sorted(root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        result = _load_command_file(py_file)
        if result.missing_deps:
            log.warning(
                "Command plugin %s skipped — missing: %s\n  Fix: pip install %s",
                py_file,
                result.missing_deps,
                " ".join(result.missing_deps),
            )
        elif result.error:
            log.error("Command plugin %s failed to load: %s", py_file, result.error)
        elif result.commands:
            log.debug(
                "Loaded command(s) from %s: %s",
                py_file,
                [c.name for c in result.commands],
            )
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Public discovery API
# ---------------------------------------------------------------------------


def discover_command_plugins(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> CommandPluginSet:
    """Discover all command plugins from user-global and project-local directories.

    User-global commands are loaded first; project-local commands are appended
    afterwards.  Because ``UnifiedCommandRegistry.register()`` is last-write-wins,
    project-local commands naturally shadow user-global commands with the same
    name when both sets are registered into the same registry.

    Args:
        project_dir: Root of the project's ``.agenthicc/`` directory.
                     Defaults to ``Path(".agenthicc")``.
        user_dir:    Root of the user's ``~/.agenthicc/`` directory.
                     Defaults to ``Path.home() / ".agenthicc"``.

    Returns:
        A :class:`CommandPluginSet` containing all load results.
    """
    user_root = (user_dir or Path.home() / ".agenthicc") / "commands"
    project_root = (project_dir or Path(".agenthicc")) / "commands"

    results: list[CommandLoadResult] = []
    results.extend(_scan_commands_dir(user_root))
    results.extend(_scan_commands_dir(project_root))
    return CommandPluginSet(results=results)
