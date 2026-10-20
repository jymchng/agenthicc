"""End-to-end recovery journey without a provider, browser, or network."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.create_workflow.definition import CreateWorkflow
from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner
from agenthicc.workflows.create_workflow.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
    PhaseArtifact,
)
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin
from agenthicc.workflows.registry import WorkflowRegistry


@dataclasses.dataclass
class E2EContext:
    intent: str
    run_id: str
    workflow_name: str
    current_phase: str
    phase_iteration: int
    shared_memory: object | None = None


class E2ERunner(BaseWorkflowRunner):
    resumed: list[E2EContext] = []

    async def run(self, intent: str) -> E2EContext:
        raise AssertionError("the recovery journey must call resume(), not run()")

    async def resume(self, context: object) -> E2EContext:
        assert isinstance(context, E2EContext)
        self.resumed.append(context)
        return context


class E2EWorkflow(WorkflowPlugin):
    name = "recovery_e2e"
    description = "deterministic recovery fixture"
    phases = [
        PhaseSpec(name="first", next="second"),
        PhaseSpec(name="second"),
    ]

    @classmethod
    def build_runner(cls, config: object, mode_manager: object) -> E2ERunner:
        return E2ERunner()

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        assert isinstance(context, E2EContext)
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "workflow_name": context.workflow_name,
            "current_phase": context.current_phase,
            "phase_iteration": context.phase_iteration,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls, payload: dict[str, object], memory: object | None = None
    ) -> E2EContext:
        return E2EContext(
            intent=str(payload["intent"]),
            run_id=str(payload["run_id"]),
            workflow_name=str(payload["workflow_name"]),
            current_phase=str(payload["current_phase"]),
            phase_iteration=int(payload["phase_iteration"]),
            shared_memory=memory,
        )

    @classmethod
    def checkpoint_context_owner_workflow_name(cls, context: object) -> str | None:
        if isinstance(context, E2EContext):
            return context.workflow_name
        return None


def test_process_restart_resume_calls_typed_runner_resume_once(tmp_path: Path) -> None:
    E2ERunner.resumed.clear()
    session_id = "e2e-recovery"
    conversation = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation-journal.jsonl",
    )
    try:
        store = WorkflowCheckpointStore(session_id, root=tmp_path / "sessions")
        from agenthicc.runners.workflow_handle import WorkflowRunHandle

        handle = WorkflowRunHandle.create(
            run_id="e2e-run",
            workflow=E2EWorkflow,
            conversation=conversation,
            intent="recover this workflow",
            checkpoint_store=store,
        )
        context = E2EContext(
            intent="recover this workflow",
            run_id="e2e-run",
            workflow_name=E2EWorkflow.name,
            current_phase="second",
            phase_iteration=2,
            shared_memory=conversation.memory,
        )
        handle.attach_context(context)
        handle.update_phase("second", 1, 2)
    finally:
        conversation.close()

    reopened = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation-journal.jsonl",
    )
    try:
        registry = WorkflowRegistry()
        registry.register(E2EWorkflow, source="project")
        coordinator = WorkflowRecoveryCoordinator(
            session_id,
            checkpoint_store=WorkflowCheckpointStore(session_id, root=tmp_path / "sessions"),
        )
        record = coordinator.inspect(
            workflow_registry=registry,
            conversation=reopened,
        )[0]
        restored = coordinator.rehydrate(
            record,
            workflow=E2EWorkflow,
            conversation=reopened,
            owner_id="e2e-owner",
        )
        runner = E2EWorkflow.build_runner(None, None)
        asyncio.run(runner.resume(restored.context))
        assert len(E2ERunner.resumed) == 1
        assert E2ERunner.resumed[0].current_phase == "second"
        assert E2ERunner.resumed[0].shared_memory is reopened.memory
        assert restored.lifecycle == "paused"
        restored.release_claim()
    finally:
        reopened.close()


def test_create_workflow_restart_resumes_generate_with_existing_authored_draft(
    tmp_path: Path,
) -> None:
    """A target workflow slug remains domain data across process-style recovery."""
    session_id = "e2e-create-workflow-recovery"
    run_id = "e2e-create-run"
    journal_path = tmp_path / "sessions" / session_id / "conversation-journal.jsonl"
    checkpoint_root = tmp_path / "sessions"
    draft = tmp_path / ".agenthicc" / "workflows" / ".drafts" / run_id / "tailor_resume"
    runner_path = draft / "runner.py"
    runner_path.parent.mkdir(parents=True)
    runner_path.write_text("# preserved draft\n", encoding="utf-8")
    original_draft = runner_path.read_bytes()

    first = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=journal_path,
    )
    from agenthicc.runners.workflow_handle import WorkflowRunHandle

    initial = WorkflowRunHandle.create(
        run_id=run_id,
        workflow=CreateWorkflow,
        conversation=first,
        intent="author tailor_resume",
        checkpoint_store=WorkflowCheckpointStore(session_id, root=checkpoint_root),
    )
    initial.attach_context(
        CreateWorkflowContext(
            intent="author tailor_resume",
            run_id=run_id,
            design="A resumable tailored workflow.",
            workflow_name="tailor_resume",
            generated_path=str(runner_path),
            generation_summary="Initial workflow draft written.",
            artifacts={
                "design": PhaseArtifact(
                    phase="design",
                    kind="design",
                    content="Approved design.",
                ),
                "generate": PhaseArtifact(
                    phase="generate",
                    kind="workflow_file",
                    content="Draft source.",
                    metadata={"path": str(runner_path)},
                ),
            },
            state=CreateWorkflowState.GENERATE,
            phase_iteration=2,
            phase_attempts={"design": 1, "generate": 2},
            completed_phases=["design"],
        )
    )
    initial.update_phase("generate", 1, 2)
    first.close()

    reopened = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=journal_path,
    )
    try:
        registry = WorkflowRegistry()
        registry.register(CreateWorkflow)
        store = WorkflowCheckpointStore(session_id, root=checkpoint_root)
        coordinator = WorkflowRecoveryCoordinator(session_id, checkpoint_store=store)
        record = coordinator.inspect(
            workflow_registry=registry,
            conversation=reopened,
        )[0]
        assert record.recoverable is True

        restored = coordinator.rehydrate(
            record,
            workflow=CreateWorkflow,
            conversation=reopened,
            owner_id="e2e-create-workflow-owner",
        )
        assert isinstance(restored.context, CreateWorkflowContext)

        dispatch_states: list[CreateWorkflowState] = []

        class _WorkflowRunProjection:
            def set(self, _value: object) -> None:
                return None

        class _Processor:
            async def emit(self, _event: object) -> None:
                return None

        runner = object.__new__(CreateWorkflowRunner)
        runner._cfg = SimpleNamespace(
            workflow_handle=restored,
            session_memory=reopened.memory,
            app_state=SimpleNamespace(workflow_run=_WorkflowRunProjection()),
            processor=_Processor(),
        )

        async def dispatch(
            state: CreateWorkflowState,
            _context: CreateWorkflowContext,
        ) -> CreateWorkflowState:
            dispatch_states.append(state)
            return CreateWorkflowState.COMPLETE

        runner._dispatch = dispatch  # type: ignore[method-assign]
        runner._reconcile_resume_cursor = lambda _context: None  # type: ignore[method-assign]
        runner._checkpoint_boundary = lambda *_args: None  # type: ignore[method-assign]

        resumed_context = asyncio.run(runner.resume(restored.context))

        assert dispatch_states == [CreateWorkflowState.GENERATE]
        assert resumed_context.run_id == run_id
        assert resumed_context.workflow_name == "tailor_resume"
        assert resumed_context.generated_path == str(runner_path)
        assert resumed_context.state is CreateWorkflowState.COMPLETE
        assert runner_path.read_bytes() == original_draft
        assert store.load(run_id).status == "complete"  # type: ignore[union-attr]
        restored.release_claim()
    finally:
        reopened.close()


def test_process_restart_resume_supports_repeated_interruptions(tmp_path: Path) -> None:
    """One durable run remains resumable across every pause/relaunch cycle."""

    session_id = "e2e-repeated-recovery"
    conversation = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation-journal.jsonl",
    )
    try:
        store = WorkflowCheckpointStore(session_id, root=tmp_path / "sessions")
        from agenthicc.runners.workflow_handle import WorkflowRunHandle

        handle = WorkflowRunHandle.create(
            run_id="repeated-run",
            workflow=E2EWorkflow,
            conversation=conversation,
            intent="pause me repeatedly",
            checkpoint_store=store,
        )
        context = E2EContext(
            intent="pause me repeatedly",
            run_id="repeated-run",
            workflow_name=E2EWorkflow.name,
            current_phase="second",
            phase_iteration=1,
            shared_memory=conversation.memory,
        )
        handle.attach_context(context)
        handle.update_phase("second", 1, 1)

        registry = WorkflowRegistry()
        registry.register(E2EWorkflow, source="project")
        coordinator = WorkflowRecoveryCoordinator(
            session_id,
            checkpoint_store=WorkflowCheckpointStore(session_id, root=tmp_path / "sessions"),
        )
        revisions: list[int] = []

        for cycle in range(3):
            record = coordinator.inspect(
                workflow_registry=registry,
                conversation=conversation,
            )[0]
            restored = coordinator.rehydrate(
                record,
                workflow=E2EWorkflow,
                conversation=conversation,
                owner_id=f"restart-owner-{cycle}",
            )
            assert restored.lifecycle == "paused"
            assert restored.context is not None
            assert restored.context.run_id == "repeated-run"

            # This is the durable portion of a process that was relaunched,
            # resumed, and interrupted again.  Save PAUSING before PAUSED so
            # a crash during pause cleanup remains recoverable too.
            restored.mark_resuming()
            restored.save_checkpoint(reason="resuming")
            assert restored.request_pause() is True
            restored.save_checkpoint(reason="pause_requested")
            restored.mark_paused(reason="escape")
            checkpoint = restored.save_checkpoint(reason="escape")
            revisions.append(checkpoint.revision)
            restored.release_claim()

        assert revisions == sorted(revisions)
        assert len(set(revisions)) == 3
        latest = store.load("repeated-run")
        assert latest is not None
        assert latest.status == "paused"
        assert latest.conversation_id == session_id
    finally:
        conversation.close()


def test_error_paused_run_survives_restart_and_keeps_one_revisioned_run(
    tmp_path: Path,
) -> None:
    """A provider error resumes exactly, then a second error advances its revision."""

    session_id = "e2e-error-recovery"
    conversation = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "error-journal.jsonl",
    )
    store = WorkflowCheckpointStore(session_id, root=tmp_path / "error-sessions")
    try:
        from agenthicc.runners.workflow_handle import WorkflowRunHandle

        handle = WorkflowRunHandle.create(
            run_id="error-run",
            workflow=E2EWorkflow,
            conversation=conversation,
            intent="recover after provider failure",
            checkpoint_store=store,
        )
        context = E2EContext(
            intent="recover after provider failure",
            run_id="error-run",
            workflow_name=E2EWorkflow.name,
            current_phase="second",
            phase_iteration=3,
            shared_memory=conversation.memory,
        )
        handle.attach_context(context)
        handle.update_phase("second", 1, 3)
        first = handle.finalize_failure(
            "provider timeout",
            kind="provider_transient",
        )
        assert first is not None
        assert first.status == "paused"
        assert first.failure_kind == "provider_transient"
    finally:
        conversation.close()

    reopened = SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "error-journal.jsonl",
    )
    try:
        registry = WorkflowRegistry()
        registry.register(E2EWorkflow, source="project")
        coordinator = WorkflowRecoveryCoordinator(
            session_id,
            checkpoint_store=WorkflowCheckpointStore(
                session_id,
                root=tmp_path / "error-sessions",
            ),
        )
        record = coordinator.inspect(
            workflow_registry=registry,
            conversation=reopened,
        )[0]
        assert record.recoverable is True
        assert record.failure_kind == "provider_transient"
        assert record.current_phase == "second"

        restored = coordinator.rehydrate(
            record,
            workflow=E2EWorkflow,
            conversation=reopened,
            owner_id="error-resume-owner",
        )
        runner = E2EWorkflow.build_runner(None, None)
        asyncio.run(runner.resume(restored.context))
        assert E2ERunner.resumed[-1].current_phase == "second"

        restored.mark_resuming()
        restored.attach_context(restored.context)
        second = restored.finalize_failure(
            "provider failed again",
            kind="provider_transient",
        )
        assert second is not None
        assert second.run_id == first.run_id
        assert second.revision > first.revision
        assert second.error_revision > first.error_revision
        restored.release_claim()
    finally:
        reopened.close()
