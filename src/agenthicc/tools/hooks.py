"""Lifecycle hooks: ABC, registry, runner, and lauren-ai adapter (PRD-04)."""

from __future__ import annotations

import abc
import asyncio
import enum
import importlib
from dataclasses import dataclass
from typing import Any

from lauren_ai._tools._hooks import (
    AfterToolHookDecision,
    BeforeToolHookDecision,
    ErrorToolHookDecision,
    ToolCallContext as LaurenToolCallContext,
    ToolHook as LaurenToolHook,
)

__all__ = [
    "HookRegistry",
    "HookRunner",
    "LaurenToolHookAdapter",
    "LifecycleHook",
    "RecoveryAction",
    "Rejection",
    "load_hook_from_dotpath",
]

# ---------------------------------------------------------------------------
# Decision / recovery types
# ---------------------------------------------------------------------------


class RecoveryAction(enum.Enum):
    """Actions an ``on_error`` hook can suggest to the executor."""

    RETRY = "retry"
    FALLBACK = "fallback"
    ESCALATE = "escalate"
    SKIP = "skip"


@dataclass(slots=True)
class Rejection:
    """Returned by ``on_before`` to prevent execution."""

    reason: str


# ---------------------------------------------------------------------------
# LifecycleHook ABC
# ---------------------------------------------------------------------------


class LifecycleHook(abc.ABC):
    """ABC for lifecycle hooks at any entity level (intent, workflow, node,
    task, agent, tool call).

    All three methods default to no-ops so concrete hooks only override the
    stages they care about.
    """

    async def on_before(self, entity: Any, ctx: Any) -> Rejection | None:
        """Called before the entity executes; return a Rejection to abort."""
        return None

    async def on_after(self, entity: Any, result: Any, ctx: Any) -> None:
        """Called after the entity executes successfully."""
        return None

    async def on_error(
        self, entity: Any, error: BaseException, ctx: Any
    ) -> RecoveryAction | None:
        """Called when the entity raises; may suggest a RecoveryAction."""
        return None


# ---------------------------------------------------------------------------
# Registry and runner
# ---------------------------------------------------------------------------

_STAGES = ("before", "after", "error")


class HookRegistry:
    """Maps (entity_type, stage) to an ordered list of hooks."""

    def __init__(self) -> None:
        self._hooks: dict[tuple[str, str], list[LifecycleHook]] = {}

    def register(self, entity_type: str, stage: str, hook: LifecycleHook) -> None:
        if stage not in _STAGES:
            raise ValueError(f"Unknown hook stage {stage!r}; expected one of {_STAGES}")
        self._hooks.setdefault((entity_type, stage), []).append(hook)

    def hooks_for(self, entity_type: str, stage: str) -> list[LifecycleHook]:
        return list(self._hooks.get((entity_type, stage), []))


class HookRunner:
    """Runs every hook registered for a stage concurrently via asyncio.gather."""

    def __init__(self, registry: HookRegistry | None = None) -> None:
        self.registry = registry or HookRegistry()

    async def run_before(
        self, entity_type: str, entity: Any, ctx: Any
    ) -> Rejection | None:
        """Run all before-hooks; return the first Rejection (by registration
        order) or None when every hook allows."""
        hooks = self.registry.hooks_for(entity_type, "before")
        if not hooks:
            return None
        results = await asyncio.gather(*(h.on_before(entity, ctx) for h in hooks))
        for result in results:
            if isinstance(result, Rejection):
                return result
        return None

    async def run_after(
        self, entity_type: str, entity: Any, result: Any, ctx: Any
    ) -> None:
        hooks = self.registry.hooks_for(entity_type, "after")
        if not hooks:
            return None
        await asyncio.gather(*(h.on_after(entity, result, ctx) for h in hooks))
        return None

    async def run_error(
        self, entity_type: str, entity: Any, error: BaseException, ctx: Any
    ) -> RecoveryAction | None:
        """Run all error-hooks; return the first non-None RecoveryAction."""
        hooks = self.registry.hooks_for(entity_type, "error")
        if not hooks:
            return None
        results = await asyncio.gather(*(h.on_error(entity, error, ctx) for h in hooks))
        for action in results:
            if action is not None:
                return action
        return None


# ---------------------------------------------------------------------------
# Dynamic loading
# ---------------------------------------------------------------------------


def load_hook_from_dotpath(dotpath: str) -> LifecycleHook:
    """Import and instantiate a hook from a dotted path.

    Accepts ``"pkg.module:HookClass"`` or ``"pkg.module.HookClass"``.
    If the resolved attribute is a class it is instantiated with no args;
    otherwise the object itself is returned.
    """
    if ":" in dotpath:
        module_path, _, attr_name = dotpath.partition(":")
    else:
        module_path, _, attr_name = dotpath.rpartition(".")
    if not module_path or not attr_name:
        raise ValueError(f"Invalid hook dotpath: {dotpath!r}")
    module = importlib.import_module(module_path)
    obj = getattr(module, attr_name)
    return obj() if isinstance(obj, type) else obj


# ---------------------------------------------------------------------------
# lauren-ai adapter
# ---------------------------------------------------------------------------


class LaurenToolHookAdapter(LaurenToolHook):
    """Wraps an agenthicc :class:`LifecycleHook` into a lauren-ai ``ToolHook``.

    Mapping:

    * ``on_before`` returning :class:`Rejection` -> ``BeforeToolHookDecision.abort``
    * ``on_error`` returning :data:`RecoveryAction.FALLBACK` ->
      ``ErrorToolHookDecision.suppress_with`` (fallback value taken from the
      lauren context state bag key ``"fallback_value"``, if present)
    """

    def __init__(self, hook: LifecycleHook) -> None:
        self._hook = hook

    async def before_tool_call(
        self, ctx: LaurenToolCallContext
    ) -> BeforeToolHookDecision:
        rejection = await self._hook.on_before(ctx.tool_name, ctx)
        if isinstance(rejection, Rejection):
            return BeforeToolHookDecision.abort(
                {"ok": False, "error": f"rejected: {rejection.reason}"}
            )
        return BeforeToolHookDecision.proceed()

    async def after_tool_call(
        self, result: Any, ctx: LaurenToolCallContext
    ) -> AfterToolHookDecision:
        await self._hook.on_after(ctx.tool_name, result, ctx)
        return AfterToolHookDecision.proceed()

    async def on_tool_error(
        self, exc: Exception, ctx: LaurenToolCallContext
    ) -> ErrorToolHookDecision:
        action = await self._hook.on_error(ctx.tool_name, exc, ctx)
        if action is RecoveryAction.FALLBACK:
            fallback = getattr(ctx, "state", {}).get("fallback_value")
            return ErrorToolHookDecision.suppress_with(fallback)
        return ErrorToolHookDecision.reraise()
