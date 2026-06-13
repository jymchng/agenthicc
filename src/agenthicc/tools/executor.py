"""AgenthiccToolExecutor — permissioned, hooked, budgeted tool dispatch (PRD-04)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from agenthicc.kernel import Event, EventProcessor

from .base import Tool, ToolResultEnvelope
from .hooks import HookRunner, RecoveryAction

__all__ = ["AgenthiccToolExecutor", "PermissionChecker", "TOOL_ENTITY"]

#: Entity type under which tool-call hooks are registered in the HookRegistry.
TOOL_ENTITY = "tool"

#: callable(tool_name, args, ctx) -> bool | None.  False denies the call;
#: True or None allows it.
PermissionChecker = Callable[[str, dict[str, Any], dict[str, Any]], bool | None]


class AgenthiccToolExecutor:
    """Dispatches tool calls through the full PRD-04 pipeline:

    permission check -> before-hooks -> ``asyncio.wait_for`` execution ->
    after-hooks (on success) / error-hooks with recovery (on failure),
    emitting ``ToolCallStarted`` / ``ToolCallComplete`` / ``PermissionDenied``
    events to the kernel :class:`EventProcessor`.
    """

    def __init__(
        self,
        event_processor: EventProcessor | None,
        hook_runner: HookRunner | None = None,
        permission_checker: PermissionChecker | None = None,
        tool_call_budget: int = 8,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._events = event_processor
        self._hooks = hook_runner or HookRunner()
        self._permission_checker = permission_checker
        self._budget = tool_call_budget
        self._timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        tool: Tool,
        args: dict[str, Any],
        ctx: dict[str, Any],
    ) -> ToolResultEnvelope:
        """Execute a single tool call and return its result envelope.

        Never raises for tool failures: every outcome (denial, rejection,
        timeout, exception) is reported through the envelope.
        """
        tool_call_id = uuid4().hex
        start = time.perf_counter()

        def envelope(ok: bool, value: Any = None, error: str | None = None) -> ToolResultEnvelope:
            return ToolResultEnvelope(
                tool_call_id=tool_call_id,
                tool_name=tool.name,
                ok=ok,
                value=value,
                error=error,
                duration_ms=(time.perf_counter() - start) * 1000.0,
            )

        # 1. Permission check (False denies; True/None allows).
        if self._permission_checker is not None:
            allowed = self._permission_checker(tool.name, args, ctx)
            if allowed is False:
                await self._emit(
                    "PermissionDenied",
                    {"tool_name": tool.name, "args": args},
                    tool_call_id,
                )
                return envelope(False, error=f"permission_denied: {tool.name}")

        await self._emit(
            "ToolCallStarted", {"tool_name": tool.name, "args": args}, tool_call_id
        )

        # 2. Before-hooks: first Rejection aborts the call.
        rejection = await self._hooks.run_before(TOOL_ENTITY, tool, ctx)
        if rejection is not None:
            env = envelope(False, error=f"rejected: {rejection.reason}")
            await self._emit_complete(env)
            return env

        # 3. Execute with timeout; error-hooks may recover (RETRY honored once).
        retried = False
        while True:
            try:
                value = await asyncio.wait_for(
                    tool.execute(args, ctx), timeout=self._timeout_seconds
                )
            except Exception as exc:  # noqa: BLE001 — every failure becomes an envelope
                action = await self._hooks.run_error(TOOL_ENTITY, tool, exc, ctx)
                if action is RecoveryAction.RETRY and not retried:
                    retried = True
                    continue
                if action is RecoveryAction.FALLBACK:
                    env = envelope(True, value=ctx.get("fallback_value"))
                elif action is RecoveryAction.SKIP:
                    env = envelope(True, value=None)
                else:  # ESCALATE or no recovery suggested
                    try:
                        from agenthicc.tools.mcp import McpToolCallError  # noqa: PLC0415
                        if isinstance(exc, McpToolCallError):
                            error_str = str(exc)  # already has human-readable message
                        else:
                            error_str = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                    except ImportError:
                        error_str = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                    env = envelope(False, error=error_str)
                await self._emit_complete(env)
                return env
            else:
                await self._hooks.run_after(TOOL_ENTITY, tool, value, ctx)
                env = envelope(True, value=value)
                await self._emit_complete(env)
                return env

    async def execute_parallel(
        self,
        calls: list[tuple[Tool, dict[str, Any]]],
        ctx: dict[str, Any],
    ) -> list[ToolResultEnvelope]:
        """Fan out independent calls via ``asyncio.gather``, capped by the
        per-turn ``tool_call_budget``.  Calls beyond the budget are not run
        and receive ``budget_exceeded`` error envelopes."""
        within = calls[: self._budget]
        excess = calls[self._budget :]

        results: list[ToolResultEnvelope] = list(
            await asyncio.gather(*(self.execute(tool, args, ctx) for tool, args in within))
        )
        for tool, _args in excess:
            results.append(
                ToolResultEnvelope(
                    tool_call_id=uuid4().hex,
                    tool_name=tool.name,
                    ok=False,
                    error=f"budget_exceeded: tool_call_budget={self._budget}",
                )
            )
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _emit(
        self, event_type: str, payload: dict[str, Any], tool_call_id: str
    ) -> None:
        if self._events is None:
            return
        await self._events.emit(
            Event.create(event_type, payload, tool_call_id=tool_call_id)
        )

    async def _emit_complete(self, env: ToolResultEnvelope) -> None:
        await self._emit(
            "ToolCallComplete",
            {
                "tool_name": env.tool_name,
                "ok": env.ok,
                "error": env.error,
                "duration_ms": env.duration_ms,
            },
            env.tool_call_id,
        )
