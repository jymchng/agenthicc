"""End-to-end tests for the PRD-147 ``create_workflow`` journey."""

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
from agenthicc.tools.approval import ApprovalResponse
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.authoring.artifact import AuthoringResumeContext
from agenthicc.workflows.authoring.definition import CreateWorkflow
from agenthicc.workflows.authoring.runner import CreateWorkflowRunner
from agenthicc.workflows.authoring.state import AuthoringContext, AuthoringState
from agenthicc.workflows.default.runner import WorkflowRunner
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.plugin import PhaseSpec
from agenthicc.workflows.registry import WorkflowRegistry, build_workflow_registry

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


def _tool_completion(tool_calls: list[tuple[str, dict[str, object]]], n: int) -> Completion:
    return Completion(
        id=f"create-workflow-tool-{n}",
        model="mock-model",
        content="",
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
                "Facebook URL. Return the title, visible text, links, and evidence; "
                "report missing tools instead of inventing them."
            ),
            next="summarize",
        ),
        PhaseSpec(
            name="summarize",
            agent_type="verifier",
            system_prompt_override=(
                "Review the parse output, verify required fields and evidence, then "
                "return the final concise summary with unresolved issues."
            ),
        ),
    ]
"""


def _envelope(source: str, name: str = "cloakbrowser_parse_fb") -> str:
    return (
        f'<workflow name="{name}" description="Parse Facebook with Cloakbrowser.">\n'
        "```python\n"
        f"{source}"
        "```\n"
        "</workflow>"
    )


class _Approval:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.requests: list[object] = []

    async def request_approval(self, request: object) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(allowed=self.allowed, message="test approval")

    def reset_turn_memory(self) -> None:
        """Match the approval-service hook used by the agent-turn retry path."""


class _BlockingApproval(_Approval):
    def __init__(self) -> None:
        super().__init__(False)
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    async def request_approval(self, request: object) -> ApprovalResponse:
        self.requests.append(request)
        self.requested.set()
        await self.release.wait()
        return ApprovalResponse(allowed=self.allowed, message="test approval")


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


def _runner(tmp_path: Path, processor, transport: MockTransport, approval: _Approval):
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
            plugin_tools=[],
            mcp_registry=None,
            mention_cache=MagicMock(),
            agents_registry=build_agents_registry(),
        )
    ), app_state


async def test_create_workflow_publishes_and_is_discoverable_after_restart(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion(_source(), n=1))
    runner, app_state = _runner(tmp_path, processor, transport, _Approval(True))
    conversation_events = []
    app_state.conversation.on_event(conversation_events.append)

    result = await runner.run("Create a workflow that uses Cloakbrowser to parse facebook.com.")
    await processor.drain()

    assert result.status == "published", result.to_dict()
    assert result.summary.startswith("Created workflow 'cloakbrowser_parse_fb'")
    assert any(
        event.kind == "text" and result.summary in str(event.payload.get("text", ""))
        for event in conversation_events
    )
    assert result.artifact is not None
    assert result.artifact.name == "cloakbrowser_parse_fb"
    assert result.artifact.manifest_path is not None
    manifest = Path(result.artifact.manifest_path)
    assert manifest.exists()
    assert result.to_dict()["artifact_kind"] == "workflow"
    assert len(result.to_dict()["artifacts"]) == 1
    assert not any(
        name.startswith("_agenthicc_workflow_cloakbrowser_parse_fb") for name in sys.modules
    )
    destination = tmp_path / ".agenthicc" / "workflows" / "cloakbrowser_parse_fb.py"
    assert destination.exists()
    assert (
        not (
            tmp_path / ".agenthicc" / "authoring" / result.run_id / "cloakbrowser_parse_fb.py"
        ).resolve()
        == destination.resolve()
    )
    assert app_state.workflow_run().status == "complete"
    assert [record.phase_name for record in app_state.workflow_run().phase_history] == [
        "interpret",
        "design",
        "stage",
        "validate",
        "review",
        "publish",
        "summarize",
    ]
    completed = [
        event for event in processor.event_log if event.event_type == "WorkflowRunCompleted"
    ]
    assert completed[-1].payload["result"]["status"] == "published"

    rebuilt = build_workflow_registry(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "global",
    )
    generated = rebuilt.get("cloakbrowser_parse_fb")
    assert generated is not None
    assert generated.name == "cloakbrowser_parse_fb"
    generated_runner = generated.build_runner(runner._cfg, None)
    assert isinstance(generated_runner, WorkflowRunner)
    assert type(generated_runner) is WorkflowRunner

    transport.queue_response(_completion("The page was parsed.", n=2))
    transport.queue_response(_completion("The parse evidence is complete.", n=3))
    await generated_runner.run("Parse the requested Facebook page.")
    await processor.drain()
    assert transport.calls[-2].system is not None
    assert "Use the configured Cloakbrowser MCP tools" in transport.calls[-2].system


async def test_create_workflow_does_not_publish_when_approval_is_denied(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion(_source(), n=1))
    approval = _Approval(False)
    runner, app_state = _runner(tmp_path, processor, transport, approval)

    result = await runner.run("Create a Cloakbrowser Facebook parser workflow.")
    await processor.drain()

    assert result.status == "rejected", result.to_dict()
    assert result.approval == "denied"
    assert result.artifact is not None
    assert approval.requests
    assert not (tmp_path / ".agenthicc" / "workflows" / "cloakbrowser_parse_fb.py").exists()
    assert app_state.workflow_run().status == "failed"


async def test_create_workflow_retries_after_invalid_source(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    invalid = _source().replace('next="summarize",', 'next="missing",')
    transport.queue_response(_completion(invalid, n=1))
    transport.queue_response(_completion(_source(), n=2))
    runner, _app_state = _runner(tmp_path, processor, transport, _Approval(True))

    result = await runner.run("Create a parser workflow and repair any validation errors.")
    await processor.drain()

    assert result.status == "published", result.to_dict()
    assert result.attempts == 2
    recovery_prompt = str(transport.calls[1].messages)
    assert "RECOVERY ATTEMPT" in recovery_prompt
    assert "phase-reference" in recovery_prompt
    assert "Return the complete corrected source file directly" in recovery_prompt
    assert (tmp_path / ".agenthicc" / "workflows" / "cloakbrowser_parse_fb.py").exists()


async def test_create_workflow_retries_non_source_response_with_actionable_feedback(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(
        _completion(
            "I inspected the registry and understand the contracts. "
            "Let me generate the complete workflow source."
        )
    )
    transport.queue_response(_completion(_source(), n=2))
    runner, app_state = _runner(tmp_path, processor, transport, _Approval(True))
    conversation_events = []
    app_state.conversation.on_event(conversation_events.append)

    result = await runner.run("Create a parser workflow after inspecting the registry.")
    await processor.drain()

    assert result.status == "published", result.to_dict()
    assert result.attempts == 2
    recovery_prompt = str(transport.calls[1].messages)
    assert "not a parseable source artifact" in recovery_prompt
    assert "tool-call activity" in recovery_prompt
    assert "complete source file directly" in recovery_prompt
    assert any(
        event.kind == "system"
        and "needs correction; retrying" in str(event.payload.get("text", ""))
        for event in conversation_events
    )


async def test_create_workflow_malformed_output_fails_without_publication(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion("not a workflow", n=1))
    transport.queue_response(_completion("still not a workflow", n=2))
    transport.queue_response(_completion("still not a workflow", n=3))
    runner, _app_state = _runner(tmp_path, processor, transport, _Approval(True))

    result = await runner.run("Create a workflow with malformed model output.")
    await processor.drain()

    assert result.status == "failed"
    assert result.error is not None
    assert "did not contain workflow Python source" in result.error
    assert "after 3 attempts" in result.error
    assert result.artifact is None
    assert not list((tmp_path / ".agenthicc" / "workflows").glob("*.py"))


async def test_create_workflow_honors_configured_generation_attempt_limit(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion("not a workflow", n=1))
    transport.queue_response(_completion("still not a workflow", n=2))
    runner, _app_state = _runner(tmp_path, processor, transport, _Approval(True))
    runner._cfg.cfg.execution.authoring_max_generation_attempts = 2

    result = await runner.run("Create a workflow with a two-attempt generation limit.")
    await processor.drain()

    assert result.status == "failed"
    assert result.attempts == 2
    assert result.error is not None
    assert "after 2 attempts" in result.error
    assert len(transport.calls) == 2


async def test_create_workflow_resume_publishes_staged_candidate_without_regeneration(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first_transport = MockTransport()
    first_transport.queue_response(_completion(_source(), n=1))
    first_runner, _app_state = _runner(tmp_path, processor, first_transport, _Approval(False))

    staged = await first_runner.run("Create a resumable parser workflow.")
    assert staged.status == "rejected"
    assert staged.artifact is not None
    assert staged.artifact.state == "staged"
    assert staged.artifact.manifest_path is not None

    second_transport = MockTransport()
    second_runner, resumed_state = _runner(tmp_path, processor, second_transport, _Approval(True))
    resumed = await second_runner.resume(AuthoringResumeContext(staged.run_id))
    await processor.drain()

    assert resumed.status == "published", resumed.to_dict()
    assert resumed.artifact is not None
    assert resumed.artifact.state == "published"
    assert resumed.artifact.manifest_path == staged.artifact.manifest_path
    assert resumed_state.workflow_run().status == "complete"
    assert (tmp_path / ".agenthicc" / "workflows" / "cloakbrowser_parse_fb.py").exists()


async def test_create_workflow_resume_revalidates_changed_staged_source(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion(_source(), n=1))
    runner, _app_state = _runner(tmp_path, processor, transport, _Approval(False))

    staged = await runner.run("Create a parser workflow and pause for review.")
    assert staged.artifact is not None
    Path(staged.artifact.staged_path).write_text("# changed after validation\n", encoding="utf-8")

    resumed_runner, _resumed_state = _runner(tmp_path, processor, MockTransport(), _Approval(True))
    result = await resumed_runner.resume(AuthoringResumeContext(staged.run_id))
    await processor.drain()

    assert result.status == "failed"
    assert result.error is not None
    assert "changed after its last validation" in result.error
    assert not (tmp_path / ".agenthicc" / "workflows" / "cloakbrowser_parse_fb.py").exists()


async def test_create_workflow_cancellation_leaves_resumable_staged_manifest(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(_completion(_source(), n=1))
    approval = _BlockingApproval()
    runner, app_state = _runner(tmp_path, processor, transport, approval)

    task = asyncio.create_task(runner.run("Create a parser workflow and then pause."))
    await asyncio.wait_for(approval.requested.wait(), timeout=5)
    run_id = app_state.workflow_run().run_id
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    staged_dir = tmp_path / ".agenthicc" / "authoring" / run_id
    assert (staged_dir / "manifest.json").exists()
    assert list(staged_dir.glob("*.py"))
    assert not (tmp_path / ".agenthicc" / "workflows" / "cloakbrowser_parse_fb.py").exists()

    resumed_runner, _resumed_state = _runner(tmp_path, processor, MockTransport(), _Approval(True))
    resumed = await resumed_runner.resume(AuthoringResumeContext(run_id))
    await processor.drain()

    assert resumed.status == "published", resumed.to_dict()


async def test_create_workflow_requires_explicit_approval_to_replace_existing_artifact(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".agenthicc" / "workflows" / "cloakbrowser_parse_fb.py"
    destination.parent.mkdir(parents=True)
    original = "# existing user workflow\n"
    destination.write_text(original, encoding="utf-8")
    transport = MockTransport()
    transport.queue_response(_completion(_source(), n=1))
    approval = _Approval(False)
    runner, _app_state = _runner(tmp_path, processor, transport, approval)

    result = await runner.run("Replace the existing parser workflow.")
    await processor.drain()

    assert result.status == "rejected"
    assert destination.read_text(encoding="utf-8") == original
    assert approval.requests
    request = approval.requests[0]
    assert request.tool_input["overwrite"] is True


async def test_headless_style_execution_emits_structured_authoring_result(
    tmp_path: Path, processor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing headless execution seam can run the builtin authoring plugin."""
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    transport.queue_response(
        _tool_completion(
            [
                (
                    "inspect_agenthicc_source",
                    {
                        "module": "agenthicc.workflows.code_plan.definition",
                        "max_chars": 12_000,
                    },
                )
            ],
            1,
        )
    )
    transport.queue_response(_completion("I inspected the current workflow API.", n=2))
    transport.queue_response(
        _tool_completion(
            [("complete_authoring_phase", {"summary": "The parser intent is explicit."})],
            3,
        )
    )
    transport.queue_response(_completion("Interpretation handed off.", n=4))
    transport.queue_response(
        _tool_completion(
            [
                (
                    "submit_generated_source",
                    {
                        "source": _source(),
                        "artifact_name": "cloakbrowser_parse_fb",
                        "artifact_description": "Parse Facebook with Cloakbrowser.",
                    },
                ),
                ("complete_authoring_phase", {"summary": "The source is ready for staging."}),
            ],
            5,
        )
    )
    transport.queue_response(_completion("Source handed off.", n=6))
    transport.queue_response(
        _tool_completion(
            [("complete_authoring_phase", {"summary": "The staged source is valid."})],
            7,
        )
    )
    transport.queue_response(_completion("Validation handed off.", n=8))
    transport.queue_response(
        _tool_completion([("complete_authoring_phase", {"summary": "Validation passed."})], 9)
    )
    transport.queue_response(_completion("Validation agent handed off.", n=10))
    transport.queue_response(_tool_completion([("request_publication_approval", {})], 11))
    transport.queue_response(_completion("Review handed off.", n=12))
    transport.queue_response(
        _tool_completion([("complete_authoring_phase", {"summary": "Publication is ready."})], 13)
    )
    transport.queue_response(_completion("Publication handed off.", n=14))
    transport.queue_response(
        _tool_completion(
            [("complete_authoring_phase", {"summary": "The workflow was published."})], 15
        )
    )
    transport.queue_response(_completion("Summary handed off.", n=16))
    approval = _Approval(True)
    runner, app_state = _runner(tmp_path, processor, transport, approval)
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
        project_plugins=[],
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
    completed = [
        event for event in processor.event_log if event.event_type == "WorkflowRunCompleted"
    ]
    assert completed[-1].payload["result"]["workflow"] == "create_workflow"
    assert (
        completed[-1]
        .payload["result"]["artifact"]["published_path"]
        .endswith("cloakbrowser_parse_fb.py")
    )


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
