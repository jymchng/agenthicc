"""ToolCapabilityGate — enforces RuntimeMode restrictions at tool invocation time (PRD-76).

Registered as a global ToolHook on AgentRunnerBase so it fires for every tool
call regardless of which @tool()-decorated function is invoked.

Tools without @set_metadata("capabilities", ...) are classified as
``ToolCapability.UNDECLARED``.  They require approval in Safe and are blocked
in Plan; Yolo remains unrestricted.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tools.context import ToolCallContext
    from agenthicc.tui.conversation_store import AppState

from agenthicc.tools.capabilities import CAPABILITIES_KEY, classify_tool_capabilities

__all__ = ["ToolCapabilityGate"]


_log = logging.getLogger(__name__)


class FailClosedGate:
    """A security hook whose own failure blocks the tool instead of skipping it.

    lauren-ai logs and *ignores* an exception raised by a ``before_tool_call``
    hook, then runs the tool. A gate that raised would therefore be bypassed.
    Subclasses implement :meth:`decide`; any exception it raises (other than
    cancellation) aborts the call with ``error_code="internal"`` (TOOLR-07).
    """

    async def before_tool_call(self, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import BeforeToolHookDecision

        try:
            return await self.decide(ctx)
        except Exception as exc:  # noqa: BLE001 - failing closed is the point
            _log.error(
                "%s failed for tool %r (%s); the call is blocked",
                type(self).__name__,
                ctx.tool_name,
                ctx.tool_use_id,
                exc_info=exc,
            )
            metadata = ctx.metadata
            if isinstance(metadata, dict):
                metadata["gate_failure"] = {
                    "gate": type(self).__name__,
                    "error": type(exc).__name__,
                }
            return BeforeToolHookDecision.abort(
                {
                    "ok": False,
                    "error_code": "internal",
                    "error": (
                        f"The permission check for '{ctx.tool_name}' failed "
                        f"({type(exc).__name__}); the tool was not run."
                    ),
                }
            )

    async def decide(self, ctx: ToolCallContext) -> object:
        """Return this gate's ``BeforeToolHookDecision`` for *ctx*."""
        raise NotImplementedError

    async def after_tool_call(self, result: object, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import AfterToolHookDecision

        return AfterToolHookDecision.proceed()

    async def on_tool_error(self, exc: Exception, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import ErrorToolHookDecision

        return ErrorToolHookDecision.reraise()


def order_security_gates(hooks: Sequence[object]) -> list[object]:
    """*hooks* with every :class:`FailClosedGate` moved last, in its own order.

    Gates decide on the input the tool will actually receive: a hook that
    rewrites ``tool_input`` must run before them, never after.
    """
    others = [hook for hook in hooks if not isinstance(hook, FailClosedGate)]
    gates = [hook for hook in hooks if isinstance(hook, FailClosedGate)]
    return [*others, *gates]


class ToolCapabilityGate(FailClosedGate):
    """Blocks tools whose capabilities are not allowed in the current mode.

    Reads RuntimeMode.blocked_capabilities from AppState.active_mode() on
    every invocation — mode changes via Shift+Tab take effect immediately on
    the next tool call, even within the same agent turn.

    When a tool is blocked:
    - BeforeToolHookDecision.abort() is returned.
    - The model receives {"ok": False, "error": "..."} as the tool result.
    - The tool function never executes.
    """

    def __init__(
        self,
        app_state: AppState,
        *,
        type_name: str = "",
        type_denied_capabilities: frozenset[str] = frozenset(),
    ) -> None:
        self._app_state = app_state
        self._type_name = type_name
        self._type_denied_capabilities = type_denied_capabilities

    async def decide(self, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import BeforeToolHookDecision

        mode = self._app_state.active_mode()
        blocked = mode.blocked_capabilities
        if not blocked:
            return BeforeToolHookDecision.proceed()

        raw_caps = ctx.get_metadata(CAPABILITIES_KEY)
        tool_caps = classify_tool_capabilities(raw_caps)
        denied = tool_caps & blocked
        if denied:
            caps_str = ", ".join(sorted(denied))
            type_denied = denied & self._type_denied_capabilities
            if type_denied and self._type_name:
                type_caps = ", ".join(sorted(type_denied))
                error = (
                    f"Tool '{ctx.tool_name}' requires {type_caps} capability, blocked by the "
                    f"{self._type_name} type's permissions.deny_capabilities constraint. "
                    "This type-level restriction cannot be widened by the worker."
                )
            else:
                reason = (
                    "has no declared capability metadata"
                    if "undeclared" in denied
                    else f"requires {caps_str} capability"
                )
                error = (
                    f"Tool '{ctx.tool_name}' {reason}, "
                    f"which is blocked in {mode.name} mode. "
                    f"Switch to Yolo mode or annotate it as read-only."
                )
            return BeforeToolHookDecision.abort(
                {"ok": False, "error_code": "blocked_by_mode", "error": error}
            )
        return BeforeToolHookDecision.proceed()
