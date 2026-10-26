"""Plugin dependency declarations, read without executing the plugin.

A tool or command plugin declares the packages it needs either with a
module-level literal ``DEPENDENCIES = ["pkg>=1.0", ...]`` or with a
``<stem>.requirements.txt`` file beside it. Both are read statically, so a
plugin module is imported once — after its dependencies are known to be
installed — and its import-time side effects run exactly once. Agenthicc never
installs plugin dependencies itself; a plugin with missing dependencies is
reported and skipped.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

__all__ = ["PluginDependencyError", "declared_dependencies", "missing_requirements"]


class PluginDependencyError(ValueError):
    """``DEPENDENCIES`` exists but is not a literal list or tuple of strings."""


def declared_dependencies(path: Path) -> list[str]:
    """Return the requirements the plugin at *path* declares.

    A module-level ``DEPENDENCIES`` literal takes precedence over the
    ``<stem>.requirements.txt`` sidecar. A file that does not parse declares
    nothing here; importing it reports the syntax error.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "DEPENDENCIES" for t in targets):
            continue
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)) or not all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts
        ):
            raise PluginDependencyError(
                f"{path.name}: DEPENDENCIES must be a literal list of requirement strings"
            )
        return [str(item.value) for item in value.elts if isinstance(item, ast.Constant)]
    return _sidecar_requirements(path)


def _sidecar_requirements(path: Path) -> list[str]:
    sidecar = path.with_name(f"{path.stem}.requirements.txt")
    if not sidecar.is_file():
        return []
    return [
        line.strip()
        for line in sidecar.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def missing_requirements(requirements: list[str]) -> list[str]:
    """Return the requirements whose distribution is not installed."""
    # Distribution metadata loads the email and zipfile packages; only plugins
    # that declare requirements pay for it.
    import importlib.metadata

    missing: list[str] = []
    for requirement in requirements:
        distribution = re.split(r"[>=<!~\[;\s]", requirement, maxsplit=1)[0].strip()
        try:
            importlib.metadata.version(distribution)
        except (importlib.metadata.PackageNotFoundError, ValueError):
            missing.append(requirement)
    return missing
