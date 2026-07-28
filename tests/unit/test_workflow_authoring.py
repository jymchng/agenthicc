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
from agenthicc.workflows.authoring.runner import CreateWorkflowRunner
from agenthicc.workflows.registry import build_workflow_registry

pytestmark = pytest.mark.unit


_VALID_SOURCE = """\
from agenthicc.workflows.default.runner import WorkflowRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowContext, WorkflowPlugin


class ExampleWorkflowRunner(WorkflowRunner):
    async def run(self, intent: str) -> WorkflowContext:
        return await super().run(intent)

    async def resume(self, context: object) -> object:
        return await super().resume(context)


class ExampleWorkflow(WorkflowPlugin):
    name = "example_workflow"
    description = "A test workflow."
    phases = [
        PhaseSpec(
            name="parse",
            system_prompt_override="Parse the runtime task and return verified data.",
            next="summarize",
        ),
        PhaseSpec(
            name="summarize",
            system_prompt_override="Summarize the verified parse output and report gaps.",
        ),
    ]

    @classmethod
    def build_runner(cls, config, mode_manager):
        return ExampleWorkflowRunner(cls, config, mode_manager)
"""


_DECLARATIVE_SOURCE = """\
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class ExampleWorkflow(WorkflowPlugin):
    name = "example_workflow"
    description = "A declarative test workflow."
    phases = [
        PhaseSpec(
            name="parse",
            system_prompt_override="Parse the runtime task and return verified data.",
            next="summarize",
        ),
        PhaseSpec(
            name="summarize",
            system_prompt_override="Summarize the verified parse output and report gaps.",
        ),
    ]
"""


_DIRECT_CUSTOM_SOURCE = """\
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class DirectRunner(BaseWorkflowRunner):
    async def run(self, intent: str) -> object:
        return intent

    async def resume(self, context: object) -> object:
        return context


class ExampleWorkflow(WorkflowPlugin):
    name = "example_workflow"
    description = "A direct custom test workflow."
    phases = [
        PhaseSpec(
            name="run",
            system_prompt_override="Execute the runtime task and return verified output.",
        ),
    ]

    @classmethod
    def build_runner(cls, config, mode_manager):
        return DirectRunner()
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


def test_validation_requires_a_declared_custom_runner_when_factory_is_present() -> None:
    source = _VALID_SOURCE.replace(
        "class ExampleWorkflowRunner(WorkflowRunner):",
        "class ExampleWorkflowRunner:",
    )
    report = validate_workflow_candidate(WorkflowCandidate("example_workflow", source))

    assert report.valid is False
    assert "runner-class" in {item.code for item in report.findings}


def test_validation_accepts_declarative_workflow_with_inherited_runner() -> None:
    report = validate_workflow_candidate(WorkflowCandidate("example_workflow", _DECLARATIVE_SOURCE))

    assert report.valid is True


def test_validation_accepts_direct_custom_runner_without_super_delegation() -> None:
    report = validate_workflow_candidate(
        WorkflowCandidate("example_workflow", _DIRECT_CUSTOM_SOURCE)
    )

    assert report.valid is True


def test_create_workflow_defines_an_explicit_prompt_for_each_phase() -> None:
    assert [phase.name for phase in CreateWorkflow.phases] == [
        "interpret",
        "design",
        "stage",
        "validate",
        "review",
        "publish",
        "summarize",
    ]
    assert all(phase.system_prompt_override.strip() for phase in CreateWorkflow.phases)


def test_create_workflow_prompt_teaches_runner_and_toml_contract() -> None:
    runner = object.__new__(CreateWorkflowRunner)

    prompt = runner._generation_prompt("Create a configurable release workflow.")

    assert "generate the source directly" in prompt.lower()
    assert "complete raw Python source file" in prompt
    assert "Do not wrap the code" in prompt
    assert "Return ONLY this envelope" not in prompt
    assert "<workflow" not in prompt
    assert "system_prompt_override" in prompt
    assert "eight" in prompt
    assert "super()" in prompt
    assert "inherited" in prompt
    assert "WorkflowParams" in prompt
    assert "build_params(source)" in prompt
    assert "[workflows.<name>]" in prompt
    assert "provider switching" in prompt
    assert "copy-ready" in prompt
    assert "agenthicc.toml" in prompt
    assert "Never include API keys" in prompt


def test_validation_accepts_phase_name_as_positional_argument() -> None:
    source = _VALID_SOURCE.replace(
        'PhaseSpec(\n            name="parse",',
        'PhaseSpec("parse",',
    )

    assert validate_workflow_candidate(WorkflowCandidate("example_workflow", source)).valid is True


@pytest.mark.parametrize(
    "phase_prompt",
    [
        'system_prompt_override=""',
        "system_prompt_override=build_prompt()",
    ],
)
def test_validation_rejects_missing_or_non_literal_phase_prompt(phase_prompt: str) -> None:
    source = _DECLARATIVE_SOURCE.replace(
        'system_prompt_override="Parse the runtime task and return verified data."',
        phase_prompt,
    )

    report = validate_workflow_candidate(WorkflowCandidate("example_workflow", source))

    assert report.valid is False
    assert "phase-prompt" in {item.code for item in report.findings}


@pytest.mark.parametrize(
    ("source", "finding"),
    [
        (WorkflowCandidate("bad-name", _VALID_SOURCE), "workflow-name"),
        (
            _VALID_SOURCE.replace(
                'next="summarize",',
                'next="missing",',
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
