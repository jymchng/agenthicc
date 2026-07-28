"""End-to-end model/tool journey for the clean-slate create_workflow runner."""

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
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner
from agenthicc.workflows.create_workflow.state import CreateWorkflowState

pytestmark = pytest.mark.e2e


def _completion(number: int, *, content: str = "") -> Completion:
    return Completion(
        id=f"create-workflow-e2e-{number}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )


def _tool_completion(number: int, name: str, payload: dict[str, object]) -> Completion:
    return Completion(
        id=f"create-workflow-e2e-tool-{number}",
        model="mock-model",
        content="",
        tool_calls=[ToolCall(tool_use_id=f"tool-{number}", name=name, input=payload)],
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


def _make_runner(
    processor: EventProcessor,
    transport: MockTransport,
) -> tuple[CreateWorkflowRunner, TUIAppState]:
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
    return CreateWorkflowRunner(config), app_state


async def test_real_agent_runner_completes_all_tool_gated_phases(
    tmp_path: Path, processor: EventProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = "from agenthicc.workflows.plugin import WorkflowPlugin\n"
    transport = MockTransport()
    transport.queue_response(
        _tool_completion(
            1,
            "complete_interpret_phase",
            {"summary": "A parser workflow", "workflow_name": "parser_workflow"},
        )
    )
    transport.queue_response(_completion(2))
    transport.queue_response(
        _tool_completion(3, "complete_design_phase", {"design": "Parser design"})
    )
    transport.queue_response(_completion(4))
    transport.queue_response(
        _tool_completion(
            5,
            "write_file",
            {"path": ".agenthicc/workflows/parser_workflow.py", "content": source},
        )
    )
    transport.queue_response(
        _tool_completion(
            6,
            "complete_execute_phase",
            {
                "summary": "Source written",
                "artifact_name": "parser_workflow",
                "artifact_description": "A parser workflow",
            },
        )
    )
    transport.queue_response(_completion(7))
    transport.queue_response(
        _tool_completion(8, "complete_summarize_phase", {"summary": "Ready to reload"})
    )
    transport.queue_response(_completion(9))

    runner, app_state = _make_runner(processor, transport)
    context = await runner.run("Create a parser workflow")
    await processor.drain()

    artifact = tmp_path / ".agenthicc" / "workflows" / "parser_workflow.py"
    assert context.state is CreateWorkflowState.COMPLETE
    assert artifact.read_text(encoding="utf-8") == source
    assert [record.phase_name for record in app_state.workflow_run().phase_history] == [
        "interpret",
        "design",
        "execute",
        "summarize",
    ]
    assert app_state.workflow_run().status == "complete"


async def test_real_agent_prose_cannot_complete_interpret_phase(
    tmp_path: Path, processor: EventProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    for number in range(1, 3):
        transport.queue_response(_completion(number, content="The workflow is ready."))

    runner, app_state = _make_runner(processor, transport)
    runner._cfg.cfg.execution.authoring_max_generation_attempts = 2
    context = await runner.run("Create a workflow")

    assert context.state is CreateWorkflowState.FAILED
    assert "Interpret phase exhausted" in context.fail_reason
    assert app_state.workflow_run().status == "failed"
    assert not (tmp_path / ".agenthicc" / "workflows").exists()
