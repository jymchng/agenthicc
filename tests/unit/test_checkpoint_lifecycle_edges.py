"""Small persistence and workflow-handle lifecycle edge cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.workflows.checkpoint import CheckpointValidationError, WorkflowCheckpoint
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.plugin import PhaseSpec, WorkflowContext, WorkflowPlugin

from .test_session_workflow_durability import _conversation

pytestmark = pytest.mark.unit


def _checkpoint(session_id: str = "session-1", run_id: str = "run-1") -> WorkflowCheckpoint:
    return WorkflowCheckpoint(
        run_id=run_id,
        workflow_name="edge_plugin",
        conversation_id=session_id,
        intent="intent",
        status="paused",
        current_phase="work",
        phase_index=0,
        phase_iteration=1,
        conversation_cursor=0,
        context={"kind": "WorkflowContext", "fields": {}},
        plugin_fingerprint="fingerprint",
    )


def test_checkpoint_store_missing_delete_corrupt_and_identity_paths(tmp_path: Path) -> None:
    store = WorkflowCheckpointStore("session-1", root=tmp_path)
    assert store.load("missing") is None
    store.delete("missing")
    assert store.list_run_ids() == []
    for invalid in ("", ".", "..", "a/b", "a\\b", "a\x00b"):
        with pytest.raises(ValueError):
            store.path_for(invalid)
    with pytest.raises(CheckpointValidationError, match="different session"):
        store.save(_checkpoint(session_id="other"))

    path = store.path_for("corrupt")
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(CheckpointValidationError, match="invalid workflow checkpoint"):
        store.load("corrupt")

    identity = store.path_for("identity")
    identity.parent.mkdir(parents=True)
    identity.write_text(json.dumps(_checkpoint(run_id="other").to_dict()), encoding="utf-8")
    with pytest.raises(CheckpointValidationError, match="identity"):
        store.load("identity")


def test_workflow_handle_lifecycle_and_browser_checkpoint_hooks(tmp_path: Path) -> None:
    class Plugin(WorkflowPlugin):
        name = "edge_plugin"
        phases = [PhaseSpec(name="work")]

    conversation = _conversation(tmp_path)
    store = WorkflowCheckpointStore("session-1", root=tmp_path / "checkpoints")

    class Browser:
        def __init__(self) -> None:
            self.restored: dict[str, object] | None = None

        def checkpoint_payload(self) -> dict[str, object]:
            return {"conversation_id": "session-1", "marker": "safe"}

        def restore_checkpoint(self, payload: dict[str, object]) -> None:
            self.restored = payload

    browser = Browser()
    handle = WorkflowRunHandle.create(
        run_id="run-1",
        workflow=Plugin,
        conversation=conversation,
        intent="intent",
        checkpoint_store=store,
        browser_manager=browser,
    )
    with pytest.raises(ValueError, match="context"):
        handle.build_checkpoint()
    assert handle.request_pause() is True
    assert handle.request_pause() is False
    handle.mark_paused(reason="escape")
    handle.mark_resuming()
    handle.attach_context(
        CodePlanContext(intent="intent", run_id="run-1", state=CodePlanState.EXECUTE)
    )
    handle.update_phase("work", index=1, iteration=2)
    handle.append_continuation("continue")
    assert handle.pop_continuation() == "continue"
    assert handle.pop_continuation() is None
    checkpoint = handle.save_checkpoint(reason="resume")
    assert checkpoint.browser["marker"] == "safe"
    restored = WorkflowRunHandle.from_checkpoint(
        checkpoint,
        workflow=Plugin,
        conversation=conversation,
        checkpoint_store=store,
        browser_manager=browser,
    )
    assert restored.lifecycle == "resuming"
    assert browser.restored == checkpoint.browser
    restored.mark_terminal("complete")
    with pytest.raises(RuntimeError, match="not paused"):
        restored.mark_resuming()
    with pytest.raises(ValueError, match="terminal"):
        restored.mark_terminal("running")  # type: ignore[arg-type]
    restored.mark_terminal("failed", error="failure")
    assert restored.last_error == "failure"
    conversation.close()


def test_failure_finalization_syncs_the_forward_typed_phase(tmp_path: Path) -> None:
    """A late provider error must not be checkpointed as the bootstrap phase."""

    class Plugin(WorkflowPlugin):
        name = "cursor_plugin"
        phases = [PhaseSpec(name="init"), PhaseSpec(name="architecture")]

    conversation = _conversation(tmp_path)
    store = WorkflowCheckpointStore("session-1", root=tmp_path / "checkpoints")
    handle = WorkflowRunHandle.create(
        run_id="cursor-run",
        workflow=Plugin,
        conversation=conversation,
        intent="continue at the active phase",
        checkpoint_store=store,
    )
    handle.attach_context(
        WorkflowContext(
            intent="continue at the active phase",
            run_id="cursor-run",
            workflow_name=Plugin.name,
            current_phase="architecture",
            phase_iteration=4,
        )
    )
    # Simulate a runner that attached its typed context but had not reached
    # the normal phase-loop checkpoint before the provider raised.
    handle.update_phase("init", 0, 0, persist=False)

    checkpoint = handle.finalize_failure("429 rate limit", kind="provider_transient")

    assert checkpoint is not None
    assert checkpoint.current_phase == "architecture"
    assert checkpoint.phase_index == 1
    assert checkpoint.phase_iteration == 4
    assert checkpoint.status == "paused"
    conversation.close()


def test_checkpoint_store_accepts_idempotent_replay_and_rejects_same_revision_conflict(
    tmp_path: Path,
) -> None:
    """Duplicate finalizers cannot replace a checkpoint at the same revision."""

    store = WorkflowCheckpointStore("session-1", root=tmp_path)
    checkpoint = _checkpoint()
    path = store.save(checkpoint)
    assert store.save(checkpoint) == path

    conflicting = WorkflowCheckpoint(
        **{
            **checkpoint.__dict__,
            "current_phase": "later",
        }
    )
    with pytest.raises(CheckpointValidationError, match="conflicts"):
        store.save(conflicting)
