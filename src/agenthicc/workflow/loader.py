"""Workflow loader — Python-only plugin discovery (PRD-87).

TOML support has been removed.  Builtin workflows are Python classes.
User/project workflows must also be Python WorkflowPlugin subclasses.
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path

from agenthicc.workflow.plugin import WorkflowDefinition, WorkflowPlugin

log = logging.getLogger(__name__)


def load_builtin_workflows() -> list[WorkflowDefinition]:
    """Return WorkflowDefinition objects for all builtin workflows."""
    from agenthicc.workflow.builtins import (  # noqa: PLC0415
        Architect, CodePlan, PlanOnly, ReviewOnly, Supervised,
    )
    return [
        cls().to_definition(source="builtin")
        for cls in (PlanOnly, CodePlan, ReviewOnly, Supervised, Architect)
    ]


def load_python_workflows(path: Path, source: str = "user") -> list[WorkflowDefinition]:
    """Import a Python plugin file and return all WorkflowPlugin definitions."""
    module_name = f"_agenthicc_workflow_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            log.warning("Could not create module spec for %s", path)
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        results: list[WorkflowDefinition] = []
        for _attr_name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                obj is not WorkflowPlugin
                and issubclass(obj, WorkflowPlugin)
                and getattr(obj, "name", "") != ""
            ):
                instance = obj()
                results.append(instance.to_definition(source=source, path=str(path)))
        return results
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load Python workflows from %s: %s", path, exc)
        return []
