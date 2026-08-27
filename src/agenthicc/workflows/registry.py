"""WorkflowRegistry — stores and queries WorkflowPlugin classes (PRD-116).

WorkflowDefinition has been removed.  The registry now stores
``WorkflowEntry`` objects (plugin class + provenance); all workflow metadata
is accessed directly via the plugin class's attributes and classmethods.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.workflows.loader import BuiltinWorkflowDescriptor
    from agenthicc.workflows.plugin import WorkflowEntry, WorkflowPlugin

log = logging.getLogger(__name__)


class WorkflowRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, WorkflowEntry] = {}
        self._aliases: dict[str, str] = {}
        self._lazy: dict[str, _LazyWorkflow] = {}

    def register(
        self,
        plugin_cls: type[WorkflowPlugin],
        *,
        source: str = "builtin",
        path: str | None = None,
    ) -> None:
        from agenthicc.workflows.plugin import WorkflowEntry  # noqa: PLC0415

        name = plugin_cls.name
        if not name:
            log.warning("WorkflowPlugin subclass %r has no name — skipped", plugin_cls)
            return
        # A real plugin using an alias spelling takes precedence over the
        # built-in compatibility alias with that same spelling.
        self._aliases.pop(name, None)
        existing = self._entries.get(name)
        if existing is not None:
            if source == "user" and existing.source == "builtin":
                log.debug("User workflow %r shadows builtin", name)
            elif source == "project" and existing.source in ("builtin", "user"):
                log.warning("Project workflow %r overrides %s workflow", name, existing.source)
        self._entries[name] = WorkflowEntry(plugin_cls=plugin_cls, source=source, path=path)
        self._lazy.pop(name, None)

    def register_lazy(
        self,
        name: str,
        loader: Callable[[], type[WorkflowPlugin]],
        *,
        source: str = "builtin",
        path: str | None = None,
        mode_bindings: tuple[str, ...] = (),
    ) -> None:
        """Register a workflow whose implementation is imported on demand."""
        if not name or name in self._entries:
            return
        self._aliases.pop(name, None)
        self._lazy[name] = _LazyWorkflow(
            name=name,
            loader=loader,
            source=source,
            path=path,
            mode_bindings=mode_bindings,
        )

    def _materialize(self, name: str) -> WorkflowEntry | None:
        entry = self._entries.get(name)
        if entry is not None:
            return entry
        lazy = self._lazy.get(name)
        if lazy is None:
            return None
        plugin_cls = lazy.loader()
        self.register(plugin_cls, source=lazy.source, path=lazy.path)
        return self._entries.get(name)

    def get(self, name: str) -> type[WorkflowPlugin] | None:
        """Return the plugin class for *name*, or ``None``."""
        entry = self._materialize(self._aliases.get(name, name))
        return entry.plugin_cls if entry else None

    def get_entry(self, name: str) -> WorkflowEntry | None:
        """Return the full entry (plugin + provenance) for *name*."""
        return self._materialize(self._aliases.get(name, name))

    def register_alias(self, alias: str, target: str) -> None:
        """Register a non-discoverable spelling for a canonical workflow."""

        if not alias or alias in self._entries:
            return
        if target in self._entries or target in self._lazy:
            self._aliases[alias] = target

    def replace_with(self, other: "WorkflowRegistry") -> None:
        """Replace entries in place while preserving session-owned identity."""
        if other is self:
            return
        self._entries = dict(other._entries)
        self._aliases = dict(other._aliases)
        self._lazy = dict(other._lazy)

    def all(self) -> list[type[WorkflowPlugin]]:
        return [entry.plugin_cls for name in self.names() if (entry := self.get_entry(name))]

    def names(self) -> list[str]:
        return [*self._entries.keys(), *[name for name in self._lazy if name not in self._entries]]

    def mode_default_map(self) -> dict[str, str]:
        """Return ``{mode_name: workflow_name}`` for the first binding per mode."""
        from agenthicc.tui.runtime.mode_manager import canonical_mode_name  # noqa: PLC0415

        result: dict[str, str] = {}
        for name in self.names():
            mode_bindings = self._mode_bindings(name)
            for mode_name in mode_bindings:
                canonical = canonical_mode_name(mode_name)
                result.setdefault(canonical, name)
        return result

    def mode_available_map(self) -> dict[str, list[str]]:
        """Return ``{mode_name: [workflow_name, …]}`` for all bindings per mode."""
        from agenthicc.tui.runtime.mode_manager import canonical_mode_name  # noqa: PLC0415

        result: dict[str, list[str]] = {}
        for name in self.names():
            mode_bindings = self._mode_bindings(name)
            for mode_name in mode_bindings:
                canonical = canonical_mode_name(mode_name)
                workflows = result.setdefault(canonical, [])
                if name not in workflows:
                    workflows.append(name)
        return result

    def _mode_bindings(self, name: str) -> tuple[str, ...]:
        lazy = self._lazy.get(self._aliases.get(name, name))
        if lazy is not None and name not in self._entries:
            return lazy.mode_bindings
        entry = self._entries.get(self._aliases.get(name, name))
        return tuple(entry.plugin_cls.mode_bindings) if entry is not None else ()


class _LazyWorkflow:
    __slots__ = ("name", "loader", "source", "path", "mode_bindings")

    def __init__(
        self,
        *,
        name: str,
        loader: Callable[[], type[WorkflowPlugin]],
        source: str,
        path: str | None,
        mode_bindings: tuple[str, ...],
    ) -> None:
        self.name = name
        self.loader = loader
        self.source = source
        self.path = path
        self.mode_bindings = mode_bindings


def _make_builtin_loader(
    descriptor: "BuiltinWorkflowDescriptor",
) -> Callable[[], type[WorkflowPlugin]]:
    """Create a typed zero-argument loader without importing a workflow."""

    def load() -> type[WorkflowPlugin]:
        from agenthicc.workflows.loader import load_builtin_workflow  # noqa: PLC0415

        return load_builtin_workflow(descriptor)

    return load


def build_workflow_registry(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
    *,
    load_external: bool = True,
) -> WorkflowRegistry:
    """Build the registry: builtin → user-global → project-local.

    ``load_external=False`` is the progressive-startup form.  Built-in
    descriptors are still available for names and mode metadata, while user
    and project Python modules are loaded by the caller at their readiness
    boundary.  The default remains eager for API compatibility.
    """
    if project_dir is None:
        project_dir = Path(".agenthicc")
    if user_dir is None:
        user_dir = Path.home() / ".agenthicc"

    from agenthicc.workflows.loader import builtin_workflow_descriptors  # noqa: PLC0415

    registry = WorkflowRegistry()

    for descriptor in builtin_workflow_descriptors():
        registry.register_lazy(
            descriptor.name,
            _make_builtin_loader(descriptor),
            source="builtin",
            mode_bindings=descriptor.mode_bindings,
        )

    if load_external:
        _scan_workflow_dir(user_dir / "workflows", "user", registry)
        _scan_workflow_dir(project_dir / "workflows", "project", registry)

    return registry


def _scan_workflow_dir(
    directory: Path,
    source: str,
    registry: WorkflowRegistry,
) -> None:
    if not directory.exists():
        return
    from agenthicc.workflows.loader import load_python_workflows  # noqa: PLC0415

    paths = sorted(directory.iterdir(), key=lambda item: item.name)
    # A newly published package may coexist with a legacy ``name.py`` path
    # written by an older authoring turn. Scan files first and packages last so
    # the atomic package publication remains the authoritative registry entry
    # without breaking legacy files that have no package counterpart.
    paths.sort(key=lambda item: item.is_dir())
    for path in paths:
        # Drafts, publication backups, and temporary rename directories are
        # intentionally invisible to normal discovery.  A loader refresh must
        # never execute a partially written package or even treat it as a
        # candidate plugin.
        if path.name.startswith(("_", ".")):
            continue
        is_python_file = path.is_file() and path.suffix == ".py"
        is_workflow_package = path.is_dir() and (path / "runner.py").is_file()
        if not is_python_file and not is_workflow_package:
            continue
        if is_python_file:
            log.warning(
                "Legacy direct-published workflow %s is supported but not draft-managed; "
                "migrate it through create_workflow for manifest validation and atomic publication.",
                path,
            )
        try:
            for plugin_cls in load_python_workflows(path, source):
                registry.register(plugin_cls, source=source, path=str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load workflow(s) from %s: %s", path, exc)
