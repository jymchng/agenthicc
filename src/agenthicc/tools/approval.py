"""Tool approval system — soft-block requiring explicit human confirmation (PRD-78).

Flow for a side-effecting or unclassified tool in Safe mode:

1. ToolCapabilityGate runs first — if blocked, returns abort() and this module
   never fires.
2. ApprovalGate.before_tool_call() checks mode.approval_required.
3. If the tool's capabilities intersect approval_required, ApprovalService
   .request_approval() is called.  The calling coroutine suspends on
   asyncio.Event.wait() — the event loop remains free.
4. ApprovalOverlay is shown; user presses y/a/A/n.
5. ApprovalOverlay.handle_key() calls ApprovalService.respond(), which
   sets the event.  The suspended coroutine resumes.
6. ApprovalGate returns proceed() or abort() based on the response.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass, field
from dataclasses import replace
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from agenthicc.tools.context import ToolCallContext

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, WorkspaceAccessRequest

__all__ = [
    "ApprovalRequest",
    "ApprovalResponse",
    "ApprovalService",
    "ApprovalGate",
]


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, object]
    capabilities: frozenset[str]  # capability values that triggered the approval
    event: asyncio.Event = field(compare=False, hash=False)
    kind: str = "tool"  # "tool" | "plan_review" — controls which overlay is shown
    mode_options: tuple[str, ...] = ()
    """Canonical execution modes offered by a plan-review overlay, if any."""
    workspace_access: tuple["WorkspaceAccessRequest", ...] = ()
    """Exact outside-workspace accesses that caused this approval request."""
    request_id: str = ""
    """Stable identity used to reject stale overlay responses."""
    timeout_s: float | None = None
    """Effective question timeout, populated by :class:`ApprovalService`."""
    deadline_at: float | None = None
    """Restart-safe wall-clock deadline for a question request."""
    question_fingerprint: str | None = None
    """Bounded non-sensitive identity for repeated-timeout protection."""


@dataclass(frozen=True)
class ApprovalResponse:
    allowed: bool
    remember: bool = False  # allow all remaining calls of this capability this turn
    remember_all: bool = False  # allow all remaining calls of this capability this session
    message: str = ""  # user-typed feedback / instructions (plan_review only)
    mode: str | None = None  # selected execution mode for a plan-review handoff
    scope_grant: str | None = None  # target_turn | target_session | target_once
    outcome: str = ""  # answered | cancelled | timed_out | failed
    timed_out: bool = False
    decision_required: bool = False
    request_id: str | None = None
    timeout_s: float | None = None
    repeated_timeout: bool = False


class ApprovalService:
    """Session-scoped approval coordinator.

    One instance per session.  ApprovalGate calls request_approval()
    (agent-side, async) and ApprovalOverlay calls respond() (TUI-side, sync).

    Concurrent approvals are serialised via an asyncio.Lock so that parallel
    tool calls don't race on the single pending_approval signal slot.
    """

    def __init__(
        self,
        app_state: AppState,
        question_timeout_s: float = 60.0,
        *,
        clock: Callable[[], float] | None = None,
        question_wait_records: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        if (
            isinstance(question_timeout_s, bool)
            or not isinstance(question_timeout_s, (int, float))
            or not math.isfinite(float(question_timeout_s))
            or float(question_timeout_s) <= 0
        ):
            raise ValueError("tools.question_timeout_s must be a finite number greater than zero")
        self._app_state = app_state
        self._question_timeout_s = float(question_timeout_s)
        self._clock: Callable[[], float] = clock or time.time
        self._rehydrated_question_waits = dict(question_wait_records or {})
        self._response: ApprovalResponse | None = None
        self._responses: dict[str, ApprovalResponse] = {}
        self._active_request: ApprovalRequest | None = None
        self._timed_out_question_fingerprints: set[str] = set()
        self._remembered_turn: frozenset[str] = frozenset()
        self._remembered_all: frozenset[str] = frozenset()
        self._scope_turn: set[tuple[str, str]] = set()
        self._scope_session: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        """Agent-side: suspend until the user responds."""
        # Fast path — capability already blanket-approved in this session/turn.
        # Important: empty capabilities must never match (frozenset() <= frozenset()
        # is True in Python, which would silently auto-approve plan reviews and
        # any other non-capability request before the overlay is shown).
        if (
            not req.workspace_access
            and req.capabilities
            and req.capabilities <= self._remembered_all
        ):
            return ApprovalResponse(allowed=True)
        if (
            not req.workspace_access
            and req.capabilities
            and req.capabilities <= self._remembered_turn
        ):
            return ApprovalResponse(allowed=True)
        if req.workspace_access:
            keys = {(str(item.canonical), item.operation) for item in req.workspace_access}
            if keys <= self._scope_session or keys <= self._scope_turn:
                return ApprovalResponse(allowed=True, scope_grant="target_once")

        # Serialise concurrent approvals.  Questions use this same boundary as
        # ordinary approvals, but only questions receive the bounded deadline.
        async with self._lock:
            response: ApprovalResponse | None = None
            fingerprint = req.question_fingerprint
            resumed = (
                self._rehydrated_question_waits.pop(fingerprint, None)
                if req.kind == "questions" and fingerprint
                else None
            )
            resumed_deadline = (
                resumed.get("deadline_at")
                if isinstance(resumed, Mapping)
                and isinstance(resumed.get("deadline_at"), (int, float))
                else None
            )
            resumed_timeout_value = (
                resumed.get("timeout_s") if isinstance(resumed, Mapping) else None
            )
            resumed_timeout = (
                float(resumed_timeout_value)
                if isinstance(resumed_timeout_value, (int, float))
                and not isinstance(resumed_timeout_value, bool)
                else self._question_timeout_s
            )
            request_id = (
                str(resumed.get("request_id"))
                if isinstance(resumed, Mapping) and resumed.get("request_id")
                else req.request_id or req.tool_use_id or uuid.uuid4().hex
            )
            if req.kind == "questions":
                # Publish the durable identity even when the record is already
                # expired or has timed out before this process was restarted.
                # Terminal lifecycle events must refer to the same request that
                # produced the original wait_started event.
                object.__setattr__(req, "request_id", request_id)
                object.__setattr__(req, "timeout_s", resumed_timeout)
                object.__setattr__(
                    req,
                    "deadline_at",
                    (
                        float(resumed_deadline)
                        if isinstance(resumed_deadline, (int, float))
                        else float(self._clock()) + resumed_timeout
                    ),
                )
            if req.kind == "questions" and fingerprint in self._timed_out_question_fingerprints:
                response = self._timeout_response(
                    request_id=request_id,
                    timeout_s=self._question_timeout_s,
                    repeated=True,
                )
                self._emit_question_event("question_timed_out", req, response)
                return response

            effective_timeout = self._question_timeout_s
            if isinstance(resumed_deadline, (int, float)):
                remaining = float(resumed_deadline) - float(self._clock())
                if remaining <= 0:
                    response = self._timeout_response(
                        request_id=request_id,
                        timeout_s=resumed_timeout,
                        repeated=True,
                    )
                    if fingerprint:
                        self._timed_out_question_fingerprints.add(fingerprint)
                    self._emit_question_event("question_timed_out", req, response)
                    return response
                effective_timeout = remaining

            if req.kind == "questions":
                # Keep the original request object as the reactive overlay
                # identity.  ApprovalRequest is frozen for callers, but these
                # service-owned lifecycle fields are deliberately populated
                # before publication so existing identity-based integrations
                # continue to work.
                object.__setattr__(req, "request_id", request_id)
                object.__setattr__(req, "timeout_s", effective_timeout)
                object.__setattr__(
                    req,
                    "deadline_at",
                    (
                        float(resumed_deadline)
                        if isinstance(resumed_deadline, (int, float))
                        else float(self._clock()) + effective_timeout
                    ),
                )
                effective_req = req
            else:
                # Preserve object identity for existing overlay integrations;
                # only question requests need enriched deadline metadata.
                effective_req = req
            self._active_request = effective_req
            self._response = None
            self._take_response(request_id)
            self._app_state.pending_approval.set(effective_req)
            if effective_req.kind == "questions":
                self._emit_question_event("question_wait_started", effective_req, None)
            try:
                if effective_req.kind == "questions":
                    try:
                        await asyncio.wait_for(
                            effective_req.event.wait(), timeout=effective_timeout
                        )
                        response = self._take_response(request_id)
                    except asyncio.TimeoutError:
                        # A response committed before the timeout wake-up wins
                        # the clean race.  No await occurs between this lookup
                        # and the terminal timeout commit.
                        response = self._take_response(request_id)
                        if response is None:
                            response = self._timeout_response(
                                request_id=request_id,
                                timeout_s=effective_timeout,
                            )
                            if fingerprint:
                                self._timed_out_question_fingerprints.add(fingerprint)
                            self._emit_question_event("question_timed_out", effective_req, response)
                else:
                    await effective_req.event.wait()
                    response = self._take_response(request_id) or self._response
                response = response or ApprovalResponse(
                    allowed=False,
                    outcome="failed",
                    request_id=request_id,
                )
                response = self._normalise_response(response, effective_req)
                self._response = None
                if response.remember_all:
                    self._remembered_all = self._remembered_all | effective_req.capabilities
                elif response.remember:
                    self._remembered_turn = self._remembered_turn | effective_req.capabilities
                if effective_req.workspace_access and response.allowed:
                    keys = {
                        (str(item.canonical), item.operation)
                        for item in effective_req.workspace_access
                    }
                    if response.scope_grant == "target_session":
                        self._scope_session.update(keys)
                    elif response.scope_grant == "target_turn":
                        self._scope_turn.update(keys)
                if effective_req.kind == "questions" and not response.timed_out:
                    event_name = (
                        "question_answered"
                        if response.outcome == "answered"
                        else "question_cancelled"
                    )
                    self._emit_question_event(event_name, effective_req, response)
                return response
            except asyncio.CancelledError:
                if effective_req.kind == "questions":
                    cancelled = ApprovalResponse(
                        allowed=False,
                        outcome="cancelled",
                        request_id=request_id,
                    )
                    self._emit_question_event("question_cancelled", effective_req, cancelled)
                raise
            except Exception:
                if effective_req.kind == "questions":
                    failed = ApprovalResponse(
                        allowed=False,
                        outcome="failed",
                        request_id=request_id,
                    )
                    self._emit_question_event("question_wait_failed", effective_req, failed)
                raise
            finally:
                # Cancellation/error paths must release the modal and resume
                # the cached display clock. Identity-guard the clear so a
                # future concurrent owner cannot be removed accidentally.
                if self._app_state.pending_approval() is effective_req:
                    self._app_state.pending_approval.set(None)
                self._active_request = None

    def respond(
        self,
        allowed: bool,
        *,
        remember: bool = False,
        remember_all: bool = False,
        message: str = "",
        mode: str | None = None,
        scope_grant: str | None = None,
        request_id: str | None = None,
        outcome: str | None = None,
    ) -> bool:
        """TUI-side (sync): called from ApprovalOverlay / PlanApprovalOverlay."""
        response = ApprovalResponse(
            allowed=allowed,
            remember=remember,
            remember_all=remember_all,
            message=message,
            mode=mode,
            scope_grant=scope_grant,
            outcome=outcome or "",
            request_id=request_id,
        )
        pending = self._app_state.pending_approval()
        if pending is None:
            # Preserve the legacy diagnostic behavior: callers may inspect the
            # last response even when the overlay disappeared before delivery.
            # A request-scoped response is still rejected below when another
            # request is active, so stale callbacks cannot pollute it.
            self._response = response
            return False
        pending_id = pending.request_id or pending.tool_use_id
        if request_id is not None and request_id != pending_id:
            # A stale overlay must never answer a later request.
            return False
        self._response = response
        self._responses[pending_id] = response
        if self._active_request is pending:
            pending.event.set()
            return True
        return False

    def respond_for_request(
        self,
        request_id: str,
        allowed: bool,
        *,
        remember: bool = False,
        remember_all: bool = False,
        message: str = "",
        mode: str | None = None,
        scope_grant: str | None = None,
        outcome: str | None = None,
    ) -> bool:
        """Respond only if *request_id* is still the active request."""
        return self.respond(
            allowed,
            remember=remember,
            remember_all=remember_all,
            message=message,
            mode=mode,
            scope_grant=scope_grant,
            request_id=request_id,
            outcome=outcome,
        )

    def _take_response(self, request_id: str) -> ApprovalResponse | None:
        if request_id not in self._responses:
            return None
        return self._responses.pop(request_id)

    def _normalise_response(
        self, response: ApprovalResponse, req: ApprovalRequest
    ) -> ApprovalResponse:
        if req.kind != "questions":
            return response
        outcome = response.outcome or ("answered" if response.allowed else "cancelled")
        return replace(
            response,
            outcome=outcome,
            request_id=response.request_id or req.request_id,
            timeout_s=response.timeout_s or req.timeout_s,
        )

    def _timeout_response(
        self, *, request_id: str, timeout_s: float, repeated: bool = False
    ) -> ApprovalResponse:
        return ApprovalResponse(
            allowed=False,
            message=(
                "The user did not answer before the configured deadline. "
                "Choose the safest reasonable option from the available context, "
                "state the assumption, and do not ask the same question again "
                "solely because it timed out."
            ),
            outcome="timed_out",
            timed_out=True,
            decision_required=True,
            request_id=request_id,
            timeout_s=timeout_s,
            repeated_timeout=repeated,
        )

    def _emit_question_event(
        self,
        kind: str,
        req: ApprovalRequest,
        response: ApprovalResponse | None,
    ) -> None:
        """Persist bounded question lifecycle metadata through the TUI event stream."""
        conversation = self._app_state.conversation
        add_event = conversation.append_event
        question_count = 0
        raw_questions = (
            req.tool_input.get("questions") if isinstance(req.tool_input, dict) else None
        )
        if isinstance(raw_questions, list):
            question_count = min(len(raw_questions), 32)
        payload: dict[str, object] = {
            "request_id": req.request_id or req.tool_use_id,
            "outcome": response.outcome if response is not None else "pending",
            "timeout_s": req.timeout_s,
            "deadline_at": req.deadline_at,
            "question_count": question_count,
            "question_fingerprint": req.question_fingerprint or "",
        }
        if response is not None:
            payload["timed_out"] = response.timed_out
            payload["decision_required"] = response.decision_required
            payload["repeated_timeout"] = response.repeated_timeout
        try:
            add_event(
                kind=kind,
                payload=payload,
                event_id=f"question:{req.request_id or req.tool_use_id}:{kind}",
            )
        except Exception:  # noqa: BLE001
            # Lifecycle telemetry must never block or fail the interaction.
            return

    def reset_turn_memory(self) -> None:
        """Clear per-turn blanket approvals at the start of each new agent turn."""
        self._remembered_turn = frozenset()
        self._scope_turn.clear()
        self._timed_out_question_fingerprints.clear()


class ApprovalGate:
    """Soft-block: pauses tool execution and asks the user for approval.

    Registered as the second global hook after ToolCapabilityGate.
    If ToolCapabilityGate aborts (hard block), this hook never runs.
    """

    def __init__(
        self,
        app_state: AppState,
        service: ApprovalService,
        workspace_access: "WorkspaceAccessPolicy | None" = None,
    ) -> None:
        self._app_state = app_state
        self._service = service
        self._workspace_access = workspace_access

    async def before_tool_call(self, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import BeforeToolHookDecision  # noqa: PLC0415
        from agenthicc.tools.capabilities import (  # noqa: PLC0415
            CAPABILITIES_KEY,
            classify_tool_capabilities,
        )

        mode = self._app_state.active_mode()
        required = mode.approval_required
        raw_caps = ctx.get_metadata(CAPABILITIES_KEY)
        tool_caps = classify_tool_capabilities(raw_caps)
        needs_approval = tool_caps & required

        policy = self._workspace_access
        if policy is None:
            from agenthicc.tools.workspace_access import current_workspace_access  # noqa: PLC0415

            policy = current_workspace_access()
        if policy is not None:
            path_result = await policy.authorize_tool(
                ctx.tool_name,
                ctx.tool_input,
                frozenset(needs_approval),
            )
            metadata = ctx.metadata
            if isinstance(metadata, dict) and path_result.decisions:
                access_metadata = path_result.to_dict()
                access_metadata["workspace_root"] = str(policy.scope.primary_root)
                access_metadata["mode"] = policy.mode_name
                metadata["workspace_access"] = access_metadata
            if not path_result.allowed:
                return BeforeToolHookDecision.abort(
                    {
                        "ok": False,
                        "code": path_result.code,
                        "error": f"{path_result.code}: {path_result.error}",
                    }
                )
            if path_result.approval_handled:
                return BeforeToolHookDecision.proceed()

        # PRD-79: --dangerously-skip-permissions bypasses ordinary capability
        # prompts, but deliberately cannot turn Safe into Yolo or bypass the
        # workspace boundary approval above.
        if self._app_state.cli_flags.dangerously_skip_permissions:
            return BeforeToolHookDecision.proceed()

        if not needs_approval:
            return BeforeToolHookDecision.proceed()

        req = ApprovalRequest(
            tool_name=ctx.tool_name,
            tool_use_id=getattr(ctx, "tool_use_id", "") or "",
            tool_input=dict(ctx.tool_input or {}),
            capabilities=frozenset(needs_approval),
            event=asyncio.Event(),
        )
        response = await self._service.request_approval(req)
        if response.allowed:
            return BeforeToolHookDecision.proceed()
        return BeforeToolHookDecision.abort(
            {
                "ok": False,
                "error": f"User denied permission to run '{ctx.tool_name}'.",
            }
        )

    async def after_tool_call(self, result: object, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import AfterToolHookDecision  # noqa: PLC0415

        return AfterToolHookDecision.proceed()

    async def on_tool_error(self, exc: Exception, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import ErrorToolHookDecision  # noqa: PLC0415

        return ErrorToolHookDecision.reraise()
