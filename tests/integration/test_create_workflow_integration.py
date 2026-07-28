"""Integration coverage for the explicit create_workflow state machine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner
from agenthicc.workflows.create_workflow.state import CreateWorkflowContext, CreateWorkflowState

pytestmark = pytest.mark.integration


@pytest.fixture
async def processor(tmp_path: Path):
    kernel_state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snapshot.json"),
        ),
        policy=SecurityPolicy(),
    )
    processor = EventProcessor(initial_state=kernel_state, persist=False)
    task = asyncio.create_task(processor.run())
    await asyncio.sleep(0)
    yield processor
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _make_runner(processor: EventProcessor) -> tuple[CreateWorkflowRunner, TUIAppState]:
    app_state = TUIAppState.create()
    config = WorkflowConfig(
        conv_store=app_state.conversation,
        app_state=app_state,
        processor=processor,
        agent_runner=MagicMock(),
        approval_svc=None,
        cfg=AgenthiccConfig(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=MagicMock(),
    )
    return CreateWorkflowRunner(config), app_state


def _invoke_named_tool(tools: list[object], name: str, *args: object) -> object:
    for candidate in tools:
        if getattr(candidate, "__name__", "") == name:
            return candidate(*args)
    raise AssertionError(f"missing phase tool {name}")


async def test_real_processor_observes_all_phase_boundaries_and_artifacts(
    processor: EventProcessor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner, app_state = _make_runner(processor)
    calls: list[str] = []

    async def fake_turn(*_args: object, **kwargs: object) -> None:
        phase = str(kwargs["phase_name"])
        calls.append(phase)
        tools = kwargs["tools"]
        if phase == "interpret":
            await _invoke_named_tool(
                tools, "complete_interpret_phase", "A parser workflow", "parser_workflow"
            )
        elif phase == "design":
            await _invoke_named_tool(tools, "complete_design_phase", "A typed parser design")
        elif phase == "execute":
            path = tmp_path / ".agenthicc" / "workflows" / "parser_workflow.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                "from agenthicc.workflows.plugin import WorkflowPlugin\n", encoding="utf-8"
            )
            await _invoke_named_tool(
                tools,
                "complete_execute_phase",
                "The source was written.",
                "parser_workflow",
                "A parser workflow",
            )
        else:
            await _invoke_named_tool(tools, "complete_summarize_phase", "Ready to reload.")

    runner._run_turn = fake_turn  # type: ignore[method-assign]
    runner._phase_tools = lambda: []  # type: ignore[method-assign]

    context = await runner.run("Create a parser workflow")
    await processor.drain()

    assert context.state is CreateWorkflowState.COMPLETE
    assert calls == ["interpret", "design", "execute", "summarize"]
    assert set(context.phase_artifacts) == set(calls)
    assert context.artifact_path == str(
        (tmp_path / ".agenthicc" / "workflows" / "parser_workflow.py").resolve()
    )
    assert app_state.workflow_run().status == "complete"
    assert [record.phase_name for record in app_state.workflow_run().phase_history] == calls


async def test_prose_never_advances_and_hits_bounded_attempts(
    processor: EventProcessor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner, app_state = _make_runner(processor)
    runner._cfg.cfg.execution.authoring_max_generation_attempts = 2
    calls = 0

    async def prose_only(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1

    runner._run_turn = prose_only  # type: ignore[method-assign]
    runner._phase_tools = lambda: []  # type: ignore[method-assign]

    context = await runner.run("Create a workflow")

    assert context.state is CreateWorkflowState.FAILED
    assert calls == 2
    assert "Interpret phase exhausted" in context.fail_reason
    assert app_state.workflow_run().status == "failed"
    assert not (tmp_path / ".agenthicc" / "workflows").exists()


async def test_invalid_handoff_is_retried_with_the_same_phase(
    processor: EventProcessor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner, _ = _make_runner(processor)
    prompts: list[str] = []
    attempts = 0

    async def retrying_turn(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        prompts.append(str(args[0]))
        tools = kwargs["tools"]
        if attempts == 1:
            await _invoke_named_tool(tools, "complete_interpret_phase", "normalized", "Bad Name")
        else:
            await _invoke_named_tool(tools, "complete_interpret_phase", "normalized", "good_name")

    runner._run_turn = retrying_turn  # type: ignore[method-assign]
    runner._phase_tools = lambda: []  # type: ignore[method-assign]

    result = await runner._interpret(
        CreateWorkflowContext(intent="intent", run_id="run"),
        max_agent_turns=2,
    )
    assert result is CreateWorkflowState.DESIGN
    assert attempts == 2
    assert "RETRY REQUIRED" in prompts[1]
    assert "complete_interpret_phase(summary, workflow_name)" in prompts[1]


async def test_resume_uses_current_state_and_preserves_completed_artifacts(
    processor: EventProcessor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner, app_state = _make_runner(processor)
    context = CreateWorkflowContext(
        intent="Create a reporting workflow",
        run_id="resume-run",
        state=CreateWorkflowState.DESIGN,
        workflow_name="reporting_workflow",
        interpreted_intent="A reporting workflow",
    )
    context.add_artifact(
        "interpret",
        context.interpreted_intent,
        data={"workflow_name": context.workflow_name},
        attempts=1,
    )

    async def finish_remaining(*_args: object, **kwargs: object) -> None:
        phase = kwargs["phase_name"]
        tools = kwargs["tools"]
        if phase == "design":
            await _invoke_named_tool(tools, "complete_design_phase", "Reporting design")
        elif phase == "execute":
            path = tmp_path / ".agenthicc" / "workflows" / "reporting_workflow.py"
            path.parent.mkdir(parents=True)
            path.write_text("source", encoding="utf-8")
            await _invoke_named_tool(
                tools, "complete_execute_phase", "written", "reporting_workflow", "Reports"
            )
        else:
            await _invoke_named_tool(tools, "complete_summarize_phase", "complete")

    runner._run_turn = finish_remaining  # type: ignore[method-assign]
    runner._phase_tools = lambda: []  # type: ignore[method-assign]
    resumed = await runner.resume(context)

    assert resumed is context
    assert resumed.state is CreateWorkflowState.COMPLETE
    assert resumed.phase_artifacts["interpret"].summary == "A reporting workflow"
    assert app_state.workflow_run().run_id == "resume-run"
