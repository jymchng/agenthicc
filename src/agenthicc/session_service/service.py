"""Client-neutral session coordinator and event projection (PRD-150)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeAlias

from .models import (
    CommandResult,
    EventDurability,
    SessionCommand,
    SessionError,
    SessionEvent,
    SessionSnapshot,
    SessionState,
)
from .store import SessionEventStore

__all__ = ["SessionEventStore", "SessionService", "SessionSubscription"]

TurnHandler: TypeAlias = Callable[[SessionCommand, str, "SessionService"], Awaitable[None]]
_SENSITIVE = re.compile(
    r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|cookie|private[_-]?key)"
)
_TERMINAL_STATES = {SessionState.COMPLETED, SessionState.FAILED, SessionState.CANCELLED}
log = logging.getLogger(__name__)


def _redact(value: object, key: str = "") -> object:
    if _SENSITIVE.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact(child, str(child_key)) for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, tuple):
        return [_redact(child) for child in value]
    if isinstance(value, str) and len(value) > 64_000:
        return value[:63_999] + "…"
    return value


def _redacted_mapping(payload: Mapping[str, object]) -> dict[str, object]:
    redacted = _redact(payload)
    if not isinstance(redacted, Mapping):
        raise TypeError("redaction of a mapping must produce a mapping")
    return {str(key): value for key, value in redacted.items()}


def _count(value: object, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _canonical_path(path: str | Path) -> str:
    """Resolve *path* to its canonical absolute form (touches the filesystem)."""
    return str(Path(path).resolve())


def _read_lines(path: Path) -> list[str] | None:
    """Return the lines of *path*, or ``None`` when it does not exist."""
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readlines()
    except FileNotFoundError:
        return None


@dataclass(eq=False)
class SessionSubscription:
    """Bounded async event subscription used by in-process and HTTP clients."""

    service: SessionService
    session_id: str
    client_id: str
    capabilities: frozenset[str]
    queue: asyncio.Queue[SessionEvent | SessionError | None]
    closed: bool = False

    def __aiter__(self) -> AsyncIterator[SessionEvent]:
        return self

    async def __anext__(self) -> SessionEvent:
        item = await self.queue.get()
        if item is None:
            self.closed = True
            raise StopAsyncIteration
        if isinstance(item, SessionError):
            self.closed = True
            raise item
        return item

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.service.unsubscribe(self)
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


@dataclass
class _Runtime:
    snapshot: SessionSnapshot
    events: list[SessionEvent]
    command_results: dict[str, CommandResult]
    earliest_sequence: int
    deleted: bool = False


class SessionService:
    """Single source of truth for client-neutral session coordination.

    The service owns only the session command/projection contract. Agent
    execution remains in the existing runner and workflow boundaries through a
    registered turn handler.
    """

    def __init__(
        self,
        *,
        store: SessionEventStore | None = None,
        store_root: str | Path | None = None,
        background_inbox: object | None = None,
        default_capabilities: frozenset[str] | None = None,
        max_subscription_queue: int = 256,
    ) -> None:
        self.store = store or SessionEventStore(store_root)
        self.background_inbox = background_inbox
        self.default_capabilities = (
            default_capabilities
            if default_capabilities is not None
            else frozenset({"read", "control"})
        )
        self.max_subscription_queue = max(8, max_subscription_queue)
        self._runtimes: dict[str, _Runtime] = {}
        self._subscriptions: dict[str, set[SessionSubscription]] = {}
        self._turn_handlers: dict[str, TurnHandler] = {}
        self._turn_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        # Session runtimes are deliberately materialized on demand.  The
        # store's small metadata index lets listing and selection avoid reading
        # every historical JSONL event stream during process startup.
        self._metadata = self.store.session_metadata()

    async def _ensure_runtime_locked(self, session_id: str) -> _Runtime:
        """Materialize one session while the service lock is held."""
        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            return runtime
        if not self.store.exists(session_id):
            raise SessionError("not_found", f"session not found: {session_id}", status=404)
        events = self.store.all_events(session_id)
        if not events:
            raise SessionError("not_found", f"session not found: {session_id}", status=404)
        runtime = self._runtime_from_events(events)
        self._runtimes[session_id] = runtime
        self._subscriptions.setdefault(session_id, set())
        self._update_metadata(runtime)
        return runtime

    @staticmethod
    def _metadata_for_runtime(runtime: _Runtime) -> dict[str, object]:
        snapshot = runtime.snapshot
        return {
            "session_id": snapshot.session_id,
            "project_root": snapshot.project_root,
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
            "state": snapshot.state.value,
            "last_event_sequence": snapshot.last_event_sequence,
            "capabilities": list(snapshot.capabilities),
            "deleted": runtime.deleted,
        }

    @staticmethod
    def _snapshot_from_metadata(metadata: Mapping[str, object]) -> SessionSnapshot:
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session metadata has no session_id")
        raw_state = metadata.get("state", SessionState.CREATED.value)
        try:
            state = SessionState(raw_state if isinstance(raw_state, str) else "created")
        except ValueError:
            state = SessionState.CREATED
        capabilities = metadata.get("capabilities", [])
        safe_capabilities = (
            tuple(item for item in capabilities if isinstance(item, str))
            if isinstance(capabilities, (list, tuple))
            else ()
        )
        project_root = metadata.get("project_root", "")
        created_at_value = metadata.get("created_at")
        updated_at_value = metadata.get("updated_at")
        sequence_value = metadata.get("last_event_sequence")
        return SessionSnapshot(
            schema_version=1,
            session_id=session_id,
            project_root=project_root if isinstance(project_root, str) else "",
            created_at=(
                float(created_at_value) if isinstance(created_at_value, (int, float)) else 0.0
            ),
            updated_at=(
                float(updated_at_value) if isinstance(updated_at_value, (int, float)) else 0.0
            ),
            state=state,
            last_event_sequence=sequence_value if isinstance(sequence_value, int) else 0,
            capabilities=safe_capabilities,
        )

    def _update_metadata(self, runtime: _Runtime) -> None:
        """Publish a small redacted index record after a durable change."""
        metadata = self._metadata_for_runtime(runtime)
        self._metadata[runtime.snapshot.session_id] = metadata
        try:
            self.store.update_session_metadata(runtime.snapshot.session_id, metadata)
        except (OSError, ValueError) as exc:
            # The event was already durably appended.  The next selected
            # session access can reconstruct this acceleration record.
            log.warning("session metadata update failed: %s", exc)

    @staticmethod
    def _initial_snapshot(
        session_id: str,
        *,
        project_root: str,
        parent_session_id: str | None = None,
        capabilities: frozenset[str] = frozenset(),
    ) -> SessionSnapshot:
        now = time.time()
        return SessionSnapshot(
            schema_version=1,
            session_id=session_id,
            project_root=str(Path(project_root).resolve()),
            created_at=now,
            updated_at=now,
            state=SessionState.CREATED,
            parent_session_id=parent_session_id,
            capabilities=tuple(sorted(capabilities)),
        )

    def _runtime_from_events(self, events: list[SessionEvent]) -> _Runtime:
        first = events[0]
        payload = first.payload
        project_root = payload.get("project_root")
        parent = payload.get("parent_session_id")
        capabilities = payload.get("capabilities", [])
        capability_set = (
            frozenset(item for item in capabilities if isinstance(item, str))
            if isinstance(capabilities, (list, tuple, set, frozenset))
            else frozenset()
        )
        snapshot = self._initial_snapshot(
            first.session_id,
            project_root=project_root if isinstance(project_root, str) else ".",
            parent_session_id=parent if isinstance(parent, str) else None,
            capabilities=capability_set,
        )
        runtime = _Runtime(snapshot, [], {}, events[0].sequence)
        for event in events:
            runtime.events.append(event)
            runtime.snapshot = self._apply_event(runtime.snapshot, event)
            self._restore_command_result(runtime, event)
            if event.kind == "session_deleted":
                runtime.deleted = True
        return runtime

    @staticmethod
    def _restore_command_result(runtime: _Runtime, event: SessionEvent) -> None:
        if event.kind != "command_accepted":
            return
        key = event.payload.get("idempotency_key")
        result = event.payload.get("result")
        if not isinstance(key, str) or not isinstance(result, Mapping):
            return
        runtime.command_results[key] = CommandResult(
            ok=bool(result.get("ok", True)),
            command_id=str(result.get("command_id", "")),
            session_id=(
                str(result["session_id"]) if isinstance(result.get("session_id"), str) else None
            ),
            code=str(result.get("code", "ok")),
            message=str(result.get("message", "")),
            data=dict(result.get("data", {})) if isinstance(result.get("data"), Mapping) else {},
        )

    @staticmethod
    def _apply_event(snapshot: SessionSnapshot, event: SessionEvent) -> SessionSnapshot:
        payload = event.payload
        state = snapshot.state
        active_turn_id = snapshot.active_turn_id
        workflow = dict(snapshot.workflow)
        agent = dict(snapshot.agent)
        queue = dict(snapshot.queue)
        approvals = snapshot.approvals_pending
        questions = snapshot.questions_pending
        jobs = snapshot.background_jobs_running
        terminals = snapshot.terminals_running
        project_root = snapshot.project_root
        parent = snapshot.parent_session_id
        capabilities = snapshot.capabilities
        readiness = dict(snapshot.readiness)

        if event.kind == "session_created":
            state = SessionState.IDLE
            value = payload.get("project_root")
            if isinstance(value, str):
                project_root = value
            value = payload.get("parent_session_id")
            if isinstance(value, str):
                parent = value
            raw_capabilities = payload.get("capabilities")
            if isinstance(raw_capabilities, (list, tuple, set)):
                capabilities = tuple(
                    sorted(item for item in raw_capabilities if isinstance(item, str))
                )
            raw_agent = payload.get("agent")
            if isinstance(raw_agent, Mapping):
                agent = dict(raw_agent)
            raw_workflow = payload.get("workflow")
            if isinstance(raw_workflow, Mapping):
                workflow = dict(raw_workflow)
        elif event.kind in {"turn_queued", "turn_started"}:
            state = SessionState.RUNNING
            event_turn_id = event.turn_id or payload.get("turn_id")
            active_turn_id = event_turn_id if isinstance(event_turn_id, str) else active_turn_id
            if event.kind == "turn_queued":
                queue["depth"] = _count(queue.get("depth")) + 1
        elif event.kind == "turn_completed":
            state = SessionState.IDLE
            active_turn_id = None
            queue["depth"] = max(0, _count(queue.get("depth")) - 1)
        elif event.kind in {"cancel_requested", "turn_failed", "turn_cancelled", "cancelled"}:
            state = SessionState.CANCELLED if event.kind != "turn_failed" else SessionState.FAILED
            if event.kind != "cancel_requested":
                active_turn_id = None
                queue["depth"] = max(0, _count(queue.get("depth")) - 1)
        elif event.kind == "waiting_approval":
            state = SessionState.WAITING_APPROVAL
            approvals += 1
        elif event.kind == "waiting_question":
            state = SessionState.WAITING_QUESTION
            questions += 1
        elif event.kind in {"approval_resolved", "answer_resolved"}:
            approvals = max(0, approvals - (1 if event.kind == "approval_resolved" else 0))
            questions = max(0, questions - (1 if event.kind == "answer_resolved" else 0))
            state = SessionState.RUNNING
        elif event.kind in {"session_resumed", "retry_scheduled"}:
            state = SessionState.RUNNING
        elif event.kind == "session_archived":
            state = SessionState.ARCHIVED
        elif event.kind == "session_deleted":
            state = SessionState.ARCHIVED
        elif event.kind == "session_completed":
            state = SessionState.COMPLETED
        elif event.kind == "workflow_phase_changed":
            workflow.update({key: value for key, value in payload.items() if key != "secret"})
        elif event.kind == "session_readiness_changed":
            provider_status = payload.get("provider_history_status")
            provider_ready = payload.get("provider_history_ready")
            workflow_status = payload.get("workflow_recovery_status")
            workflow_resumable = payload.get("workflow_resumable")
            if isinstance(provider_status, str) and provider_status in {
                "unknown",
                "ready",
                "repaired",
                "repair_required",
                "irrecoverable",
                "journal_corrupt",
            }:
                readiness["provider_history_status"] = provider_status
            if provider_ready is None or isinstance(provider_ready, bool):
                readiness["provider_history_ready"] = provider_ready
            if isinstance(workflow_status, str) and workflow_status in {
                "unknown",
                "none",
                "resumable",
                "blocked",
            }:
                readiness["workflow_recovery_status"] = workflow_status
            if workflow_resumable is None or isinstance(workflow_resumable, bool):
                readiness["workflow_resumable"] = workflow_resumable
            readiness["session_viewable"] = True
        elif event.kind == "workflow_recovery_available":
            count = _count(payload.get("count"))
            invalid_ids = payload.get("invalid_run_ids", [])
            has_invalid = isinstance(invalid_ids, (list, tuple)) and bool(invalid_ids)
            readiness["workflow_recovery_status"] = (
                "resumable" if count else "blocked" if has_invalid else "none"
            )
            readiness["workflow_resumable"] = count > 0
        elif event.kind == "workflow_paused_after_error":
            readiness["workflow_recovery_status"] = "resumable"
            readiness["workflow_resumable"] = True
        elif event.kind == "workflow_resume_started":
            readiness["workflow_recovery_status"] = "unknown"
            readiness["workflow_resumable"] = None
        elif event.kind in {"workflow_run_completed", "workflow_discarded"}:
            readiness["workflow_recovery_status"] = "none"
            readiness["workflow_resumable"] = False
        elif event.kind == "workflow_failure_diagnostic":
            readiness["workflow_recovery_status"] = "blocked"
            readiness["workflow_resumable"] = False
        elif event.kind == "agent_changed":
            agent.update({key: value for key, value in payload.items() if key != "secret"})
        elif event.kind == "job_changed":
            running_count = payload.get("running_count", jobs)
            if isinstance(running_count, int) and not isinstance(running_count, bool):
                jobs = running_count
        elif event.kind == "terminal_changed":
            running_count = payload.get("running_count", terminals)
            if isinstance(running_count, int) and not isinstance(running_count, bool):
                terminals = running_count
        return replace(
            snapshot,
            updated_at=event.occurred_at,
            state=state,
            active_turn_id=active_turn_id,
            parent_session_id=parent,
            project_root=project_root,
            workflow=workflow,
            agent=agent,
            queue=queue,
            approvals_pending=approvals,
            questions_pending=questions,
            background_jobs_running=jobs,
            terminals_running=terminals,
            last_event_sequence=max(snapshot.last_event_sequence, event.sequence),
            capabilities=capabilities,
            readiness=readiness,
        )

    @staticmethod
    def _require(capabilities: frozenset[str], required: str) -> None:
        if required not in capabilities and "admin" not in capabilities:
            raise SessionError("forbidden", f"client lacks {required!r} capability", status=403)

    @staticmethod
    def _project_payload(
        payload: Mapping[str, object], capabilities: frozenset[str]
    ) -> dict[str, object]:
        if "private" not in capabilities and "admin" not in capabilities:
            return _redacted_mapping(payload)
        return dict(payload)

    def _project_snapshot(
        self, snapshot: SessionSnapshot, capabilities: frozenset[str]
    ) -> SessionSnapshot:
        self._require(capabilities, "read")
        return replace(
            snapshot,
            project_root=snapshot.project_root
            if "workspace" in capabilities or "admin" in capabilities
            else "<workspace>",
            workflow=self._project_payload(snapshot.workflow, capabilities),
            agent=self._project_payload(snapshot.agent, capabilities),
        )

    def _runtime(self, session_id: str) -> _Runtime:
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            raise SessionError("not_found", f"session not found: {session_id}", status=404)
        return runtime

    def _active_runtime(self, session_id: str) -> _Runtime:
        runtime = self._runtime(session_id)
        if runtime.deleted:
            raise SessionError("not_found", f"session not found: {session_id}", status=404)
        return runtime

    async def create_session(
        self,
        *,
        project_root: str | Path = ".",
        client_id: str = "local",
        capabilities: frozenset[str] | None = None,
        agent: Mapping[str, object] | None = None,
        workflow: Mapping[str, object] | None = None,
        parent_session_id: str | None = None,
    ) -> SessionSnapshot:
        caps = capabilities if capabilities is not None else self.default_capabilities
        self._require(caps, "control")
        session_id = f"sess_{uuid.uuid4().hex}"
        async with self._lock:
            runtime = _Runtime(
                self._initial_snapshot(
                    session_id,
                    project_root=str(project_root),
                    parent_session_id=parent_session_id,
                    capabilities=caps,
                ),
                [],
                {},
                1,
            )
            self._runtimes[session_id] = runtime
            self._subscriptions[session_id] = set()
            await self._append_locked(
                runtime,
                source="session_service",
                kind="session_created",
                payload={
                    "project_root": await asyncio.to_thread(_canonical_path, project_root),
                    "client_id": client_id,
                    "capabilities": sorted(caps),
                    "agent": dict(agent or {}),
                    "workflow": dict(workflow or {}),
                    "parent_session_id": parent_session_id,
                },
            )
            runtime.snapshot = replace(
                runtime.snapshot,
                agent=dict(agent or {}),
                workflow=dict(workflow or {}),
            )
            self._update_metadata(runtime)
            return self._project_snapshot(runtime.snapshot, caps)

    async def ensure_session(
        self,
        session_id: str,
        *,
        project_root: str | Path = ".",
        capabilities: frozenset[str] | None = None,
    ) -> SessionSnapshot:
        """Adopt an existing runner session into the service projection."""

        caps = capabilities if capabilities is not None else self.default_capabilities
        async with self._lock:
            existing = self._runtimes.get(session_id)
            if existing is None and self.store.exists(session_id):
                existing = await self._ensure_runtime_locked(session_id)
            if existing is not None:
                return self._project_snapshot(existing.snapshot, caps)
            runtime = _Runtime(
                self._initial_snapshot(
                    session_id,
                    project_root=str(project_root),
                    capabilities=caps,
                ),
                [],
                {},
                1,
            )
            self._runtimes[session_id] = runtime
            self._subscriptions[session_id] = set()
            await self._append_locked(
                runtime,
                source="session_service",
                kind="session_created",
                payload={
                    "project_root": await asyncio.to_thread(_canonical_path, project_root),
                    "capabilities": sorted(caps),
                },
            )
            return self._project_snapshot(runtime.snapshot, caps)

    async def snapshot(
        self, session_id: str, *, capabilities: frozenset[str] | None = None
    ) -> SessionSnapshot:
        caps = capabilities if capabilities is not None else self.default_capabilities
        async with self._lock:
            await self._ensure_runtime_locked(session_id)
            runtime = self._active_runtime(session_id)
            return self._project_snapshot(runtime.snapshot, caps)

    async def list_sessions(
        self,
        *,
        project_root: str | Path | None = None,
        capabilities: frozenset[str] | None = None,
    ) -> list[SessionSnapshot]:
        caps = capabilities if capabilities is not None else self.default_capabilities
        self._require(caps, "read")
        target = (
            await asyncio.to_thread(_canonical_path, project_root)
            if project_root is not None
            else None
        )
        async with self._lock:
            # Refresh only bounded index metadata. This picks up sessions
            # created by another client without replaying their event logs.
            self._metadata = self.store.session_metadata()
            snapshots: list[SessionSnapshot] = []
            for session_id, metadata in self._metadata.items():
                if bool(metadata.get("deleted", False)):
                    continue
                runtime = self._runtimes.get(session_id)
                snapshot = (
                    runtime.snapshot
                    if runtime is not None
                    else self._snapshot_from_metadata(metadata)
                )
                if target is None or snapshot.project_root == target:
                    snapshots.append(self._project_snapshot(snapshot, caps))
        return sorted(snapshots, key=lambda item: item.updated_at, reverse=True)

    def set_background_inbox(self, inbox: object) -> None:
        """Attach the durable background-interaction inbox to this service."""

        self.background_inbox = inbox

    @staticmethod
    def _project_inbox_item(
        item: Mapping[str, object], capabilities: frozenset[str]
    ) -> dict[str, object]:
        view = dict(item)
        if "workspace" in capabilities or "admin" in capabilities:
            return view
        view["project_root"] = "<workspace>"
        view["workspace"] = "<workspace>"
        target = view.get("target")
        if isinstance(target, Mapping):
            projected_target = dict(target)
            accesses = projected_target.get("workspace_access")
            if isinstance(accesses, list):
                projected_target["workspace_access"] = [
                    {"canonical": "<workspace>", "operation": entry.get("operation", "")}
                    if isinstance(entry, Mapping)
                    else {}
                    for entry in accesses
                ]
            view["target"] = projected_target
        return view

    async def inbox_items(
        self,
        *,
        project_root: str | Path | None = None,
        item_type: str | None = None,
        older_than_s: float | None = None,
        status: str | None = None,
        limit: int = 100,
        capabilities: frozenset[str] | None = None,
    ) -> list[dict[str, object]]:
        """List durable operator interactions under the caller's read scope."""

        from agenthicc.background.inbox import BackgroundInteractionInbox

        caps = capabilities if capabilities is not None else self.default_capabilities
        self._require(caps, "read")
        inbox = self.background_inbox
        if not isinstance(inbox, BackgroundInteractionInbox):
            raise SessionError(
                "unavailable", "background interaction inbox is not configured", status=503
            )
        try:
            await asyncio.to_thread(inbox.reconcile)
            items = await asyncio.to_thread(
                inbox.list_items,
                project_root=project_root,
                item_type=item_type,
                older_than_s=older_than_s,
                status=status,
                limit=limit,
            )
        except (TypeError, ValueError) as exc:
            raise SessionError("invalid_request", str(exc), status=400) from exc
        result: list[dict[str, object]] = []
        for item in items:
            result.append(self._project_inbox_item(item.to_dict(), caps))
        return result

    async def respond_to_inbox(
        self,
        item_id: str,
        response: Mapping[str, object],
        *,
        expected_version: int,
        idempotency_key: str,
        capabilities: frozenset[str] | None = None,
    ) -> dict[str, object]:
        """Commit one exact inbox response or return its current conflict view."""

        from agenthicc.background.inbox import (
            BackgroundInteractionInbox,
            InboxConflict,
        )

        caps = capabilities if capabilities is not None else self.default_capabilities
        self._require(caps, "control")
        inbox = self.background_inbox
        if not isinstance(inbox, BackgroundInteractionInbox):
            raise SessionError(
                "unavailable", "background interaction inbox is not configured", status=503
            )
        try:
            item, replay = await asyncio.to_thread(
                inbox.respond,
                item_id,
                response,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
        except KeyError as exc:
            raise SessionError("not_found", f"inbox item not found: {item_id}", status=404) from exc
        except InboxConflict as exc:
            current = (
                self._project_inbox_item(exc.current.to_dict(), caps)
                if exc.current is not None
                else {}
            )
            raise SessionError(
                "conflict", str(exc), status=409, details={"current": current}
            ) from exc
        except (TypeError, ValueError) as exc:
            raise SessionError("invalid_request", str(exc), status=400) from exc
        return {
            "item": self._project_inbox_item(item.to_dict(), caps),
            "idempotent_replay": replay,
        }

    async def expire_inbox_item(
        self,
        item_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        reason: str = "expired by operator",
        capabilities: frozenset[str] | None = None,
    ) -> dict[str, object]:
        """Expire one exact pending item with control-scope enforcement."""

        from agenthicc.background.inbox import (
            BackgroundInteractionInbox,
            InboxConflict,
        )

        caps = capabilities if capabilities is not None else self.default_capabilities
        self._require(caps, "control")
        inbox = self.background_inbox
        if not isinstance(inbox, BackgroundInteractionInbox):
            raise SessionError(
                "unavailable", "background interaction inbox is not configured", status=503
            )
        try:
            item = await asyncio.to_thread(
                inbox.expire,
                item_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                reason=reason,
            )
        except KeyError as exc:
            raise SessionError("not_found", f"inbox item not found: {item_id}", status=404) from exc
        except InboxConflict as exc:
            current = (
                self._project_inbox_item(exc.current.to_dict(), caps)
                if exc.current is not None
                else {}
            )
            raise SessionError(
                "conflict", str(exc), status=409, details={"current": current}
            ) from exc
        except (TypeError, ValueError) as exc:
            raise SessionError("invalid_request", str(exc), status=400) from exc
        return self._project_inbox_item(item.to_dict(), caps)

    async def events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        capabilities: frozenset[str] | None = None,
    ) -> list[SessionEvent]:
        caps = capabilities if capabilities is not None else self.default_capabilities
        self._require(caps, "read")
        async with self._lock:
            await self._ensure_runtime_locked(session_id)
            runtime = self._active_runtime(session_id)
            if runtime.events and after_sequence < runtime.earliest_sequence - 1:
                raise SessionError(
                    "replay_gap",
                    f"events before sequence {runtime.earliest_sequence} are compacted",
                    status=409,
                )
            return [
                self._project_event(event, caps)
                for event in runtime.events
                if event.sequence > after_sequence and event.durability == EventDurability.DURABLE
            ]

    async def compact(self, session_id: str, *, before_sequence: int) -> int:
        """Compact durable history and advance the service replay boundary."""

        async with self._lock:
            await self._ensure_runtime_locked(session_id)
            runtime = self._active_runtime(session_id)
            earliest = self.store.compact(session_id, before_sequence=before_sequence)
            if earliest:
                runtime.events = [event for event in runtime.events if event.sequence >= earliest]
                runtime.earliest_sequence = earliest
            return earliest

    def _project_event(self, event: SessionEvent, capabilities: frozenset[str]) -> SessionEvent:
        self._require(capabilities, "read")
        if (
            event.visibility == "private"
            and "private" not in capabilities
            and "admin" not in capabilities
        ):
            raise SessionError("forbidden", "event is private", status=403)
        return replace(event, payload=self._project_payload(event.payload, capabilities))

    async def subscribe(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        client_id: str = "local",
        capabilities: frozenset[str] | None = None,
        max_queue: int | None = None,
    ) -> SessionSubscription:
        caps = capabilities if capabilities is not None else self.default_capabilities
        replay = await self.events(session_id, after_sequence=after_sequence, capabilities=caps)
        queue: asyncio.Queue[SessionEvent | SessionError | None] = asyncio.Queue(
            maxsize=max_queue or self.max_subscription_queue
        )
        subscription = SessionSubscription(self, session_id, client_id, caps, queue)
        for event in replay:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull as exc:
                raise SessionError(
                    "replay_overflow", "subscription replay exceeds queue capacity", status=409
                ) from exc
        async with self._lock:
            self._subscriptions.setdefault(session_id, set()).add(subscription)
        return subscription

    async def unsubscribe(self, subscription: SessionSubscription) -> None:
        async with self._lock:
            self._subscriptions.get(subscription.session_id, set()).discard(subscription)

    def register_turn_handler(self, session_id: str, handler: TurnHandler) -> None:
        self._active_runtime(session_id)
        self._turn_handlers[session_id] = handler

    async def publish(
        self,
        session_id: str,
        *,
        source: str,
        kind: str,
        payload: Mapping[str, object] | None = None,
        turn_id: str | None = None,
        durability: EventDurability = EventDurability.DURABLE,
        visibility: str = "session",
    ) -> SessionEvent:
        async with self._lock:
            runtime = await self._ensure_runtime_locked(session_id)
            if runtime.deleted:
                raise SessionError("not_found", f"session not found: {session_id}", status=404)
            return await self._append_locked(
                runtime,
                source=source,
                kind=kind,
                payload=payload,
                turn_id=turn_id,
                durability=durability,
                visibility=visibility,
            )

    async def publish_kernel_event(self, session_id: str, event: object) -> SessionEvent:
        """Project one applied kernel event into the neutral event vocabulary."""

        event_type = getattr(event, "event_type", None)
        payload = getattr(event, "payload", {})
        if not isinstance(event_type, str) or not event_type:
            raise SessionError("invalid_event", "kernel event has no event_type")
        if not isinstance(payload, Mapping):
            payload = {}
        kind = re.sub(r"(?<!^)(?=[A-Z])", "_", event_type).lower()
        raw_turn_id = payload.get("turn_id")
        return await self.publish(
            session_id,
            source="kernel",
            kind=kind,
            payload=payload,
            turn_id=raw_turn_id if isinstance(raw_turn_id, str) else None,
        )

    async def import_kernel_log(self, session_id: str, path: str | Path) -> int:
        """Import a legacy kernel JSONL log once for compatibility attachment."""

        lines = await asyncio.to_thread(_read_lines, Path(path))
        if lines is None:
            return 0
        async with self._lock:
            runtime = await self._ensure_runtime_locked(session_id)
            if any(event.source == "kernel" for event in runtime.events):
                return 0
        from agenthicc.kernel import Event

        imported = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                kernel_event = Event.from_dict(json.loads(line))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            await self.publish_kernel_event(session_id, kernel_event)
            imported += 1
        return imported

    async def _append_locked(
        self,
        runtime: _Runtime,
        *,
        source: str,
        kind: str,
        payload: Mapping[str, object] | None = None,
        turn_id: str | None = None,
        durability: EventDurability = EventDurability.DURABLE,
        visibility: str = "session",
    ) -> SessionEvent:
        event = SessionEvent.create(
            session_id=runtime.snapshot.session_id,
            sequence=runtime.snapshot.last_event_sequence + 1,
            source=source,
            kind=kind,
            payload=_redacted_mapping(dict(payload or {})),
            turn_id=turn_id,
            durability=durability,
            visibility=visibility,
        )
        if durability == EventDurability.DURABLE:
            self.store.append(event)
        runtime.events.append(event)
        runtime.snapshot = self._apply_event(runtime.snapshot, event)
        if runtime.earliest_sequence == 0:
            runtime.earliest_sequence = event.sequence
        self._update_metadata(runtime)
        await self._notify(runtime, event)
        return event

    async def _notify(self, runtime: _Runtime, event: SessionEvent) -> None:
        for subscription in list(self._subscriptions.get(runtime.snapshot.session_id, set())):
            if subscription.closed:
                continue
            try:
                subscription.queue.put_nowait(self._project_event(event, subscription.capabilities))
            except (asyncio.QueueFull, SessionError):
                while not subscription.queue.empty():
                    try:
                        subscription.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                subscription.queue.put_nowait(
                    SessionError("backpressure", "subscription queue overflow", status=409)
                )
                subscription.closed = True

    async def submit(self, command: SessionCommand | Mapping[str, object]) -> CommandResult:
        if isinstance(command, Mapping):
            command = SessionCommand.from_mapping(command)
        session_id = command.session_id
        if not session_id:
            raise SessionError("invalid_command", "session_id is required")
        handler: TurnHandler | None = None
        turn_id: str | None = None
        cancel_task: asyncio.Task[None] | None = None
        async with self._lock:
            runtime = await self._ensure_runtime_locked(session_id)
            existing = runtime.command_results.get(command.idempotency_key)
            if existing is not None:
                return replace(existing, replayed=True)
            if runtime.deleted:
                raise SessionError("not_found", f"session not found: {session_id}", status=404)
            required = "read" if command.kind == "attach" else "control"
            self._require(command.capabilities, required)
            if (
                command.expected_sequence is not None
                and command.expected_sequence != runtime.snapshot.last_event_sequence
            ):
                raise SessionError(
                    "stale_sequence",
                    f"expected sequence {command.expected_sequence}, current is {runtime.snapshot.last_event_sequence}",
                    status=409,
                )
            if runtime.snapshot.state == SessionState.ARCHIVED and command.kind not in {
                "attach",
                "resume",
            }:
                raise SessionError(
                    "invalid_state", "archived sessions accept only attach or resume"
                )
            data: dict[str, object] = {}
            if command.kind == "attach":
                data["snapshot"] = self._project_snapshot(
                    runtime.snapshot, command.capabilities
                ).to_dict()
                result = CommandResult(True, command.command_id, session_id, data=data)
            elif command.kind == "submit_message":
                text = command.payload.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise SessionError("invalid_command", "submit_message requires non-empty text")
                turn_id = f"turn_{uuid.uuid4().hex}"
                await self._append_locked(
                    runtime,
                    source="session_service",
                    kind="turn_queued",
                    payload={"text": text, "client_id": command.client_id},
                    turn_id=turn_id,
                )
                data = {"accepted": True, "turn_id": turn_id}
                result = CommandResult(True, command.command_id, session_id, data=data)
                handler = self._turn_handlers.get(session_id)
            elif command.kind in {"cancel", "interrupt"}:
                await self._append_locked(
                    runtime,
                    source="session_service",
                    kind="cancel_requested",
                    payload={"client_id": command.client_id},
                    turn_id=runtime.snapshot.active_turn_id,
                )
                data = {"cancel_requested": True}
                result = CommandResult(True, command.command_id, session_id, data=data)
                cancel_task = self._turn_tasks.get(session_id)
            elif command.kind in {"approve", "reject", "answer"}:
                kind = (
                    "approval_resolved"
                    if command.kind in {"approve", "reject"}
                    else "answer_resolved"
                )
                await self._append_locked(
                    runtime, source="session_service", kind=kind, payload=command.payload
                )
                result = CommandResult(
                    True, command.command_id, session_id, data={"resolved": True}
                )
            elif command.kind in {"resume", "retry"}:
                kind = "session_resumed" if command.kind == "resume" else "retry_scheduled"
                await self._append_locked(
                    runtime, source="session_service", kind=kind, payload=command.payload
                )
                result = CommandResult(
                    True,
                    command.command_id,
                    session_id,
                    data={"state": runtime.snapshot.state.value},
                )
            elif command.kind == "archive":
                await self._append_locked(
                    runtime, source="session_service", kind="session_archived", payload={}
                )
                result = CommandResult(
                    True, command.command_id, session_id, data={"state": "archived"}
                )
            elif command.kind == "invoke_command":
                await self._append_locked(
                    runtime,
                    source="session_service",
                    kind="command_invoked",
                    payload=command.payload,
                )
                result = CommandResult(
                    True, command.command_id, session_id, data={"accepted": True}
                )
            elif command.kind == "fork":
                child = await self._fork_locked(runtime, command)
                result = CommandResult(
                    True, command.command_id, session_id, data={"session_id": child}
                )
            elif command.kind == "delete":
                await self._append_locked(
                    runtime,
                    source="session_service",
                    kind="session_deleted",
                    payload={"client_id": command.client_id},
                )
                result = CommandResult(True, command.command_id, session_id, data={"deleted": True})
                runtime.deleted = True
                self._update_metadata(runtime)
                subscriptions = self._subscriptions.pop(session_id, set())
                for subscription in subscriptions:
                    subscription.closed = True
                    while not subscription.queue.empty():
                        try:
                            subscription.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    subscription.queue.put_nowait(None)
            else:
                raise SessionError("unknown_command", f"unknown session command: {command.kind}")
            runtime.command_results[command.idempotency_key] = result
            await self._append_locked(
                runtime,
                source="session_service",
                kind="command_accepted",
                payload={
                    "idempotency_key": command.idempotency_key,
                    "result": result.to_dict(),
                },
            )
        if handler is not None and turn_id is not None:
            task = asyncio.create_task(
                self._run_turn_handler(session_id, command, turn_id, handler),
                name=f"session-turn-{turn_id}",
            )
            self._turn_tasks[session_id] = task
            task.add_done_callback(lambda _: self._turn_tasks.pop(session_id, None))
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        return result

    async def _run_turn_handler(
        self, session_id: str, command: SessionCommand, turn_id: str, handler: TurnHandler
    ) -> None:
        await self.publish(
            session_id, source="session_service", kind="turn_started", turn_id=turn_id
        )
        try:
            await handler(command, turn_id, self)
        except asyncio.CancelledError:
            await self.publish(
                session_id, source="session_service", kind="turn_cancelled", turn_id=turn_id
            )
            raise
        except Exception as exc:  # noqa: BLE001
            await self.publish(
                session_id,
                source="session_service",
                kind="turn_failed",
                payload={"error_type": type(exc).__name__, "error": str(exc)},
                turn_id=turn_id,
            )
        else:
            await self.publish(
                session_id, source="session_service", kind="turn_completed", turn_id=turn_id
            )

    async def _fork_locked(self, runtime: _Runtime, command: SessionCommand) -> str:
        child_id = f"sess_{uuid.uuid4().hex}"
        child_snapshot = self._initial_snapshot(
            child_id,
            project_root=runtime.snapshot.project_root,
            parent_session_id=runtime.snapshot.session_id,
            capabilities=command.capabilities,
        )
        child = _Runtime(child_snapshot, [], {}, 1)
        self._runtimes[child_id] = child
        self._subscriptions[child_id] = set()
        await self._append_locked(
            child,
            source="session_service",
            kind="session_created",
            payload={
                "project_root": runtime.snapshot.project_root,
                "parent_session_id": runtime.snapshot.session_id,
                "forked_from_sequence": runtime.snapshot.last_event_sequence,
                "capabilities": sorted(command.capabilities),
            },
        )
        return child_id

    async def close(self) -> None:
        tasks = list(self._turn_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for subscriptions in list(self._subscriptions.values()):
            for subscription in list(subscriptions):
                await subscription.close()
