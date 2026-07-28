"""Unit contracts for the clean-slate ``create_workflow`` implementation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.tools.capabilities import tool_execute, tool_write
from agenthicc.tools.fs.agent_tools import write_file
from agenthicc.workflows.create_workflow.definition import CreateWorkflow, CreateWorkflowParams
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.create_workflow.phase_tools import (
    make_design_tools,
    make_execute_tools,
    make_interpret_tools,
    make_summarize_tools,
)
from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner
from agenthicc.workflows.create_workflow.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
)

pytestmark = pytest.mark.unit


def test_definition_is_explicit_and_tool_gated() -> None:
    assert CreateWorkflow.name == "create_workflow"
    assert CreateWorkflow.phase_names() == ["interpret", "design", "execute", "summarize"]
    assert all(phase.max_turns == 20 for phase in CreateWorkflow.phases)
    assert all(phase.max_iterations == 20 for phase in CreateWorkflow.phases)
    assert all(phase.system_prompt_override for phase in CreateWorkflow.phases)
    assert "write_file" in CreateWorkflow.get_phase("execute").system_prompt_override
    assert "only tool call" in CreateWorkflow.get_phase("summarize").system_prompt_override


@pytest.mark.parametrize(
    ("phase", "handoff", "next_state"),
    [
        ("interpret", "complete_interpret_phase(summary, workflow_name)", "DESIGN"),
        ("design", "complete_design_phase(design)", "EXECUTE"),
        (
            "execute",
            "complete_execute_phase(summary, artifact_name, artifact_description)",
            "SUMMARIZE",
        ),
        ("summarize", "complete_summarize_phase(summary)", "complete"),
    ],
)
def test_phase_prompt_names_only_transition_contract(
    phase: str, handoff: str, next_state: str
) -> None:
    prompt = CreateWorkflow.get_phase(phase).system_prompt_override
    assert handoff in prompt
    assert "prose alone cannot" in prompt.lower() or "only tool call" in prompt.lower()
    assert next_state.lower() in prompt.lower()


def test_params_are_typed_and_phase_specific() -> None:
    params = CreateWorkflow.build_params(
        {"interpret_model": "planner", "execute_model": "builder", "ignored": 12}
    )
    assert isinstance(params, CreateWorkflowParams)
    assert params.model_for_phase("interpret", "global") == "planner"
    assert params.model_for_phase("execute", "global") == "builder"
    assert params.model_for_phase("design", "global") == "global"


def test_context_records_bounded_phase_artifacts() -> None:
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


def test_state_terminal_boundary_is_explicit() -> None:
    assert not CreateWorkflowState.INTERPRET.is_terminal
    assert not CreateWorkflowState.SUMMARIZE.is_terminal
    assert CreateWorkflowState.COMPLETE.is_terminal
    assert CreateWorkflowState.FAILED.is_terminal


@pytest.mark.asyncio
async def test_interpret_handoff_rejects_invalid_name_without_transition() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    (handoff,) = make_interpret_tools(event, data)

    result = await handoff("normalized", "Not a Python name")
    assert result["ok"] is False
    assert not event.is_set()
    assert "workflow_name" in str(result["error"])

    result = await handoff("normalized", "demo_workflow")
    assert result["ok"] is True
    assert event.is_set()
    assert data["workflow_name"] == "demo_workflow"


@pytest.mark.asyncio
async def test_empty_design_and_summary_are_not_handoffs() -> None:
    design_event = asyncio.Event()
    design_data: dict[str, object] = {}
    (design,) = make_design_tools(design_event, design_data)
    assert (await design(" "))["ok"] is False
    assert not design_event.is_set()

    summary_event = asyncio.Event()
    summary_data: dict[str, object] = {}
    (summary,) = make_summarize_tools(summary_event, summary_data)
    assert (await summary(" "))["ok"] is False
    assert not summary_event.is_set()


@pytest.mark.asyncio
async def test_execute_handoff_requires_exact_existing_artifact(tmp_path: Path) -> None:
    root = tmp_path / ".agenthicc" / "workflows"
    event = asyncio.Event()
    data: dict[str, object] = {}
    (handoff,) = make_execute_tools(
        event,
        data,
        expected_root=root,
        expected_name="demo_workflow",
    )

    missing = await handoff("done", "demo_workflow", "A demo")
    assert missing["ok"] is False
    assert not event.is_set()

    root.mkdir(parents=True)
    artifact = root / "demo_workflow.py"
    artifact.write_text("source", encoding="utf-8")
    accepted = await handoff("done", "demo_workflow", "A demo")
    assert accepted["ok"] is True
    assert data["artifact_path"] == str(artifact.resolve())


@pytest.mark.asyncio
async def test_execute_handoff_rejects_wrong_name_and_path_escape(tmp_path: Path) -> None:
    root = tmp_path / ".agenthicc" / "workflows"
    root.mkdir(parents=True)
    (root / "expected.py").write_text("source", encoding="utf-8")
    event = asyncio.Event()
    data: dict[str, object] = {}
    (handoff,) = make_execute_tools(event, data, expected_root=root, expected_name="expected")

    wrong = await handoff("done", "other", "A workflow")
    assert wrong["ok"] is False
    assert not event.is_set()
    escaped = await handoff("done", "../expected", "A workflow")
    assert escaped["ok"] is False
    assert not event.is_set()


def test_allowed_tool_surface_is_read_only_except_execute_write() -> None:
    async def shell() -> dict[str, object]:
        return {}

    @tool_execute
    async def run_command() -> dict[str, object]:
        return {}

    @tool_write
    async def custom_writer() -> dict[str, object]:
        return {}

    runner = object.__new__(CreateWorkflowRunner)
    design = runner._allowed_tool_names([write_file, shell, run_command, custom_writer], "design")
    execute = runner._allowed_tool_names([write_file, shell, run_command, custom_writer], "execute")
    assert "read_file" in design
    assert "write_file" not in design
    assert "shell" not in design
    assert "run_command" not in execute
    assert "custom_writer" not in execute
    assert "write_file" in execute


def test_retry_prompt_is_self_contained_and_actionable() -> None:
    prompt = CreateWorkflowRunner._phase_retry_prompt(
        phase_name="design",
        original_text="design the workflow",
        system_prompt="Design instructions",
        last_error="phase handoff was not called",
    )
    assert "RETRY REQUIRED" in prompt
    assert "design the workflow" in prompt
    assert "Phase instructions:\nDesign instructions" in prompt
    assert "complete_design_phase(design)" in prompt
    assert "only that handoff tool" in prompt


@pytest.mark.asyncio
async def test_inner_loop_retries_prose_until_tool_handoff() -> None:
    runner = object.__new__(CreateWorkflowRunner)
    runner._cfg = SimpleNamespace(
        cfg=SimpleNamespace(execution=SimpleNamespace(authoring_max_generation_attempts=3))
    )
    context = CreateWorkflowContext(intent="intent", run_id="run")
    prompts: list[str] = []
    calls = 0

    async def fake_turn(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        prompts.append(str(args[0]))
        if calls == 2:
            tools = kwargs["tools"]
            for candidate in tools:  # type: ignore[union-attr]
                if getattr(candidate, "__name__", "") == "complete_design_phase":
                    await candidate("complete design")
                    return

    runner._run_turn = fake_turn  # type: ignore[method-assign]
    runner._phase_tools = lambda: []  # type: ignore[method-assign]

    result = await runner._drive_phase(
        context,
        phase_name="design",
        text="design",
        system_prompt="design instructions",
        active_agent="planner",
        max_agent_turns=4,
        tools_factory=make_design_tools,
        excluded_capabilities=frozenset(),
    )
    assert result[0] == {"design": "complete design"}
    assert result[1] == 2
    assert "RETRY REQUIRED" in prompts[1]


def test_inspection_tool_can_read_private_current_symbol() -> None:
    tools = make_inspection_tools()
    source_tool = next(tool for tool in tools if tool.__name__ == "inspect_agenthicc_source")
    result = asyncio.run(source_tool("agenthicc.workflows.plugin", "_parse_output_schema"))
    assert result["ok"] is True
    assert "def _parse_output_schema" in result["source"]
