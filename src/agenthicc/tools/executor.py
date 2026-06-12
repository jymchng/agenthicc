"""AgenthiccToolExecutor — permissioned, hooked, budgeted tool dispatch (PRD-04)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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


class _BridgedEmitter:
    """Proxies EventProcessor.emit() from a worker thread to the main event loop.

    Worker threads run their own event loops and cannot await coroutines on
    the main loop directly.  This class wraps ``emit()`` so that each call
    is submitted to the main loop via ``run_coroutine_threadsafe`` and the
    calling thread blocks until the main loop has processed it.
    """

    def __init__(self, real_events: EventProcessor | None, main_loop: asyncio.AbstractEventLoop) -> None:
        self._real = real_events
        self._loop = main_loop

    async def emit(self, event: Event) -> None:
        if self._real is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._real.emit(event), self._loop)
        fut.result(timeout=10.0)


class AgenthiccToolExecutor:
    """Dispatches tool calls through the full PRD-04 pipeline:

    permission check -> before-hooks -> ``asyncio.wait_for`` execution ->
    after-hooks (on success) / error-hooks with recovery (on failure),
    emitting ``ToolCallStarted`` / ``ToolCallComplete`` / ``PermissionDenied``
    events to the kernel :class:`EventProcessor`.

    Parallel calls are dispatched via a :class:`~concurrent.futures.ThreadPoolExecutor`
    so each tool call runs in its own OS thread.  Event emission from worker
    threads is bridged back to the main event loop transparently.
    """

    def __init__(
        self,
        event_processor: EventProcessor | None,
        hook_runner: HookRunner | None = None,
        permission_checker: PermissionChecker | None = None,
        tool_call_budget: int = 8,
        *,
        timeout_seconds: float = 30.0,
        max_workers: int | None = None,
    ) -> None:
        self._events = event_processor
        self._hooks = hook_runner or HookRunner()
        self._permission_checker = permission_checker
        self._budget = tool_call_budget
        self._timeout_seconds = timeout_seconds
        self._thread_pool = ThreadPoolExecutor(
            max_workers=max_workers if max_workers is not None else tool_call_budget,
            thread_name_prefix="agenthicc-tool",
        )

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the backing thread pool."""
        self._thread_pool.shutdown(wait=wait)

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
                    env = envelope(False, error=f"{type(exc).__name__}: {exc}")
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
        """Fan out independent calls via a :class:`~concurrent.futures.ThreadPoolExecutor`.

        Each call runs in its own OS thread with a fresh event loop.  Event
        emission is bridged back to the main loop so the kernel queue stays
        on one loop.  Calls beyond ``tool_call_budget`` are not run and
        receive ``budget_exceeded`` error envelopes.
        """
        loop = asyncio.get_running_loop()
        within = calls[: self._budget]
        excess = calls[self._budget :]

        def _run_in_thread(tool: Tool, args: dict[str, Any]) -> ToolResultEnvelope:
            thread_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(thread_loop)
            # Local executor shares hooks/permissions but bridges emit() to main loop.
            local = AgenthiccToolExecutor(
                event_processor=None,
                hook_runner=self._hooks,
                permission_checker=self._permission_checker,
                tool_call_budget=self._budget,
                timeout_seconds=self._timeout_seconds,
            )
            local._events = _BridgedEmitter(self._events, loop)
            try:
                return thread_loop.run_until_complete(local.execute(tool, args, ctx))
            finally:
                thread_loop.close()

        futures = [
            loop.run_in_executor(self._thread_pool, _run_in_thread, tool, args)
            for tool, args in within
        ]
        results: list[ToolResultEnvelope] = list(await asyncio.gather(*futures))

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
