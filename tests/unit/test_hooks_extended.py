"""Extended unit tests for lifecycle hooks covering uncovered lines.

Targeted lines in hooks.py: 66, 70, 76, 94, 141, 199-200
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from agenthicc.tools.hooks import (
    HookRegistry,
    HookRunner,
    LifecycleHook,
    LaurenToolHookAdapter,
    RecoveryAction,
    Rejection,
    load_hook_from_dotpath,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class NullHook(LifecycleHook):
    """A no-op hook that records calls."""

    def __init__(self) -> None:
        self.before_called = 0
        self.after_called = 0
        self.error_called = 0

    async def on_before(self, entity: Any, ctx: Any) -> Rejection | None:
        self.before_called += 1
        return None

    async def on_after(self, entity: Any, result: Any, ctx: Any) -> None:
        self.after_called += 1

    async def on_error(self, entity, error, ctx) -> RecoveryAction | None:
        self.error_called += 1
        return None


class RejectHook(LifecycleHook):
    async def on_before(self, entity: Any, ctx: Any) -> Rejection | None:
        return Rejection(reason="blocked")


class FallbackHook(LifecycleHook):
    async def on_error(self, entity, error, ctx) -> RecoveryAction | None:
        return RecoveryAction.FALLBACK


class RetryHook(LifecycleHook):
    async def on_error(self, entity, error, ctx) -> RecoveryAction | None:
        return RecoveryAction.RETRY


def _lauren_ctx(tool_name: str = "my_tool") -> Any:
    from lauren_ai._tools._hooks import ToolCallContext

    return ToolCallContext(
        agent_context=None,
        tool_use_id="tu-1",
        turn=0,
        tool_name=tool_name,
        tool_input={"x": 1},
    )


# ---------------------------------------------------------------------------
# HookRegistry — line 66 (invalid stage) and line 70 (set_default)
# ---------------------------------------------------------------------------


class TestLifecycleHookDefaultImplementations:
    """Cover the default no-op methods on the LifecycleHook ABC (lines 66, 70, 76)."""

    async def test_default_on_before_returns_none(self):
        """Line 66: default on_before → None."""
        hook = NullHook()
        # Call the *base* default by bypassing our NullHook override
        result = await LifecycleHook.on_before(hook, "entity", {})
        assert result is None

    async def test_default_on_after_returns_none(self):
        """Line 70: default on_after → None."""
        hook = NullHook()
        result = await LifecycleHook.on_after(hook, "entity", "result", {})
        assert result is None

    async def test_default_on_error_returns_none(self):
        """Line 76: default on_error → None."""
        hook = NullHook()
        result = await LifecycleHook.on_error(hook, "entity", RuntimeError("x"), {})
        assert result is None

    async def test_pure_default_hook_all_methods_return_none(self):
        """Instantiate a concrete subclass that doesn't override anything."""

        class MinimalHook(LifecycleHook):
            pass

        hook = MinimalHook()
        assert await hook.on_before("e", {}) is None
        assert await hook.on_after("e", "r", {}) is None
        assert await hook.on_error("e", RuntimeError("x"), {}) is None


class TestHookRegistryEdgeCases:
    def test_register_invalid_stage_raises_value_error(self):
        """Line 66: unknown stage should raise ValueError."""
        registry = HookRegistry()
        with pytest.raises(ValueError, match="Unknown hook stage"):
            registry.register("tool", "unknown_stage", NullHook())

    def test_hooks_for_unknown_entity_returns_empty(self):
        """Line 70 (_hooks.get default): unknown (entity, stage) → []."""
        registry = HookRegistry()
        hooks = registry.hooks_for("nonexistent_entity", "before")
        assert hooks == []

    def test_hooks_for_returns_copy(self):
        """Mutating the returned list must not affect the registry."""
        registry = HookRegistry()
        hook = NullHook()
        registry.register("tool", "before", hook)
        returned = registry.hooks_for("tool", "before")
        returned.clear()
        assert len(registry.hooks_for("tool", "before")) == 1


# ---------------------------------------------------------------------------
# HookRunner — line 76 (run_before no hooks), line 94 (run_after no hooks),
#              line 141 (run_error all None)
# ---------------------------------------------------------------------------


class TestHookRunnerEdgeCases:
    async def test_run_before_no_hooks_returns_none(self):
        """Line 76: no hooks registered → returns None immediately."""
        runner = HookRunner()
        result = await runner.run_before("tool", object(), {})
        assert result is None

    async def test_run_after_no_hooks_returns_none(self):
        """Line 94: no hooks registered → returns None immediately."""
        runner = HookRunner()
        result = await runner.run_after("tool", object(), "result", {})
        assert result is None

    async def test_run_error_no_hooks_returns_none(self):
        runner = HookRunner()
        result = await runner.run_error("tool", object(), RuntimeError("x"), {})
        assert result is None

    async def test_run_error_all_none_returns_none(self):
        """Line 141: all error hooks return None → runner returns None."""
        registry = HookRegistry()
        registry.register("tool", "error", NullHook())
        registry.register("tool", "error", NullHook())
        runner = HookRunner(registry)
        result = await runner.run_error("tool", object(), RuntimeError("x"), {})
        assert result is None

    async def test_run_before_returns_rejection_when_hook_rejects(self):
        registry = HookRegistry()
        registry.register("tool", "before", RejectHook())
        runner = HookRunner(registry)
        result = await runner.run_before("tool", object(), {})
        assert isinstance(result, Rejection)
        assert result.reason == "blocked"

    async def test_run_after_all_hooks_called(self):
        log: list[str] = []

        class LogHook(LifecycleHook):
            def __init__(self, label: str) -> None:
                self.label = label

            async def on_after(self, entity, result, ctx) -> None:
                log.append(self.label)

        registry = HookRegistry()
        registry.register("tool", "after", LogHook("a"))
        registry.register("tool", "after", LogHook("b"))
        runner = HookRunner(registry)
        await runner.run_after("tool", object(), "result", {})
        assert set(log) == {"a", "b"}

    async def test_run_error_returns_first_non_none_action(self):
        """Line 139: first non-None recovery action is returned."""
        registry = HookRegistry()
        registry.register("tool", "error", NullHook())  # returns None
        registry.register("tool", "error", RetryHook())  # returns RETRY
        registry.register("tool", "error", FallbackHook())  # returns FALLBACK
        runner = HookRunner(registry)
        result = await runner.run_error("tool", object(), RuntimeError("x"), {})
        assert result is RecoveryAction.RETRY


# ---------------------------------------------------------------------------
# load_hook_from_dotpath — lines 66, 70 in loading context
# ---------------------------------------------------------------------------


class TestLoadHookFromDotpath:
    def test_load_invalid_dotpath_raises_value_error(self):
        """Single token with no dot or colon → ValueError."""
        with pytest.raises(ValueError, match="Invalid hook dotpath"):
            load_hook_from_dotpath("nodots")

    def test_load_nonexistent_module_raises_import_error(self):
        """Module does not exist → ImportError."""
        with pytest.raises((ImportError, ModuleNotFoundError)):
            load_hook_from_dotpath("completely.nonexistent.module.Hook")

    def test_load_class_via_colon_form_instantiates_it(self):
        """'pkg.module:ClassName' → instance of the class."""
        hook = load_hook_from_dotpath("agenthicc.tools.hooks:LifecycleHook")
        assert isinstance(hook, LifecycleHook)

    def test_load_class_via_dot_form_instantiates_it(self):
        """'pkg.module.ClassName' → instance of the class."""
        hook = load_hook_from_dotpath("agenthicc.tools.hooks.LifecycleHook")
        assert isinstance(hook, LifecycleHook)

    def test_load_non_class_object_returns_it_directly(self):
        """If the resolved attribute is not a class, return it as-is."""
        # _STAGES is a tuple (not a class)
        result = load_hook_from_dotpath("agenthicc.tools.hooks:_STAGES")
        assert result == ("before", "after", "error")

    def test_load_hook_from_dynamic_module(self, tmp_path, monkeypatch):
        """Create a temporary module on sys.path and load a hook from it."""
        pkg_dir = tmp_path / "dynpkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "hooks_mod.py").write_text(
            "from agenthicc.tools.hooks import LifecycleHook\n"
            "class MyHook(LifecycleHook): pass\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        hook = load_hook_from_dotpath("dynpkg.hooks_mod:MyHook")
        assert isinstance(hook, LifecycleHook)


# ---------------------------------------------------------------------------
# LaurenToolHookAdapter — lines 199-200 (after_tool_call)
# ---------------------------------------------------------------------------


class TestLaurenToolHookAdapterExtended:
    async def test_after_tool_call_returns_proceed_decision(self):
        """Line 199-200: after_tool_call always returns AfterToolHookDecision.proceed()."""
        from lauren_ai._tools._hooks import AfterToolHookDecision, _NO_REPLACE

        adapter = LaurenToolHookAdapter(NullHook())
        ctx = _lauren_ctx()
        decision = await adapter.after_tool_call("some_result", ctx)
        # proceed() is identified by _replacement being the _NO_REPLACE sentinel
        assert decision._replacement is _NO_REPLACE

    async def test_after_tool_call_invokes_underlying_hook(self):
        hook = NullHook()
        adapter = LaurenToolHookAdapter(hook)
        ctx = _lauren_ctx()
        await adapter.after_tool_call("result", ctx)
        assert hook.after_called == 1

    async def test_before_tool_call_proceeds_when_no_rejection(self):
        hook = NullHook()
        adapter = LaurenToolHookAdapter(hook)
        ctx = _lauren_ctx()
        decision = await adapter.before_tool_call(ctx)
        assert decision._aborted is False

    async def test_before_tool_call_aborts_on_rejection(self):
        adapter = LaurenToolHookAdapter(RejectHook())
        ctx = _lauren_ctx()
        decision = await adapter.before_tool_call(ctx)
        assert decision._aborted is True
        assert "blocked" in decision._abort_result["error"]

    async def test_on_tool_error_suppresses_on_fallback(self):
        adapter = LaurenToolHookAdapter(FallbackHook())
        ctx = _lauren_ctx()
        ctx.state["fallback_value"] = "cached"
        decision = await adapter.on_tool_error(RuntimeError("boom"), ctx)
        assert decision._suppressed is True
        assert decision._fallback == "cached"

    async def test_on_tool_error_reaises_for_non_fallback(self):
        adapter = LaurenToolHookAdapter(RetryHook())
        ctx = _lauren_ctx()
        decision = await adapter.on_tool_error(RuntimeError("x"), ctx)
        assert decision._suppressed is False

    async def test_on_tool_error_reraises_when_hook_returns_none(self):
        adapter = LaurenToolHookAdapter(NullHook())
        ctx = _lauren_ctx()
        decision = await adapter.on_tool_error(RuntimeError("x"), ctx)
        assert decision._suppressed is False
