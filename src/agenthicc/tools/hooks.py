"""Compatibility helpers built on lauren-ai's canonical tool hooks.

Agenthicc does not duplicate hook decisions or lifecycle execution.  The
classes below only provide registry/configuration conveniences; actual hook
dispatch is always performed by ``lauren_ai._tools.ToolExecutor``.
"""

from __future__ import annotations

import asyncio
import importlib

from lauren_ai import (
    AfterToolHookDecision,
    BeforeToolHookDecision,
    ErrorToolHookDecision,
    ToolCallContext,
    ToolHook,
)
from lauren_ai._tools._hooks import (
    _NO_REPLACE,
)

__all__ = [
    "AfterToolHookDecision",
    "BeforeToolHookDecision",
    "ErrorToolHookDecision",
    "HookRegistry",
    "HookRunner",
    "LaurenToolHookAdapter",
    "LifecycleHook",
    "ToolCallContext",
    "ToolHook",
    "load_hook_from_dotpath",
]


class LifecycleHook(ToolHook):
    """Agenthicc compatibility name for lauren-ai's ``ToolHook`` base class."""


class HookRegistry:
    """Registry of lauren-ai hooks keyed by entity type and stage."""

    def __init__(self) -> None:
        self._hooks: dict[tuple[str, str], list[ToolHook | str]] = {}

    def register(self, entity_type: str, stage: str, hook: ToolHook | str) -> None:
        """Register a hook instance or ``module:attribute`` dotpath."""
        if stage not in {"before", "after", "error"}:
            raise ValueError(f"Unsupported hook stage: {stage!r}")
        self._hooks.setdefault((entity_type, stage), []).append(hook)

    def get(self, entity_type: str, stage: str) -> list[ToolHook]:
        """Resolve wildcard and exact hooks in registration order."""
        values = [
            *self._hooks.get(("*", stage), []),
            *self._hooks.get((entity_type, stage), []),
        ]
        return [
            load_hook_from_dotpath(value) if isinstance(value, str) else value for value in values
        ]


class HookRunner:
    """Small test/configuration helper using lauren-ai decision classes."""

    def __init__(
        self,
        registry: HookRegistry | None = None,
        hooks: list[ToolHook] | None = None,
    ) -> None:
        self._registry = registry or HookRegistry()
        self._hooks = list(hooks or [])

    def _get(self, entity_type: str, stage: str) -> list[ToolHook]:
        return [*self._hooks, *self._registry.get(entity_type, stage)]

    async def run_before(
        self,
        entity_type: str,
        tool_call: object,
        ctx: ToolCallContext,
    ) -> BeforeToolHookDecision | None:
        """Run all before hooks and return the first abort in registration order."""
        hooks = self._get(entity_type, "before")
        decisions = await asyncio.gather(
            *(hook.before_tool_call(ctx) for hook in hooks),
            return_exceptions=True,
        )
        for decision in decisions:
            if isinstance(decision, BaseException):
                return BeforeToolHookDecision.abort(
                    {"ok": False, "error": f"before hook failed: {decision}"}
                )
            if decision._aborted:
                return decision
        return None

    async def run_after(
        self,
        entity_type: str,
        result: object,
        ctx: ToolCallContext,
    ) -> object:
        """Run after hooks using lauren-ai's replacement decisions."""
        hooks = list(reversed(self._get(entity_type, "after")))
        decisions = await asyncio.gather(
            *(hook.after_tool_call(result, ctx) for hook in hooks),
            return_exceptions=True,
        )
        current = result
        for decision in decisions:
            if isinstance(decision, AfterToolHookDecision):
                replacement = decision._replacement
                if replacement is not _NO_REPLACE:
                    current = replacement
        return current

    async def run_error(
        self,
        entity_type: str,
        exc: Exception,
        ctx: ToolCallContext,
    ) -> ErrorToolHookDecision | None:
        """Run error hooks and return the first lauren-ai decision."""
        hooks = self._get(entity_type, "error")
        decisions = await asyncio.gather(
            *(hook.on_tool_error(exc, ctx) for hook in hooks),
            return_exceptions=True,
        )
        for decision in decisions:
            if isinstance(decision, ErrorToolHookDecision):
                return decision
        return None


class LaurenToolHookAdapter(LifecycleHook):
    """Adapt an object exposing lauren-ai's hook methods to ``ToolHook``."""

    def __init__(self, lauren_hook: ToolHook) -> None:
        self._lauren_hook = lauren_hook

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        return await self._lauren_hook.before_tool_call(ctx)

    async def after_tool_call(
        self,
        result: object,
        ctx: ToolCallContext,
    ) -> AfterToolHookDecision:
        return await self._lauren_hook.after_tool_call(result, ctx)

    async def on_tool_error(
        self,
        exc: Exception,
        ctx: ToolCallContext,
    ) -> ErrorToolHookDecision:
        return await self._lauren_hook.on_tool_error(exc, ctx)


def load_hook_from_dotpath(dotpath: str) -> ToolHook:
    """Import a lauren-ai ``ToolHook`` from ``module:attribute``."""
    module_name, separator, attribute = dotpath.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"Hook path must be module:attribute, got {dotpath!r}")
    loaded = getattr(importlib.import_module(module_name), attribute)
    hook = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(hook, ToolHook):
        raise TypeError(f"Loaded hook {dotpath!r} is not a lauren-ai ToolHook")
    return hook
