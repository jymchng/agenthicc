"""End-to-end coverage for PRD-147 tool and command authoring workflows."""

from __future__ import annotations

import asyncio
import sys
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
from agenthicc.plugins.discovery import discover_project_tools
from agenthicc.tools.approval import ApprovalResponse
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.authoring.artifact import AuthoringResumeContext
from agenthicc.workflows.authoring.definition import CreateCommands, CreateTools
from agenthicc.workflows.authoring.runner import CreateCommandRunner, CreateToolRunner
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.e2e


def _completion(content: str, n: int = 1) -> Completion:
    return Completion(
        id=f"extension-authoring-{n}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )


def _tool_completion(tool_calls: list[tuple[str, dict[str, object]]], n: int) -> Completion:
    return Completion(
        id=f"extension-authoring-tool-{n}",
        model="mock-model",
        content="",
        tool_calls=[
            ToolCall(tool_use_id=f"tool-{n}-{index}", name=name, input=payload)
            for index, (name, payload) in enumerate(tool_calls)
        ],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )


def _tool_source() -> str:
    return '''\
from lauren_ai import tool

ARTIFACT_NAME = "project_status"
ARTIFACT_DESCRIPTION = "Generated project status tool."


@tool(name="project_status", description="Return project status.")
async def project_status(topic: str = "project") -> dict[str, object]:
    """Return a bounded project status value."""
    return {"topic": topic, "status": "ready"}


TOOLS = [project_status]
'''


def _command_source() -> str:
    return """\
from agenthicc.commands import Command, CommandContext

ARTIFACT_NAME = "project_status_commands"
ARTIFACT_DESCRIPTION = "Generated project status commands."


def handle_project_status(ctx: CommandContext) -> bool:
    ctx.console.print("project ready")
    return True


COMMAND = Command(
    "/project-status",
    "Show project status.",
    handler=handle_project_status,
)
"""


class _Approval:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.requests: list[object] = []

    async def request_approval(self, request: object) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(allowed=self.allowed, message="test approval")

    def reset_turn_memory(self) -> None:
        pass


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
    runner_type: type[CreateToolRunner] | type[CreateCommandRunner],
    processor: EventProcessor,
    transport: MockTransport,
    approval: _Approval,
):
    app_state = TUIAppState.create()
    runner = runner_type(
        WorkflowConfig(
            conv_store=app_state.conversation,
            app_state=app_state,
            processor=processor,
            agent_runner=AgentRunnerBase(transport=transport, signals=SignalBus()),
            approval_svc=approval,
            cfg=AgenthiccConfig(),
            skills={},
            plugin_tools=[],
            mcp_registry=None,
            mention_cache=MagicMock(),
            agents_registry=build_agents_registry(),
        )
    )
    return runner, app_state


@pytest.mark.parametrize(
    ("kind", "runner_type", "name", "source", "destination_kind"),
    [
        ("tool", CreateToolRunner, "project_status", _tool_source(), "tools"),
        ("command", CreateCommandRunner, "project_status_commands", _command_source(), "commands"),
    ],
)
async def test_authoring_publishes_and_discovers_each_extension_kind(
    tmp_path: Path,
    processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    runner_type: type[CreateToolRunner] | type[CreateCommandRunner],
    name: str,
    source: str,
    destination_kind: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion(source))
    approval = _Approval(True)
    runner, app_state = _runner(runner_type, processor, transport, approval)
    conversation_events = []
    app_state.conversation.on_event(conversation_events.append)
    modules_before = set(sys.modules)

    result = await runner.run(f"Create a {kind} extension for project status.")
    await processor.drain()

    assert result.status == "published", result.to_dict()
    assert result.summary.startswith(f"Created {kind} '")
    assert any(
        event.kind == "text" and result.summary in str(event.payload.get("text", ""))
        for event in conversation_events
    )
    assert result.artifact_kind == kind
    assert result.artifact is not None
    assert result.artifact.state == "published"
    assert result.artifact.published_path is not None
    destination = tmp_path / ".agenthicc" / destination_kind / f"{name}.py"
    assert Path(result.artifact.published_path) == destination
    assert destination.exists()
    generation_prompt = str(transport.calls[0].messages)
    assert "complete raw Python source" in generation_prompt
    assert "Return ONLY this envelope" not in generation_prompt
    assert "ARTIFACT_NAME" in generation_prompt
    assert ("@tool" in generation_prompt) is (kind == "tool")
    assert ("COMMAND" in generation_prompt) is (kind == "command")
    assert not {
        module_name
        for module_name in sys.modules
        if module_name not in modules_before and module_name.startswith("_agenthicc_")
    }
    assert approval.requests[0].tool_name == f"publish_{kind}"
    assert [record.phase_name for record in app_state.workflow_run().phase_history] == [
        "interpret",
        "design",
        "stage",
        "validate",
        "review",
        "publish",
        "summarize",
    ]

    if kind == "tool":
        discovered = discover_project_tools(
            project_dir=tmp_path / ".agenthicc", user_dir=tmp_path / "global"
        )
        assert [tool.__name__ for tool in discovered.all_tools] == ["project_status"]
    else:
        from agenthicc.commands.plugin_loader import discover_command_plugins

        discovered = discover_command_plugins(
            project_dir=tmp_path / ".agenthicc", user_dir=tmp_path / "global"
        )
        assert [command.name for command in discovered.all_commands] == ["/project-status"]


@pytest.mark.parametrize(
    ("kind", "runner_type", "name", "source", "destination_kind"),
    [
        ("tool", CreateToolRunner, "project_status", _tool_source(), "tools"),
        ("command", CreateCommandRunner, "project_status_commands", _command_source(), "commands"),
    ],
)
async def test_authoring_denial_preserves_staged_source_and_never_publishes(
    tmp_path: Path,
    processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    runner_type: type[CreateToolRunner] | type[CreateCommandRunner],
    name: str,
    source: str,
    destination_kind: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion(source))
    approval = _Approval(False)
    runner, _app_state = _runner(runner_type, processor, transport, approval)

    result = await runner.run(f"Create a {kind} extension.")
    await processor.drain()

    assert result.status == "rejected"
    assert result.artifact is not None
    assert result.artifact.state == "staged"
    assert Path(result.artifact.staged_path).exists()
    assert not (tmp_path / ".agenthicc" / destination_kind / f"{name}.py").exists()
    assert approval.requests[0].tool_input["overwrite"] is False


@pytest.mark.parametrize(
    ("kind", "runner_type", "name", "source"),
    [
        ("tool", CreateToolRunner, "project_status", _tool_source()),
        ("command", CreateCommandRunner, "project_status_commands", _command_source()),
    ],
)
async def test_authoring_retries_contract_validation_before_publication(
    tmp_path: Path,
    processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    runner_type: type[CreateToolRunner] | type[CreateCommandRunner],
    name: str,
    source: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    invalid = "TOOLS = [missing]" if kind == "tool" else "COMMANDS = ['not a command']"
    transport = MockTransport()
    transport.queue_response(_completion(invalid, 1))
    transport.queue_response(_completion(source, 2))
    runner, _app_state = _runner(runner_type, processor, transport, _Approval(True))

    result = await runner.run(f"Repair and create a {kind} extension.")
    await processor.drain()

    assert result.status == "published", result.to_dict()
    assert result.attempts == 2
    assert len(transport.calls) == 2
    assert "RECOVERY ATTEMPT" in str(transport.calls[1].messages)


@pytest.mark.parametrize(
    ("kind", "runner_type", "name", "source", "destination_kind"),
    [
        ("tool", CreateToolRunner, "project_status", _tool_source(), "tools"),
        ("command", CreateCommandRunner, "project_status_commands", _command_source(), "commands"),
    ],
)
async def test_authoring_requires_approval_to_replace_existing_extension(
    tmp_path: Path,
    processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    runner_type: type[CreateToolRunner] | type[CreateCommandRunner],
    name: str,
    source: str,
    destination_kind: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".agenthicc" / destination_kind / f"{name}.py"
    destination.parent.mkdir(parents=True)
    original = "# preserve this existing extension\n"
    destination.write_text(original, encoding="utf-8")
    transport = MockTransport()
    transport.queue_response(_completion(source))
    approval = _Approval(False)
    runner, _app_state = _runner(runner_type, processor, transport, approval)

    result = await runner.run(f"Replace the existing {kind} extension.")
    await processor.drain()

    assert result.status == "rejected"
    assert destination.read_text(encoding="utf-8") == original
    assert approval.requests[0].tool_input["overwrite"] is True


@pytest.mark.parametrize(
    ("kind", "runner_type", "name"),
    [
        ("tool", CreateToolRunner, "project_status"),
        ("command", CreateCommandRunner, "project_status_commands"),
    ],
)
async def test_authoring_malformed_model_output_fails_without_partial_publication(
    tmp_path: Path,
    processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    runner_type: type[CreateToolRunner] | type[CreateCommandRunner],
    name: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion("not an authoring envelope", 1))
    transport.queue_response(_completion("still not an authoring envelope", 2))
    transport.queue_response(_completion("still not an authoring envelope", 3))
    runner, _app_state = _runner(runner_type, processor, transport, _Approval(True))

    result = await runner.run(f"Create a malformed {kind} extension.")
    await processor.drain()

    assert result.status == "failed"
    assert result.attempts == 3
    assert result.artifact is None
    assert result.error is not None
    assert f"did not contain {kind} Python source" in result.error
    assert "after 3 attempts" in result.error
    assert not list(
        (tmp_path / ".agenthicc" / ("tools" if kind == "tool" else "commands")).glob(f"{name}.py")
    )


@pytest.mark.parametrize(
    ("runner_type", "kind", "name", "source"),
    [
        (CreateToolRunner, "tool", "project_status", _tool_source()),
        (CreateCommandRunner, "command", "project_status_commands", _command_source()),
    ],
)
async def test_authoring_resume_reuses_staged_candidate_without_model_call(
    tmp_path: Path,
    processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
    runner_type: type[CreateToolRunner] | type[CreateCommandRunner],
    kind: str,
    name: str,
    source: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    first_transport = MockTransport()
    first_transport.queue_response(_completion(source))
    first_runner, _app_state = _runner(runner_type, processor, first_transport, _Approval(False))
    staged = await first_runner.run(f"Create a resumable {kind} extension.")
    assert staged.status == "rejected"
    assert staged.artifact is not None

    second_transport = MockTransport()
    resumed_runner, _resumed_state = _runner(
        runner_type, processor, second_transport, _Approval(True)
    )
    resumed = await resumed_runner.resume(AuthoringResumeContext(staged.run_id))
    await processor.drain()

    assert resumed.status == "published", resumed.to_dict()
    assert resumed.artifact is not None
    assert resumed.artifact.sha256 == staged.artifact.sha256
    assert second_transport.calls == []
    assert Path(resumed.artifact.published_path or "").exists()


async def test_command_resume_rejects_changed_staged_source(
    tmp_path: Path,
    processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion(_command_source()))
    runner, _app_state = _runner(CreateCommandRunner, processor, transport, _Approval(False))
    staged = await runner.run("Create a command and pause for review.")
    assert staged.artifact is not None
    Path(staged.artifact.staged_path).write_text("COMMANDS = []\n", encoding="utf-8")

    resumed_runner, _resumed_state = _runner(
        CreateCommandRunner, processor, MockTransport(), _Approval(True)
    )
    result = await resumed_runner.resume(AuthoringResumeContext(staged.run_id))
    await processor.drain()

    assert result.status == "failed"
    assert result.error is not None
    assert "changed after its last validation" in result.error
    assert not (tmp_path / ".agenthicc" / "commands" / "project_status_commands.py").exists()


@pytest.mark.parametrize(
    ("plugin", "workflow_name", "kind", "name", "source", "destination_kind"),
    [
        (CreateTools, "create_tools", "tool", "project_status", _tool_source(), "tools"),
        (
            CreateCommands,
            "create_commands",
            "command",
            "project_status_commands",
            _command_source(),
            "commands",
        ),
    ],
)
async def test_headless_authoring_fails_closed_without_explicit_permission(
    tmp_path: Path,
    processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
    plugin: type[CreateTools] | type[CreateCommands],
    workflow_name: str,
    kind: str,
    name: str,
    source: str,
    destination_kind: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(
        _tool_completion(
            [("complete_authoring_phase", {"summary": "The extension contract is explicit."})],
            1,
        )
    )
    transport.queue_response(_completion("Interpretation handed off.", n=2))
    transport.queue_response(
        _tool_completion(
            [
                (
                    "submit_generated_source",
                    {
                        "source": source,
                        "artifact_name": name,
                        "artifact_description": f"Generated {kind}.",
                    },
                ),
                ("complete_authoring_phase", {"summary": "The source is ready for staging."}),
            ],
            3,
        )
    )
    transport.queue_response(_completion("Source handed off.", n=4))
    transport.queue_response(
        _tool_completion(
            [("complete_authoring_phase", {"summary": "The staged source is valid."})],
            5,
        )
    )
    transport.queue_response(_completion("Validation handed off.", n=6))
    transport.queue_response(
        _tool_completion([("complete_authoring_phase", {"summary": "Validation passed."})], 7)
    )
    transport.queue_response(_completion("Validation agent handed off.", n=8))
    transport.queue_response(_tool_completion([("request_publication_approval", {})], 9))
    transport.queue_response(_completion("Review handed off.", n=10))
    transport.queue_response(
        _tool_completion(
            [("complete_authoring_phase", {"summary": "The rejected result is ready."})], 11
        )
    )
    transport.queue_response(_completion("Summary handed off.", n=12))
    approval = _Approval(False)
    runner, app_state = _runner(
        CreateToolRunner if kind == "tool" else CreateCommandRunner,
        processor,
        transport,
        approval,
    )
    registry = WorkflowRegistry()
    registry.register(plugin, source="builtin")
    session = SimpleNamespace(
        session_id="headless-authoring",
        workflow_registry=registry,
        agent_runner=runner._cfg.agent_runner,
        app_state=app_state,
        processor=processor,
        approval_svc=approval,
        cfg=runner._cfg.cfg,
        skills={},
        project_plugins=[],
        mcp_registry=None,
        mention_cache=runner._cfg.mention_cache,
        agents_registry=runner._cfg.agents_registry,
        memory_router=None,
        semantic_index=None,
        mode_manager=None,
    )

    from agenthicc.runners.headless import execute_workflow

    result = await execute_workflow(session, workflow_name, f"Create a {kind}.")
    await processor.drain()

    assert result.status == "failed"
    completed = [
        event for event in processor.event_log if event.event_type == "WorkflowRunCompleted"
    ]
    assert completed[-1].payload["result"]["status"] == "rejected"
    assert not (tmp_path / ".agenthicc" / destination_kind / f"{name}.py").exists()
