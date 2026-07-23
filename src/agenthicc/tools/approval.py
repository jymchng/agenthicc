"""Tool approval system — soft-block requiring explicit human confirmation (PRD-78).

Flow for a side-effecting tool in Guard mode:

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


@dataclass(frozen=True)
class ApprovalResponse:
    allowed: bool
    remember: bool = False  # allow all remaining calls of this capability this turn
    remember_all: bool = False  # allow all remaining calls of this capability this session
    message: str = ""  # user-typed feedback / instructions (plan_review only)


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
        self._lock = asyncio.Lock()

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        """Agent-side: suspend until the user responds."""
        # Fast path — capability already blanket-approved in this session/turn.
        # Guard: empty capabilities must never match (frozenset() <= frozenset()
        # is True in Python, which would silently auto-approve plan reviews and
        # any other non-capability request before the overlay is shown).
        if req.capabilities and req.capabilities <= self._remembered_all:
            return ApprovalResponse(allowed=True)
        if req.capabilities and req.capabilities <= self._remembered_turn:
            return ApprovalResponse(allowed=True)

        # Serialise concurrent approvals.
        async with self._lock:
            self._response = None
            self._app_state.pending_approval.set(req)
            await req.event.wait()
            self._app_state.pending_approval.set(None)
            response = self._response or ApprovalResponse(allowed=False)
            self._response = None
            if response.remember_all:
                self._remembered_all = self._remembered_all | req.capabilities
            elif response.remember:
                self._remembered_turn = self._remembered_turn | req.capabilities
            return response

    def respond(
        self,
        allowed: bool,
        *,
        remember: bool = False,
        remember_all: bool = False,
        message: str = "",
    ) -> None:
        """TUI-side (sync): called from ApprovalOverlay / PlanApprovalOverlay."""
        self._response = ApprovalResponse(
            allowed=allowed,
            remember=remember,
            remember_all=remember_all,
            message=message,
        )
        pending = self._app_state.pending_approval()
        if pending is not None:
            pending.event.set()

    def reset_turn_memory(self) -> None:
        """Clear per-turn blanket approvals at the start of each new agent turn."""
        self._remembered_turn = frozenset()


class ApprovalGate:
    """Soft-block: pauses tool execution and asks the user for approval.

    Registered as the second global hook after ToolCapabilityGate.
    If ToolCapabilityGate aborts (hard block), this hook never runs.
    """

    def __init__(self, app_state: AppState, service: ApprovalService) -> None:
        self._app_state = app_state
        self._service = service

    async def before_tool_call(self, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import BeforeToolHookDecision  # noqa: PLC0415
        from agenthicc.tools.capabilities import CAPABILITIES_KEY  # noqa: PLC0415

        # PRD-79: --dangerously-skip-permissions bypasses all approval prompts.
        if getattr(self._app_state, "cli_flags", None) is not None:
            if self._app_state.cli_flags.dangerously_skip_permissions:
                return BeforeToolHookDecision.proceed()

        mode = self._app_state.active_mode()
        required = mode.approval_required
        if not required:
            return BeforeToolHookDecision.proceed()

        raw_caps = ctx.get_metadata(CAPABILITIES_KEY)
        tool_caps: frozenset[str] = (
            frozenset(item for item in raw_caps if isinstance(item, str))
            if isinstance(raw_caps, (set, frozenset))
            else frozenset()
        )
        needs_approval = tool_caps & required
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
