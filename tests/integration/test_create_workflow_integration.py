"""Integration coverage for create_workflow phase orchestration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agenthicc.workflows.authoring.runner import CreateWorkflowRunner
from agenthicc.workflows.authoring.state import CreateWorkflowContext, CreateWorkflowState

pytestmark = pytest.mark.integration


def _runner() -> CreateWorkflowRunner:
    runner = object.__new__(CreateWorkflowRunner)
    runner._cfg = MagicMock()
    runner._cfg.cfg.execution.max_agent_turns = 20
    runner._cfg.cfg.execution.authoring_max_generation_attempts = 20
    runner._cfg.cfg.execution.authoring_max_phase_turns = 20
    runner._cfg.cfg.agents.skill_permissions_for.return_value = frozenset()
    runner._cfg.terminal_wait_policies = {}
    runner._cfg.memory_router = None
    runner._cfg.semantic_index = None
    runner._cfg.mcp_registry = None
    runner._cfg.all_plugin_tools.return_value = []
    runner._cfg.approval_svc = None
    runner._cfg.completed_turns = 0
    runner._cfg.app_state.active_mode.return_value = SimpleNamespace()
    runner._mode_manager = None
    runner._shared_memory = object()
    runner._project_root = Path(".").resolve()
    runner._model_id = "test-model"
    return runner


@pytest.mark.asyncio
async def test_phase_methods_update_context_and_route_explicitly() -> None:
    runner = _runner()
    context = CreateWorkflowContext(intent="make a parser", run_id="run")

    async def fake_turn(*_args: object, **kwargs: object) -> None:
        phase = kwargs["phase_name"]
        tools = kwargs["tools"]
        target = {
            "interpret": "complete_interpret_phase",
            "design": "complete_design_phase",
        }[phase]
        for tool in tools:
            if getattr(tool, "__name__", "") == target:
                if phase == "interpret":
                    await tool("normalized", "parser_workflow")
                else:
                    await tool("design")
                return

    runner._run_turn = fake_turn  # type: ignore[method-assign]
    runner._phase_tools = lambda: []  # type: ignore[method-assign]

    state = await runner._interpret(context, max_agent_turns=20)
    assert state is CreateWorkflowState.DESIGN
    assert context.workflow_name == "parser_workflow"

    state = await runner._design(context, max_agent_turns=20)
    assert state is CreateWorkflowState.EXECUTE
    assert context.design == "design"
    assert set(context.phase_artifacts) == {"interpret", "design"}


@pytest.mark.asyncio
async def test_execute_transition_does_not_write_or_parse_source(tmp_path: Path) -> None:
    runner = _runner()
    runner._project_root = tmp_path.resolve()
    context = CreateWorkflowContext(
        intent="make a parser",
        run_id="run",
        workflow_name="parser_workflow",
        design="write a workflow",
    )
    source = "not required to be parsed by the runner"
    path = tmp_path / ".agenthicc" / "workflows" / "parser_workflow.py"

    async def fake_turn(*_args: object, **kwargs: object) -> None:
        tools = kwargs["tools"]
        path.parent.mkdir(parents=True)
        path.write_text(source, encoding="utf-8")
        for tool in tools:
            if getattr(tool, "__name__", "") == "complete_execute_phase":
                await tool("written", "parser_workflow", "A parser")
                return

    runner._run_turn = fake_turn  # type: ignore[method-assign]
    runner._phase_tools = lambda: []  # type: ignore[method-assign]
    runner._cfg.app_state.active_mode.return_value = SimpleNamespace()

    state = await runner._execute(context, max_agent_turns=20)

    assert state is CreateWorkflowState.SUMMARIZE
    assert path.read_text(encoding="utf-8") == source
    assert context.artifact_description == "A parser"


@pytest.mark.asyncio
async def test_execute_retries_handoff_after_interrupted_write(tmp_path: Path) -> None:
    runner = _runner()
    runner._project_root = tmp_path.resolve()
    context = CreateWorkflowContext(
        intent="make a parser",
        run_id="run",
        workflow_name="parser_workflow",
        design="write a workflow",
    )
    path = tmp_path / ".agenthicc" / "workflows" / "parser_workflow.py"
    calls = 0

    async def fake_turn(*_args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        tools = kwargs["tools"]
        if calls == 1:
            path.parent.mkdir(parents=True)
            path.write_text("agent source", encoding="utf-8")
            return
        for tool in tools:
            if getattr(tool, "__name__", "") == "complete_execute_phase":
                await tool("handoff after retry", "parser_workflow", "A parser")
                return

    runner._run_turn = fake_turn  # type: ignore[method-assign]
    runner._phase_tools = lambda: []  # type: ignore[method-assign]

    state = await runner._execute(context, max_agent_turns=20)

    assert state is CreateWorkflowState.SUMMARIZE
    assert calls == 2
    assert context.artifact_path == str(path.resolve())
