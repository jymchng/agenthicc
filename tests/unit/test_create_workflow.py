"""Unit tests for the focused ``create_workflow`` state machine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.tools.fs.agent_tools import write_file
from agenthicc.workflows.authoring.definition import CreateWorkflow, CreateWorkflowParams
from agenthicc.workflows.authoring.phase_tools import (
    make_design_tools,
    make_execute_tools,
    make_interpret_tools,
    make_summarize_tools,
)
from agenthicc.workflows.authoring.inspection_tools import make_inspection_tools
from agenthicc.workflows.authoring.runner import CreateWorkflowRunner
from agenthicc.workflows.authoring.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
)

pytestmark = pytest.mark.unit


def test_definition_has_four_tool_gated_phases() -> None:
    assert [phase.name for phase in CreateWorkflow.phases] == [
        "interpret",
        "design",
        "execute",
        "summarize",
    ]
    assert all(phase.max_turns == 20 for phase in CreateWorkflow.phases)
    assert all(phase.max_iterations == 20 for phase in CreateWorkflow.phases)
    assert all(phase.system_prompt_override for phase in CreateWorkflow.phases)
    assert "write_file" in CreateWorkflow.get_phase("execute").system_prompt_override


def test_params_provide_phase_model_overrides() -> None:
    params = CreateWorkflow.build_params(
        {
            "interpret_model": "planner-model",
            "execute_model": "executor-model",
            "ignored": 42,
        }
    )

    assert isinstance(params, CreateWorkflowParams)
    assert params.model_for_phase("interpret", "global") == "planner-model"
    assert params.model_for_phase("execute", "global") == "executor-model"
    assert params.model_for_phase("design", "global") == "global"


def test_context_captures_structured_phase_artifacts() -> None:
    context = CreateWorkflowContext(intent="make a workflow", run_id="run")

    artifact = context.add_artifact(
        "interpret",
        "normalized intent",
        data={"workflow_name": "demo_workflow"},
        attempts=2,
    )

    assert artifact.data == {"workflow_name": "demo_workflow"}
    assert context.phase_attempts == {"interpret": 2}
    assert "normalized intent" in context.as_system_block()


@pytest.mark.asyncio
async def test_interpret_handoff_requires_name_and_summary() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    (handoff,) = make_interpret_tools(event, data)

    rejected = await handoff("summary", "Invalid Name")
    assert rejected["ok"] is False
    assert not event.is_set()

    accepted = await handoff("summary", "demo_workflow")
    assert accepted["ok"] is True
    assert event.is_set()
    assert data["workflow_name"] == "demo_workflow"


@pytest.mark.asyncio
async def test_design_and_summary_handoffs_are_tool_only() -> None:
    design_event = asyncio.Event()
    design_data: dict[str, object] = {}
    (design,) = make_design_tools(design_event, design_data)
    result = await design("a complete design")
    assert result["ok"] is True
    assert design_event.is_set()
    assert design_data["design"] == "a complete design"

    summary_event = asyncio.Event()
    summary_data: dict[str, object] = {}
    (summary,) = make_summarize_tools(summary_event, summary_data)
    result = await summary("written and ready")
    assert result["ok"] is True
    assert summary_event.is_set()
    assert summary_data["summary"] == "written and ready"


@pytest.mark.asyncio
async def test_source_inspection_allows_private_symbols() -> None:
    _documentation, source = make_inspection_tools()

    result = await source(
        "agenthicc.workflows.plugin",
        "_parse_output_schema",
    )

    assert result["ok"] is True
    assert "def _parse_output_schema" in result["source"]


@pytest.mark.asyncio
async def test_execute_handoff_requires_exact_agent_written_path(tmp_path: Path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    (handoff,) = make_execute_tools(
        event,
        data,
        expected_root=tmp_path / ".agenthicc" / "workflows",
        expected_name="demo_workflow",
    )

    missing = await handoff("done", "demo_workflow", "Demo")
    assert missing["ok"] is False
    assert not event.is_set()

    path = tmp_path / ".agenthicc" / "workflows" / "demo_workflow.py"
    path.parent.mkdir(parents=True)
    path.write_text("agent-owned source", encoding="utf-8")
    accepted = await handoff("done", "demo_workflow", "Demo")
    assert accepted["ok"] is True
    assert event.is_set()
    assert data["artifact_path"] == str(path.resolve())


@pytest.mark.asyncio
async def test_execute_handoff_rejects_mismatched_name(tmp_path: Path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    root = tmp_path / ".agenthicc" / "workflows"
    root.mkdir(parents=True)
    (root / "actual_workflow.py").write_text("source", encoding="utf-8")
    (handoff,) = make_execute_tools(
        event,
        data,
        expected_root=root,
        expected_name="expected_workflow",
    )

    result = await handoff("done", "actual_workflow", "A workflow")

    assert result["ok"] is False
    assert not event.is_set()


def test_execute_allowlist_contains_writer_but_not_shell() -> None:
    runner = object.__new__(CreateWorkflowRunner)
    names = runner._allowed_tool_names([write_file], "execute")

    assert "write_file" in names
    assert "read_file" in names
    assert "shell" not in names
    assert "run_bash" not in names

    design_names = runner._allowed_tool_names([write_file], "design")
    assert "write_file" not in design_names


def test_execute_allowlist_blocks_unannotated_execution_names() -> None:
    async def shell() -> dict[str, object]:
        return {}

    runner = object.__new__(CreateWorkflowRunner)
    names = runner._allowed_tool_names([shell], "execute")

    assert "shell" not in names


def test_attempt_and_turn_limits_are_configurable() -> None:
    runner = object.__new__(CreateWorkflowRunner)
    runner._cfg = SimpleNamespace(
        cfg=SimpleNamespace(
            execution=SimpleNamespace(
                authoring_max_generation_attempts=13,
                authoring_max_phase_turns=7,
            )
        )
    )

    assert runner._attempt_limit() == 13
    assert runner._phase_turn_limit("execute") == 7


@pytest.mark.asyncio
async def test_drive_phase_retries_until_handoff_tool_is_called() -> None:
    runner = object.__new__(CreateWorkflowRunner)
    context = CreateWorkflowContext(intent="intent", run_id="run")
    attempts: list[int] = []

    async def fake_turn(*_args: object, **kwargs: object) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 2:
            tools = kwargs["tools"]
            for tool in tools:  # type: ignore[union-attr]
                if getattr(tool, "__name__", "") == "complete_design_phase":
                    await tool("design")
                    break

    runner._run_turn = fake_turn  # type: ignore[method-assign]
    runner._phase_tools = lambda: []  # type: ignore[method-assign]

    result = await runner._drive_phase(
        context,
        phase_name="design",
        text="design",
        system_prompt="design",
        active_agent="planner",
        max_agent_turns=20,
        tools_factory=make_design_tools,
        excluded_capabilities=frozenset(),
    )

    assert result[0] == {"design": "design"}
    assert result[1] == 2


def test_state_terminal_values_are_explicit() -> None:
    assert not CreateWorkflowState.INTERPRET.is_terminal
    assert not CreateWorkflowState.SUMMARIZE.is_terminal
    assert CreateWorkflowState.COMPLETE.is_terminal
    assert CreateWorkflowState.FAILED.is_terminal
