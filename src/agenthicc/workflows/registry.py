"""WorkflowRegistry — discover, store, and query workflow definitions (PRD-87)."""
from __future__ import annotations

import logging
from pathlib import Path

from agenthicc.workflows.plugin import WorkflowDefinition

log = logging.getLogger(__name__)


class WorkflowRegistry:
    def __init__(self) -> None:
        self._defs: dict[str, WorkflowDefinition] = {}

    def register(self, defn: WorkflowDefinition) -> None:
        existing = self._defs.get(defn.name)
        if existing is not None:
            if defn.source == "user" and existing.source == "builtin":
                log.debug("User workflow %r shadows builtin", defn.name)
            elif defn.source == "project" and existing.source in ("builtin", "user"):
                log.warning(
                    "Project workflow %r overrides %s workflow",
                    defn.name, existing.source,
                )
        self._defs[defn.name] = defn

    def get(self, name: str) -> WorkflowDefinition | None:
        return self._defs.get(name)

    def all(self) -> list[WorkflowDefinition]:
        return list(self._defs.values())

    def names(self) -> list[str]:
        return list(self._defs.keys())

    def mode_default_map(self) -> dict[str, str]:
        """Return {mode_name: workflow_name} for the first-registered binding per mode."""
        result: dict[str, str] = {}
        for defn in self._defs.values():
            for mode_name in defn.mode_bindings:
                result.setdefault(mode_name, defn.name)
        return result

    def mode_available_map(self) -> dict[str, list[str]]:
        """Return {mode_name: [workflow_name, …]} for all bindings per mode."""
        result: dict[str, list[str]] = {}
        for defn in self._defs.values():
            for mode_name in defn.mode_bindings:
                result.setdefault(mode_name, []).append(defn.name)
        return result


def build_workflow_registry(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> WorkflowRegistry:
    """Build the registry: builtin → user-global → project-local (Python only)."""
    if project_dir is None:
        project_dir = Path(".agenthicc")
    if user_dir is None:
        user_dir = Path.home() / ".agenthicc"

    from agenthicc.workflows.loader import load_builtin_workflows  # noqa: PLC0415
    registry = WorkflowRegistry()

    for defn in load_builtin_workflows():
        registry.register(defn)

    _scan_workflow_dir(user_dir / "workflows", "user", registry)
    _scan_workflow_dir(project_dir / "workflows", "project", registry)

    return registry


def _scan_workflow_dir(
    directory: Path, source: str, registry: WorkflowRegistry,
) -> None:
    if not directory.exists():
        return
    from agenthicc.workflows.loader import load_python_workflows  # noqa: PLC0415
    for path in sorted(directory.iterdir()):
        if path.name.startswith("_") or path.suffix != ".py":
            continue
        try:
            for defn in load_python_workflows(path, source):
                registry.register(defn)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load workflow(s) from %s: %s", path, exc)
