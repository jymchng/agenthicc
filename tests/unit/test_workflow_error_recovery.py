"""Regression coverage for PRD-173 workflow error durability."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.tui.conversation_store import AppState
from agenthicc.runners.tui_session import TUISession
from agenthicc.workflows.checkpoint import CheckpointValidationError
from agenthicc.workflows.code_plan.definition import CodePlan
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.plugin import WorkflowContext

from .test_session_workflow_durability import _conversation

pytestmark = pytest.mark.unit


def _handle(tmp_path: Path) -> tuple[WorkflowRunHandle, WorkflowCheckpointStore]:
    conversation = _conversation(tmp_path, "session-recovery")
    store = WorkflowCheckpointStore("session-recovery", root=tmp_path / "checkpoints")
    handle = WorkflowRunHandle.create(
        run_id="error-run",
        workflow=CodePlan,
        conversation=conversation,
        intent="recover this workflow",
        checkpoint_store=store,
    )
    return handle, store


def test_typed_failure_becomes_error_paused_checkpoint(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        context = CodePlanContext(
            intent=handle.original_intent,
            run_id=handle.run_id,
            state=CodePlanState.EXECUTE,
            phase_iteration=2,
            shared_memory=handle.conversation.memory,
        )
        handle.attach_context(context)
        handle.update_phase("execute", index=1, iteration=2)

        checkpoint = handle.finalize_failure(
            "provider timeout while calling tool",
            kind="provider_transient",
        )

        assert checkpoint is not None
        assert checkpoint.status == "paused"
        assert checkpoint.pause_reason == "provider_transient"
        assert checkpoint.failure_kind == "provider_transient"
        assert checkpoint.failure_message == "provider timeout while calling tool"
        assert checkpoint.last_safe_boundary == "execute"
        assert handle.lifecycle == "paused"
        coordinator = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store)
        assert coordinator.recoverable(conversation=handle.conversation)
        revision = checkpoint.revision
        repeated = handle.finalize_failure(
            "provider timeout while calling tool",
            kind="provider_transient",
        )
        assert repeated is not None
        assert repeated.revision == revision
        assert store.load(handle.run_id).revision == revision  # type: ignore[union-attr]
        duplicate_observer = handle.finalize_failure(
            "generic cleanup failure",
            kind="workflow_error",
        )
        assert duplicate_observer is not None
        assert duplicate_observer.revision == revision
        assert duplicate_observer.failure_kind == "provider_transient"
    finally:
        handle.conversation.close()


def test_bootstrap_failure_writes_diagnostic_only_fallback(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_bootstrap_context(
            WorkflowContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                workflow_name=handle.workflow_name,
            )
        )
        assert handle.finalize_failure("build_runner import failed", kind="configuration") is None
        assert handle.lifecycle == "failed"

        fallback = store.load_recovery_error(handle.run_id)
        assert fallback is not None
        assert fallback["diagnostic_only"] is True
        assert fallback["resumable"] is False
        assert fallback["failure_kind"] == "configuration"
        assert "recover this workflow" not in str(fallback)

        records = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store).inspect(
            conversation=handle.conversation
        )
        assert len(records) == 1
        assert records[0].recoverable is False
        assert records[0].diagnostic_only is True
        assert records[0].error_code == "recovery_diagnostic_only"
    finally:
        handle.conversation.close()


def test_checkpoint_failure_is_not_silently_reported_as_saved(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_context(
            CodePlanContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                state=CodePlanState.PLAN,
                shared_memory=handle.conversation.memory,
            )
        )
        handle.update_phase("plan")

        def fail_save(_checkpoint: object) -> object:
            raise OSError("disk unavailable")

        store.save = fail_save  # type: ignore[assignment]
        assert handle.finalize_failure("agent turn failed", kind="phase_execution") is None
        assert handle.lifecycle == "failed"
        fallback = store.load_recovery_error(handle.run_id)
        assert fallback is not None
        assert fallback["failure_kind"] == "checkpoint_storage"
        assert fallback["resumable"] is False
        records = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store).inspect(
            conversation=handle.conversation
        )
        assert records[0].recoverable is False
        assert records[0].error_code == "recovery_diagnostic_only"
    finally:
        handle.conversation.close()


def test_fallback_is_removed_by_explicit_workflow_delete(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_bootstrap_context(
            WorkflowContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                workflow_name=handle.workflow_name,
            )
        )
        handle.finalize_failure("invalid plugin", kind="plugin_incompatible")
        assert store.recovery_error_path_for(handle.run_id).exists()
        store.delete(handle.run_id)
        assert store.load_recovery_error(handle.run_id) is None
        assert store.list_run_ids() == []
    finally:
        handle.conversation.close()


def test_context_not_ready_checkpoint_is_not_resumable(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_bootstrap_context(
            WorkflowContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                workflow_name=handle.workflow_name,
            )
        )
        handle.update_phase("plan")
        records = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store).inspect(
            conversation=handle.conversation
        )
        assert records[0].recoverable is False
        assert records[0].error_code == "context_not_ready"
    finally:
        handle.conversation.close()


def test_stale_checkpoint_revision_cannot_overwrite_newer_state(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_context(
            CodePlanContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                state=CodePlanState.PLAN,
                shared_memory=handle.conversation.memory,
            )
        )
        current = handle.save_checkpoint(reason="current")
        with pytest.raises(CheckpointValidationError, match="older than"):
            store.save(replace(current, revision=current.revision - 1, reason="stale"))
        assert store.load(handle.run_id).reason == "current"  # type: ignore[union-attr]
    finally:
        handle.conversation.close()


def test_stale_fallback_revision_cannot_overwrite_newer_diagnostic(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_bootstrap_context(
            WorkflowContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                workflow_name=handle.workflow_name,
            )
        )
        handle.finalize_failure("setup failed", kind="configuration")
        current = store.load_recovery_error(handle.run_id)
        assert current is not None
        with pytest.raises(CheckpointValidationError, match="older than"):
            store.save_recovery_error({**current, "record_revision": 0})
        assert (
            store.load_recovery_error(handle.run_id)["record_revision"]
            == current["record_revision"]
        )  # type: ignore[index]
    finally:
        handle.conversation.close()


def test_checkpoint_codec_failure_is_diagnostic_as_serialization_error(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_context(
            CodePlanContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                state=CodePlanState.PLAN,
                shared_memory=handle.conversation.memory,
            )
        )
        handle.update_phase("plan")

        def fail_build(**_kwargs: object) -> object:
            raise CheckpointValidationError("unsupported context payload")

        handle.build_checkpoint = fail_build  # type: ignore[method-assign,assignment]
        assert handle.finalize_failure("phase failed", kind="phase_execution") is None
        fallback = store.load_recovery_error(handle.run_id)
        assert fallback is not None
        assert fallback["failure_kind"] == "checkpoint_serialization"
    finally:
        handle.conversation.close()


def test_successful_resume_clears_previous_failure_metadata(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_context(
            CodePlanContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                state=CodePlanState.PLAN,
                shared_memory=handle.conversation.memory,
            )
        )
        handle.update_phase("plan")
        assert handle.finalize_failure("temporary provider failure", kind="provider_transient")
        handle.mark_resuming()
        handle.mark_terminal("complete")
        checkpoint = handle.save_checkpoint(reason="complete")
        assert checkpoint.status == "complete"
        assert checkpoint.failure_kind is None
        assert checkpoint.failure_message is None
        assert checkpoint.pause_reason == "none"
    finally:
        handle.conversation.close()


def test_resume_then_failure_advances_the_same_run_revision(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        context = CodePlanContext(
            intent=handle.original_intent,
            run_id=handle.run_id,
            state=CodePlanState.EXECUTE,
            shared_memory=handle.conversation.memory,
        )
        handle.attach_context(context)
        first = handle.save_checkpoint(reason="first error boundary")
        first_failure = handle.finalize_failure("first provider failure", kind="provider_transient")
        assert first_failure is not None

        record = WorkflowRecoveryCoordinator("session-recovery", checkpoint_store=store).inspect(
            workflow_registry=None,
            conversation=handle.conversation,
        )[0]
        restored = WorkflowRecoveryCoordinator(
            "session-recovery", checkpoint_store=store
        ).rehydrate(
            record,
            workflow=CodePlan,
            conversation=handle.conversation,
            owner_id="repeat-error-owner",
        )
        restored.mark_resuming()
        second = restored.finalize_failure("second provider failure", kind="provider_transient")
        assert second is not None
        assert second.run_id == first.run_id
        assert second.revision > first.revision
        restored.release_claim()
    finally:
        handle.conversation.close()


def test_tui_failure_boundary_projects_paused_and_releases_error_claim(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_context(
            CodePlanContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                state=CodePlanState.PLAN,
                shared_memory=handle.conversation.memory,
            )
        )
        handle.update_phase("plan")
        handle.claim("tui:test")
        session = object.__new__(TUISession)
        session._ctx = SimpleNamespace(app_state=AppState.create())
        session._workflow_handle = handle
        session._publish_session_event = lambda *_args, **_kwargs: None
        session._release_workflow_claim = lambda current: current.release_claim()

        session._fail_workflow_run("provider timed out", kind="timeout")

        assert handle.lifecycle == "paused"
        assert handle.failure_kind == "timeout"
        assert store.claim_owner(handle.run_id) is None
        assert session._ctx.app_state.conversation.notification() is not None
        assert handle.run_id in session._ctx.app_state.conversation.notification()
    finally:
        handle.conversation.close()


def test_failure_diagnostic_is_bounded_and_redacts_credentials(tmp_path: Path) -> None:
    handle, store = _handle(tmp_path)
    try:
        handle.attach_context(
            CodePlanContext(
                intent=handle.original_intent,
                run_id=handle.run_id,
                state=CodePlanState.PLAN,
                shared_memory=handle.conversation.memory,
            )
        )
        handle.update_phase("plan")
        secret = "super-secret-provider-key"
        checkpoint = handle.finalize_failure(
            f"Authorization: Bearer {secret}, token={secret}",
            kind="provider_transient",
        )
        assert checkpoint is not None
        assert secret not in (checkpoint.failure_message or "")
        assert "[redacted]" in (checkpoint.failure_message or "")
        assert len(checkpoint.failure_message or "") <= 512
        assert store.load_recovery_error(handle.run_id) is None
    finally:
        handle.conversation.close()
