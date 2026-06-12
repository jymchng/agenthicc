"""Unit tests for lifecycle hooks: registry ordering, rejection short-circuit,
error recovery, executor retry, and the lauren-ai adapter (PRD-04)."""

from __future__ import annotations

from typing import Any

import pytest

from agenthicc.tools import (
    AgenthiccToolExecutor,
    HookRegistry,
    HookRunner,
    LaurenToolHookAdapter,
    LifecycleHook,
    RecoveryAction,
    Rejection,
    Tool,
    load_hook_from_dotpath,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingHook(LifecycleHook):
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log

    async def on_before(self, entity: Any, ctx: Any) -> Rejection | None:
        self.log.append(f"{self.name}:before")
        return None

    async def on_after(self, entity: Any, result: Any, ctx: Any) -> None:
        self.log.append(f"{self.name}:after")

    async def on_error(self, entity, error, ctx) -> RecoveryAction | None:
        self.log.append(f"{self.name}:error")
        return None


class RejectingHook(LifecycleHook):
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def on_before(self, entity: Any, ctx: Any) -> Rejection | None:
        return Rejection(reason=self.reason)


class RecoveringHook(LifecycleHook):
    def __init__(self, action: RecoveryAction) -> None:
        self.action = action
        self.error_calls = 0

    async def on_error(self, entity, error, ctx) -> RecoveryAction | None:
        self.error_calls += 1
        return self.action


class FlakyTool(Tool):
    """Tool that always raises, counting invocations."""

    name = "flaky"
    description = "Always raises"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: dict, context: dict) -> Any:
        self.calls += 1
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Registry / runner
# ---------------------------------------------------------------------------


async def test_hooks_run_in_registration_order():
    log: list[str] = []
    registry = HookRegistry()
    registry.register("tool", "before", RecordingHook("first", log))
    registry.register("tool", "before", RecordingHook("second", log))
    registry.register("tool", "before", RecordingHook("third", log))

    names = [type(h).__name__ for h in registry.hooks_for("tool", "before")]
    assert names == ["RecordingHook"] * 3

    runner = HookRunner(registry)
    rejection = await runner.run_before("tool", object(), {})
    assert rejection is None
    assert log == ["first:before", "second:before", "third:before"]


async def test_hooks_for_is_isolated_per_entity_and_stage():
    registry = HookRegistry()
    hook = RecordingHook("h", [])
    registry.register("tool", "before", hook)
    assert registry.hooks_for("tool", "after") == []
    assert registry.hooks_for("workflow", "before") == []
    assert registry.hooks_for("tool", "before") == [hook]


async def test_rejection_short_circuits_before_stage():
    """The first Rejection (by registration order) wins and aborts execution."""
    log: list[str] = []
    registry = HookRegistry()
    registry.register("tool", "before", RejectingHook("rate-limited"))
    registry.register("tool", "before", RejectingHook("second-reason"))
    registry.register("tool", "before", RecordingHook("spy", log))
    runner = HookRunner(registry)

    rejection = await runner.run_before("tool", object(), {})
    assert isinstance(rejection, Rejection)
    assert rejection.reason == "rate-limited"

    # And in the executor a rejection prevents the tool from running.
    tool = FlakyTool()
    executor = AgenthiccToolExecutor(None, runner)
    env = await executor.execute(tool, {}, {})
    assert not env.ok
    assert "rate-limited" in env.error
    assert tool.calls == 0


async def test_run_after_invokes_all_hooks():
    log: list[str] = []
    registry = HookRegistry()
    registry.register("tool", "after", RecordingHook("a", log))
    registry.register("tool", "after", RecordingHook("b", log))
    runner = HookRunner(registry)

    await runner.run_after("tool", object(), "result", {})
    assert log == ["a:after", "b:after"]


async def test_run_error_returns_first_non_none_recovery_action():
    registry = HookRegistry()
    silent = RecoveringHook(None)  # type: ignore[arg-type]
    silent.action = None
    fallback = RecoveringHook(RecoveryAction.FALLBACK)
    escalate = RecoveringHook(RecoveryAction.ESCALATE)
    registry.register("tool", "error", silent)
    registry.register("tool", "error", fallback)
    registry.register("tool", "error", escalate)
    runner = HookRunner(registry)

    action = await runner.run_error("tool", object(), RuntimeError("x"), {})
    assert action is RecoveryAction.FALLBACK
    # All hooks still ran (gather, not sequential short-circuit).
    assert silent.error_calls == fallback.error_calls == escalate.error_calls == 1


async def test_run_error_returns_none_without_hooks():
    runner = HookRunner()
    assert await runner.run_error("tool", object(), RuntimeError("x"), {}) is None
    assert await runner.run_before("tool", object(), {}) is None


# ---------------------------------------------------------------------------
# RETRY in the executor
# ---------------------------------------------------------------------------


async def test_retry_recovery_causes_exactly_one_retry():
    """A hook returning RETRY makes the executor re-run the tool exactly once,
    even if the hook keeps asking for RETRY."""
    registry = HookRegistry()
    retry_hook = RecoveringHook(RecoveryAction.RETRY)
    registry.register("tool", "error", retry_hook)
    runner = HookRunner(registry)

    tool = FlakyTool()
    executor = AgenthiccToolExecutor(None, runner)
    env = await executor.execute(tool, {}, {})

    assert tool.calls == 2, "tool must run original + exactly one retry"
    assert retry_hook.error_calls == 2
    assert not env.ok
    assert "boom" in env.error


# ---------------------------------------------------------------------------
# Dotpath loading
# ---------------------------------------------------------------------------


async def test_load_hook_from_dotpath_colon_and_dot_forms():
    hook = load_hook_from_dotpath("agenthicc.tools.hooks:LifecycleHook")
    assert isinstance(hook, LifecycleHook)
    hook2 = load_hook_from_dotpath("agenthicc.tools.hooks.LifecycleHook")
    assert isinstance(hook2, LifecycleHook)
    with pytest.raises(ValueError):
        load_hook_from_dotpath("nodots")


# ---------------------------------------------------------------------------
# lauren-ai adapter
# ---------------------------------------------------------------------------


def _lauren_ctx(tool_name: str = "echo") -> Any:
    from lauren_ai._tools._hooks import ToolCallContext

    return ToolCallContext(
        agent_context=None,
        tool_use_id="toolu_01",
        turn=0,
        tool_name=tool_name,
        tool_input={"q": 1},
    )


async def test_adapter_maps_rejection_to_lauren_abort_decision():
    adapter = LaurenToolHookAdapter(RejectingHook("forbidden by policy"))
    decision = await adapter.before_tool_call(_lauren_ctx())
    assert decision._aborted is True
    assert "forbidden by policy" in decision._abort_result["error"]


async def test_adapter_proceeds_when_hook_allows():
    adapter = LaurenToolHookAdapter(RecordingHook("ok", []))
    decision = await adapter.before_tool_call(_lauren_ctx())
    assert decision._aborted is False
    assert decision._modified_input is None


async def test_adapter_maps_fallback_to_suppressed_error_decision():
    adapter = LaurenToolHookAdapter(RecoveringHook(RecoveryAction.FALLBACK))
    ctx = _lauren_ctx()
    ctx.state["fallback_value"] = "cached-answer"
    decision = await adapter.on_tool_error(RuntimeError("boom"), ctx)
    assert decision._suppressed is True
    assert decision._fallback == "cached-answer"


async def test_adapter_reraises_for_non_fallback_actions():
    adapter = LaurenToolHookAdapter(RecoveringHook(RecoveryAction.ESCALATE))
    decision = await adapter.on_tool_error(RuntimeError("boom"), _lauren_ctx())
    assert decision._suppressed is False
