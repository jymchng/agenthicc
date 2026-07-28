"""End-to-end tests for the PRD-147 ``create_workflow`` journey."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport

from agenthicc.agents.registry import build_agents_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tools.approval import ApprovalResponse
from agenthicc.tools.fs.agent_tools import write_file
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.authoring.definition import CreateWorkflow
from agenthicc.workflows.authoring.runner import CreateWorkflowRunner
from agenthicc.workflows.authoring.state import AuthoringContext, AuthoringState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.plugin import PhaseSpec
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.e2e


def _completion(content: str, n: int = 1) -> Completion:
    return Completion(
        id=f"create-workflow-{n}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )


def _tool_completion(
    tool_calls: list[tuple[str, dict[str, object]]], n: int, content: str = ""
) -> Completion:
    return Completion(
        id=f"create-workflow-tool-{n}",
        model="mock-model",
        content=content,
        tool_calls=[
            ToolCall(tool_use_id=f"tool-{n}-{index}", name=name, input=payload)
            for index, (name, payload) in enumerate(tool_calls)
        ],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )


def _source(name: str = "cloakbrowser_parse_fb") -> str:
    return f"""\
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class CloakbrowserParseFacebook(WorkflowPlugin):
    name = "{name}"
    description = "Parse Facebook with the Cloakbrowser MCP tools."
    phases = [
        PhaseSpec(
            name="parse",
            agent_type="executor",
            system_prompt_override=(
                "Use the configured Cloakbrowser MCP tools to parse the runtime "
                "Facebook URL and return evidence."
            ),
            next="summarize",
        ),
        PhaseSpec(
            name="summarize",
            agent_type="verifier",
            system_prompt_override="Review the parse output and return a concise summary.",
        ),
    ]
"""


class _Approval:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.requests: list[object] = []

    async def request_approval(self, request: object) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(allowed=self.allowed, message="test approval")

    def reset_turn_memory(self) -> None:
        """Match the approval-service hook used by the agent-turn retry path."""


@pytest.fixture
async def processor(tmp_path: Path):
    kernel_state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snapshot.json"),
        ),
        policy=SecurityPolicy(),
    )
    event_processor = EventProcessor(initial_state=kernel_state, persist=False)
    task = asyncio.create_task(event_processor.run())
    await asyncio.sleep(0)
    yield event_processor
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _runner(
    tmp_path: Path,
    processor,
    transport: MockTransport,
    approval: _Approval,
    *,
    plugin_tools: list[object] | None = None,
    phase_specs: tuple[PhaseSpec, ...] | None = None,
):
    app_state = TUIAppState.create()
    return CreateWorkflowRunner(
        WorkflowConfig(
            conv_store=app_state.conversation,
            app_state=app_state,
            processor=processor,
            agent_runner=AgentRunnerBase(transport=transport, signals=SignalBus()),
            approval_svc=approval,
            cfg=AgenthiccConfig(),
            skills={},
            plugin_tools=plugin_tools or [],
            mcp_registry=None,
            mention_cache=MagicMock(),
            agents_registry=build_agents_registry(),
        ),
        phase_specs=phase_specs,
    ), app_state


def _queue_agent_write(
    transport: MockTransport,
    source: str,
    *,
    name: str = "cloakbrowser_parse_fb",
    first: int = 1,
) -> None:
    transport.queue_response(
        _tool_completion(
            [
                (
                    "complete_design_phase",
                    {"summary": "The implementation specification is complete."},
                )
            ],
            first,
        )
    )
    transport.queue_response(_completion("Design handoff is complete.", n=first + 1))
    transport.queue_response(
        _tool_completion(
            [
                (
                    "write_file",
                    {
                        "path": f".agenthicc/workflows/{name}.py",
                        "content": source,
                    },
                )
            ],
            first + 2,
        )
    )
    transport.queue_response(
        _tool_completion(
            [
                (
                    "complete_execute_phase",
                    {
                        "summary": "The complete source was written by write_file.",
                        "artifact_name": name,
                        "artifact_description": "A specialized workflow.",
                    },
                )
            ],
            first + 3,
        )
    )
    transport.queue_response(_completion("The execute handoff is complete.", n=first + 4))


def _queue_full_journey(transport: MockTransport, source: str) -> None:
    transport.queue_response(
        _tool_completion(
            [("complete_interpret_phase", {"summary": "The workflow intent is explicit."})], 1
        )
    )
    transport.queue_response(_completion("Interpretation handed off.", n=2))
    _queue_agent_write(transport, source, first=3)
    transport.queue_response(
        _tool_completion(
            [("complete_summarize_phase", {"summary": "The agent-written path is ready."})], 8
        )
    )
    transport.queue_response(_completion("Summary handed off.", n=9))


async def test_create_workflow_agent_writes_exact_source_and_runner_only_reports(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent writes the file; the runner never promotes assistant prose."""

    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    raw_source = "this is intentionally not Python and remains agent-owned\n"
    _queue_agent_write(transport, raw_source)
    approval = _Approval(False)
    runner, app_state = _runner(
        tmp_path,
        processor,
        transport,
        approval,
        plugin_tools=[write_file],
    )

    def fail_parse(_text: str):
        pytest.fail("create_workflow must not parse generated source")

    def fail_validate(_candidate):
        pytest.fail("create_workflow must not validate generated source")

    monkeypatch.setattr(runner, "_parse_candidate", fail_parse)
    monkeypatch.setattr(runner, "_validate_candidate", fail_validate)

    result = await runner.run("Create a parser workflow.")
    await processor.drain()

    destination = tmp_path / ".agenthicc" / "workflows" / "cloakbrowser_parse_fb.py"
    assert result.status == "complete", result.to_dict()
    assert result.approval == "not-requested"
    assert result.artifact is not None
    assert result.artifact.state == "agent-written"
    assert result.artifact.published_path is None
    assert result.artifact.manifest_path is None
    assert destination.read_text(encoding="utf-8") == raw_source
    assert not (tmp_path / ".agenthicc" / "authoring").exists()
    assert approval.requests == []
    assert app_state.workflow_run().status == "complete"
    assert "runner did not copy, publish, parse, or validate" in result.summary


async def test_create_workflow_has_design_execute_and_agent_write(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    _queue_full_journey(transport, _source())
    runner, app_state = _runner(
        tmp_path,
        processor,
        transport,
        _Approval(False),
        plugin_tools=[write_file],
        phase_specs=tuple(CreateWorkflow.phases),
    )

    result = await runner.run("Create a Cloakbrowser Facebook parser workflow.")
    await processor.drain()

    assert result.status == "complete", result.to_dict()
    assert result.artifact is not None
    assert result.artifact.state == "agent-written"
    assert [record.phase_name for record in app_state.workflow_run().phase_history] == [
        "interpret",
        "design",
        "execute",
        "summarize",
    ]
    assert (tmp_path / ".agenthicc" / "workflows" / "cloakbrowser_parse_fb.py").exists()
    assert not (tmp_path / ".agenthicc" / "authoring").exists()


async def test_create_workflow_prose_without_agent_write_is_not_an_artifact(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(
        _tool_completion(
            [("complete_interpret_phase", {"summary": "The workflow intent is explicit."})],
            1,
        )
    )
    transport.queue_response(_completion("Interpretation handed off.", n=2))
    transport.queue_response(_completion("Let me inspect the contracts first.", n=3))
    runner, _app_state = _runner(tmp_path, processor, transport, _Approval(True))
    runner._phase_specs = {phase.name: phase for phase in CreateWorkflow.phases}

    result = await runner.run("Create a parser workflow.")
    await processor.drain()

    assert result.status == "failed", result.to_dict()
    assert result.attempts == 20
    assert result.artifact is None
    assert not list((tmp_path / ".agenthicc").glob("workflows/*.py"))
    assert not (tmp_path / ".agenthicc" / "authoring").exists()
    assert "transition tool was not called successfully" in (result.error or "")


async def test_create_workflow_does_not_resume_runner_owned_source(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    runner, app_state = _runner(tmp_path, processor, transport, _Approval(True))

    from agenthicc.workflows.authoring.artifact import AuthoringResumeContext

    result = await runner.resume(AuthoringResumeContext("a" * 32))
    await processor.drain()

    assert result.status == "failed"
    assert "no runner-owned staged artifact" in (result.error or "")
    assert app_state.workflow_run().status == "failed"
    assert transport.calls == []


async def test_headless_style_execution_reports_four_phase_agent_owned_run(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headless adapter uses the same direct-write workflow contract."""

    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    _queue_full_journey(transport, _source())
    approval = _Approval(False)
    runner, app_state = _runner(
        tmp_path,
        processor,
        transport,
        approval,
        plugin_tools=[write_file],
    )
    registry = WorkflowRegistry()
    registry.register(CreateWorkflow, source="builtin")
    session = SimpleNamespace(
        session_id="authoring-session",
        workflow_registry=registry,
        agent_runner=runner._cfg.agent_runner,
        app_state=app_state,
        processor=processor,
        approval_svc=approval,
        cfg=runner._cfg.cfg,
        skills={},
        project_plugins=[write_file],
        mcp_registry=None,
        mention_cache=runner._cfg.mention_cache,
        agents_registry=runner._cfg.agents_registry,
        memory_router=None,
        semantic_index=None,
        mode_manager=None,
    )

    from agenthicc.runners.headless import execute_workflow

    result = await execute_workflow(session, "create_workflow", "Create a parser workflow.")
    await processor.drain()

    assert result.status == "complete"
    assert result.phases == ("interpret", "design", "execute", "summarize")
    completed = [
        event for event in processor.event_log if event.event_type == "WorkflowRunCompleted"
    ]
    payload = completed[-1].payload["result"]
    assert payload["status"] == "complete"
    assert payload["artifact"]["state"] == "agent-written"
    assert payload["artifact"]["published_path"] is None


async def test_interpretation_exhaustion_is_structured_without_missing_tool_exception(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inspection-only agent turn retries instead of raising a transition ValueError."""

    monkeypatch.chdir(tmp_path)
    runner, _app_state = _runner(tmp_path, processor, MockTransport(), _Approval(True))
    runner._phase_specs = {
        "interpret": PhaseSpec(
            name="interpret",
            agent_type="planner",
            max_iterations=2,
            system_prompt_override="Interpret the authoring intent.",
        )
    }
    runner._run_id = "a" * 32
    calls = 0

    async def inspection_only_turn(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(runner, "_run_authoring_turn", inspection_only_turn)
    context = AuthoringContext(intent="Create a parser workflow.", run_id=runner._run_id)

    state = await runner._interpret(context)

    assert state is AuthoringState.SUMMARIZE
    assert calls == 2
    assert context.result is not None
    assert context.result.status == "failed"
    assert "transition tool was not called successfully" in (context.result.error or "")


async def test_design_without_handoff_retries_without_capturing_assistant_text(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner, _app_state = _runner(tmp_path, processor, MockTransport(), _Approval(True))
    runner._cfg.cfg.execution.authoring_max_generation_attempts = 2
    prompts: list[str] = []

    async def source_only_turn(text: str, *args, **kwargs) -> None:
        del args
        prompts.append(text)
        output = kwargs.get("output")
        if isinstance(output, list):
            output.append("The source generation was interrupted before a complete file.")

    monkeypatch.setattr(runner, "_run_authoring_turn", source_only_turn)

    candidate, report, attempts, source_text = await runner._generate(
        "Create a Cloakbrowser parser workflow."
    )

    assert candidate is None
    assert not report.valid
    assert attempts == 2
    assert source_text.startswith("The source generation")
    assert len(prompts) == 2
    assert not list((tmp_path / ".agenthicc").glob("workflows/*.py"))
