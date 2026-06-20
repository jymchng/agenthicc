"""Workflow loader — Python-only plugin discovery (PRD-87, PRD-116).

Returns ``list[type[WorkflowPlugin]]`` directly.  WorkflowDefinition has been
removed; the registry stores plugin classes wrapped in WorkflowEntry.
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path

from agenthicc.workflows.plugin import WorkflowPlugin

log = logging.getLogger(__name__)


def load_builtin_workflows() -> list[type[WorkflowPlugin]]:
    """Return all builtin WorkflowPlugin subclasses (PRD-112, PRD-116)."""
    from agenthicc.workflows.code_plan.definition import CodePlan       # noqa: PLC0415
    return [CodePlan, ]


def load_python_workflows(
    path: Path, source: str = "user",
) -> list[type[WorkflowPlugin]]:
    """Import *path* and return every WorkflowPlugin subclass found in it."""
    module_name = f"_agenthicc_workflow_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            log.warning("Could not create module spec for %s", path)
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        results: list[type[WorkflowPlugin]] = []
        for _attr_name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                obj is not WorkflowPlugin
                and issubclass(obj, WorkflowPlugin)
                and getattr(obj, "name", "") != ""
            ):
                results.append(obj)
        return results
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load Python workflows from %s: %s", path, exc)
        return []
