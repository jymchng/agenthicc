"""Unit coverage for PRD-147 workflow-authoring contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.workflows.authoring.artifact import (
    WorkflowCandidate,
    parse_workflow_response,
    validate_workflow_candidate,
)
from agenthicc.workflows.authoring.definition import CreateWorkflow
from agenthicc.workflows.registry import build_workflow_registry

pytestmark = pytest.mark.unit


_VALID_SOURCE = """\
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class ExampleWorkflow(WorkflowPlugin):
    name = "example_workflow"
    description = "A test workflow."
    phases = [
        PhaseSpec(name="parse", next="summarize"),
        PhaseSpec(name="summarize"),
    ]
"""


def test_parse_workflow_envelope_and_validate_without_importing() -> None:
    candidate = parse_workflow_response(
        '<workflow name="example_workflow" description="A test workflow.">\n'
        "```python\n"
        f"{_VALID_SOURCE}"
        "```\n"
        "</workflow>"
    )

    assert candidate.name == "example_workflow"
    assert candidate.description == "A test workflow."
    assert validate_workflow_candidate(candidate).valid is True


def test_parse_plain_python_recovers_class_level_name() -> None:
    candidate = parse_workflow_response(_VALID_SOURCE)

    assert candidate.name == "example_workflow"
    assert validate_workflow_candidate(candidate).valid is True


def test_validation_accepts_phase_name_as_positional_argument() -> None:
    source = _VALID_SOURCE.replace(
        'PhaseSpec(name="parse", next="summarize")',
        'PhaseSpec("parse", next="summarize")',
    )

    assert validate_workflow_candidate(WorkflowCandidate("example_workflow", source)).valid is True


@pytest.mark.parametrize(
    ("source", "finding"),
    [
        (WorkflowCandidate("bad-name", _VALID_SOURCE), "workflow-name"),
        (
            _VALID_SOURCE.replace(
                'PhaseSpec(name="parse", next="summarize")',
                'PhaseSpec(name="parse", next="missing")',
            ),
            "phase-reference",
        ),
        (
            _VALID_SOURCE.replace(
                "from agenthicc.workflows.plugin",
                "from subprocess import run\nfrom agenthicc.workflows.plugin",
            ),
            "unsafe-import",
        ),
        (
            _VALID_SOURCE.replace("class ExampleWorkflow", "class ExampleWorkflow\n    broken"),
            "syntax",
        ),
    ],
)
def test_validation_rejects_unsafe_or_invalid_candidates(
    source: str | WorkflowCandidate, finding: str
) -> None:
    candidate = (
        source
        if isinstance(source, WorkflowCandidate)
        else WorkflowCandidate("example_workflow", source)
    )
    report = validate_workflow_candidate(candidate)

    assert report.valid is False
    assert finding in {item.code for item in report.findings}


def test_workflow_registry_exposes_create_workflow_as_builtin(tmp_path: Path) -> None:
    registry = build_workflow_registry(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "global",
    )

    assert registry.get("create_workflow") is CreateWorkflow
    assert registry.get_entry("create_workflow").source == "builtin"
