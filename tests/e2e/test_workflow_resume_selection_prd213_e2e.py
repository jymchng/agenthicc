"""End-to-end TUI journey for PRD-213 without a live model provider."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.tui.runtime import RuntimeMode
from agenthicc.tui.workspace.components import FooterComponent
from agenthicc.workflows.plugin import PhaseSpec, WorkflowContext, WorkflowPlugin

from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_resume_selects_footer_and_next_turn_until_explicit_reset(
    tmp_path: Path,
) -> None:
    session, ctx, _workspace, _input = _make_session(initial_workflow="old_selection")
    conversation = SessionConversation.open(
        "prd213-e2e",
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
    ctx.app_state.active_mode.set(RuntimeMode("Plan", default_workflow="mode_default"))
    calls: list[tuple[str, str]] = []
    handle: WorkflowRunHandle

    class ResumedWorkflow(WorkflowPlugin):
        name = "checkpoint_workflow"
        phases = [PhaseSpec(name="work")]

        @classmethod
        def build_runner(cls, _config: object, _mode: object) -> object:
            class Runner:
                async def resume(self, context: object) -> object:
                    calls.append(("resume", "saved-run"))
                    handle.mark_terminal("complete")
                    handle.save_checkpoint(reason="e2e completed")
                    ctx.app_state.workflow_run.set(SimpleNamespace(status="complete"))
                    return context

                async def run(self, text: str) -> None:
                    calls.append(("run", text))
                    ctx.app_state.workflow_run.set(SimpleNamespace(status="running"))

            return Runner()

    handle = WorkflowRunHandle.create(
        run_id="saved-run",
        workflow=ResumedWorkflow,
        conversation=conversation,
        intent="finish saved work",
        checkpoint_store=store,
        provider_profile=ctx.cfg.execution.profile,
    )
    handle.attach_context(
        WorkflowContext(
            intent="finish saved work",
            run_id=handle.run_id,
            workflow_name=ResumedWorkflow.name,
            current_phase="work",
            phase_iteration=1,
        )
    )
    handle.update_phase("work", 0, 1)
    handle.request_pause()
    handle.mark_paused(reason="e2e setup")
    handle.save_checkpoint(reason="e2e setup")
    session._workflow_handle = handle
    ctx.workflow_registry.register(ResumedWorkflow)

    async def emit(*_args: object, **_kwargs: object) -> None:
        return None

    ctx.processor.emit = emit

    try:
        assert session.route("/workflow resume saved-run") is True
        assert session._workflow_override == ResumedWorkflow.name
        assert ctx.app_state.conversation.workflow_override() == ResumedWorkflow.name

        footer = Console(record=True, width=120)
        footer.print(FooterComponent(ctx.app_state).render())
        assert f"⬡ {ResumedWorkflow.name}" in footer.export_text()

        resume_task = session._agent_task
        assert resume_task is not None
        await resume_task
        assert calls == [("resume", "saved-run")]
        assert session._workflow_handle is None
        assert session._workflow_override == ResumedWorkflow.name
        assert ctx.app_state.conversation.workflow_override() == ResumedWorkflow.name

        # Avoid writing a new test run into the user's default session store;
        # the completed checkpoint has already been verified above.
        ctx.session_conversation = None
        session._workflow_recovery_records = {}
        session._workflow_recovery_errors = {}
        await session.run_turn("ordinary follow-up")
        assert calls[-1] == ("run", "ordinary follow-up")
        assert session._workflow_override == ResumedWorkflow.name

        session._reset_workflow_to_mode_default()
        assert session._workflow_override is None
        assert ctx.app_state.conversation.workflow_override() is None
    finally:
        session._release_workflow_claim(handle)
        conversation.close()
