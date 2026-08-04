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
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ApprovalResponse:
    allowed: bool
    remember: bool = False  # allow all remaining calls of this capability this turn
    remember_all: bool = False  # allow all remaining calls of this capability this session
    message: str = ""  # user-typed feedback / instructions (plan_review only)
    mode: str | None = None  # selected execution mode for a plan-review handoff
    scope_grant: str | None = None  # target_turn | target_session | target_once


class ApprovalService:
    """Session-scoped approval coordinator.

    One instance per session.  ApprovalGate calls request_approval()
    (agent-side, async) and ApprovalOverlay calls respond() (TUI-side, sync).

    Concurrent approvals are serialised via an asyncio.Lock so that parallel
    tool calls don't race on the single pending_approval signal slot.
    """

    def __init__(self, app_state: AppState) -> None:
        self._app_state = app_state
        self._response: ApprovalResponse | None = None
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

        # Serialise concurrent approvals.
        async with self._lock:
            self._response = None
            self._app_state.pending_approval.set(req)
            try:
                await req.event.wait()
                response = self._response or ApprovalResponse(allowed=False)
                self._response = None
                if response.remember_all:
                    self._remembered_all = self._remembered_all | req.capabilities
                elif response.remember:
                    self._remembered_turn = self._remembered_turn | req.capabilities
                if req.workspace_access and response.allowed:
                    keys = {(str(item.canonical), item.operation) for item in req.workspace_access}
                    if response.scope_grant == "target_session":
                        self._scope_session.update(keys)
                    elif response.scope_grant == "target_turn":
                        self._scope_turn.update(keys)
                return response
            finally:
                # Cancellation/error paths must release the modal and resume
                # the cached display clock. Identity-guard the clear so a
                # future concurrent owner cannot be removed accidentally.
                if self._app_state.pending_approval() is req:
                    self._app_state.pending_approval.set(None)

    def respond(
        self,
        allowed: bool,
        *,
        remember: bool = False,
        remember_all: bool = False,
        message: str = "",
        mode: str | None = None,
        scope_grant: str | None = None,
    ) -> None:
        """TUI-side (sync): called from ApprovalOverlay / PlanApprovalOverlay."""
        self._response = ApprovalResponse(
            allowed=allowed,
            remember=remember,
            remember_all=remember_all,
            message=message,
            mode=mode,
            scope_grant=scope_grant,
        )
        pending = self._app_state.pending_approval()
        if pending is not None:
            pending.event.set()

    def reset_turn_memory(self) -> None:
        """Clear per-turn blanket approvals at the start of each new agent turn."""
        self._remembered_turn = frozenset()
        self._scope_turn.clear()


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
