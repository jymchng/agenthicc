"""ToolCapabilityGate — enforces RuntimeMode restrictions at tool invocation time (PRD-76).

Registered as a global ToolHook on AgentRunnerBase so it fires for every tool
call regardless of which @tool()-decorated function is invoked.

Tools without @set_metadata("capabilities", ...) have no declared capabilities
and pass through the gate unconditionally (open-by-default).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tools.context import ToolCallContext

from agenthicc.tools.capabilities import CAPABILITIES_KEY

__all__ = ["ToolCapabilityGate"]


class ToolCapabilityGate:
    """Blocks tools whose capabilities are not allowed in the current mode.

    Reads RuntimeMode.blocked_capabilities from AppState.active_mode() on
    every invocation — mode changes via Shift+Tab take effect immediately on
    the next tool call, even within the same agent turn.

    When a tool is blocked:
    - BeforeToolHookDecision.abort() is returned.
    - The model receives {"ok": False, "error": "..."} as the tool result.
    - The tool function never executes.
    """

    def __init__(self, app_state: AppState) -> None:
        self._app_state = app_state

    async def before_tool_call(self, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import BeforeToolHookDecision  # noqa: PLC0415

        mode = self._app_state.active_mode()
        blocked = mode.blocked_capabilities
        if not blocked:
            return BeforeToolHookDecision.proceed()

        raw_caps = ctx.get_metadata(CAPABILITIES_KEY)
        tool_caps: frozenset[str] = (
            frozenset(item for item in raw_caps if isinstance(item, str))
            if isinstance(raw_caps, (set, frozenset))
            else frozenset()
        )
        denied = tool_caps & blocked
        if denied:
            caps_str = ", ".join(sorted(denied))
            return BeforeToolHookDecision.abort(
                {
                    "ok": False,
                    "error": (
                        f"Tool '{ctx.tool_name}' requires {caps_str} capability, "
                        f"which is blocked in {mode.name} mode. "
                        f"Switch to Auto or Debug mode to use this tool."
                    ),
                }
            )
        return BeforeToolHookDecision.proceed()

    async def after_tool_call(self, result: object, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import AfterToolHookDecision  # noqa: PLC0415

        return AfterToolHookDecision.proceed()

    async def on_tool_error(self, exc: Exception, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import ErrorToolHookDecision  # noqa: PLC0415

        return ErrorToolHookDecision.reraise()
