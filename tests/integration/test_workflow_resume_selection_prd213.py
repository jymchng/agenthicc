"""Integration coverage for PRD-213 resume selection and its UI projection."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.tui.workspace.components import FooterComponent
from agenthicc.workflows.plugin import PhaseSpec, WorkflowContext, WorkflowPlugin

from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_latest_checkpoint_resume_rehydrates_selection_before_dispatch(
    tmp_path: Path,
) -> None:
    """A restarted TUI derives selection from disk, not its mode/default selector."""
    session, ctx, _workspace, _input = _make_session(initial_workflow="stale-workflow")
    conversation = SessionConversation.open(
        "prd213-restart",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    ctx.session_conversation = conversation
    ctx.session_memory = conversation.memory
    store = WorkflowCheckpointStore(conversation.conversation_id, root=tmp_path / "checkpoints")
    session._workflow_recovery = WorkflowRecoveryCoordinator(
        conversation.conversation_id,
        checkpoint_store=store,
    )

    class RecoveredWorkflow(WorkflowPlugin):
        name = "recovered_workflow"
        phases = [PhaseSpec(name="work")]

    handle = WorkflowRunHandle.create(
        run_id="restart-run",
        workflow=RecoveredWorkflow,
        conversation=conversation,
        intent="resume the saved work",
        checkpoint_store=store,
        provider_profile=ctx.cfg.execution.profile,
    )
    handle.attach_context(
        WorkflowContext(
            intent="resume the saved work",
            run_id=handle.run_id,
            workflow_name=RecoveredWorkflow.name,
            current_phase="work",
            phase_iteration=2,
        )
    )
    handle.update_phase("work", 0, 2)
    handle.request_pause()
    handle.mark_paused(reason="integration setup")
    handle.save_checkpoint(reason="integration setup")

    # Simulate a process restart: no attached handle, but a stale invocation
    # selection is still present in this new TUI session.
    session._workflow_handle = None
    ctx.workflow_registry.register(RecoveredWorkflow)
    started = asyncio.Event()

    async def wait_for_release(*_args: object, **_kwargs: object) -> None:
        started.set()
        await asyncio.Event().wait()

    session._resume_workflow_task = wait_for_release  # type: ignore[method-assign]
    published: list[tuple[str, dict[str, object]]] = []

    class SessionService:
        async def publish(self, *_args: object, **kwargs: object) -> None:
            payload = kwargs.get("payload", {})
            published.append((str(kwargs["kind"]), payload if isinstance(payload, dict) else {}))

    ctx.session_service = SessionService()

    try:
        assert session._handle_workflow_resume(None) is True
        # This assertion is synchronous with the accepted persisted transition:
        # it does not wait for the scheduled agent task to run.
        assert session._workflow_override == RecoveredWorkflow.name
        assert ctx.app_state.conversation.workflow_override() == RecoveredWorkflow.name
        assert session._workflow_handle is not None
        assert session._workflow_handle.run_id == "restart-run"

        footer = Console(record=True, width=120)
        footer.print(FooterComponent(ctx.app_state).render())
        assert f"⬡ {RecoveredWorkflow.name}" in footer.export_text()

        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert published == [
            (
                "workflow_resume_started",
                {
                    "run_id": "restart-run",
                    "workflow": RecoveredWorkflow.name,
                    "phase": "work",
                },
            )
        ]

        task = session._agent_task
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session._workflow_override == RecoveredWorkflow.name
        assert ctx.app_state.conversation.workflow_override() == RecoveredWorkflow.name
    finally:
        if session._agent_task is not None and not session._agent_task.done():
            session._agent_task.cancel()
            await asyncio.gather(session._agent_task, return_exceptions=True)
        session._release_workflow_claim(session._workflow_handle)
        conversation.close()


@pytest.mark.asyncio
async def test_continuation_resume_queues_message_before_resume_started() -> None:
    session, ctx, _workspace, _input = _make_session()
    session._set_workflow_override("stale-workflow")

    class RecoveredWorkflow(WorkflowPlugin):
        name = "continuation_workflow"
        phases = [PhaseSpec(name="work")]

    class Handle:
        run_id = "continuation-run"
        workflow_name = RecoveredWorkflow.name
        lifecycle = "paused"
        checkpoint_supported = True
        claim_owner_id = "already-owned"
        current_phase = "work"
        context = WorkflowContext(
            intent="continue",
            run_id="continuation-run",
            workflow_name=RecoveredWorkflow.name,
            current_phase="work",
        )

        def mark_resuming(self) -> None:
            self.lifecycle = "resuming"

        def persist_checkpoint(self, *, reason: str) -> None:
            assert reason == "resuming"

    session._publish_session_event = (  # type: ignore[method-assign]
        lambda kind, payload=None, *, turn_id=None: events.append((kind, payload or {}, turn_id))
    )
    events: list[tuple[str, dict[str, object], str | None]] = []
    session._activate_streaming_input = lambda: None  # type: ignore[method-assign]
    session._resume_workflow_task = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: asyncio.sleep(0)
    )
    ctx.workflow_registry.register(RecoveredWorkflow)
    session._workflow_handle = Handle()  # type: ignore[assignment]

    assert session._start_workflow_continuation("continue the saved work") is True
    assert session._workflow_override == RecoveredWorkflow.name
    assert ctx.app_state.conversation.workflow_override() == RecoveredWorkflow.name

    task = session._agent_task
    assert task is not None
    await task
    assert [event[0] for event in events] == ["turn_queued", "workflow_resume_started"]
    assert events[0][1]["text"] == "continue the saved work"
    assert events[1][1]["workflow"] == RecoveredWorkflow.name
