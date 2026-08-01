"""Integration coverage for package-style workflow discovery and reload."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.workflows.create_workflow.validation import validate_workflow_file
from agenthicc.workflows.registry import build_workflow_registry

pytestmark = pytest.mark.integration


def _write_package(root: Path, description: str) -> Path:
    package = root / "workflows" / "release_check"
    package.mkdir(parents=True, exist_ok=True)
    (package / "tools.py").write_text(
        f"DESCRIPTION = {description!r}\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        "from .runner import ReleaseCheck\n",
        encoding="utf-8",
    )
    (package / "runner.py").write_text(
        "from .tools import DESCRIPTION\n"
        "from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin\n"
        "\n"
        "class ReleaseCheck(WorkflowPlugin):\n"
        '    name = "release_check"\n'
        "    description = DESCRIPTION\n"
        '    phases = [PhaseSpec(name="verify")]\n',
        encoding="utf-8",
    )
    return package


def test_validation_and_registry_use_the_same_package_entry_point(tmp_path: Path) -> None:
    project = tmp_path / ".agenthicc"
    package = _write_package(project, "first version")

    report = validate_workflow_file(
        str(package),
        expected_name="release_check",
        root=tmp_path,
    )
    registry = build_workflow_registry(
        project_dir=project,
        user_dir=tmp_path / "missing-user-config",
    )

    assert report.ok, report.render()
    entry = registry.get_entry("release_check")
    assert entry is not None
    assert entry.source == "project"
    assert entry.path == str(package)
    assert entry.plugin_cls.description == "first version"


def test_rebuilding_registry_reloads_sibling_modules(tmp_path: Path) -> None:
    project = tmp_path / ".agenthicc"
    package = _write_package(project, "before reload")
    first = build_workflow_registry(project_dir=project, user_dir=tmp_path / "missing")

    (package / "tools.py").write_text(
        'DESCRIPTION = "after reload"\n',
        encoding="utf-8",
    )
    second = build_workflow_registry(project_dir=project, user_dir=tmp_path / "missing")

    first_plugin = first.get("release_check")
    second_plugin = second.get("release_check")
    assert first_plugin is not None
    assert second_plugin is not None
    assert first_plugin.description == "before reload"
    assert second_plugin.description == "after reload"
