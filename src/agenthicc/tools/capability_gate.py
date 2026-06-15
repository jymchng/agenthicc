"""ToolCapabilityGate — enforces RuntimeMode restrictions at tool invocation time (PRD-76).

Registered as a global ToolHook on AgentRunnerBase so it fires for every tool
call regardless of which @tool()-decorated function is invoked.

Tools without @set_metadata("capabilities", ...) have no declared capabilities
and pass through the gate unconditionally (open-by-default).
"""
from __future__ import annotations

from typing import Any

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

    def __init__(self, app_state: Any) -> None:
        self._app_state = app_state

    async def before_tool_call(self, ctx: Any) -> Any:
        from lauren_ai._tools._hooks import BeforeToolHookDecision  # noqa: PLC0415

        mode    = self._app_state.active_mode()
        blocked = mode.blocked_capabilities
        if not blocked:
            return BeforeToolHookDecision.proceed()

        tool_caps: frozenset = ctx.get_metadata(CAPABILITIES_KEY) or frozenset()
        denied = tool_caps & blocked
        if denied:
            caps_str = ", ".join(sorted(denied))
            return BeforeToolHookDecision.abort({
                "ok":    False,
                "error": (
                    f"Tool '{ctx.tool_name}' requires {caps_str} capability, "
                    f"which is blocked in {mode.name} mode. "
                    f"Switch to Auto or Debug mode to use this tool."
                ),
            })
        return BeforeToolHookDecision.proceed()

    async def after_tool_call(self, result: Any, ctx: Any) -> Any:
        from lauren_ai._tools._hooks import AfterToolHookDecision  # noqa: PLC0415
        return AfterToolHookDecision.proceed()

    async def on_tool_error(self, exc: Exception, ctx: Any) -> Any:
        from lauren_ai._tools._hooks import ErrorToolHookDecision  # noqa: PLC0415
        return ErrorToolHookDecision.reraise()
