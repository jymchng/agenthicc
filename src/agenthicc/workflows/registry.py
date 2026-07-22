"""WorkflowRegistry — stores and queries WorkflowPlugin classes (PRD-116).

WorkflowDefinition has been removed.  The registry now stores
``WorkflowEntry`` objects (plugin class + provenance); all workflow metadata
is accessed directly via the plugin class's attributes and classmethods.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agenthicc.workflows.plugin import WorkflowEntry, WorkflowPlugin

log = logging.getLogger(__name__)


class WorkflowRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, WorkflowEntry] = {}

    def register(
        self,
        plugin_cls: type[WorkflowPlugin],
        *,
        source: str = "builtin",
        path: str | None = None,
    ) -> None:
        name = plugin_cls.name
        if not name:
            log.warning("WorkflowPlugin subclass %r has no name — skipped", plugin_cls)
            return
        existing = self._entries.get(name)
        if existing is not None:
            if source == "user" and existing.source == "builtin":
                log.debug("User workflow %r shadows builtin", name)
            elif source == "project" and existing.source in ("builtin", "user"):
                log.warning("Project workflow %r overrides %s workflow", name, existing.source)
        self._entries[name] = WorkflowEntry(plugin_cls=plugin_cls, source=source, path=path)

    def get(self, name: str) -> type[WorkflowPlugin] | None:
        """Return the plugin class for *name*, or ``None``."""
        entry = self._entries.get(name)
        return entry.plugin_cls if entry else None

    def get_entry(self, name: str) -> WorkflowEntry | None:
        """Return the full entry (plugin + provenance) for *name*."""
        return self._entries.get(name)

    def all(self) -> list[type[WorkflowPlugin]]:
        return [e.plugin_cls for e in self._entries.values()]

    def names(self) -> list[str]:
        return list(self._entries.keys())

    def mode_default_map(self) -> dict[str, str]:
        """Return ``{mode_name: workflow_name}`` for the first binding per mode."""
        result: dict[str, str] = {}
        for entry in self._entries.values():
            for mode_name in entry.plugin_cls.mode_bindings:
                result.setdefault(mode_name, entry.plugin_cls.name)
        return result

    def mode_available_map(self) -> dict[str, list[str]]:
        """Return ``{mode_name: [workflow_name, …]}`` for all bindings per mode."""
        result: dict[str, list[str]] = {}
        for entry in self._entries.values():
            for mode_name in entry.plugin_cls.mode_bindings:
                result.setdefault(mode_name, []).append(entry.plugin_cls.name)
        return result


def build_workflow_registry(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> WorkflowRegistry:
    """Build the registry: builtin → user-global → project-local."""
    if project_dir is None:
        project_dir = Path(".agenthicc")
    if user_dir is None:
        user_dir = Path.home() / ".agenthicc"

    from agenthicc.workflows.loader import load_builtin_workflows  # noqa: PLC0415

    registry = WorkflowRegistry()

    for plugin_cls in load_builtin_workflows():
        registry.register(plugin_cls, source="builtin")

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

    for path in sorted(directory.iterdir()):
        if path.name.startswith("_") or path.suffix != ".py":
            continue
        try:
            for plugin_cls in load_python_workflows(path, source):
                registry.register(plugin_cls, source=source, path=str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load workflow(s) from %s: %s", path, exc)
