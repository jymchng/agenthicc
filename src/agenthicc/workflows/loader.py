"""Workflow plugin discovery for files and package-style workflows.

Legacy plugins remain supported as ``<name>.py`` files.  New plugins may be a
directory containing ``runner.py`` plus workflow-local modules such as
``tools.py`` or ``state.py``.  Directory plugins are executed as temporary
packages so their relative imports work during discovery and reload.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.workflows.plugin import WorkflowPlugin

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuiltinWorkflowDescriptor:
    """Import-free metadata for a built-in workflow.

    Registry construction is on the interactive startup path.  Keeping the
    module and attribute name here lets the registry expose workflow names and
    mode bindings without importing every workflow implementation.  The
    implementation is imported only when the workflow is selected (or when a
    caller explicitly asks for all plugin classes).
    """

    name: str
    module: str
    attribute: str
    mode_bindings: tuple[str, ...] = ()


_BUILTIN_WORKFLOW_DESCRIPTORS = (
    BuiltinWorkflowDescriptor(
        "code_plan", "agenthicc.workflows.code_plan.definition", "CodePlan", ("Plan",)
    ),
    BuiltinWorkflowDescriptor(
        "copy_website", "agenthicc.workflows.copy_website", "CopyWebsiteWorkflow"
    ),
    BuiltinWorkflowDescriptor(
        "create_workflow",
        "agenthicc.workflows.create_workflow.definition",
        "CreateWorkflow",
    ),
    BuiltinWorkflowDescriptor(
        "goal_flow", "agenthicc.workflows.goal_flow.runner", "GoalFlowWorkflow"
    ),
    BuiltinWorkflowDescriptor(
        "make_agenthicc_tool",
        "agenthicc.workflows.make_agenthicc_tool.runner",
        "MakeAgenthiccToolWorkflow",
    ),
    BuiltinWorkflowDescriptor(
        "make_book",
        "agenthicc.workflows.make_book.runner",
        "MakeBookWorkflow",
    ),
    BuiltinWorkflowDescriptor(
        "reconstruct_site", "agenthicc.workflows.reconstruct_site", "ReconstructSiteWorkflow"
    ),
    BuiltinWorkflowDescriptor(
        "site_imitate", "agenthicc.workflows.site_imitate.runner", "SiteImitateWorkflow"
    ),
)


def builtin_workflow_descriptors() -> tuple[BuiltinWorkflowDescriptor, ...]:
    """Return built-in metadata without importing workflow implementations."""
    return _BUILTIN_WORKFLOW_DESCRIPTORS


def load_builtin_workflow(descriptor: BuiltinWorkflowDescriptor) -> type[WorkflowPlugin]:
    """Import and return one built-in workflow described by *descriptor*."""
    from agenthicc.workflows.plugin import WorkflowPlugin  # noqa: PLC0415

    module = __import__(descriptor.module, fromlist=[descriptor.attribute])
    plugin_cls = getattr(module, descriptor.attribute)
    if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, WorkflowPlugin):
        raise TypeError(f"{descriptor.module}.{descriptor.attribute} is not a WorkflowPlugin")
    return plugin_cls


def load_builtin_workflows() -> list[type[WorkflowPlugin]]:
    """Return every built-in :class:`WorkflowPlugin` definition."""
    return [load_builtin_workflow(item) for item in _BUILTIN_WORKFLOW_DESCRIPTORS]


def load_python_workflows(
    path: Path,
    source: str = "user",
) -> list[type[WorkflowPlugin]]:
    """Import a workflow file or package and return its plugin classes.

    ``source`` remains part of the public signature for compatibility; source
    provenance is recorded by :mod:`agenthicc.workflows.registry`.
    """
    try:
        from agenthicc.workflows.plugin import WorkflowPlugin  # noqa: PLC0415

        modules = load_python_workflow_modules(path)
        results: list[type[WorkflowPlugin]] = []
        seen: set[type[WorkflowPlugin]] = set()
        for module in modules:
            for _attr_name, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    obj is not WorkflowPlugin
                    and issubclass(obj, WorkflowPlugin)
                    and getattr(obj, "name", "") != ""
                    and obj not in seen
                ):
                    seen.add(obj)
                    results.append(obj)
        return results
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load Python workflows from %s: %s", path, exc)
        return []


def load_python_workflow_modules(path: Path) -> list[ModuleType]:
    """Execute *path* and return its primary module namespace(s).

    Unlike :func:`load_python_workflows`, import errors are propagated.  The
    deterministic create-workflow validator uses this to show the actual
    failure to the authoring agent.  Temporary module names are removed before
    returning, while returned module objects remain available for inspection.
    """
    resolved = path.expanduser()
    module_name = _temporary_module_name(resolved)
    temporary_names: set[str] = set()
    try:
        if resolved.is_dir():
            runner = resolved / "runner.py"
            if not runner.is_file():
                raise FileNotFoundError(f"workflow directory {resolved} must contain runner.py")
            modules = _execute_workflow_package(resolved, module_name, temporary_names)
        else:
            if not resolved.is_file():
                raise FileNotFoundError(f"workflow path does not exist: {resolved}")
            modules = [_execute_module(resolved, module_name, temporary_names)]
        return modules
    finally:
        # A workflow package may import tools.py, state.py, or other local
        # helpers. Remove the entire temporary package namespace so reloads
        # cannot retain stale code.
        for name in list(sys.modules):
            if name == module_name or name.startswith(f"{module_name}."):
                temporary_names.add(name)
        for name in temporary_names:
            sys.modules.pop(name, None)


def _temporary_module_name(path: Path) -> str:
    """Build a collision-resistant module name for a discovered workflow."""
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    stem = "".join(char if char.isalnum() or char == "_" else "_" for char in path.stem)
    return f"_agenthicc_workflow_{stem}_{digest}"


def _execute_module(
    path: Path,
    module_name: str,
    temporary_names: set[str],
) -> ModuleType:
    """Execute one Python source file under *module_name*."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not create a Python module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    temporary_names.add(module_name)
    spec.loader.exec_module(module)
    return module


def _execute_workflow_package(
    path: Path,
    package_name: str,
    temporary_names: set[str],
) -> list[ModuleType]:
    """Execute a directory workflow as a package and return package/runner."""
    init_path = path / "__init__.py"
    if init_path.is_file():
        package_spec = importlib.util.spec_from_file_location(
            package_name,
            init_path,
            submodule_search_locations=[str(path)],
        )
        if package_spec is None or package_spec.loader is None:
            raise ImportError(f"could not create a package spec for {path}")
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[package_name] = package
        temporary_names.add(package_name)
        package_spec.loader.exec_module(package)
    else:
        # A directory workflow does not need an __init__.py. This lightweight
        # namespace package is enough for ``from .tools import ...``.
        package = ModuleType(package_name)
        package.__file__ = str(path / "runner.py")
        package.__package__ = package_name
        setattr(package, "__path__", [str(path)])
        package_spec = importlib.machinery.ModuleSpec(
            package_name,
            loader=None,
            is_package=True,
        )
        package_spec.submodule_search_locations = [str(path)]
        package.__spec__ = package_spec
        sys.modules[package_name] = package
        temporary_names.add(package_name)

    runner_name = f"{package_name}.runner"
    runner = sys.modules.get(runner_name)
    if runner is None:
        runner = _execute_module(path / "runner.py", runner_name, temporary_names)
    return [package, runner]
