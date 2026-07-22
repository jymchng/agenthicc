"""Coverage for the lauren-ai-backed Agenthicc tool execution adapter."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lauren_ai import ToolContext, tool
from lauren_ai._tools import ToolResult as LaurenToolResult
from lauren_ai._tools._hooks import (
    AfterToolHookDecision,
    BeforeToolHookDecision,
    ErrorToolHookDecision,
    ToolHook,
)

from agenthicc.tools import (
    AgenthiccToolExecutor,
    ApprovalDecision,
    HookRegistry,
    HookRunner,
    Tool,
    ToolBase,
    ToolCallContext,
    ToolErrorKind,
    ToolResult,
    ToolResultEnvelope,
    ToolSandbox,
    WorkspaceView,
)
from agenthicc.tools.executor import normalize_result
from agenthicc.tools.hooks import LaurenToolHookAdapter, load_hook_from_dotpath

pytestmark = pytest.mark.unit


class LegacyEcho(Tool):
    name = "legacy_echo"
    description = "Echo through the legacy Agenthicc contract."
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(
        self,
        args: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        return {"text": args["text"], "root": context.get("workspace_root", "")}


class TypedEcho(ToolBase):
    name = "typed_echo"
    description = "Echo through the typed Agenthicc contract."
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(
        self,
        context: ToolCallContext,
        args: dict[str, object],
    ) -> ToolResult:
        return ToolResult.success({"text": args["text"], "tool": context.tool_name})


@tool()
async def add_numbers(left: int, right: int) -> dict[str, int]:
    """Add two numbers."""
    return {"sum": left + right}


@tool()
async def context_value(label: str, ctx: ToolContext) -> str:
    """Read adapter metadata through lauren's canonical ToolContext."""
    return f"{label}:{ctx.get_metadata('source', 'missing')}"


@tool()
async def slow_tool() -> str:
    """A deliberately slow callable."""
    await asyncio.sleep(0.05)
    return "late"


@tool()
async def network_failure() -> str:
    """Raise a network exception for taxonomy coverage."""
    raise httpx.ReadTimeout("connection timed out")


@tool()
async def permission_failure() -> str:
    """Raise a local policy exception for taxonomy coverage."""
    raise PermissionError("outside workspace")


@tool()
async def provider_failure() -> str:
    """Raise an exception from a provider-shaped module."""
    raise ProviderFailure("provider unavailable")


class ProviderFailure(RuntimeError):
    __module__ = "openai.errors"


class EventSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


class RecordingHook(ToolHook):
    def __init__(
        self,
        *,
        before: BeforeToolHookDecision | None = None,
        after: AfterToolHookDecision | None = None,
        error: ErrorToolHookDecision | None = None,
    ) -> None:
        self.before = before
        self.after = after
        self.error = error
        self.calls: list[str] = []

    async def before_tool_call(self, ctx: object) -> BeforeToolHookDecision:
        self.calls.append("before")
        return self.before or BeforeToolHookDecision.proceed()

    async def after_tool_call(self, result: object, ctx: object) -> AfterToolHookDecision:
        self.calls.append("after")
        return self.after or AfterToolHookDecision.proceed()

    async def on_tool_error(self, exc: Exception, ctx: object) -> ErrorToolHookDecision:
        self.calls.append("error")
        return self.error or ErrorToolHookDecision.reraise()


def _json_value(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        return result
    assert isinstance(result, str)
    value = json.loads(result)
    assert isinstance(value, dict)
    return value


@pytest.mark.asyncio
async def test_legacy_and_typed_tools_share_one_executor() -> None:
    executor = AgenthiccToolExecutor(
        [LegacyEcho(), TypedEcho()],
        sandbox=ToolSandbox(root="."),
    )

    legacy = await executor.execute("legacy_echo", {"text": "hello"}, "legacy-1")
    typed = await executor.execute("typed_echo", {"text": "world"}, "typed-1")

    assert legacy.ok is True
    assert _json_value(legacy.value) == {"text": "hello", "root": str(WorkspaceView(".").root)}
    assert typed.ok is True
    assert _json_value(typed.value) == {"text": "world", "tool": "typed_echo"}
    assert legacy.duration_ms >= 0


@pytest.mark.asyncio
async def test_lauren_callable_and_context_injection() -> None:
    executor = AgenthiccToolExecutor()
    executor.register(add_numbers, source="builtin")
    executor.register(context_value, source="plugin")

    added = await executor.execute("add_numbers", {"left": 2, "right": 3}, "add-1")
    context = await executor.execute("context_value", {"label": "source"}, "ctx-1")

    assert _json_value(added.value) == {"sum": 5}
    assert context.value == "source:plugin"
    assert executor.get_metadata("context_value") is not None


def test_result_helpers_and_normalization_shapes() -> None:
    local = ToolResult.success("value").with_duration(12.5)
    assert local.to_dict() == {
        "ok": True,
        "value": "value",
        "error": None,
        "duration_ms": 12.5,
        "error_kind": None,
    }
    assert normalize_result(local) is local
    envelope = ToolResultEnvelope("id", "name", True, value="v", duration_ms=2)
    assert normalize_result(envelope).value == "v"
    assert normalize_result(LaurenToolResult.error("bad", tool_use_id="id")).ok is False
    assert normalize_result('{"ok": false, "error": "blocked"}').error == "blocked"
    assert normalize_result({"ok": False, "error": "denied"}).ok is False


@pytest.mark.asyncio
async def test_sync_callable_and_registration_validation() -> None:
    def sync_echo(value: str) -> str:
        return value

    executor = AgenthiccToolExecutor()
    executor.register(sync_echo, name="sync_echo")
    result = await executor.execute("sync_echo", {"value": "ok"}, "sync-1")
    assert result.value == "ok"
    assert executor.names == ["sync_echo"]
    with pytest.raises(TypeError):
        executor.register(object())


@pytest.mark.asyncio
async def test_approval_signal_path_and_approved_callback() -> None:
    requires_signal = AgenthiccToolExecutor()
    requires_signal.register(add_numbers, requires_approval=True)
    signal_result = await requires_signal.execute(
        "add_numbers", {"left": 1, "right": 2}, "signal-1"
    )
    assert signal_result.error_kind == ToolErrorKind.approval_required.value

    async def approve(meta: object, ctx: ToolCallContext) -> ApprovalDecision:
        return ApprovalDecision.approved

    approved = AgenthiccToolExecutor(approval_handler=approve)
    approved.register(add_numbers, requires_approval=True)
    result = await approved.execute("add_numbers", {"left": 1, "right": 2}, "approve-1")
    assert result.ok is True

    legacy_approved = AgenthiccToolExecutor(approval_handler=approve)
    legacy_approved.register(LegacyEcho(), requires_approval=True)
    legacy_result = await legacy_approved.execute(
        "legacy_echo",
        {"text": "approved"},
        "approve-legacy-1",
    )
    assert legacy_result.ok is True


@pytest.mark.asyncio
async def test_approval_uses_lauren_hook_modified_input_before_callback() -> None:
    hook = RecordingHook(
        before=BeforeToolHookDecision.modify({"left": 7, "right": 2}),
    )
    seen_inputs: list[dict[str, object]] = []

    async def approve(meta: object, ctx: ToolCallContext) -> ApprovalDecision:
        seen_inputs.append(dict(ctx.tool_input))
        return ApprovalDecision.approved

    executor = AgenthiccToolExecutor(global_hooks=[hook], approval_handler=approve)
    executor.register(add_numbers, requires_approval=True)
    result = await executor.execute("add_numbers", {"left": 1, "right": 2}, "approve-2")

    assert _json_value(result.value) == {"sum": 9}
    assert seen_inputs == [{"left": 7, "right": 2}]
    assert hook.calls == ["before", "after"]


@pytest.mark.asyncio
async def test_catalog_contains_capability_and_source_metadata() -> None:
    from agenthicc.tools.capabilities import tool_write

    @tool_write
    @tool()
    async def write_something(path: str) -> dict[str, str]:
        return {"path": path}

    executor = AgenthiccToolExecutor()
    executor.register(write_something, source="builtin")
    record = executor.catalog()[0]

    assert record["name"] == "write_something"
    assert record["source"] == "builtin"
    assert record["destructive"] is True
    assert record["capabilities"] == ["write"]


@pytest.mark.asyncio
async def test_unknown_tool_is_structured_and_emits_completion() -> None:
    sink = EventSink()
    result = await AgenthiccToolExecutor(event_sink=sink).execute("missing", {}, "missing-1")

    assert result.ok is False
    assert result.error_kind == ToolErrorKind.unknown.value
    assert len(sink.events) == 1
    assert sink.events[0].__class__.__name__ == "ToolCallComplete"


@pytest.mark.asyncio
async def test_hooks_abort_replace_and_suppress_using_lauren_decisions() -> None:
    abort_hook = RecordingHook(
        before=BeforeToolHookDecision.abort({"ok": False, "error": "blocked"})
    )
    blocked = await AgenthiccToolExecutor([add_numbers], global_hooks=[abort_hook]).execute(
        "add_numbers", {"left": 1, "right": 2}, "blocked-1"
    )
    assert blocked.ok is False
    assert blocked.error == "blocked"
    assert abort_hook.calls == ["before"]

    replace_hook = RecordingHook(after=AfterToolHookDecision.replace("replacement"))
    replaced = await AgenthiccToolExecutor([add_numbers], global_hooks=[replace_hook]).execute(
        "add_numbers", {"left": 1, "right": 2}, "replace-1"
    )
    assert replaced.ok is True
    assert replaced.value == "replacement"
    assert replace_hook.calls == ["before", "after"]

    suppress_hook = RecordingHook(error=ErrorToolHookDecision.suppress_with("fallback"))
    suppressed = await AgenthiccToolExecutor(
        [network_failure], global_hooks=[suppress_hook]
    ).execute("network_failure", {}, "fallback-1")
    assert suppressed.ok is True
    assert suppressed.value == "fallback"
    assert suppress_hook.calls == ["before", "error"]


@pytest.mark.asyncio
async def test_approval_timeout_and_error_taxonomy() -> None:
    async def deny(meta: object, ctx: ToolCallContext) -> ApprovalDecision:
        return ApprovalDecision.denied

    denied_executor = AgenthiccToolExecutor(approval_handler=deny)
    denied_executor.register(add_numbers, requires_approval=True)
    denied = await denied_executor.execute("add_numbers", {"left": 1, "right": 2}, "deny-1")
    assert denied.error_kind == ToolErrorKind.denied.value

    timeout_executor = AgenthiccToolExecutor()
    timeout_executor.register(slow_tool, timeout_s=0.001)
    timeout = await timeout_executor.execute("slow_tool", {}, "timeout-1")
    assert timeout.error_kind == ToolErrorKind.timeout.value

    network = await AgenthiccToolExecutor([network_failure]).execute(
        "network_failure", {}, "network-1"
    )
    permission = await AgenthiccToolExecutor([permission_failure]).execute(
        "permission_failure", {}, "permission-1"
    )
    provider = await AgenthiccToolExecutor([provider_failure]).execute(
        "provider_failure", {}, "provider-1"
    )
    assert network.error_kind == ToolErrorKind.network.value
    assert permission.error_kind == ToolErrorKind.denied.value
    assert provider.error_kind == ToolErrorKind.provider.value


@pytest.mark.asyncio
async def test_structured_legacy_denial_is_classified_without_retry() -> None:
    calls = 0

    class DenyingTool(Tool):
        name = "denying_tool"

        async def execute(
            self,
            args: dict[str, object],
            context: dict[str, object],
        ) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"ok": False, "error": "permission_denied: outside workspace"}

    executor = AgenthiccToolExecutor([DenyingTool()])
    result = await executor.execute("denying_tool", {}, "denied-1")

    assert result.ok is False
    assert result.error_kind == ToolErrorKind.denied.value
    assert calls == 1


@pytest.mark.asyncio
async def test_parallel_execution_preserves_order_and_events() -> None:
    sink = EventSink()
    executor = AgenthiccToolExecutor([add_numbers], event_sink=sink)
    results = await executor.execute_parallel(
        [
            ("add_numbers", {"left": 1, "right": 2}, "first"),
            ("add_numbers", {"left": 4, "right": 5}, "second"),
        ]
    )

    assert [_json_value(result.value) for result in results] == [
        {"sum": 3},
        {"sum": 9},
    ]
    assert [event.__class__.__name__ for event in sink.events].count("ToolCallStarted") == 2
    assert [event.__class__.__name__ for event in sink.events].count("ToolCallComplete") == 2


@pytest.mark.asyncio
async def test_hook_registry_and_runner_use_lauren_decision_types() -> None:
    hook = RecordingHook()
    registry = HookRegistry()
    registry.register("tool", "before", hook)
    runner = HookRunner(registry)
    ctx = ToolCallContext(
        agent_context=None,
        tool_use_id="hook-1",
        turn=0,
        tool_name="add_numbers",
        tool_input={},
    )

    decision = await runner.run_before("tool", object(), ctx)
    assert decision is None
    assert hook.calls == ["before"]

    with pytest.raises(ValueError):
        registry.register("tool", "unknown", hook)


@pytest.mark.asyncio
async def test_hook_runner_after_and_error_paths_use_lauren_decisions() -> None:
    after_hook = RecordingHook(after=AfterToolHookDecision.replace("changed"))
    error_hook = RecordingHook(error=ErrorToolHookDecision.suppress_with("fallback"))
    registry = HookRegistry()
    registry.register("tool", "after", after_hook)
    registry.register("tool", "error", error_hook)
    runner = HookRunner(registry)
    ctx = ToolCallContext(
        agent_context=None,
        tool_use_id="hook-2",
        turn=0,
        tool_name="tool",
        tool_input={},
    )

    changed = await runner.run_after("tool", ToolResult.success("old"), ctx)
    decision = await runner.run_error("tool", RuntimeError("bad"), ctx)
    assert changed == "changed"
    assert decision is error_hook.error


@pytest.mark.asyncio
async def test_hook_dotpath_adapter_and_result_normalization() -> None:
    loaded = load_hook_from_dotpath(f"{__name__}:RecordingHook")
    adapter = LaurenToolHookAdapter(loaded)
    ctx = ToolCallContext(
        agent_context=None,
        tool_use_id="hook-3",
        turn=0,
        tool_name="tool",
        tool_input={},
    )
    assert (await adapter.before_tool_call(ctx))._aborted is False
    assert normalize_result({"ok": False, "error": "bad"}).ok is False
    assert normalize_result("value").value == "value"


@pytest.mark.asyncio
async def test_hook_runner_abort_empty_error_and_adapter_stages() -> None:
    abort_hook = RecordingHook(before=BeforeToolHookDecision.abort({"ok": False, "error": "no"}))
    registry = HookRegistry()
    registry.register("tool", "before", abort_hook)
    runner = HookRunner(registry)
    ctx = ToolCallContext(
        agent_context=None,
        tool_use_id="hook-4",
        turn=0,
        tool_name="tool",
        tool_input={},
    )
    decision = await runner.run_before("tool", object(), ctx)
    assert decision is abort_hook.before
    assert await HookRunner().run_error("tool", RuntimeError("x"), ctx) is None

    adapter = LaurenToolHookAdapter(abort_hook)
    assert (await adapter.before_tool_call(ctx))._aborted is True
    assert isinstance(await adapter.after_tool_call("x", ctx), AfterToolHookDecision)
    assert isinstance(await adapter.on_tool_error(RuntimeError("x"), ctx), ErrorToolHookDecision)
    with pytest.raises(ValueError):
        load_hook_from_dotpath("invalid")
    with pytest.raises(TypeError):
        load_hook_from_dotpath(f"{__name__}:ProviderFailure")
