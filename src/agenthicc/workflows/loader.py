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
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Literal

from agenthicc.plugins.source import execute_extension_source
from agenthicc.workflows.registry import BuiltinWorkflowDescriptor, builtin_workflow_descriptors

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from agenthicc.workflows.plugin import WorkflowPlugin

log = logging.getLogger(__name__)


def load_builtin_workflow(descriptor: BuiltinWorkflowDescriptor) -> type[WorkflowPlugin]:
    """Import and return one built-in workflow described by *descriptor*."""
    from agenthicc.workflows.plugin import WorkflowPlugin

    module = __import__(descriptor.module, fromlist=[descriptor.attribute])
    plugin_cls = getattr(module, descriptor.attribute)
    if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, WorkflowPlugin):
        raise TypeError(f"{descriptor.module}.{descriptor.attribute} is not a WorkflowPlugin")
    return plugin_cls


def load_builtin_workflows() -> list[type[WorkflowPlugin]]:
    """Return every built-in :class:`WorkflowPlugin` definition."""
    return [load_builtin_workflow(item) for item in builtin_workflow_descriptors()]


WorkflowLoadCode = Literal[
    "pending_marker",
    "untrusted",
    "untrusted_modified",
    "import_error",
    "invalid_manifest",
    "name_conflict",
]


def load_python_workflows(
    path: Path,
    source: str = "user",
    *,
    on_skip: Callable[[Path, WorkflowLoadCode], None] | None = None,
) -> list[type[WorkflowPlugin]]:
    """Import a trusted workflow file or package and return its plugin classes.

    Every Python file of a workflow package must be trusted, because the
    entry module can import any of them; an untrusted workflow is skipped
    without being imported. ``source`` remains part of the public signature;
    source provenance is recorded by :mod:`agenthicc.workflows.registry`.
    """
    from agenthicc.plugins.trust import current_extension_trust

    trust = current_extension_trust()
    if path.is_dir():
        from agenthicc.workflows.create_workflow.authoring import PENDING_MARKER_NAME

        marker = path / PENDING_MARKER_NAME
        if marker.exists() or marker.is_symlink():
            if on_skip is not None:
                on_skip(path, "pending_marker")
            return []
    sources = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    untrusted = [
        source_file for source_file in sources if not trust.permits(source_file, kind="workflow")
    ]
    if untrusted:
        if on_skip is not None:
            for source_file in untrusted:
                refusal = trust.check(source_file, kind="workflow")
                code: WorkflowLoadCode = "untrusted"
                if refusal is not None:
                    if refusal.reason == "changed since it was trusted":
                        code = "untrusted_modified"
                    elif refusal.reason == "authored publication pending":
                        code = "pending_marker"
                on_skip(source_file, code)
        return []
    try:
        from agenthicc.workflows.plugin import WorkflowPlugin

        with open_python_workflow_modules(path) as modules:
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
        if not results and on_skip is not None:
            on_skip(path, "invalid_manifest")
        return results
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load Python workflows from %s: %s", path, exc)
        if on_skip is not None:
            on_skip(path, "import_error")
        return []


@contextmanager
def open_python_workflow_modules(path: Path) -> Iterator[list[ModuleType]]:
    """Execute a workflow and keep relative imports available during inspection."""
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
        yield modules
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
    execute_extension_source(path, module)
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
        execute_extension_source(init_path, package)
    else:
        # A directory workflow does not need an __init__.py. This lightweight
        # namespace package is enough for ``from .tools import ...``.
        package = ModuleType(package_name)
        package.__file__ = str(path / "runner.py")
        package.__package__ = package_name
        package.__path__ = [str(path)]
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
