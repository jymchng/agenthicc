"""End-to-end recovery journey without a provider, browser, or network."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.base_runner import BaseWorkflowRunner
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
