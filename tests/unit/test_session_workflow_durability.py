"""Unit coverage for session-wide conversation and workflow checkpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.memory.journal import ConversationJournal, fold_resume_state
from agenthicc.runners.session_conversation import ConversationBusyError, SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.workflows.checkpoint import (
    CheckpointValidationError,
    WorkflowCheckpoint,
    context_from_payload,
    context_to_payload,
    workflow_fingerprint,
)
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.create_workflow.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
    PhaseArtifact,
)
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

pytestmark = pytest.mark.unit


def _conversation(tmp_path: Path, session_id: str = "session-1") -> SessionConversation:
    return SessionConversation.open(
        session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )


def test_one_session_conversation_rehydrates_direct_and_workflow_history(tmp_path: Path) -> None:
    conversation = _conversation(tmp_path)
    conversation.memory.add_user("before plan")
    conversation.memory.add_assistant({"content": "direct answer"})
    assert conversation.conversation_id == "session-1"
    assert conversation.cursor == 2
    conversation.close()

    resumed = _conversation(tmp_path)
    assert resumed.messages == [
        {"role": "user", "content": "before plan"},
        {"content": "direct answer"},
    ]
    resumed.close()


@pytest.mark.asyncio
async def test_conversation_lock_rejects_cross_owner_mutation(tmp_path: Path) -> None:
    conversation = _conversation(tmp_path)
    await conversation.acquire("workflow-run")
    with pytest.raises(ConversationBusyError):
        await conversation.acquire("other-run")
    conversation.release("workflow-run")
    await conversation.acquire("other-run")
    conversation.release("other-run")
    conversation.close()


def test_journal_abort_closes_resume_marker(tmp_path: Path) -> None:
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    journal.turn_started("turn-1", "intent", 0)
    journal.turn_aborted("turn-1", reason="escape")
    journal.close()
    assert fold_resume_state(tmp_path / "journal.jsonl") is None


def test_code_plan_context_checkpoint_excludes_memory_and_restores_state() -> None:
    memory = object()
    context = CodePlanContext(
        intent="implement feature",
        run_id="run-1",
        plan="step one",
        state=CodePlanState.REVIEW,
        phase_iteration=3,
    )
    payload = context_to_payload(context)
    assert "shared_memory" not in json.dumps(payload)
    restored = context_from_payload(payload, memory=memory)  # type: ignore[arg-type]
    assert isinstance(restored, CodePlanContext)
    assert restored.state is CodePlanState.REVIEW
    assert restored.phase_iteration == 3
    assert restored.shared_memory is memory


def test_create_workflow_context_checkpoint_preserves_artifacts() -> None:
    context = CreateWorkflowContext(
        intent="author a workflow",
        run_id="run-2",
        workflow_name="doc_review",
        state=CreateWorkflowState.VALIDATE,
        artifacts={
            "design": PhaseArtifact(
                phase="design",
                kind="design",
                content="draft then review",
                metadata={"approved": True},
            )
        },
    )
    restored = context_from_payload(context_to_payload(context))
    assert isinstance(restored, CreateWorkflowContext)
    assert restored.state is CreateWorkflowState.VALIDATE
    assert restored.artifacts["design"].metadata == {"approved": True}


def test_checkpoint_store_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    store = WorkflowCheckpointStore("session-1", root=tmp_path)
    checkpoint = WorkflowCheckpoint(
        run_id="run-1",
        workflow_name="code_plan",
        conversation_id="session-1",
        intent="implement feature",
        status="paused",
        current_phase="execute",
        phase_index=1,
        phase_iteration=2,
        conversation_cursor=7,
        context={"kind": "CodePlanContext", "fields": {}},
        plugin_fingerprint="fingerprint",
    )
    path = store.save(checkpoint)
    assert store.load("run-1") == checkpoint
    assert path.stat().st_mode & 0o777 == 0o600

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["intent"] = "tampered"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointValidationError):
        store.load("run-1")


def test_workflow_handle_rehydrates_typed_context_and_rejects_plugin_drift(tmp_path: Path) -> None:
    class Plugin(WorkflowPlugin):
        name = "checkpoint_plugin"
        phases = [PhaseSpec(name="plan")]

    conversation = _conversation(tmp_path)
    store = WorkflowCheckpointStore("session-1", root=tmp_path / "checkpoints")
    handle = WorkflowRunHandle.create(
        run_id="run-3",
        workflow=Plugin,
        conversation=conversation,
        intent="intent",
        checkpoint_store=store,
    )
    handle.attach_context(
        CodePlanContext(
            intent="intent",
            run_id="run-3",
            state=CodePlanState.EXECUTE,
        )
    )
    handle.request_pause()
    handle.mark_paused()
    checkpoint = handle.save_checkpoint(reason="escape")
    restored = WorkflowRunHandle.from_checkpoint(
        checkpoint,
        workflow=Plugin,
        conversation=conversation,
        checkpoint_store=store,
    )
    assert isinstance(restored.context, CodePlanContext)
    assert restored.context.state is CodePlanState.EXECUTE
    assert restored.conversation is conversation
    assert workflow_fingerprint(Plugin) == checkpoint.plugin_fingerprint

    class ChangedPlugin(WorkflowPlugin):
        name = "checkpoint_plugin"
        phases = [PhaseSpec(name="different")]

    with pytest.raises(ValueError, match="fingerprint"):
        WorkflowRunHandle.from_checkpoint(
            checkpoint,
            workflow=ChangedPlugin,
            conversation=conversation,
            checkpoint_store=store,
        )
    conversation.close()


def test_custom_checkpoint_hooks_are_used_and_share_session_memory(tmp_path: Path) -> None:
    class CustomContext:
        def __init__(self, state: str, memory: object) -> None:
            self.state = state
            self.shared_memory = memory

    class CustomPlugin(WorkflowPlugin):
        name = "custom_checkpoint"
        phases = [PhaseSpec(name="work")]

        @classmethod
        def checkpoint_context_to_payload(cls, context: object) -> dict[str, object] | None:
            if not isinstance(context, CustomContext):
                return None
            return {"state": context.state}

        @classmethod
        def checkpoint_context_from_payload(
            cls, payload: dict[str, object], memory: object | None = None
        ) -> object:
            return CustomContext(str(payload["state"]), memory)

    conversation = _conversation(tmp_path)
    store = WorkflowCheckpointStore("session-1", root=tmp_path / "custom")
    handle = WorkflowRunHandle.create(
        run_id="custom-run",
        workflow=CustomPlugin,
        conversation=conversation,
        intent="custom",
        checkpoint_store=store,
    )
    handle.attach_context(CustomContext("work", conversation.memory))
    handle.request_pause()
    handle.mark_paused()
    checkpoint = handle.save_checkpoint(reason="escape")
    restored = WorkflowRunHandle.from_checkpoint(
        checkpoint,
        workflow=CustomPlugin,
        conversation=conversation,
        checkpoint_store=store,
    )
    assert isinstance(restored.context, CustomContext)
    assert restored.context.state == "work"
    assert restored.context.shared_memory is conversation.memory
    conversation.close()


def test_checkpoint_rejects_memory_older_than_cursor(tmp_path: Path) -> None:
    class Plugin(WorkflowPlugin):
        name = "cursor_plugin"
        phases = [PhaseSpec(name="work")]

    conversation = _conversation(tmp_path)
    store = WorkflowCheckpointStore("session-1", root=tmp_path / "cursor")
    checkpoint = WorkflowCheckpoint(
        run_id="cursor-run",
        workflow_name=Plugin.name,
        conversation_id="session-1",
        intent="cursor",
        status="paused",
        current_phase="work",
        phase_index=0,
        phase_iteration=1,
        conversation_cursor=1,
        context={"kind": "WorkflowContext", "fields": {}},
        plugin_fingerprint=workflow_fingerprint(Plugin),
    )
    store.save(checkpoint)
    loaded = store.load("cursor-run")
    assert loaded is not None
    with pytest.raises(ValueError, match="older"):
        WorkflowRunHandle.from_checkpoint(
            loaded,
            workflow=Plugin,
            conversation=conversation,
            checkpoint_store=store,
        )
    conversation.close()


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "a\x00b"])
def test_durable_identifiers_cannot_escape_storage(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        WorkflowCheckpointStore(bad, root=tmp_path)
    with pytest.raises(ValueError):
        SessionConversation.open(bad, max_tokens=100, journal_path=tmp_path / "j.jsonl")
