"""Command plugin discovery and dynamic loading (PRD-46).

Loads user-defined slash commands from:
  ~/.agenthicc/commands/*.py   (user-global)
  .agenthicc/commands/*.py     (project-local, shadows user-global)

Each file must export ``COMMAND: Command`` and/or ``COMMANDS: list[Command]``.
It may also export ``DEPENDENCIES: list[str]`` (PEP-508 requirements checked
before import, same pattern as PRD-24 tool plugins).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["CommandLoadResult", "CommandPluginSet", "discover_command_plugins"]


@dataclass
class CommandLoadResult:
    """Outcome of loading a single command plugin file."""

    path: Path
    commands: list = field(default_factory=list)  # list[Command]
    error: str | None = None
    missing_deps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing_deps


@dataclass
class CommandPluginSet:
    """Aggregated result of scanning one or more command directories."""

    results: list[CommandLoadResult] = field(default_factory=list)

    @property
    def all_commands(self) -> list:
        """Flat list of Command objects from every successfully loaded file."""
        return [cmd for r in self.results for cmd in r.commands if r.ok]

    @property
    def failed(self) -> list[CommandLoadResult]:
        return [r for r in self.results if not r.ok]


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _check_missing_deps(requirements: list[str]) -> list[str]:
    """Return the subset of *requirements* whose package is not importable."""
    import importlib.metadata  # noqa: PLC0415
    import re  # noqa: PLC0415

    missing: list[str] = []
    for req in requirements:
        pkg = re.split(r"[>=<!~\[]", req)[0].strip()
        try:
            importlib.metadata.version(pkg)
        except Exception:
            missing.append(req)
    return missing


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------


def _load_command_file(path: Path) -> CommandLoadResult:
    """Import a single command plugin file and extract COMMAND / COMMANDS.

    Steps:
    1. Probe-import to read the DEPENDENCIES list (same as PRD-24 tool plugins).
    2. Check for missing deps; bail early if any are absent.
    3. Full import via importlib.util.
    4. Extract COMMAND (single) and/or COMMANDS (list).
    5. Validate types; set source_id when the author left it at the default.
    """
    # Imports from agenthicc.commands.command are done lazily here to avoid
    # circular imports: a loaded plugin imports Command from agenthicc.commands,
    # not from this loader module itself.
    module_name = f"_agenthicc_cmd_{path.stem}_{abs(hash(str(path)))}"

    # ── Step 1: probe-import to read DEPENDENCIES ─────────────────────────────
    declared_deps: list[str] = []
    try:
        probe_spec = importlib.util.spec_from_file_location(f"{module_name}_probe", path)
        if probe_spec and probe_spec.loader:
            probe = importlib.util.module_from_spec(probe_spec)
            probe_spec.loader.exec_module(probe)  # type: ignore[union-attr]
            declared_deps = list(getattr(probe, "DEPENDENCIES", []))
    except Exception:
        pass  # syntax errors surface properly in Step 3 below

    # ── Step 2: check missing deps ────────────────────────────────────────────
    if declared_deps:
        missing = _check_missing_deps(declared_deps)
        if missing:
            return CommandLoadResult(path=path, missing_deps=missing)

    # ── Step 3: full import ───────────────────────────────────────────────────
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return CommandLoadResult(path=path, error="could not create module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        return CommandLoadResult(path=path, error=f"{type(exc).__name__}: {exc}")

    # ── Step 4: extract exported commands ─────────────────────────────────────
    # Lazy import to avoid circular dependency (plugin files import Command too).
    from agenthicc.commands.command import Command  # noqa: PLC0415

    single = getattr(module, "COMMAND", None)
    multi = getattr(module, "COMMANDS", None)

    # A file with neither export is silently skipped (e.g. a helper module).
    if single is None and multi is None:
        return CommandLoadResult(path=path, commands=[])

    commands: list = []

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
    """Load all non-private *.py files directly under *root* (no recursion)."""
    if not root.is_dir():
        return []

    results: list[CommandLoadResult] = []
    for py_file in sorted(root.glob("*.py")):
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
