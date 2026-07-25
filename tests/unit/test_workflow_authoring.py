"""Unit coverage for PRD-147 workflow-authoring contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.workflows.authoring.artifact import (
    WorkflowCandidate,
    parse_workflow_response,
    validate_command_candidate,
    validate_tool_candidate,
    validate_workflow_candidate,
)
from agenthicc.workflows.authoring.definition import CreateCommands, CreateTools, CreateWorkflow
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


def test_workflow_registry_exposes_plural_authoring_workflows_and_singular_aliases(
    tmp_path: Path,
) -> None:
    registry = build_workflow_registry(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "global",
    )

    assert registry.get("create_tools") is CreateTools
    assert registry.get("create_tool") is CreateTools
    assert registry.get("create_commands") is CreateCommands
    assert registry.get("create_command") is CreateCommands
    assert "create_tools" in registry.names()
    assert "create_tool" not in registry.names()


def test_tool_contract_validator_accepts_loader_compatible_source() -> None:
    source = """\
from lauren_ai import tool

@tool(name="ping", description="Ping")
async def ping(value: str = "ok") -> dict[str, object]:
    return {"value": value}

TOOLS = [ping]
"""

    assert validate_tool_candidate(WorkflowCandidate("project_tools", source)).valid is True


def test_command_contract_validator_accepts_loader_compatible_source() -> None:
    source = """\
from agenthicc.commands import Command, CommandContext

def handle(ctx: CommandContext) -> bool:
    return True

COMMAND = Command("/ping", "Ping the project", handler=handle)
"""

    assert validate_command_candidate(WorkflowCandidate("project_commands", source)).valid is True


@pytest.mark.parametrize(
    ("validator", "source", "finding"),
    [
        (validate_tool_candidate, "TOOLS = [missing]", "tool-import"),
        (
            validate_tool_candidate,
            "from lauren_ai import tool\ndef ping(): pass\nTOOLS = [ping]",
            "tool-decorator",
        ),
        (
            validate_command_candidate,
            "from agenthicc.commands import Command\nCOMMANDS = ['bad']",
            "command-entry",
        ),
        (
            validate_command_candidate,
            "from agenthicc.commands import Command\nCOMMAND = Command('bad', 'Bad')",
            "command-name",
        ),
    ],
)
def test_extension_contract_validators_reject_malformed_exports(validator, source, finding) -> None:
    report = validator(WorkflowCandidate("project_extension", source))

    assert report.valid is False
    assert finding in {item.code for item in report.findings}
