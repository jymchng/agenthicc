"""End-to-end lifecycle journey for a generated-workflow-shaped runner."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.phase_lifecycle import (
    PhaseAnnotation,
    checkpoint_phase_boundary,
    publish_phase_annotation,
)
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


@dataclasses.dataclass
class E2ELifecycleContext:
    intent: str
    run_id: str
    state: str = "first"
    phase_iteration: int = 0
    phase_attempts: dict[str, int] = dataclasses.field(default_factory=dict)
    completed_phases: list[str] = dataclasses.field(default_factory=list)
    shared_memory: object | None = None


class E2ELifecycleRunner(BaseWorkflowRunner):
    workflow_name = "lifecycle_e2e"

    def __init__(self, config: object, _mode_manager: object | None = None) -> None:
        self._cfg = config

    @staticmethod
    def _phase_names() -> tuple[str, ...]:
        return tuple(spec.name for spec in E2ELifecycleWorkflow.phases)

    async def run(self, intent: str) -> E2ELifecycleContext:
        handle = self._cfg.workflow_handle
        context = E2ELifecycleContext(
            intent=intent,
            run_id=handle.run_id,
            shared_memory=self._cfg.session_memory,
        )
        return await self._drive(context)

    async def resume(self, context: object) -> E2ELifecycleContext:
        assert isinstance(context, E2ELifecycleContext)
        context.shared_memory = self._cfg.session_memory
        return await self._drive(context)

    async def _drive(self, context: E2ELifecycleContext) -> E2ELifecycleContext:
        names = self._phase_names()
        handle = self._cfg.workflow_handle
        while context.state != "complete":
            phase = context.state
            index = names.index(phase)
            context.phase_iteration += 1
            context.phase_attempts[phase] = context.phase_attempts.get(phase, 0) + 1
            publish_phase_annotation(
                self._cfg,
                PhaseAnnotation(
                    workflow_name=self.workflow_name,
                    phase_name=phase,
                    phase_index=index,
                    total_phases=len(names),
                    run_id=context.run_id,
                    intent=context.intent,
                    model_id="e2e-model",
                    phase_iteration=context.phase_iteration,
                    phase_attempt=context.phase_attempts[phase],
                    plan_version="lifecycle_e2e.v1",
                ),
                context,
            )
            next_state = names[index + 1] if index + 1 < len(names) else "complete"
            context.completed_phases.append(phase)
            context.state = next_state
            checkpoint_phase_boundary(
                self._cfg,
                context,
                completed_phase=phase,
                next_phase=None if next_state == "complete" else next_state,
                phase_index=index if next_state == "complete" else names.index(next_state),
                phase_iteration=context.phase_iteration,
            )
        handle.attach_context(context)
        return context


class E2ELifecycleWorkflow(WorkflowPlugin):
    name = "lifecycle_e2e"
    description = "deterministic phase lifecycle E2E fixture"
    phases = [PhaseSpec(name="first", next="second"), PhaseSpec(name="second")]

    @classmethod
    def build_runner(cls, config: object, mode_manager: object | None) -> E2ELifecycleRunner:
        return E2ELifecycleRunner(config, mode_manager)

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        assert isinstance(context, E2ELifecycleContext)
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "state": context.state,
            "phase_iteration": context.phase_iteration,
            "phase_attempts": dict(context.phase_attempts),
            "completed_phases": list(context.completed_phases),
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls, payload: dict[str, object], memory: object | None = None
    ) -> E2ELifecycleContext:
        return E2ELifecycleContext(
            intent=str(payload["intent"]),
            run_id=str(payload["run_id"]),
            state=str(payload["state"]),
            phase_iteration=int(payload["phase_iteration"]),
            phase_attempts={
                str(key): int(value) for key, value in payload.get("phase_attempts", {}).items()
            }
            if isinstance(payload.get("phase_attempts"), dict)
            else {},
            completed_phases=[str(item) for item in payload.get("completed_phases", [])]
            if isinstance(payload.get("completed_phases"), list)
            else [],
            shared_memory=memory,
        )


def test_generated_shape_publishes_each_phase_and_resumes_from_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        conversation = SessionConversation.open(
            "lifecycle-e2e-session",
            max_tokens=10_000,
            journal_path=tmp_path / "conversation.jsonl",
        )
        try:
            store = WorkflowCheckpointStore("lifecycle-e2e-session", root=tmp_path / "sessions")
            phases: list[str] = []
            config = SimpleNamespace(
                workflow_handle=None,
                session_memory=conversation.memory,
                conversation_id=conversation.conversation_id,
                app_state=SimpleNamespace(
                    update_workflow_phase=lambda **data: phases.append(str(data["phase_name"]))
                ),
            )
            handle = WorkflowRunHandle.create(
                run_id="lifecycle-e2e-run",
                workflow=E2ELifecycleWorkflow,
                conversation=conversation,
                intent="run all phases",
                checkpoint_store=store,
            )
            config.workflow_handle = handle
            result = await E2ELifecycleRunner(config).run("run all phases")

            saved = store.load("lifecycle-e2e-run")
            assert saved is not None
            assert saved.reason == "phase_boundary:second:completed"
            assert saved.current_phase is None
            assert result.completed_phases == ["first", "second"]
            assert phases == ["first", "second"]

            reopened = SessionConversation.open(
                "lifecycle-e2e-session",
                max_tokens=10_000,
                journal_path=tmp_path / "conversation.jsonl",
            )
            try:
                restored = WorkflowRunHandle.from_checkpoint(
                    saved,
                    workflow=E2ELifecycleWorkflow,
                    conversation=reopened,
                    checkpoint_store=store,
                )
                assert isinstance(restored.context, E2ELifecycleContext)
                assert restored.context.shared_memory is reopened.memory
                assert restored.context.state == "complete"
            finally:
                reopened.close()
        finally:
            conversation.close()

    asyncio.run(scenario())
