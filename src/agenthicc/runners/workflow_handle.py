"""Session-owned workflow lifecycle handle (PRD-156)."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.prompt_contract import PromptContract
from agenthicc.workflows.checkpoint import (
    CheckpointValidationError,
    WorkflowCheckpoint,
    context_from_payload,
    context_to_payload,
    workflow_fingerprint,
)

__all__ = ["WorkflowFailureKind", "WorkflowRunHandle", "WorkflowLifecycle"]

WorkflowLifecycle = Literal[
    "running",
    "pausing",
    "paused",
    "resuming",
    "complete",
    "failed",
    "discarded",
]


class WorkflowFailureKind(str, Enum):
    """Stable failure categories persisted in workflow checkpoints."""

    PROVIDER_TRANSIENT = "provider_transient"
    TOOL_TRANSIENT = "tool_transient"
    PHASE_EXECUTION = "phase_execution"
    USER_CANCELLED = "user_cancelled"
    PROCESS_INTERRUPTED = "process_interrupted"
    CONFIGURATION = "configuration"
    TIMEOUT = "timeout"
    CHECKPOINT_SERIALIZATION = "checkpoint_serialization"
    CHECKPOINT_STORAGE = "checkpoint_storage"
    WORKFLOW_INVARIANT = "workflow_invariant"
    PLUGIN_INCOMPATIBLE = "plugin_incompatible"
    WORKFLOW_ERROR = "workflow_error"


def _normalize_failure_kind(kind: WorkflowFailureKind | str) -> str:
    """Convert extension-provided labels to the stable persisted vocabulary."""
    value = kind.value if isinstance(kind, WorkflowFailureKind) else str(kind)
    try:
        return WorkflowFailureKind(value).value
    except ValueError:
        return WorkflowFailureKind.WORKFLOW_ERROR.value


@dataclass
class WorkflowRunHandle:
    """Durable workflow identity and context, referencing session memory."""

    run_id: str
    workflow_name: str
    conversation: SessionConversation
    original_intent: str
    plugin_fingerprint: str
    checkpoint_store: WorkflowCheckpointStore
    workflow: type[object] | None = None
    context: object | None = None
    lifecycle: WorkflowLifecycle = "running"
    current_phase: str | None = None
    phase_index: int = 0
    phase_iteration: int = 0
    checkpoint_revision: int = 0
    checkpoint_supported: bool = True
    pause_requested: bool = False
    active_turn_id: str | None = None
    continuation_messages: list[str] = field(default_factory=list)
    last_error: str = ""
    created_at: float = field(default_factory=time.time)
    browser_manager: object | None = None
    provider_profile: str = ""
    workspace_root: str = ""
    cache_contract_version: str = ""
    cache_epoch: str = ""
    stable_prompt_fingerprint: str = ""
    dynamic_prompt_fingerprint: str = ""
    cache_provider_capability: str = ""
    cache_status: str = ""
    cache_invalidation_reason: str = ""
    # The bootstrap context is sufficient to identify a run, but not to
    # resume a specialized runner. It becomes true when a runner attaches its
    # typed context or a rehydrated checkpoint has already passed validation.
    context_ready: bool = False
    pause_reason: str = "none"
    failure_kind: str | None = None
    failure_message: str | None = None
    last_safe_boundary: str | None = None
    error_revision: int = 0
    claim_owner_id: str | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        workflow: type[object],
        conversation: SessionConversation,
        intent: str,
        checkpoint_store: WorkflowCheckpointStore,
        browser_manager: object | None = None,
        provider_profile: str = "",
        workspace_root: str = "",
    ) -> "WorkflowRunHandle":
        """Create a handle for a new workflow run."""
        return cls(
            run_id=run_id,
            workflow_name=str(getattr(workflow, "name", workflow.__name__)),
            conversation=conversation,
            original_intent=intent,
            plugin_fingerprint=workflow_fingerprint(workflow),
            checkpoint_store=checkpoint_store,
            workflow=workflow,
            browser_manager=browser_manager,
            provider_profile=provider_profile,
            workspace_root=workspace_root,
        )

    def attach_context(self, context: object) -> None:
        """Attach the typed context created by a runner."""
        self.context = context
        self.context_ready = True

    def attach_bootstrap_context(self, context: object) -> None:
        """Attach an identity-only context before runner construction.

        A bootstrap context makes setup failures attributable to a durable run,
        but is intentionally not considered sufficient for resume until the
        runner attaches its specialized context.
        """
        self.context = context
        self.context_ready = False

    def update_phase(
        self,
        phase: str | None,
        index: int = 0,
        iteration: int = 0,
        *,
        persist: bool = True,
    ) -> None:
        """Publish and, by default, durably persist the current phase.

        Phase entry is the minimum safe recovery boundary.  Every built-in
        runner attaches its typed context before calling this method, so a
        process that disappears inside the phase can resume that exact state
        instead of falling back to the first phase.
        """
        self.current_phase = phase
        self.phase_index = index
        self.phase_iteration = iteration
        if persist and self.context is not None and self.checkpoint_supported:
            self.persist_checkpoint(reason="phase_started")

    def claim(self, owner_id: str) -> None:
        """Acquire the durable run lease for this process."""
        self.checkpoint_store.acquire_claim(self.run_id, owner_id)
        self.claim_owner_id = owner_id

    def release_claim(self) -> None:
        """Release this handle's durable run lease, if it owns one."""
        owner_id = self.claim_owner_id
        if owner_id is None:
            return
        self.checkpoint_store.release_claim(self.run_id, owner_id)
        self.claim_owner_id = None

    def record_prompt_contract(self, contract: PromptContract) -> None:
        """Record redacted cache metadata for the next workflow checkpoint."""

        previous_epoch = self.cache_epoch
        previous_stable_fingerprint = self.stable_prompt_fingerprint
        self.cache_contract_version = contract.contract_version
        self.cache_epoch = contract.cache_epoch.value
        self.stable_prompt_fingerprint = contract.stable_fingerprint
        self.dynamic_prompt_fingerprint = contract.dynamic_fingerprint
        self.cache_provider_capability = contract.provider_capability
        self.cache_status = contract.cache_status
        self.cache_invalidation_reason = (
            "initial"
            if not previous_epoch
            else (
                "phase_context_changed"
                if previous_epoch == self.cache_epoch
                else (
                    "stable_contract_changed"
                    if previous_stable_fingerprint != contract.stable_fingerprint
                    else "connection_changed"
                )
            )
        )

    def request_pause(self) -> bool:
        """Request a cooperative pause exactly once."""
        if self.lifecycle not in {"running", "resuming"}:
            return False
        self.pause_requested = True
        self.lifecycle = "pausing"
        return True

    def is_pause_requested(self) -> bool:
        """Return whether cancellation should be interpreted as a pause."""
        return self.pause_requested and self.lifecycle in {"pausing", "paused"}

    def mark_paused(self, *, reason: str = "escape") -> None:
        """Mark the run paused after its checkpoint is durably written."""
        self.lifecycle = "paused"
        self.pause_requested = True
        self.last_error = reason
        self.pause_reason = reason

    def mark_resuming(self) -> None:
        """Move a paused run into active continuation."""
        if self.lifecycle not in {"paused", "pausing", "resuming"}:
            raise RuntimeError(f"workflow {self.run_id} is not paused")
        self.lifecycle = "resuming"
        self.pause_requested = False

    def mark_terminal(self, status: WorkflowLifecycle, *, error: str = "") -> None:
        """Set a terminal workflow lifecycle."""
        if status not in {"complete", "failed", "discarded"}:
            raise ValueError(f"not a terminal lifecycle: {status}")
        self.lifecycle = status
        self.last_error = error
        self.pause_requested = False
        if status == "complete":
            # A successful resumed run supersedes the previous paused error.
            # Keep the revision counter for audit ordering, but do not publish
            # stale failure metadata on a terminal success checkpoint.
            self.pause_reason = "none"
            self.failure_kind = None
            self.failure_message = None
            self.last_safe_boundary = None

    @staticmethod
    def _safe_failure_message(error: object) -> str:
        """Return a bounded, single-line diagnostic safe for persistence/UI."""
        text = re.sub(r"\s+", " ", str(error)).strip()
        # Do not retain common credential-bearing fields if an exception
        # includes a provider request dump. The full exception remains a log
        # concern, never a checkpoint concern.
        text = re.sub(
            r"(?i)(authorization|api[_ -]?key|token|password)\s*[:=]\s*[^,;]+",
            r"\1=[redacted]",
            text,
        )
        return text[:512]

    def _save_failure_diagnostic(self, *, error: str, kind: str) -> None:
        """Best-effort durable record for a failure without a valid context."""
        try:
            self.checkpoint_store.save_recovery_error(
                {
                    "run_id": self.run_id,
                    "session_id": self.conversation.conversation_id,
                    "workflow_name": self.workflow_name,
                    "plugin_fingerprint": self.plugin_fingerprint,
                    "intent_digest": hashlib.sha256(
                        self.original_intent.encode("utf-8", errors="replace")
                    ).hexdigest(),
                    "phase": self.current_phase,
                    "phase_index": self.phase_index,
                    "failure_kind": kind,
                    "failure_message": error,
                    "record_revision": self.error_revision,
                    "context_ready": self.context_ready,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
            )
        except Exception:
            # The caller has already classified this as non-recoverable. The
            # in-memory fields and UI event still explain the failure when the
            # filesystem cannot accept even the fallback record.
            return

    def finalize_failure(
        self,
        error: object,
        *,
        kind: WorkflowFailureKind | str = WorkflowFailureKind.WORKFLOW_ERROR,
        recoverable: bool = True,
        boundary: str | None = None,
    ) -> WorkflowCheckpoint | None:
        """Persist one idempotent workflow failure disposition.

        Valid typed contexts become ``paused`` and remain eligible for exact
        resume. Missing/unsupported contexts and checkpoint failures become a
        terminal diagnostic with a fallback envelope. Repeated callers return
        without changing a committed terminal/paused disposition.
        """
        normalized_kind = _normalize_failure_kind(kind)
        if self.lifecycle in {"complete", "discarded"}:
            return None
        if self.lifecycle == "failed" and self.failure_kind and self.last_error:
            # A prior finalizer already committed a terminal disposition (and
            # possibly a fallback envelope). Do not replace its more precise
            # storage/invariant diagnostic with a later cleanup message.
            return None
        if self.lifecycle == "paused" and self.failure_kind == normalized_kind and self.last_error:
            load_checkpoint = getattr(self.checkpoint_store, "load", None)
            if callable(load_checkpoint):
                try:
                    checkpoint = load_checkpoint(self.run_id)
                    if isinstance(checkpoint, WorkflowCheckpoint):
                        return checkpoint
                except Exception:
                    pass
            return None

        safe_error = self._safe_failure_message(error)
        self.last_error = safe_error
        self.failure_kind = normalized_kind
        self.failure_message = safe_error
        self.last_safe_boundary = boundary or self.current_phase
        self.pause_reason = normalized_kind
        self.error_revision += 1

        can_resume = (
            recoverable
            and self.context_ready
            and self.context is not None
            and self.checkpoint_supported
        )
        if can_resume:
            self.lifecycle = "paused"
            self.pause_requested = True
            try:
                checkpoint = self.save_checkpoint(reason=normalized_kind)
            except Exception as exc:
                self.checkpoint_supported = False
                self.lifecycle = "failed"
                self.pause_requested = False
                self.last_error = (
                    f"{safe_error}; checkpoint save failed: {self._safe_failure_message(exc)}"
                )
                self.failure_message = self.last_error[:512]
                checkpoint_failure_kind = (
                    WorkflowFailureKind.CHECKPOINT_SERIALIZATION.value
                    if isinstance(exc, (CheckpointValidationError, TypeError, ValueError))
                    else WorkflowFailureKind.CHECKPOINT_STORAGE.value
                )
                self._save_failure_diagnostic(
                    error=self.failure_message,
                    kind=checkpoint_failure_kind,
                )
                return None
            return checkpoint

        self.lifecycle = "failed"
        self.pause_requested = False
        self._save_failure_diagnostic(error=safe_error, kind=normalized_kind)
        return None

    def append_continuation(self, text: str) -> None:
        """Record a queued continuation exactly once in FIFO order."""
        self.continuation_messages.append(text)

    def pop_continuation(self) -> str | None:
        """Claim the oldest queued continuation."""
        if not self.continuation_messages:
            return None
        return self.continuation_messages.pop(0)

    def build_checkpoint(self, *, reason: str = "") -> WorkflowCheckpoint:
        """Build a checkpoint from the current context and journal.

        Checkpoint context has no framework-imposed serialized byte ceiling.
        The context still must use a declared JSON-compatible codec, while
        the checkpoint store and the backing filesystem determine practical
        capacity.
        """
        if self.context is None:
            raise ValueError("cannot checkpoint a workflow before its context exists")
        context_payload = context_to_payload(self.context, workflow=self.workflow)
        browser_payload: dict[str, object] = {}
        if self.browser_manager is not None:
            exporter = getattr(self.browser_manager, "checkpoint_payload", None)
            if callable(exporter):
                candidate = exporter()
                if isinstance(candidate, dict):
                    browser_payload = candidate
        return WorkflowCheckpoint(
            run_id=self.run_id,
            workflow_name=self.workflow_name,
            conversation_id=self.conversation.conversation_id,
            intent=self.original_intent,
            status=self.lifecycle,
            current_phase=self.current_phase,
            phase_index=self.phase_index,
            phase_iteration=self.phase_iteration,
            conversation_cursor=self.conversation.cursor,
            context=context_payload,
            plugin_fingerprint=self.plugin_fingerprint,
            revision=self.checkpoint_revision + 1,
            reason=reason or self.last_error,
            browser=browser_payload,
            provider_profile=self.provider_profile,
            workspace_root=self.workspace_root,
            cache_contract_version=self.cache_contract_version,
            cache_epoch=self.cache_epoch,
            stable_prompt_fingerprint=self.stable_prompt_fingerprint,
            dynamic_prompt_fingerprint=self.dynamic_prompt_fingerprint,
            cache_provider_capability=self.cache_provider_capability,
            cache_status=self.cache_status,
            cache_invalidation_reason=self.cache_invalidation_reason,
            context_ready=self.context_ready,
            pause_reason=self.pause_reason,
            failure_kind=self.failure_kind,
            failure_message=self.failure_message,
            last_safe_boundary=self.last_safe_boundary,
            error_revision=self.error_revision,
        )

    def save_checkpoint(self, *, reason: str = "") -> WorkflowCheckpoint:
        """Atomically persist the current run and update its revision."""
        checkpoint = self.build_checkpoint(reason=reason)
        self.checkpoint_store.save(checkpoint)
        delete_recovery_error = getattr(self.checkpoint_store, "delete_recovery_error", None)
        if callable(delete_recovery_error):
            try:
                delete_recovery_error(self.run_id)
            except Exception:
                # The primary checkpoint is already durable. A stale fallback
                # is less harmful than misclassifying this successful save as
                # a storage failure; the recovery coordinator prefers the
                # validated primary record on the next inspection.
                pass
        self.checkpoint_revision = checkpoint.revision
        return checkpoint

    def persist_checkpoint(self, *, reason: str = "") -> WorkflowCheckpoint | None:
        """Persist a recoverable boundary and fail closed on codec errors.

        ``None`` means checkpointing is already disabled or no context exists.
        A newly discovered codec/storage error is re-raised so the owning
        failure finalizer can persist a diagnostic and tell the user whether
        resume is possible; it must not disappear as a successful transition.
        """
        if self.context is None or not self.checkpoint_supported:
            return None
        try:
            return self.save_checkpoint(reason=reason)
        except Exception as exc:
            from agenthicc.workflows.checkpoint import CheckpointValidationError

            if isinstance(exc, CheckpointValidationError):
                self.checkpoint_supported = False
                raise
            raise

    def persist_context_transition(self, *, reason: str = "phase_transition") -> None:
        """Persist a typed context's newly selected non-terminal state.

        Specialized runners often return the next enum value from a phase
        method before their next loop iteration. This helper closes that small
        crash window without serializing live resources or making the handle a
        second workflow state machine.
        """
        context = self.context
        state = getattr(context, "state", None)
        state_name = getattr(state, "name", None)
        if not isinstance(state_name, str) or state_name.lower() in {
            "complete",
            "exited",
            "failed",
        }:
            return
        phase = state_name.lower()
        phases = getattr(self.workflow, "phases", ())
        index = next(
            (i for i, candidate in enumerate(phases) if getattr(candidate, "name", "") == phase),
            self.phase_index,
        )
        self.current_phase = phase
        self.phase_index = index
        iteration = getattr(context, "phase_iteration", self.phase_iteration)
        if isinstance(iteration, int) and iteration >= 0:
            self.phase_iteration = iteration
        self.persist_checkpoint(reason=reason)

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint: WorkflowCheckpoint,
        *,
        workflow: type[object],
        conversation: SessionConversation,
        checkpoint_store: WorkflowCheckpointStore,
        browser_manager: object | None = None,
        recover_interrupted: bool = False,
    ) -> "WorkflowRunHandle":
        """Rehydrate a handle and typed context from a validated checkpoint."""
        expected = workflow_fingerprint(workflow)
        if checkpoint.plugin_fingerprint != expected:
            raise ValueError("workflow plugin fingerprint does not match checkpoint")
        if checkpoint.conversation_id != conversation.conversation_id:
            raise ValueError("workflow checkpoint belongs to a different session conversation")
        if conversation.cursor < checkpoint.conversation_cursor:
            raise ValueError("session conversation is older than the workflow checkpoint")
        restored_lifecycle: WorkflowLifecycle = checkpoint.status  # type: ignore[assignment]
        interrupted = recover_interrupted and checkpoint.status in {"running", "resuming"}
        if interrupted:
            restored_lifecycle = "paused"
        handle = cls(
            run_id=checkpoint.run_id,
            workflow_name=checkpoint.workflow_name,
            conversation=conversation,
            original_intent=checkpoint.intent,
            plugin_fingerprint=checkpoint.plugin_fingerprint,
            checkpoint_store=checkpoint_store,
            workflow=workflow,
            lifecycle=(
                "paused" if checkpoint.status in {"paused", "pausing"} else restored_lifecycle
            ),
            current_phase=checkpoint.current_phase,
            phase_index=checkpoint.phase_index,
            phase_iteration=checkpoint.phase_iteration,
            checkpoint_revision=checkpoint.revision,
            pause_requested=checkpoint.status in {"paused", "pausing"} or interrupted,
            last_error=(
                checkpoint.failure_message
                or checkpoint.reason
                or ("process_interrupted" if interrupted else "")
            ),
            browser_manager=browser_manager,
            provider_profile=checkpoint.provider_profile,
            workspace_root=checkpoint.workspace_root,
            cache_contract_version=checkpoint.cache_contract_version,
            cache_epoch=checkpoint.cache_epoch,
            stable_prompt_fingerprint=checkpoint.stable_prompt_fingerprint,
            dynamic_prompt_fingerprint=checkpoint.dynamic_prompt_fingerprint,
            cache_provider_capability=checkpoint.cache_provider_capability,
            cache_status=checkpoint.cache_status,
            cache_invalidation_reason=checkpoint.cache_invalidation_reason,
            context_ready=checkpoint.context_ready,
            pause_reason=checkpoint.pause_reason,
            failure_kind=checkpoint.failure_kind,
            failure_message=checkpoint.failure_message,
            last_safe_boundary=checkpoint.last_safe_boundary,
            error_revision=checkpoint.error_revision,
        )
        if browser_manager is not None:
            importer = getattr(browser_manager, "restore_checkpoint", None)
            if callable(importer):
                importer(checkpoint.browser)
        handle.context = context_from_payload(
            checkpoint.context,
            memory=conversation.memory,
            workflow=workflow,
        )
        return handle
