"""Durable workflow recovery coordination (PRD-170).

The checkpoint store owns bytes on disk; this module owns the decision about
which records are recoverable and how a checkpoint becomes one session-bound
``WorkflowRunHandle``.  Keeping that decision outside ``TUISession`` lets the
TUI, headless runner, and future session-service clients share the same
fail-closed rules.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agenthicc.runners.workflow_checkpoint_store import (
    WorkflowCheckpointStore,
)
from agenthicc.workflows.checkpoint import (
    CheckpointValidationError,
    WorkflowCheckpoint,
    context_from_payload,
    workflow_fingerprint,
)

if TYPE_CHECKING:
    from agenthicc.runners.session_conversation import SessionConversation
    from agenthicc.runners.workflow_handle import WorkflowRunHandle
    from agenthicc.workflows.registry import WorkflowRegistry

__all__ = [
    "RECOVERABLE_WORKFLOW_STATUSES",
    "WorkflowRecoveryCoordinator",
    "WorkflowRecoveryRecord",
]

RECOVERABLE_WORKFLOW_STATUSES = frozenset({"running", "pausing", "paused", "resuming"})


@dataclass(frozen=True)
class WorkflowRecoveryRecord:
    """Safe inspection result for one workflow checkpoint."""

    run_id: str
    checkpoint: WorkflowCheckpoint | None = None
    error_code: str | None = None
    error: str | None = None
    interrupted: bool = False
    tool_tail_needs_repair: bool = False
    session_id: str = ""

    @property
    def recoverable(self) -> bool:
        """Whether the record can be offered to a resume command."""
        return self.checkpoint is not None and self.error_code is None

    @property
    def workflow_name(self) -> str:
        """Return a safe workflow name for UI diagnostics."""
        return self.checkpoint.workflow_name if self.checkpoint is not None else ""

    @property
    def conversation_id(self) -> str:
        """Return the durable conversation identity, if one was loaded."""
        if self.checkpoint is not None:
            return self.checkpoint.conversation_id
        return self.session_id

    @property
    def intent(self) -> str:
        """Return the bounded original intent stored in the checkpoint."""
        return self.checkpoint.intent if self.checkpoint is not None else ""

    @property
    def phase_index(self) -> int:
        """Return the last durably entered phase index."""
        return self.checkpoint.phase_index if self.checkpoint is not None else 0

    @property
    def checkpoint_revision(self) -> int:
        """Return the monotonic checkpoint revision."""
        return self.checkpoint.revision if self.checkpoint is not None else 0

    @property
    def journal_cursor(self) -> int:
        """Return the provider-memory journal cursor recorded by the checkpoint."""
        return self.checkpoint.conversation_cursor if self.checkpoint is not None else 0

    @property
    def plugin_fingerprint(self) -> str:
        """Return the saved plugin topology fingerprint."""
        return self.checkpoint.plugin_fingerprint if self.checkpoint is not None else ""

    @property
    def provider_profile(self) -> str:
        """Return the saved non-secret provider profile identity."""
        return self.checkpoint.provider_profile if self.checkpoint is not None else ""

    @property
    def workspace_root(self) -> str:
        """Return the saved canonical workspace identity."""
        return self.checkpoint.workspace_root if self.checkpoint is not None else ""

    @property
    def current_phase(self) -> str | None:
        """Return the last durably entered phase, if available."""
        return self.checkpoint.current_phase if self.checkpoint is not None else None

    @property
    def status(self) -> str:
        """Return the persisted status or ``invalid`` for corrupt records."""
        return self.checkpoint.status if self.checkpoint is not None else "invalid"

    @property
    def display_error(self) -> str:
        """Return a bounded user-facing diagnostic without sensitive payloads."""
        if self.error:
            return self.error
        return "workflow checkpoint is not recoverable"


class WorkflowRecoveryCoordinator:
    """Inspect, validate, claim, and rehydrate one session's workflow runs."""

    def __init__(
        self,
        session_id: str,
        *,
        checkpoint_store: WorkflowCheckpointStore | None = None,
    ) -> None:
        self.store = checkpoint_store or WorkflowCheckpointStore(session_id)
        if self.store.session_id != session_id:
            raise ValueError("checkpoint store belongs to a different session")
        self.session_id = session_id

    def inspect(
        self,
        *,
        workflow_registry: "WorkflowRegistry | None" = None,
        conversation: "SessionConversation | None" = None,
        provider_profile: str | None = None,
        workspace_root: str | None = None,
        include_terminal: bool = False,
    ) -> list[WorkflowRecoveryRecord]:
        """Return deterministic recovery records for this session.

        Invalid records are retained in the result so callers can display a
        useful diagnostic and leave the checkpoint available for inspection or
        explicit reset. Terminal records are omitted by default.
        """
        records: list[WorkflowRecoveryRecord] = []
        for run_id in self.store.list_run_ids():
            try:
                checkpoint = self.store.load(run_id)
            except (CheckpointValidationError, ValueError) as exc:
                records.append(
                    WorkflowRecoveryRecord(
                        run_id=run_id,
                        session_id=self.session_id,
                        error_code="checkpoint_corrupt",
                        error=str(exc),
                    )
                )
                continue
            if checkpoint is None:
                continue
            if not include_terminal and checkpoint.status not in RECOVERABLE_WORKFLOW_STATUSES:
                continue

            error_code: str | None = None
            error: str | None = None
            if checkpoint.status in RECOVERABLE_WORKFLOW_STATUSES:
                error_code, error = self._validate_recovery(
                    checkpoint,
                    workflow_registry=workflow_registry,
                    conversation=conversation,
                    provider_profile=provider_profile,
                    workspace_root=workspace_root,
                )
            records.append(
                WorkflowRecoveryRecord(
                    run_id=run_id,
                    session_id=self.session_id,
                    checkpoint=checkpoint,
                    error_code=error_code,
                    error=error,
                    interrupted=checkpoint.status in {"running", "resuming"},
                    tool_tail_needs_repair=(
                        conversation is not None and conversation.journal.resume_state() is not None
                    ),
                )
            )

        records.sort(
            key=lambda item: (
                -(item.checkpoint.created_at if item.checkpoint is not None else 0.0),
                item.run_id,
            )
        )
        return records

    def recoverable(
        self,
        *,
        workflow_registry: "WorkflowRegistry | None" = None,
        conversation: "SessionConversation | None" = None,
        provider_profile: str | None = None,
        workspace_root: str | None = None,
    ) -> list[WorkflowRecoveryRecord]:
        """Return only valid records eligible for `/workflow resume`."""
        return [
            record
            for record in self.inspect(
                workflow_registry=workflow_registry,
                conversation=conversation,
                provider_profile=provider_profile,
                workspace_root=workspace_root,
            )
            if record.recoverable
        ]

    def rehydrate(
        self,
        record: WorkflowRecoveryRecord,
        *,
        workflow: type[object],
        conversation: "SessionConversation",
        browser_manager: object | None = None,
        owner_id: str | None = None,
    ) -> "WorkflowRunHandle":
        """Claim and restore one checkpoint into the supplied session memory."""
        if not record.recoverable or record.checkpoint is None:
            raise ValueError(record.display_error)
        checkpoint = record.checkpoint
        if checkpoint.conversation_id != conversation.conversation_id:
            raise ValueError("workflow checkpoint belongs to a different session conversation")
        if owner_id is not None:
            self.store.acquire_claim(record.run_id, owner_id)
        try:
            from agenthicc.runners.workflow_handle import WorkflowRunHandle

            # Reconcile the provider-facing message projection before the
            # workflow adds its resume instruction. Completed tool results are
            # already durable and are not executed again; only an unanswered
            # tail is synthesized by the canonical memory repair hook.
            journal = conversation.journal
            incomplete = journal.resume_state()
            if incomplete is not None:
                try:
                    repair = getattr(conversation.memory, "ensure_valid_and_persist", None)
                    if callable(repair):
                        repair()
                    else:
                        conversation.memory.ensure_valid()
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(
                        "conversation_tool_tail_invalid: saved tool-call history "
                        f"could not be repaired ({type(exc).__name__}: {exc})"
                    ) from exc
                journal.turn_recovered(incomplete.turn_id)

            handle = WorkflowRunHandle.from_checkpoint(
                checkpoint,
                workflow=workflow,
                conversation=conversation,
                checkpoint_store=self.store,
                browser_manager=browser_manager,
                recover_interrupted=record.interrupted,
            )
            if owner_id is not None:
                handle.claim_owner_id = owner_id
            return handle
        except Exception:
            if owner_id is not None:
                self.store.release_claim(record.run_id, owner_id)
            raise

    def discard(
        self,
        record: WorkflowRecoveryRecord,
        *,
        reason: str = "reset by user",
        owner_id: str | None = None,
    ) -> WorkflowCheckpoint:
        """Persist an auditable terminal discard for a valid checkpoint.

        Incompatible but structurally valid checkpoints must remain inspectable
        after reset. A corrupt checkpoint has no trustworthy payload to turn
        into a terminal record and is therefore rejected rather than deleted.
        """
        checkpoint = record.checkpoint
        if checkpoint is None:
            raise ValueError(record.display_error)
        if checkpoint.conversation_id != self.session_id:
            raise ValueError("workflow checkpoint belongs to a different session")
        if owner_id is not None:
            self.store.acquire_claim(record.run_id, owner_id)
        try:
            discarded = replace(
                checkpoint,
                status="discarded",
                reason=reason[:512],
                revision=checkpoint.revision + 1,
            )
            self.store.save(discarded)
            return discarded
        finally:
            if owner_id is not None:
                self.store.release_claim(record.run_id, owner_id)

    @staticmethod
    def _validate_recovery(
        checkpoint: WorkflowCheckpoint,
        *,
        workflow_registry: "WorkflowRegistry | None",
        conversation: "SessionConversation | None",
        provider_profile: str | None,
        workspace_root: str | None,
    ) -> tuple[str | None, str | None]:
        if workflow_registry is not None:
            workflow = workflow_registry.get(checkpoint.workflow_name)
            if workflow is None:
                return (
                    "plugin_not_loaded",
                    f"workflow {checkpoint.workflow_name!r} is not loaded",
                )
            if workflow_fingerprint(workflow) != checkpoint.plugin_fingerprint:
                return (
                    "plugin_fingerprint_mismatch",
                    f"workflow {checkpoint.workflow_name!r} changed since this run was saved",
                )
            if conversation is not None:
                try:
                    restored = context_from_payload(
                        checkpoint.context,
                        memory=conversation.memory,
                        workflow=workflow,
                    )
                except (CheckpointValidationError, TypeError, ValueError) as exc:
                    error_code = (
                        "custom_context_codec_missing"
                        if checkpoint.context.get("kind") == "CustomContext"
                        and not callable(getattr(workflow, "checkpoint_context_from_payload", None))
                        else "context_restore_failed"
                    )
                    return (
                        error_code,
                        f"saved workflow context cannot be restored: {type(exc).__name__}: {exc}",
                    )
                context_run_id = getattr(restored, "run_id", None)
                if context_run_id != checkpoint.run_id:
                    return (
                        "context_identity_mismatch",
                        "saved workflow context has a different run id",
                    )
                context_workflow = getattr(restored, "workflow_name", None)
                if isinstance(context_workflow, str) and context_workflow not in {
                    "",
                    checkpoint.workflow_name,
                }:
                    return (
                        "context_identity_mismatch",
                        "saved workflow context has a different workflow name",
                    )
                context_phase = getattr(restored, "current_phase", None)
                if not isinstance(context_phase, str) or not context_phase:
                    state = getattr(restored, "state", None)
                    state_name = getattr(state, "name", None)
                    if isinstance(state_name, str) and state_name.lower() not in {
                        "complete",
                        "exited",
                        "failed",
                    }:
                        context_phase = state_name.lower()
                if (
                    checkpoint.current_phase is not None
                    and isinstance(context_phase, str)
                    and context_phase != checkpoint.current_phase
                ):
                    return (
                        "checkpoint_phase_mismatch",
                        "checkpoint phase does not match the saved workflow context",
                    )
                phases = getattr(workflow, "phases", ())
                phase_names = [getattr(phase, "name", "") for phase in phases]
                if (
                    checkpoint.current_phase in phase_names
                    and phase_names.index(checkpoint.current_phase) != checkpoint.phase_index
                ):
                    return (
                        "checkpoint_phase_mismatch",
                        "checkpoint phase index does not match the workflow topology",
                    )
                context_iteration = getattr(restored, "phase_iteration", None)
                if (
                    isinstance(context_iteration, int)
                    and context_iteration != checkpoint.phase_iteration
                ):
                    return (
                        "checkpoint_iteration_mismatch",
                        "checkpoint iteration does not match the saved workflow context",
                    )
        if conversation is not None and conversation.cursor < checkpoint.conversation_cursor:
            return (
                "conversation_cursor_mismatch",
                "session conversation is older than the workflow checkpoint",
            )
        if (
            provider_profile is not None
            and checkpoint.provider_profile
            and checkpoint.provider_profile != provider_profile
        ):
            return (
                "provider_profile_mismatch",
                f"checkpoint requires provider profile {checkpoint.provider_profile!r}",
            )
        if (
            workspace_root is not None
            and checkpoint.workspace_root
            and checkpoint.workspace_root != workspace_root
        ):
            return (
                "workspace_mismatch",
                "checkpoint belongs to a different workspace root",
            )
        return None, None
