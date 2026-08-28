"""Shared phase publication, boundary, and resume primitives.

The workflow engine already owns the durable handle and the reactive TUI state.
This module deliberately does not introduce another state machine or storage
format.  It only centralises the ordering-sensitive calls that custom runners
must make so generated workflows and built-ins use the same lifecycle.

Runtime phase values are dynamic execution state.  They are projected to
``AppState`` and ``WorkflowRunHandle``; they are never appended to a stable
prompt prefix.  A boundary checkpoint is distinct from the phase-entry
checkpoint written by :meth:`WorkflowRunHandle.update_phase`.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable, Sequence
from typing import Protocol, cast

__all__ = [
    "PhaseAnnotation",
    "PhaseBoundaryError",
    "ResumeResolution",
    "checkpoint_phase_boundary",
    "publish_phase_annotation",
    "reconcile_phase_cursor",
]

log = logging.getLogger(__name__)


class _WorkflowHandle(Protocol):
    def attach_context(self, context: object) -> None: ...

    def update_phase(
        self,
        phase: str | None,
        index: int,
        iteration: int,
        *,
        persist: bool = True,
    ) -> None: ...

    def save_checkpoint(self, *, reason: str = "") -> object: ...


class _AppState(Protocol):
    def update_workflow_phase(
        self,
        *,
        workflow_name: str,
        phase_name: str,
        phase_index: int,
        total_phases: int,
        run_id: str,
        intent: str,
        model_id: str,
    ) -> None: ...


@dataclasses.dataclass(frozen=True, slots=True)
class PhaseAnnotation:
    """Validated dynamic projection for the phase currently being entered."""

    workflow_name: str
    phase_name: str
    phase_index: int
    total_phases: int
    run_id: str
    intent: str
    model_id: str
    phase_iteration: int
    phase_attempt: int = 0
    status: str = "running"
    plan_version: str = ""

    def __post_init__(self) -> None:
        if not self.workflow_name.strip():
            raise ValueError("phase annotation workflow_name must not be empty")
        if not self.phase_name.strip():
            raise ValueError("phase annotation phase_name must not be empty")
        if not self.run_id.strip():
            raise ValueError("phase annotation run_id must not be empty")
        if self.phase_index < 0 or self.total_phases < 1:
            raise ValueError("phase annotation plan position is invalid")
        if self.phase_index >= self.total_phases:
            raise ValueError("phase annotation phase_index exceeds total_phases")
        if self.phase_iteration < 0 or self.phase_attempt < 0:
            raise ValueError("phase annotation iteration and attempt must be non-negative")
        if not self.status.strip():
            raise ValueError("phase annotation status must not be empty")


class PhaseBoundaryError(RuntimeError):
    """A boundary could not be durably checkpointed.

    The original exception is chained.  Runners must let this reach the
    session-owned failure finalizer; a UI update is never evidence of durable
    progress.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class ResumeResolution:
    """Pure result of reconciling durable phase evidence with a cursor."""

    phase_name: str
    phase_index: int
    completed_phases: tuple[str, ...]
    source: str
    reconciled: bool
    diagnostic: str = ""


def publish_phase_annotation(
    config: object,
    annotation: PhaseAnnotation,
    context: object,
    *,
    display_name: str | None = None,
    persist_entry: bool = True,
) -> None:
    """Publish one phase to the existing handle and AppState contracts.

    ``persist_entry`` is normally true.  Boundary code uses false while it
    positions the handle for a single explicit post-phase checkpoint, avoiding
    a misleading second phase-start write.
    """

    try:
        handle_value = object.__getattribute__(config, "workflow_handle")
    except AttributeError:
        handle_value = None
    if handle_value is not None:
        handle = cast(_WorkflowHandle, handle_value)
        handle.attach_context(context)
        handle.update_phase(
            annotation.phase_name,
            annotation.phase_index,
            annotation.phase_iteration,
            persist=persist_entry,
        )

    try:
        app_state_value = object.__getattribute__(config, "app_state")
    except AttributeError:
        app_state_value = None
    try:
        update = (
            object.__getattribute__(app_state_value, "update_workflow_phase")
            if app_state_value is not None
            else None
        )
    except AttributeError:
        update = None
    if callable(update):
        app_state = cast(_AppState, app_state_value)
        app_state.update_workflow_phase(
            workflow_name=annotation.workflow_name,
            phase_name=display_name or annotation.phase_name,
            phase_index=annotation.phase_index,
            total_phases=annotation.total_phases,
            run_id=annotation.run_id,
            intent=annotation.intent,
            model_id=annotation.model_id,
        )


def checkpoint_phase_boundary(
    config: object,
    context: object,
    *,
    completed_phase: str,
    next_phase: str | None,
    phase_index: int,
    phase_iteration: int,
    outcome: str = "completed",
) -> object | None:
    """Persist a completed phase boundary through the existing run handle.

    The context must already contain the selected next state and committed
    phase output.  With no handle (for a deliberately lightweight headless
    adapter) there is no durable store to call, so this returns ``None``; a
    real session always supplies a handle and receives a durable checkpoint.
    """

    if not completed_phase.strip():
        raise ValueError("completed_phase must not be empty")
    safe_outcome = " ".join(str(outcome).split())[:96] or "completed"
    completed = completed_phase.strip()[:128]
    next_name = next_phase.strip()[:128] if isinstance(next_phase, str) else ""
    reason = f"phase_boundary:{completed}:{safe_outcome}"
    boundary_key = "|".join(
        (completed, next_name, str(phase_index), str(phase_iteration), safe_outcome)
    )
    try:
        handle_value = object.__getattribute__(config, "workflow_handle")
    except AttributeError:
        handle_value = None
    if handle_value is None:
        return None

    handle = cast(_WorkflowHandle, handle_value)
    try:
        checkpoint_supported = object.__getattribute__(handle, "checkpoint_supported")
    except AttributeError:
        checkpoint_supported = True
    if checkpoint_supported is False:
        raise PhaseBoundaryError(
            f"could not persist boundary after phase {completed_phase!r}: "
            "workflow checkpointing is unavailable"
        )
    # A runner may be re-entered after a successful boundary (for example, a
    # retrying framework callback).  Do not turn the same durable transition
    # into a second checkpoint revision.  Runners keep this marker in their
    # typed context, so the rule does not require a process-local cache.
    try:
        marker = object.__getattribute__(context, "last_boundary")
    except AttributeError:
        marker = None
    if isinstance(marker, dict) and marker.get("boundary_key") == boundary_key:
        if marker.get("durable") is True:
            return None

    try:
        handle.attach_context(context)
        # Checkpoint fields use the selected next cursor.  Terminal transitions
        # have no executable current phase but retain the last phase index for
        # auditability.
        handle.update_phase(
            next_phase,
            phase_index,
            phase_iteration,
            persist=False,
        )
        checkpoint = handle.save_checkpoint(reason=reason)
        if isinstance(marker, dict):
            marker["boundary_key"] = boundary_key
            marker["durable"] = True
            try:
                revision = object.__getattribute__(checkpoint, "revision")
            except AttributeError:
                revision = checkpoint.get("revision") if isinstance(checkpoint, dict) else None
            if isinstance(revision, int) and not isinstance(revision, bool):
                marker["checkpoint_revision"] = revision
        # The checkpoint store is authoritative. Journal metadata is useful
        # for reconciliation after an older cursor, but a journal filesystem
        # failure must not turn an already durable boundary into a false
        # failure or cause the phase's side effects to be repeated.
        try:
            conversation = object.__getattribute__(handle, "conversation")
            journal = object.__getattribute__(conversation, "journal")
            record_boundary = object.__getattribute__(journal, "workflow_phase_boundary")
            try:
                plan_version = object.__getattribute__(context, "plan_version")
            except AttributeError:
                plan_version = ""
            record_boundary(
                object.__getattribute__(handle, "run_id"),
                object.__getattribute__(handle, "workflow_name"),
                completed_phase=completed,
                next_phase=next_phase,
                phase_index=phase_index,
                phase_iteration=phase_iteration,
                outcome=safe_outcome,
                plan_version=str(plan_version),
                boundary_key=boundary_key,
            )
        except (AttributeError, TypeError):
            # Lightweight/headless adapters and legacy journal implementations
            # do not necessarily expose the optional auxiliary index.
            pass
        except Exception as exc:  # noqa: BLE001 - primary checkpoint is safe
            log.warning("workflow boundary journal write failed: %s", type(exc).__name__)
        return checkpoint
    except Exception as exc:  # noqa: BLE001 - preserve the framework boundary
        raise PhaseBoundaryError(
            f"could not persist boundary after phase {completed_phase!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _normalise_names(values: Iterable[str], allowed: set[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        name = str(value).strip().lower()
        if name in allowed and name not in result:
            result.append(name)
    return tuple(result)


def reconcile_phase_cursor(
    phase_names: Sequence[str],
    current_phase: str | None,
    *,
    completed_phases: Iterable[str] = (),
    receipt_phases: Iterable[str] = (),
    journal_phases: Iterable[str] = (),
    terminal_phase: str = "",
    preserve_current: bool = False,
) -> ResumeResolution:
    """Resolve a cursor from a canonical plan and durable phase evidence.

    Receipts are considered only when they form a contiguous prefix of the
    canonical plan.  A later receipt therefore proves that an older checkpoint
    is stale, while an isolated or out-of-order record cannot make the runner
    skip work.  A valid partial cursor is retained when no later committed
    prefix exists.  ``preserve_current`` is used for explicit rejection/
    re-entry cursors whose downstream receipts were invalidated.
    """

    names = tuple(str(name).strip().lower() for name in phase_names if str(name).strip())
    if not names or len(set(names)) != len(names):
        raise ValueError("phase_names must be non-empty and unique")
    allowed = set(names)
    current = str(current_phase or "").strip().lower()
    completed = _normalise_names(completed_phases, allowed)
    receipts = _normalise_names(receipt_phases, allowed)
    journal = _normalise_names(journal_phases, allowed)
    # Evidence sources are all durable observations of the same ordered plan.
    # Combining their verified phase names lets a journal advance a checkpoint
    # that predates the manifest (and vice versa) without allowing an isolated
    # out-of-order record to skip the contiguous-prefix check below.
    evidence = set(receipts) | set(completed) | set(journal)

    prefix_length = 0
    while prefix_length < len(names) and names[prefix_length] in evidence:
        prefix_length += 1
    prefix = names[:prefix_length]

    if preserve_current and current in allowed:
        selected = current
        source = "reentry_cursor"
    elif evidence:
        selected = (
            names[prefix_length]
            if prefix_length < len(names)
            else str(terminal_phase).strip().lower()
        )
        if not selected:
            selected = names[-1]
        source = (
            "phase_receipts"
            if receipts
            else "workflow_journal"
            if journal
            else "durable_phase_state"
        )
    elif current in allowed:
        selected = current
        source = "checkpoint_cursor"
    else:
        selected = names[0]
        source = "safe_fallback"

    index = names.index(selected) if selected in allowed else len(names)
    reconciled = bool(current and selected != current)
    diagnostic = (
        f"resume cursor {current or '<missing>'} resolved to {selected} from {source}; "
        f"verified contiguous prefix length={prefix_length}"
        if reconciled or source in {"phase_receipts", "safe_fallback"}
        else ""
    )
    return ResumeResolution(
        phase_name=selected,
        phase_index=index,
        completed_phases=prefix,
        source=source,
        reconciled=reconciled,
        diagnostic=diagnostic,
    )
