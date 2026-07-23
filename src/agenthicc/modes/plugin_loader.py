"""Mode plugin loader — discovers and loads MODE/MODES exports from .py files.

Layout convention
-----------------
A mode plugin is a Python file that exports either:

  - ``MODE`` — a single :class:`~agenthicc.modes.Mode` instance, or
  - ``MODES`` — a list of :class:`~agenthicc.modes.Mode` instances.

Optional exports:

  - ``DEPENDENCIES`` — list of pip-installable package names; the loader
    checks these via :func:`importlib.metadata.version` before executing the
    file.  Missing deps produce a :class:`ModeLoadResult` with
    ``ok=False`` and a non-empty ``missing_deps`` list.

Plugin files whose name starts with ``_`` are silently skipped.

Discovery paths
---------------
User-global:  ``~/.agenthicc/modes/``
Project-local: ``.agenthicc/modes/``

Project plugins take precedence over user plugins: if both directories
contain a mode with the same ``name``, the project version is registered
last (and therefore wins in :class:`~agenthicc.modes.ModeRegistry`).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .mode import Mode

log = logging.getLogger(__name__)

__all__ = [
    "ModeLoadResult",
    "ModePluginSet",
    "load_mode_file",
    "discover_modes",
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ModeLoadResult:
    """Outcome of loading a single mode plugin file."""

    path: Path
    modes: list[Mode] = field(default_factory=list)
    error: str | None = None
    missing_deps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing_deps


@dataclass
class ModePluginSet:
    """Aggregated results from a full plugin-discovery scan.

    Attributes
    ----------
    results:
        All :class:`ModeLoadResult` instances produced during discovery,
        including failed ones.

    Properties
    ----------
    all_modes:
        Flattened list of :class:`~agenthicc.modes.mode.Mode` instances from
        every result that loaded without errors or missing dependencies.
    failed:
        Subset of *results* where :attr:`ModeLoadResult.ok` is ``False``.
    """

    results: list[ModeLoadResult]

    @property
    def all_modes(self) -> list[Mode]:
        """All successfully loaded :class:`~agenthicc.modes.mode.Mode` instances."""
        modes: list[Mode] = []
        for r in self.results:
            if r.ok:
                modes.extend(r.modes)
        return modes

    @property
    def failed(self) -> list[ModeLoadResult]:
        """Results where loading failed (error or missing dependencies)."""
        return [r for r in self.results if not r.ok]


# ---------------------------------------------------------------------------
# Dependency checking
# ---------------------------------------------------------------------------


def _check_missing_deps(deps: list[str]) -> list[str]:
    missing: list[str] = []
    for dep in deps:
        # Strip version specifiers to get the bare package name.
        pkg = dep.split(">=")[0].split("<=")[0].split("==")[0].split("!=")[0].strip()
        try:
            importlib.metadata.version(pkg)
        except Exception:
            missing.append(dep)
    return missing


# ---------------------------------------------------------------------------
# Core file loader
# ---------------------------------------------------------------------------


def load_mode_file(path: Path) -> ModeLoadResult:
    """Load a single mode plugin file and return a :class:`ModeLoadResult`.

    Steps
    -----
    1. Check ``DEPENDENCIES`` (if declared) against installed packages.
    2. Import the module via :func:`importlib.util`.
    3. Extract ``MODE`` (single) or ``MODES`` (list).
    4. If ``source_id`` is still ``"builtin"``, replace it with
       ``"mode-plugin:<stem>"`` so the registry can later remove it.
    """
    # ── Step 1: probe for DEPENDENCIES ────────────────────────────────────
    declared_deps: list[str] = []
    try:
        probe_spec = importlib.util.spec_from_file_location("_mode_dep_probe", path)
        if probe_spec and probe_spec.loader:
            probe_mod = importlib.util.module_from_spec(probe_spec)
            probe_spec.loader.exec_module(probe_mod)
            declared_deps = list(getattr(probe_mod, "DEPENDENCIES", []))
    except SyntaxError as exc:
        return ModeLoadResult(path=path, error=f"SyntaxError: {exc}")
    except Exception:
        pass  # will be caught properly in step 2

    missing = _check_missing_deps(declared_deps)
    if missing:
        return ModeLoadResult(path=path, missing_deps=missing)

    # ── Step 2: full import ────────────────────────────────────────────────
    module_name = f"_agenthicc_mode_plugin_{path.stem}_{abs(hash(str(path)))}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return ModeLoadResult(path=path, error="could not create module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except SyntaxError as exc:
        return ModeLoadResult(path=path, error=f"SyntaxError: {exc}")
    except Exception as exc:
        return ModeLoadResult(path=path, error=f"{type(exc).__name__}: {exc}")

    # ── Step 3: extract MODE / MODES ──────────────────────────────────────
    source_tag = f"mode-plugin:{path.stem}"

    raw_modes: list[Mode] = []
    single = getattr(module, "MODE", None)
    if isinstance(single, Mode):
        raw_modes.append(single)
    else:
        many = getattr(module, "MODES", None)
        if isinstance(many, (list, tuple)):
            for item in many:
                if isinstance(item, Mode):
                    raw_modes.append(item)

    # ── Step 4: fix up source_id ──────────────────────────────────────────
    modes: list[Mode] = []
    for m in raw_modes:
        if m.source_id == "builtin":
            from dataclasses import replace

            m = replace(m, source_id=source_tag)
        modes.append(m)

    return ModeLoadResult(path=path, modes=modes)


# ---------------------------------------------------------------------------
# Directory scanner
# ---------------------------------------------------------------------------


def _scan_mode_directory(root: Path) -> list[ModeLoadResult]:
    """Load all non-private *.py files under *root*."""
    if not root.is_dir():
        return []
    results: list[ModeLoadResult] = []
    for py_file in sorted(root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        result = load_mode_file(py_file)
        if result.missing_deps:
            log.warning(
                "Mode plugin %s skipped — missing dependencies: %s",
                py_file,
                result.missing_deps,
            )
        elif result.error:
            log.error("Mode plugin %s failed to load: %s", py_file, result.error)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Public discovery API
# ---------------------------------------------------------------------------


def discover_modes(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> list[ModeLoadResult]:
    """Discover mode plugins from user-global and project-local directories.

    User plugins are loaded first; project plugins are appended and thus
    override user plugins with the same mode name when registered.
    """
    user_root = (user_dir or Path.home() / ".agenthicc") / "modes"
    project_root = (project_dir or Path(".agenthicc")) / "modes"

    results: list[ModeLoadResult] = []
    results.extend(_scan_mode_directory(user_root))
    results.extend(_scan_mode_directory(project_root))
    return results


def discover_mode_plugins(
    project_dir: Path | str | None = None,
    user_dir: Path | str | None = None,
) -> ModePluginSet:
    """Discover mode plugins and return a :class:`ModePluginSet`.

    This is the primary public entry point.  It calls :func:`discover_modes`
    internally and wraps the results in a :class:`ModePluginSet` for convenient
    access to :attr:`~ModePluginSet.all_modes` and :attr:`~ModePluginSet.failed`.

    Parameters
    ----------
    project_dir:
        Root directory for the project-local ``.agenthicc/modes/`` scan.
        Accepts a :class:`pathlib.Path` or a string.  Defaults to the
        current working directory.
    user_dir:
        Base for the user-global ``~/.agenthicc/modes/`` scan.
        Accepts a :class:`pathlib.Path` or a string.  Defaults to
        ``Path.home() / ".agenthicc"``.

    Returns
    -------
    ModePluginSet
        Aggregated results from all scanned files.
    """
    # discover_modes expects the .agenthicc subdirectory (not the project root)
    # for project_dir, and the user's .agenthicc directory (not their home) for
    # user_dir.  We translate the caller-facing "project root" / "user home"
    # convention here so that discover_mode_plugins has an intuitive interface.
    _project: Path | None = None
    if project_dir is not None:
        _project = Path(project_dir) / ".agenthicc"

    _user: Path | None = None
    if user_dir is not None:
        _user = Path(user_dir) / ".agenthicc"

    results = discover_modes(project_dir=_project, user_dir=_user)
    return ModePluginSet(results=results)
