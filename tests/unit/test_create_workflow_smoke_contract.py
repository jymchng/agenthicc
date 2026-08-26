"""Unit coverage for the bounded generated-workflow smoke contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.workflows.create_workflow.inspection_tools import _RUNNER_EXAMPLE
from agenthicc.workflows.create_workflow.smoke import run_generated_workflow_smoke

pytestmark = pytest.mark.unit


def test_custom_runner_smoke_exercises_fake_runtime_and_checkpoint(tmp_path: Path) -> None:
    workflow = tmp_path / "release_check"
    workflow.mkdir()
    (workflow / "runner.py").write_text(_RUNNER_EXAMPLE, encoding="utf-8")

    report = run_generated_workflow_smoke(
        workflow,
        expected_name="release_check",
        root=tmp_path,
    )
    assert report.ok is True, report.render()
    statuses = {check.category: check.status for check in report.checks}
    assert statuses["fake_runtime"] == "pass"
    assert statuses["failure_finalization"] == "pass"
    assert statuses["no_external_calls"] == "pass"


def test_smoke_rejects_external_service_imports(tmp_path: Path) -> None:
    workflow = tmp_path / "unsafe"
    workflow.mkdir()
    (workflow / "runner.py").write_text(
        "import socket\n\nfrom agenthicc.workflows.plugin import WorkflowPlugin\n",
        encoding="utf-8",
    )
    report = run_generated_workflow_smoke(workflow, root=tmp_path)
    assert report.ok is False
    assert any(
        check.category == "no_external_calls" and check.status == "fail" for check in report.checks
    )
