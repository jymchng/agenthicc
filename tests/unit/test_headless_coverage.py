"""Focused coverage for headless policy and validation boundaries."""

from __future__ import annotations

import io
from types import SimpleNamespace
from pathlib import Path

import pytest

from agenthicc.cli.context import CLIContext
from agenthicc.runners.headless import (
    WorkflowExecutionResult,
    _HeadlessApprovalService,
    _optional_request_field,
    _resolve_headless_session,
    _workflow_failure_kind,
    execute_workflow,
)


def _workflow_session(plugin: type[object], tmp_path: object) -> tuple[object, object, object]:
    from tests.unit.test_workflow_cli import _make_session
    from agenthicc.runners.session_conversation import SessionConversation
    from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore

    session = _make_session(plugin)
    session.cfg.execution = SimpleNamespace(profile="default")
    conversation = SessionConversation.open(
        session.session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",  # type: ignore[operator]
    )
    store = WorkflowCheckpointStore(session.session_id, root=tmp_path / "sessions")  # type: ignore[operator]
    session.session_conversation = conversation
    return session, conversation, store


@pytest.mark.asyncio
async def test_headless_approval_policy_and_result_serialization() -> None:
    denied = await _HeadlessApprovalService(False).request_approval(object())
    assert denied.allowed is False
    assert "dangerously-skip-permissions" in denied.message
    questions = await _HeadlessApprovalService(True).request_approval(
        SimpleNamespace(kind="questions", request_id="question-1")
    )
    assert questions.allowed is False
    assert questions.outcome == "cancelled"
    workspace = await _HeadlessApprovalService(True).request_approval(
        SimpleNamespace(workspace_access=object())
    )
    assert workspace.allowed is False
    allowed = await _HeadlessApprovalService(True).request_approval(object())
    assert allowed.allowed is True
    assert _HeadlessApprovalService(True).respond(True) is None
    assert _HeadlessApprovalService(True).reset_turn_memory() is None
    assert _optional_request_field(object(), "missing", "default") == "default"
    assert _workflow_failure_kind(RuntimeError("profile is invalid")) == "configuration"

    result = WorkflowExecutionResult(
        "session",
        "goal_flow",
        "run",
        "complete",
        ("plan",),
        phase_metadata={"plan": {"approved": True}},
    )
    payload = result.to_dict()
    assert payload["event_type"] == "WorkflowRunCompleted"
    assert payload["phases"] == ["plan"]
    assert payload["phase_metadata"] == {"plan": {"approved": True}}


def test_headless_session_resolution_preserves_explicit_resume() -> None:
    resume_id, lease = _resolve_headless_session(CLIContext(resume_id="session"))
    assert resume_id == "session"
    assert lease is None


def test_headless_session_and_workflow_resume_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.runners import headless

    class _OpenCoordinator:
        def select_latest_for_cwd(self, _cwd: object, *, entrypoint: str) -> tuple[str, str]:
            assert entrypoint == "headless"
            return ("selected-session", "lease")

    monkeypatch.setattr("agenthicc.runners.session_lease.SessionOpenCoordinator", _OpenCoordinator)
    selected, lease = headless._resolve_headless_session(CLIContext(continue_session=True))
    assert selected == "selected-session"
    assert lease == "lease"

    class _Recovery:
        def __init__(self, _session_id: str) -> None:
            pass

        def select_for_resume(self, **kwargs: object) -> SimpleNamespace:
            assert kwargs["workflow_name"] == "goal_flow"
            return SimpleNamespace(run_id="workflow-run")

    monkeypatch.setattr(
        "agenthicc.runners.workflow_recovery.WorkflowRecoveryCoordinator", _Recovery
    )
    session = SimpleNamespace(
        session_id="selected-session",
        session_conversation=object(),
        workspace_scope=SimpleNamespace(primary_root="/repo"),
        workflow_registry=object(),
        cfg=SimpleNamespace(execution=SimpleNamespace(profile="profile")),
    )
    assert headless._select_headless_workflow_resume(session, "goal_flow") == "workflow-run"
    session.session_conversation = None
    assert headless._select_headless_workflow_resume(session, "goal_flow") is None
    resume_id, lease = _resolve_headless_session(CLIContext(continue_session=False))
    assert resume_id is None
    assert lease is None


@pytest.mark.asyncio
async def test_execute_workflow_rejects_invalid_requests_before_provider_calls() -> None:
    class _Registry:
        def get(self, _name: str) -> None:
            return None

        def names(self) -> tuple[str, ...]:
            return ("available",)

    session = SimpleNamespace(workflow_registry=_Registry(), agent_runner=None)
    with pytest.raises(ValueError, match="Unknown workflow"):
        await execute_workflow(session, "missing", "intent")

    class _Plugin:
        name = "available"

    class _AvailableRegistry(_Registry):
        def get(self, _name: str) -> _Plugin:
            return _Plugin()

    session.workflow_registry = _AvailableRegistry()
    with pytest.raises(RuntimeError, match="No LLM configured"):
        await execute_workflow(session, "available", "intent")
    session.agent_runner = object()
    with pytest.raises(ValueError, match="must not be empty"):
        await execute_workflow(session, "available", "  ")
    with pytest.raises(ValueError, match="durable session conversation"):
        await execute_workflow(session, "available", "intent", resume_run_id="run")


@pytest.mark.asyncio
async def test_headless_close_releases_optional_session_resources(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.runners.headless import _close_headless_session

    class _TaskOwner:
        released = False

        def release(self) -> None:
            self.released = True

    class _Processor:
        async def drain(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    class _Closable:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _AsyncClosable:
        def __init__(self) -> None:
            self.closed = False

        async def close_session(self) -> None:
            self.closed = True

        async def close(self) -> None:
            self.closed = True

        async def shutdown(self) -> None:
            self.closed = True

    async def never() -> None:
        await __import__("asyncio").Event().wait()

    import asyncio

    projection_task = asyncio.create_task(never())
    processor_task = asyncio.create_task(never())
    log = _Closable()
    memory = _Closable()
    mcp = _AsyncClosable()
    browser = _AsyncClosable()
    terminal = _AsyncClosable()
    service = _AsyncClosable()
    owner = _TaskOwner()
    session = SimpleNamespace(
        kernel_projection_task=projection_task,
        processor=_Processor(),
        session_log=log,
        session_memory=memory,
        mcp_registry=mcp,
        browser_manager=browser,
        terminal_manager=terminal,
        session_service=service,
        owner_lease=owner,
        session_id="session",
    )
    metadata: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "agenthicc.runners.tui_session._write_cassette_meta",
        lambda *args: metadata.append(args),
    )
    await _close_headless_session(session, processor_task, tmp_path)  # type: ignore[arg-type]
    assert log.closed and memory.closed
    assert mcp.closed and browser.closed and terminal.closed and service.closed
    assert owner.released
    assert metadata


@pytest.mark.asyncio
async def test_execute_workflow_publishes_success_and_paused_failure_events(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_workflow_cli import _make_plugin
    from agenthicc.workflows.plugin import PhaseSpec, WorkflowContext, WorkflowPlugin

    class _Service:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def publish(self, _session_id: str, *, kind: str, **_kwargs: object) -> None:
            self.events.append(kind)

    success, conversation, store = _workflow_session(_make_plugin(), tmp_path)
    success.session_service = _Service()
    monkeypatch.setattr(
        "agenthicc.runners.workflow_checkpoint_store.WorkflowCheckpointStore",
        lambda _session_id: store,
    )
    task = __import__("asyncio").create_task(success.processor.run())
    try:
        await __import__("asyncio").sleep(0)
        result = await execute_workflow(success, "demo", "success")
        assert result.status == "complete", result.error
        assert "workflow_started" in success.session_service.events
        assert "workflow_run_completed" in success.session_service.events
    finally:
        await success.processor.stop()
        await task
        conversation.close()
    monkeypatch.undo()

    class _FailingWorkflow(WorkflowPlugin):
        name = "headless_failure"
        phases = [PhaseSpec(name="plan")]

        @classmethod
        def build_runner(cls, _config: object, _mode_manager: object) -> object:
            class _Runner:
                async def run(self, _intent: str) -> object:
                    raise RuntimeError("429 provider failure")

                async def resume(self, _context: object) -> object:
                    raise AssertionError("not resuming")

            return _Runner()

        @classmethod
        def create_initial_context(cls, intent: str, run_id: str, _memory: object) -> object:
            return WorkflowContext(
                intent=intent, run_id=run_id, workflow_name=cls.name, current_phase="plan"
            )

    failure, failure_conversation, failure_store = _workflow_session(_FailingWorkflow, tmp_path)
    failure.session_service = _Service()
    monkeypatch.setattr(
        "agenthicc.runners.workflow_checkpoint_store.WorkflowCheckpointStore",
        lambda _session_id: failure_store,
    )
    task = __import__("asyncio").create_task(failure.processor.run())
    try:
        await __import__("asyncio").sleep(0)
        failed = await execute_workflow(failure, _FailingWorkflow.name, "failure")
        assert failed.status == "paused"
        assert "workflow_paused_after_error" in failure.session_service.events
        assert "workflow_run_failed" in failure.session_service.events
    finally:
        await failure.processor.stop()
        await task
        failure_conversation.close()


@pytest.mark.asyncio
async def test_plain_headless_stdin_runner_handles_eof_in_isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.runners.headless import _run_headless

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    await _run_headless(CLIContext())
