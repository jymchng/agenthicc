"""End-to-end create_workflow journey with a real agent runner and mock model."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport

from agenthicc.agents.registry import build_agents_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.authoring.definition import CreateWorkflow
from agenthicc.workflows.authoring.runner import CreateWorkflowRunner
from agenthicc.workflows.authoring.state import CreateWorkflowState
from agenthicc.workflows.config import WorkflowConfig

pytestmark = pytest.mark.e2e


def _completion(n: int, *, content: str = "") -> Completion:
    return Completion(
        id=f"create-workflow-{n}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )


def _tool_completion(n: int, name: str, payload: dict[str, object]) -> Completion:
    return Completion(
        id=f"create-workflow-tool-{n}",
        model="mock-model",
        content="",
        tool_calls=[ToolCall(tool_use_id=f"tool-{n}", name=name, input=payload)],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )


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


def _make_runner(tmp_path: Path, processor, transport: MockTransport) -> CreateWorkflowRunner:
    app_state = TUIAppState.create()
    config = WorkflowConfig(
        conv_store=app_state.conversation,
        app_state=app_state,
        processor=processor,
        agent_runner=AgentRunnerBase(transport=transport, signals=SignalBus()),
        approval_svc=None,
        cfg=AgenthiccConfig(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=build_agents_registry(),
    )
    return CreateWorkflowRunner(config), app_state  # type: ignore[return-value]


async def test_create_workflow_writes_agent_source_and_completes(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = "# intentionally not parsed by create_workflow\n"
    transport = MockTransport()
    transport.queue_response(
        _tool_completion(
            1,
            "complete_interpret_phase",
            {"summary": "Create a parser workflow.", "workflow_name": "parser_workflow"},
        )
    )
    transport.queue_response(_completion(2))
    transport.queue_response(
        _tool_completion(3, "complete_design_phase", {"design": "A parser design."})
    )
    transport.queue_response(_completion(4))
    transport.queue_response(
        _tool_completion(
            5,
            "write_file",
            {
                "path": ".agenthicc/workflows/parser_workflow.py",
                "content": source,
            },
        )
    )
    transport.queue_response(
        _tool_completion(
            6,
            "complete_execute_phase",
            {
                "summary": "The source was written.",
                "artifact_name": "parser_workflow",
                "artifact_description": "A parser workflow.",
            },
        )
    )
    transport.queue_response(_completion(7))
    transport.queue_response(
        _tool_completion(8, "complete_summarize_phase", {"summary": "Ready to reload."})
    )
    transport.queue_response(_completion(9))

    runner, app_state = _make_runner(tmp_path, processor, transport)
    context = await runner.run("Create a parser workflow.")
    await processor.drain()

    artifact = tmp_path / ".agenthicc" / "workflows" / "parser_workflow.py"
    assert context.state is CreateWorkflowState.COMPLETE
    assert artifact.read_text(encoding="utf-8") == source
    assert set(context.phase_artifacts) == {"interpret", "design", "execute", "summarize"}
    assert context.artifact_path == str(artifact.resolve())
    assert app_state.workflow_run().status == "complete"
    assert [record.phase_name for record in app_state.workflow_run().phase_history] == [
        "interpret",
        "design",
        "execute",
        "summarize",
    ]


async def test_create_workflow_does_not_advance_from_prose(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    for index in range(1, 21):
        transport.queue_response(_completion(index, content="I have a design."))
    runner, _ = _make_runner(tmp_path, processor, transport)

    context = await runner.run("Create a workflow.")

    assert context.state is CreateWorkflowState.FAILED
    assert "Interpret phase exhausted" in context.fail_reason
    assert (
        not list((tmp_path / ".agenthicc" / "workflows").glob("*.py"))
        if (tmp_path / ".agenthicc" / "workflows").exists()
        else True
    )


def test_create_workflow_is_registered_as_builtin() -> None:
    assert CreateWorkflow.name == "create_workflow"
