"""Session-owned workflow lifecycle handle (PRD-156)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.workflows.checkpoint import (
    WorkflowCheckpoint,
    context_from_payload,
    context_to_payload,
    workflow_fingerprint,
)

__all__ = ["WorkflowRunHandle", "WorkflowLifecycle"]

WorkflowLifecycle = Literal[
    "running",
    "pausing",
    "paused",
    "resuming",
    "complete",
    "failed",
    "discarded",
]


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
        )

    def attach_context(self, context: object) -> None:
        """Attach the typed context created by a runner."""
        self.context = context

    def update_phase(self, phase: str | None, index: int = 0, iteration: int = 0) -> None:
        """Publish the current phase for UI and checkpoint snapshots."""
        self.current_phase = phase
        self.phase_index = index
        self.phase_iteration = iteration

    def request_pause(self) -> bool:
        """Request a cooperative pause exactly once."""
        if self.lifecycle != "running":
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

    def append_continuation(self, text: str) -> None:
        """Record a queued continuation exactly once in FIFO order."""
        self.continuation_messages.append(text)

    def pop_continuation(self) -> str | None:
        """Claim the oldest queued continuation."""
        if not self.continuation_messages:
            return None
        return self.continuation_messages.pop(0)

    def build_checkpoint(self, *, reason: str = "") -> WorkflowCheckpoint:
        """Build a bounded checkpoint from the current context and journal."""
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
        )

    def save_checkpoint(self, *, reason: str = "") -> WorkflowCheckpoint:
        """Atomically persist the current run and update its revision."""
        checkpoint = self.build_checkpoint(reason=reason)
        self.checkpoint_store.save(checkpoint)
        self.checkpoint_revision = checkpoint.revision
        return checkpoint

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint: WorkflowCheckpoint,
        *,
        workflow: type[object],
        conversation: SessionConversation,
        checkpoint_store: WorkflowCheckpointStore,
        browser_manager: object | None = None,
    ) -> "WorkflowRunHandle":
        """Rehydrate a handle and typed context from a validated checkpoint."""
        expected = workflow_fingerprint(workflow)
        if checkpoint.plugin_fingerprint != expected:
            raise ValueError("workflow plugin fingerprint does not match checkpoint")
        if checkpoint.conversation_id != conversation.conversation_id:
            raise ValueError("workflow checkpoint belongs to a different session conversation")
        if conversation.cursor < checkpoint.conversation_cursor:
            raise ValueError("session conversation is older than the workflow checkpoint")
        handle = cls(
            run_id=checkpoint.run_id,
            workflow_name=checkpoint.workflow_name,
            conversation=conversation,
            original_intent=checkpoint.intent,
            plugin_fingerprint=checkpoint.plugin_fingerprint,
            checkpoint_store=checkpoint_store,
            workflow=workflow,
            lifecycle="paused" if checkpoint.status in {"paused", "pausing"} else checkpoint.status,  # type: ignore[arg-type]
            current_phase=checkpoint.current_phase,
            phase_index=checkpoint.phase_index,
            phase_iteration=checkpoint.phase_iteration,
            checkpoint_revision=checkpoint.revision,
            pause_requested=checkpoint.status in {"paused", "pausing"},
            last_error=checkpoint.reason,
            browser_manager=browser_manager,
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
