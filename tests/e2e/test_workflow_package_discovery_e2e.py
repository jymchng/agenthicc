"""E2E journey: author a package workflow, reload discovery, then select it."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.workflows.registry import build_workflow_registry

pytestmark = pytest.mark.e2e


def test_directory_workflow_is_available_to_the_session_registry(tmp_path: Path) -> None:
    project = tmp_path / ".agenthicc"
    package = project / "workflows" / "my_workflow"
    package.mkdir(parents=True)
    (package / "tools.py").write_text(
        "def workflow_description() -> str:\n    return 'created package workflow'\n",
        encoding="utf-8",
    )
    (package / "runner.py").write_text(
        "from .tools import workflow_description\n"
        "from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin\n"
        "\n"
        "class MyWorkflow(WorkflowPlugin):\n"
        '    name = "my_workflow"\n'
        "    description = workflow_description()\n"
        '    phases = [PhaseSpec(name="run", next=None)]\n',
        encoding="utf-8",
    )

    registry = build_workflow_registry(
        project_dir=project,
        user_dir=tmp_path / "user-config",
    )
    selected = registry.get("my_workflow")

    assert selected is not None
    assert selected.name == "my_workflow"
    assert selected.description == "created package workflow"
    entry = registry.get_entry("my_workflow")
    assert entry is not None
    assert entry.path == str(package)
