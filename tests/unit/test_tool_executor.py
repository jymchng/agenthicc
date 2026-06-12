"""Unit tests for AgenthiccToolExecutor (PRD-04)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agenthicc.tools.base import Tool, ToolResultEnvelope
from agenthicc.tools.executor import AgenthiccToolExecutor
from agenthicc.tools.hooks import HookRegistry, HookRunner, LifecycleHook, RecoveryAction, Rejection

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Minimal Tool implementations for tests
# ---------------------------------------------------------------------------


class EchoTool(Tool):
    name = "echo"
    description = "Returns args as-is"
    parameters = {}

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        return args.get("value", "ok")


class SleepTool(Tool):
    name = "sleep"
    description = "Sleeps forever"
    parameters = {}

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        await asyncio.sleep(9999)


class FailTool(Tool):
    """Raises on every call until a counter allows it through."""

    name = "fail"
    description = "Fails"
    parameters = {}

    def __init__(self, fail_n_times: int = 1) -> None:
        self._remaining = fail_n_times

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("intentional failure")
        return "recovered"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_executor(
    *,
    harness=None,
    hook_runner: HookRunner | None = None,
    permission_checker=None,
    budget: int = 8,
    timeout_seconds: float = 30.0,
) -> AgenthiccToolExecutor:
    processor = harness.processor if harness is not None else None
    return AgenthiccToolExecutor(
        event_processor=processor,
        hook_runner=hook_runner,
        permission_checker=permission_checker,
        tool_call_budget=budget,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_successful_execution_returns_ok_envelope():
    executor = make_executor()
    env = await executor.execute(EchoTool(), {"value": "hello"}, {})
    assert env.ok is True
    assert env.value == "hello"
    assert env.error is None
    assert env.tool_name == "echo"
    assert env.tool_call_id  # non-empty string


async def test_permission_checker_false_returns_ok_false(harness):
    executor = make_executor(harness=harness, permission_checker=lambda *_: False)
    env = await executor.execute(EchoTool(), {}, {})
    assert env.ok is False
    assert "permission_denied" in (env.error or "")


async def test_permission_checker_emits_permission_denied_event(harness):
    executor = make_executor(harness=harness, permission_checker=lambda *_: False)
    await executor.execute(EchoTool(), {}, {})
    await harness.processor.drain()
    assert harness.events_of_type("PermissionDenied")


async def test_timeout_returns_ok_false():
    executor = make_executor(timeout_seconds=0.001)
    env = await executor.execute(SleepTool(), {}, {})
    assert env.ok is False
    err = env.error or ""
    assert "TimeoutError" in err or "timed out" in err.lower()


async def test_before_hook_rejection_returns_ok_false():
    class RejectHook(LifecycleHook):
        async def on_before(self, entity, ctx):
            return Rejection("blocked by policy")

    registry = HookRegistry()
    registry.register("tool", "before", RejectHook())
    runner = HookRunner(registry)
    executor = make_executor(hook_runner=runner)

    env = await executor.execute(EchoTool(), {}, {})
    assert env.ok is False
    assert "blocked by policy" in (env.error or "")


async def test_error_hook_retry_succeeds():
    """A tool that fails once should succeed on retry if on_error returns RETRY."""

    class RetryHook(LifecycleHook):
        async def on_error(self, entity, error, ctx):
            return RecoveryAction.RETRY

    registry = HookRegistry()
    registry.register("tool", "error", RetryHook())
    runner = HookRunner(registry)
    executor = make_executor(hook_runner=runner)

    tool = FailTool(fail_n_times=1)
    env = await executor.execute(tool, {}, {})
    assert env.ok is True
    assert env.value == "recovered"


async def test_error_hook_fallback_returns_ok_true():
    """on_error returning FALLBACK must yield ok=True with fallback value from ctx."""

    class FallbackHook(LifecycleHook):
        async def on_error(self, entity, error, ctx):
            return RecoveryAction.FALLBACK

    registry = HookRegistry()
    registry.register("tool", "error", FallbackHook())
    runner = HookRunner(registry)
    executor = make_executor(hook_runner=runner)

    ctx = {"fallback_value": "safe_default"}
    env = await executor.execute(FailTool(fail_n_times=99), {}, ctx)
    assert env.ok is True
    assert env.value == "safe_default"


async def test_execute_parallel_budget_exceeded():
    """Calls beyond the budget receive budget_exceeded; others succeed."""
    executor = make_executor(budget=2)
    tools = [(EchoTool(), {"value": i}) for i in range(5)]
    results = await executor.execute_parallel(tools, {})
    assert len(results) == 5

    ok_count = sum(1 for r in results if r.ok)
    failed_count = sum(1 for r in results if not r.ok)
    assert ok_count == 2
    assert failed_count == 3
    for r in results[2:]:
        assert "budget_exceeded" in (r.error or "")


async def test_tool_call_started_and_complete_events_emitted(harness):
    executor = make_executor(harness=harness)
    await executor.execute(EchoTool(), {"value": "x"}, {})
    await harness.processor.drain()

    assert harness.events_of_type("ToolCallStarted")
    assert harness.events_of_type("ToolCallComplete")


async def test_tool_call_complete_event_on_error(harness):
    """ToolCallComplete is emitted even when the tool fails."""
    executor = make_executor(harness=harness)
    await executor.execute(FailTool(fail_n_times=99), {}, {})
    await harness.processor.drain()
    assert harness.events_of_type("ToolCallComplete")
