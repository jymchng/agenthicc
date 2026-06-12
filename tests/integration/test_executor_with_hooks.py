"""Integration tests for AgenthiccToolExecutor with hooks and event kernel."""

from __future__ import annotations

from typing import Any

import pytest

from agenthicc.tools.base import Tool
from agenthicc.tools.executor import AgenthiccToolExecutor
from agenthicc.tools.hooks import HookRegistry, HookRunner, LifecycleHook, RecoveryAction
from agenthicc.kernel import Event

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Minimal tools
# ---------------------------------------------------------------------------


class EchoTool(Tool):
    name = "echo"
    description = "Returns the value arg"
    parameters = {}

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        return args.get("value", "ok")


class AlwaysFailTool(Tool):
    name = "always_fail"
    description = "Always raises"
    parameters = {}

    async def execute(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        raise ValueError("always broken")


# ---------------------------------------------------------------------------
# Integration: AuditHook records calls
# ---------------------------------------------------------------------------


class AuditHook(LifecycleHook):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_after(self, entity: Any, result: Any, ctx: Any) -> None:
        self.calls.append(entity.name)


async def test_audit_hook_records_every_call(harness):
    audit = AuditHook()
    registry = HookRegistry()
    registry.register("tool", "after", audit)
    runner = HookRunner(registry)
    executor = AgenthiccToolExecutor(
        event_processor=harness.processor,
        hook_runner=runner,
    )

    await executor.execute(EchoTool(), {"value": "first"}, {})
    await executor.execute(EchoTool(), {"value": "second"}, {})

    assert len(audit.calls) == 2
    assert all(name == "echo" for name in audit.calls)


# ---------------------------------------------------------------------------
# Integration: error recovery via FALLBACK
# ---------------------------------------------------------------------------


async def test_error_recovery_fallback_returns_ok_true(harness):
    class FallbackHook(LifecycleHook):
        async def on_error(self, entity, error, ctx):
            return RecoveryAction.FALLBACK

    registry = HookRegistry()
    registry.register("tool", "error", FallbackHook())
    runner = HookRunner(registry)
    executor = AgenthiccToolExecutor(
        event_processor=harness.processor,
        hook_runner=runner,
    )

    ctx = {"fallback_value": "safe"}
    env = await executor.execute(AlwaysFailTool(), {}, ctx)
    assert env.ok is True
    assert env.value == "safe"


# ---------------------------------------------------------------------------
# Integration: events appear in kernel event_log
# ---------------------------------------------------------------------------


async def test_tool_events_appear_in_event_log(harness):
    executor = AgenthiccToolExecutor(event_processor=harness.processor)
    await executor.execute(EchoTool(), {"value": "integration"}, {})
    await harness.processor.drain()

    log = harness.processor.event_log
    types = [e.event_type for e in log]
    assert "ToolCallStarted" in types
    assert "ToolCallComplete" in types
