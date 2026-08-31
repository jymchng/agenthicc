"""Tests for workflow discovery and headless execution surfaces."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from agenthicc.cli.context import CLIContext, CLIFlags
from agenthicc.kernel import Event
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.plugin import (
    PhaseRunRecord,
    PhaseSpec,
    WorkflowContext,
    WorkflowPlugin,
    WorkflowRun,
)
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.unit


class _Processor:
    def __init__(self) -> None:
        self.event_log: list[Event] = []
        self.running = False
        self.drained = False
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.running = True
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()

    async def drain(self) -> None:
        self.drained = True


class _SessionLog:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _SessionMemory:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_plugin(name: str = "demo") -> type[WorkflowPlugin]:
    class _DemoWorkflow(WorkflowPlugin):
        phases = [PhaseSpec(name="plan", agent_type="planner")]

        @classmethod
        def build_runner(cls, config, mode_manager):
            class _Runner:
                async def run(self, intent: str) -> object:
                    assert config.processor.running is True
                    run_id = f"run-{len(config.processor.event_log) + 1}"
                    run = WorkflowRun(
                        run_id=run_id,
                        workflow_name=cls.name,
                        intent=intent,
                        current_phase=None,
                        phase_history=[
                            PhaseRunRecord(
                                phase_name="plan",
                                role="planner",
                                approved=True,
                                output_summary="done",
                                iteration=1,
                                duration_s=0.1,
                            )
                        ],
                        status="complete",
                    )
                    config.app_state.workflow_run.set(run)
                    config.processor.event_log.append(
                        Event.create(
                            "WorkflowPhaseCompleted",
                            {"run_id": run_id, "phase_name": "plan"},
                        )
                    )
                    return SimpleNamespace(run_id=run_id)

                async def resume(self, context: object) -> None:
                    return None

            return _Runner()

    _DemoWorkflow.name = name
    return _DemoWorkflow


def _make_session(plugin: type[WorkflowPlugin]) -> SimpleNamespace:
    processor = _Processor()
    registry = WorkflowRegistry()
    registry.register(plugin)
    app_state = TUIAppState.create()
    return SimpleNamespace(
        processor=processor,
        app_state=app_state,
        session_log=_SessionLog(),
        approval_svc=None,
        mode_manager=None,
        workflow_registry=registry,
        agent_runner=object(),
        session_memory=_SessionMemory(),
        skills={},
        project_plugins=SimpleNamespace(all_tools=[]),
        mcp_registry=None,
        mention_cache=object(),
        agents_registry=object(),
        cfg=SimpleNamespace(workflows={}),
        session_id="session-1",
        memory_router=None,
        semantic_index=None,
        console=None,
    )


async def test_execute_workflow_uses_plugin_factory_and_reports_phases() -> None:
    from agenthicc.runners.headless import execute_workflow

    session = _make_session(_make_plugin())
    task = asyncio.create_task(session.processor.run())
    try:
        await asyncio.sleep(0)
        result = await execute_workflow(session, "demo", "do the work")
    finally:
        await session.processor.stop()
        await task

    assert result.to_dict() == {
        "event_type": "WorkflowRunCompleted",
        "session_id": "session-1",
        "workflow": "demo",
        "run_id": "run-1",
        "status": "complete",
        "phases": ["plan"],
        "error": None,
    }
    assert session.processor.drained is True


async def test_execute_workflow_resume_uses_existing_run_and_runner_resume(
    tmp_path, monkeypatch
) -> None:
    from agenthicc.runners.headless import execute_workflow
    from agenthicc.runners.workflow_handle import WorkflowRunHandle

    # Load the recovery module before replacing the store constructor.  Its
    # type alias is intentionally imported at module scope; importing it only
    # after the patch would leak the test lambda into later TUI tests.
    from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator  # noqa: F401

    resumed_contexts: list[object] = []

    class ResumeWorkflow(WorkflowPlugin):
        name = "resume_demo"
        phases = [PhaseSpec(name="plan", agent_type="planner")]

        @classmethod
        def build_runner(cls, config, mode_manager):
            class _Runner:
                async def run(self, intent: str) -> object:
                    raise AssertionError("an existing workflow must not call run()")

                async def resume(self, context: object) -> object:
                    resumed_contexts.append(context)
                    config.app_state.workflow_run.set(
                        WorkflowRun(
                            run_id="resume-run",
                            workflow_name=cls.name,
                            intent="original intent",
                            current_phase=None,
                            status="complete",
                        )
                    )
                    return context

            return _Runner()

    session = _make_session(ResumeWorkflow)
    session.cfg.execution = SimpleNamespace(profile="")
    conversation = SessionConversation.open(
        "session-1",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    store = WorkflowCheckpointStore("session-1", root=tmp_path / "sessions")
    handle = WorkflowRunHandle.create(
        run_id="resume-run",
        workflow=ResumeWorkflow,
        conversation=conversation,
        intent="original intent",
        checkpoint_store=store,
    )
    handle.attach_context(
        WorkflowContext(
            intent="original intent",
            run_id="resume-run",
            workflow_name=ResumeWorkflow.name,
            current_phase="plan",
            phase_iteration=2,
        )
    )
    handle.update_phase("plan", 0, 2)
    handle.release_claim()
    session.session_conversation = conversation
    monkeypatch.setattr(
        "agenthicc.runners.workflow_checkpoint_store.WorkflowCheckpointStore",
        lambda _session_id: store,
    )
    processor_task = asyncio.create_task(session.processor.run())
    try:
        await asyncio.sleep(0)
        result = await execute_workflow(
            session,
            ResumeWorkflow.name,
            "continue",
            resume_run_id="resume-run",
        )
    finally:
        await session.processor.stop()
        await processor_task
        conversation.close()

    assert result.run_id == "resume-run"
    assert result.status == "complete"
    assert len(resumed_contexts) == 1
    assert isinstance(resumed_contexts[0], WorkflowContext)
    assert resumed_contexts[0].current_phase == "plan"
    checkpoint = store.load("resume-run")
    assert checkpoint is not None
    assert checkpoint.status == "complete"


async def test_execute_workflow_pauses_429_at_typed_phase_then_resumes_same_run(
    tmp_path, monkeypatch
) -> None:
    from agenthicc.runners.headless import execute_workflow
    from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator  # noqa: F401

    resumed_contexts: list[object] = []

    class RateLimitedWorkflow(WorkflowPlugin):
        name = "rate_limited_demo"
        phases = [PhaseSpec(name="init"), PhaseSpec(name="architecture")]

        @classmethod
        def build_runner(cls, config, mode_manager):
            class _Runner:
                async def run(self, intent: str) -> object:
                    context = config.workflow_handle.context
                    assert isinstance(context, WorkflowContext)
                    context.current_phase = "architecture"
                    context.phase_iteration = 4
                    config.workflow_handle.attach_context(context)
                    raise RuntimeError("429 Rate limit exceeded")

                async def resume(self, context: object) -> object:
                    resumed_contexts.append(context)
                    config.app_state.workflow_run.set(
                        WorkflowRun(
                            run_id=context.run_id,
                            workflow_name=cls.name,
                            intent="original intent",
                            current_phase=None,
                            status="complete",
                        )
                    )
                    return context

            return _Runner()

    session = _make_session(RateLimitedWorkflow)
    session.cfg.execution = SimpleNamespace(profile="")
    conversation = SessionConversation.open(
        "session-1",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    store = WorkflowCheckpointStore("session-1", root=tmp_path / "sessions")
    monkeypatch.setattr(
        "agenthicc.runners.workflow_checkpoint_store.WorkflowCheckpointStore",
        lambda _session_id: store,
    )
    session.session_conversation = conversation
    processor_task = asyncio.create_task(session.processor.run())
    try:
        await asyncio.sleep(0)
        failed = await execute_workflow(session, RateLimitedWorkflow.name, "original intent")
        assert failed.status == "paused"
        assert failed.error is not None and "429" in failed.error
        assert failed.run_id

        paused = store.load(failed.run_id)
        assert paused is not None
        assert paused.status == "paused"
        assert paused.failure_kind == "provider_transient"
        assert paused.current_phase == "architecture"
        assert paused.phase_index == 1
        assert paused.phase_iteration == 4

        resumed = await execute_workflow(
            session,
            RateLimitedWorkflow.name,
            "continue",
            resume_run_id=failed.run_id,
        )
        assert resumed.status == "complete"
        assert resumed.run_id == failed.run_id
        assert len(resumed_contexts) == 1
        assert isinstance(resumed_contexts[0], WorkflowContext)
        assert resumed_contexts[0].current_phase == "architecture"
        completed = store.load(failed.run_id)
        assert completed is not None
        assert completed.status == "complete"
    finally:
        await session.processor.stop()
        await processor_task
        conversation.close()


async def test_execute_workflow_rejects_unknown_and_empty_intents() -> None:
    from agenthicc.runners.headless import execute_workflow

    session = _make_session(_make_plugin())
    with pytest.raises(ValueError, match="Unknown workflow"):
        await execute_workflow(session, "missing", "task")
    with pytest.raises(ValueError, match="must not be empty"):
        await execute_workflow(session, "demo", " ")


async def test_headless_setup_failure_persists_diagnostic_run(tmp_path, monkeypatch) -> None:
    from agenthicc.runners.headless import execute_workflow

    class BrokenWorkflow(WorkflowPlugin):
        name = "broken_setup"
        phases = [PhaseSpec(name="plan", agent_type="planner")]

        @classmethod
        def build_params(cls, _source):
            raise RuntimeError("provider profile is invalid")

    session = _make_session(BrokenWorkflow)
    session.session_id = "headless-setup"
    session.cfg.execution = SimpleNamespace(profile="default")
    conversation = SessionConversation.open(
        session.session_id,
        max_tokens=2_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    store = WorkflowCheckpointStore(session.session_id, root=tmp_path / "sessions")
    monkeypatch.setattr(
        "agenthicc.runners.workflow_checkpoint_store.WorkflowCheckpointStore",
        lambda _session_id: store,
    )
    session.session_conversation = conversation
    try:
        result = await execute_workflow(session, BrokenWorkflow.name, "run setup")
        assert result.status == "failed"
        assert result.run_id
        fallback = store.load_recovery_error(result.run_id)
        assert fallback is not None
        assert fallback["diagnostic_only"] is True
        assert fallback["resumable"] is False
        assert fallback["failure_kind"] == "configuration"
    finally:
        conversation.close()


async def test_headless_setup_failure_preserves_a_resumable_bootstrap_context(
    tmp_path, monkeypatch
) -> None:
    from agenthicc.runners.headless import execute_workflow

    class _SetupFailureWorkflow(WorkflowPlugin):
        name = "setup_failure"
        phases = [PhaseSpec(name="plan", agent_type="planner")]

        @classmethod
        def create_initial_context(cls, intent, run_id, memory=None):
            return WorkflowContext(
                intent=intent,
                run_id=run_id,
                workflow_name=cls.name,
                current_phase="plan",
            )

        @classmethod
        def build_params(cls, _source):
            raise RuntimeError("provider configuration unavailable")

    session = _make_session(_SetupFailureWorkflow)
    session.cfg.execution = SimpleNamespace(profile="")
    conversation = SessionConversation.open(
        "session-1",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation-journal.jsonl",
    )
    store = WorkflowCheckpointStore("session-1", root=tmp_path / "sessions")
    session.session_conversation = conversation
    monkeypatch.setattr(
        "agenthicc.runners.workflow_checkpoint_store.WorkflowCheckpointStore",
        lambda _session_id: store,
    )
    try:
        result = await execute_workflow(session, "setup_failure", "configure the workflow")
        assert result.status == "paused"
        assert result.run_id
        checkpoint = store.load(result.run_id)
        assert checkpoint is not None
        assert checkpoint.context_ready is True
        assert checkpoint.status == "paused"
        assert checkpoint.failure_kind == "configuration"
    finally:
        conversation.close()


async def test_headless_approval_defaults_to_deny() -> None:
    from agenthicc.runners.headless import _HeadlessApprovalService

    response = await _HeadlessApprovalService(False).request_approval(object())

    assert response.allowed is False
    assert "dangerously-skip-permissions" in response.message


async def test_run_headless_workflow_starts_and_closes_session(monkeypatch) -> None:
    from agenthicc.runners import headless

    session = _make_session(_make_plugin())

    async def _build(*args, **kwargs):
        assert kwargs["headless"] is True
        return session

    monkeypatch.setattr("agenthicc.runners.tui_session._build_session_context", _build)

    result = await headless.run_headless_workflow(
        CLIContext(flags=CLIFlags(dangerously_skip_permissions=True)),
        "demo",
        "run once",
    )

    assert result.status == "complete"
    assert session.session_log.closed is True
    assert session.processor.drained is True
    assert session.session_memory.closed is True
    assert getattr(session.approval_svc, "_allow") is True


async def test_headless_workflow_stream_runs_each_nonempty_stdin_line(monkeypatch, capsys) -> None:
    from agenthicc.runners import headless

    session = _make_session(_make_plugin())
    monkeypatch.setattr(
        "agenthicc.runners.tui_session._build_session_context",
        lambda *args, **kwargs: _async_session(session, kwargs),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("first task\n\nsecond task\n"))

    await headless._run_headless_workflow_stream(
        CLIContext(
            workflow_name="demo",
            mode_name="Plan",
            flags=CLIFlags(dangerously_skip_permissions=True),
        )
    )

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == {
        "status": "ready",
        "mode": "headless",
        "workflow": "demo",
        "session_id": "session-1",
    }
    assert [line["status"] for line in lines[1:]] == ["complete", "complete"]
    assert [line["phases"] for line in lines[1:]] == [["plan"], ["plan"]]
    assert session.session_log.closed is True


async def _async_session(session: SimpleNamespace, kwargs: dict[str, object]) -> SimpleNamespace:
    assert kwargs["headless"] is True
    assert kwargs["mode_name"] == "Plan"
    assert kwargs["workflow_name"] == "demo"
    return session


def test_workflows_list_json_includes_phase_topology(monkeypatch, capsys) -> None:
    from agenthicc.cli.commands import workflows

    registry = WorkflowRegistry()
    registry.register(_make_plugin(), source="project", path=".agenthicc/workflows/demo.py")
    monkeypatch.setattr(workflows, "_workflow_registry", lambda: registry)

    workflows.workflows_list(CLIContext(), True)

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "demo"
    assert payload[0]["source"] == "project"
    assert payload[0]["phases"][0]["name"] == "plan"


def test_workflows_cli_parser_supports_run_and_headless_workflow(monkeypatch) -> None:
    from agenthicc.cli.parser import _parse_args

    monkeypatch.setattr(
        "sys.argv",
        [
            "agenthicc",
            "workflows",
            "run",
            "demo",
            "--intent",
            "do work",
            "--json",
        ],
    )
    args = _parse_args()
    assert args._entry.path == ("workflows", "run")
    assert args.workflow_name == "demo"
    assert args.intent == "do work"
    assert args.json is True

    monkeypatch.setattr(
        "sys.argv",
        ["agenthicc", "--headless", "--mode", "Plan", "--workflow", "demo"],
    )
    args = _parse_args()
    assert args.headless is True
    assert args.mode_name == "Plan"
    assert args.workflow_name == "demo"


async def test_workflows_run_cli_serializes_result(monkeypatch, capsys) -> None:
    from agenthicc.cli.commands.workflows import workflows_run
    from agenthicc.runners.headless import WorkflowExecutionResult

    async def _run(ctx, workflow_name, intent):
        assert workflow_name == "demo"
        assert intent == "do work"
        return WorkflowExecutionResult("session-1", "demo", "run-1", "complete", ("plan",))

    monkeypatch.setattr("agenthicc.runners.headless.run_headless_workflow", _run)
    await workflows_run(CLIContext(), "demo", "do work", True)

    assert json.loads(capsys.readouterr().out)["status"] == "complete"
