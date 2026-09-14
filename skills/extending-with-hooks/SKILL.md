---
name: extending-with-hooks
version: 1.0.0
tags: [hooks, lifecycle, audit, tool-policy, plugins]
description: >-
  Implement and register LifecycleHook subclasses that observe, rewrite, abort, or
  recover tool calls. Covers the three lauren-ai ToolHook stages and their decision
  factories, HookRegistry and HookRunner, and dotpath wiring from the [hooks] config.
---

# Skill: Extending with Hooks

## Read this first

`src/agenthicc/tools/hooks.py` is a **compatibility and configuration layer over
lauren-ai's canonical hooks**. It is not a second hook engine. Its module
docstring is explicit:

> Agenthicc does not duplicate hook decisions or lifecycle execution. The classes
> below only provide registry/configuration conveniences; actual hook dispatch is
> always performed by `lauren_ai._tools.ToolExecutor`.

So the correct mental model is: **lauren-ai owns dispatch, ordering, approval
signals, and result semantics. Agenthicc supplies the hook classes and the
configuration plumbing that feeds them.**

The kernel still carries a `HookRegistered` event and a matching state shape for
compatibility (`src/agenthicc/kernel/state.py:154,198`), but neither is the
source of runtime hook registration.

## When to use this skill

Use this skill when you need to:

- Observe or rewrite tool calls and results
- Abort a tool call before it runs, or modify its input
- Swallow a tool error and substitute a result
- Register hooks from configuration instead of code
- Understand which hook stages actually exist

If you want *policy* (capability ceilings, approvals) rather than *observation*,
the built-in `ToolCapabilityGate` and `ApprovalGate` already do it — see the last
section.

---

## The `ToolHook` contract

`LifecycleHook` is a thin compatibility alias for lauren-ai's `ToolHook`:

```python
from agenthicc.tools.hooks import LifecycleHook
# LifecycleHook is a ToolHook subclass with an empty body.
```

There are exactly **three** stages, all `async`, and all with no-op defaults:

```python
class ToolHook:
    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        return BeforeToolHookDecision.proceed()

    async def after_tool_call(self, result, ctx: ToolCallContext) -> AfterToolHookDecision:
        return AfterToolHookDecision.proceed()

    async def on_tool_error(self, exc: Exception, ctx: ToolCallContext) -> ErrorToolHookDecision:
        return ErrorToolHookDecision.reraise()
```

There is no `pre_execute`/`post_execute`/`on_error` triple and no
`RecoveryAction` enum. Overriding a stage you do not need is unnecessary — the
defaults are already correct.

### Decisions

Always construct decisions with the factory class-methods, never directly.

| Stage | Factories | Effect |
|---|---|---|
| `before_tool_call` | `BeforeToolHookDecision.proceed()` | Continue unchanged |
| | `BeforeToolHookDecision.modify(new_input)` | Replace the input dict for later hooks and the tool |
| | `BeforeToolHookDecision.abort(result)` | Skip the tool; later before-hooks, the tool, and all after/error hooks are skipped |
| `after_tool_call` | `AfterToolHookDecision.proceed()` | Pass the result through |
| | `AfterToolHookDecision.replace(result)` | Replace the result for later hooks and the caller |
| `on_tool_error` | `ErrorToolHookDecision.reraise()` | Propagate the error |
| | `ErrorToolHookDecision.suppress_with(result)` | Swallow the error; later error hooks and all after hooks are skipped |

### `ToolCallContext`

```text
agent_context, tool_use_id, turn,
metadata, state, tool_state, dependencies, extras,
tool_name, tool_input
```

`ctx.get_metadata(key, default=None)` checks tool-level static metadata first,
then delegates to agent-level runtime metadata, then returns `default`.

> **Important:** hooks are resolved as DI singletons and shared across every
> tool call that uses them. Do **not** keep per-call mutable state on `self`.
> Use `ctx.state` (or `ctx.tool_state`) for anything call-scoped.

---

## Writing hooks

```python
# myapp/hooks.py
from __future__ import annotations

import logging
from typing import Any

from agenthicc.tools.hooks import (
    AfterToolHookDecision,
    BeforeToolHookDecision,
    ErrorToolHookDecision,
    LifecycleHook,
    ToolCallContext,
)

logger = logging.getLogger(__name__)


class FileWriteAuditHook(LifecycleHook):
    """Audit every file_write call without changing its behaviour."""

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        logger.info("tool starting", extra={"tool": ctx.tool_name, "turn": ctx.turn})
        return BeforeToolHookDecision.proceed()

    async def after_tool_call(
        self, result: Any, ctx: ToolCallContext
    ) -> AfterToolHookDecision:
        logger.info("tool finished", extra={"tool": ctx.tool_name})
        return AfterToolHookDecision.proceed()

    async def on_tool_error(
        self, exc: Exception, ctx: ToolCallContext
    ) -> ErrorToolHookDecision:
        logger.error("tool failed: %s", exc, extra={"tool": ctx.tool_name})
        return ErrorToolHookDecision.reraise()


class ReadOnlyGuardHook(LifecycleHook):
    """Hard-block a tool by aborting the call with a synthetic result."""

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        if ctx.tool_name == "run_bash":
            return BeforeToolHookDecision.abort(
                {"ok": False, "error": "run_bash is disabled in this environment"}
            )
        return BeforeToolHookDecision.proceed()


class RedactingHook(LifecycleHook):
    """Rewrite tool input before dispatch."""

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        if "token" in ctx.tool_input:
            return BeforeToolHookDecision.modify({**ctx.tool_input, "token": "***"})
        return BeforeToolHookDecision.proceed()


class CachedFallbackHook(LifecycleHook):
    """Swallow a known error and substitute a cached result."""

    def __init__(self, cache: dict[str, Any]) -> None:
        self._cache = cache

    async def on_tool_error(
        self, exc: Exception, ctx: ToolCallContext
    ) -> ErrorToolHookDecision:
        key = str(ctx.tool_input.get("path", ""))
        if isinstance(exc, FileNotFoundError) and key in self._cache:
            return ErrorToolHookDecision.suppress_with(self._cache[key])
        return ErrorToolHookDecision.reraise()
```

Note the shape of the stateful case: `CachedFallbackHook` keeps shared config on
`self`, which is fine, but it never stores *per-call* state there — that would
leak across concurrent tool calls.

---

## `HookRegistry` and `HookRunner`

```python
from agenthicc.tools.hooks import HookRegistry, HookRunner

registry = HookRegistry()

# Register an instance, or a "module:attribute" dotpath.
registry.register("file_write", "before", "myapp.hooks.FileWriteAuditHook")
registry.register("file_write", "after", "myapp.hooks.FileWriteAuditHook")
registry.register("*", "error", "myapp.hooks.CachedFallbackHook")

runner = HookRunner(registry)
runner.run_before("file_write", tool_call, ctx)     # -> decision or None
runner.run_after("file_write", result, ctx)         # -> possibly replaced result
runner.run_error("file_write", exc, ctx)            # -> decision or None
```

Things worth knowing before you rely on this:

- **Stage names are `"before"`, `"after"`, `"error"`.** Anything else raises
  `ValueError(f"Unsupported hook stage: {stage!r}")`.
- `HookRunner(...)` also accepts `hooks=[...]` for a list of instances supplied
  directly; those run before the registry's hooks.
- `HookRunner` runs the hooks of a stage with `asyncio.gather`, i.e.
  **concurrently, not sequentially**. Do not depend on one hook's mutation being
  visible to a sibling in the same stage. `run_before` returns the first abort in
  registration order; `run_error` returns the first decision returned.
- `run_after` iterates the hooks in **reverse** registration order.
- Wildcard `"*"` registrations resolve before exact-name registrations.

`HookRegistry.get()` and the runner are convenience helpers for configuration and
tests. In the live runtime, dispatch is still lauren-ai's.

---

## Dotpath loading

```python
from agenthicc.tools.hooks import load_hook_from_dotpath

hook = load_hook_from_dotpath("myapp.hooks:FileWriteAuditHook")
```

The separator is a **colon** — `module:attribute`, not `module.attribute`. The
loader partitions on `":"`, so a dotted path fails with:

```text
ValueError: Hook path must be module:attribute, got 'myapp.hooks.FileWriteAuditHook'
```

If the attribute is a class it is instantiated with no arguments; if it is a
value it is used as-is. Either way it must be a `ToolHook` or it raises
`TypeError: Loaded hook '...' is not a lauren-ai ToolHook`.

---

## Configuration

Static hooks come from the `[hooks]` table. `load_config` flattens the nested
tables into dotted keys on `AgenthiccConfig.hooks: dict[str, list[str]]`
(`src/agenthicc/config.py:1494`), where a nested table's path becomes the key
(`_flatten_hooks`, `:1825`):

```toml
[hooks]
"intent.pre_validate" = ["myapp.hooks:validate_intent"]

[hooks.file_write]
before = ["myapp.hooks:FileWriteAuditHook"]
error  = ["myapp.hooks:CachedFallbackHook"]
```

The first form produces the key `"intent.pre_validate"`; the second produces
`"file_write.before"` and `"file_write.error"`. The config template documents the
same convention with the comment `"intent.pre_validate" = ["module:function"]`
(`src/agenthicc/config_template.py:256`).

Numbers are not `ToolHook`s — the config layer only *carries* the dotpaths and
their arguments. A consumer decides what `module:function` means for its own
lifecycle point.

---

## How hooks actually reach the runtime

Two paths, both ending in lauren-ai's executor:

1. `AgenthiccToolExecutor` takes hooks directly
   (`src/agenthicc/tools/executor.py:197`):

   ```python
   from agenthicc.tools.executor import AgenthiccToolExecutor

   executor = AgenthiccToolExecutor(global_hooks=[FileWriteAuditHook()])
   ```

   Constructor: `(tools=None, *, sandbox=None, global_hooks=None,
   approval_handler=None, event_sink=None, default_timeout_s=30.0)`. The
   executor forwards `global_hooks` to lauren-ai's `LaurenToolExecutor`
   (`:401`).

2. The agent turn builds its own hook list and passes it as `global_hooks` to
   `AgentRunnerBase` (`src/agenthicc/runners/agent_turn.py:1364`). The built-ins
   it installs are the ones to copy from (`:1280-1301`):

   | Built-in | Role |
   |---|---|
   | `_ToolOutputCaptureHook` | Captures tool output, command outcomes, tool state |
   | `ToolCapabilityGate` | Enforces the capability ceiling for the active mode |
   | `ApprovalGate` | Raises approval requests through the approval service |

   Subagents assemble a similar list, adding `_MutationEvidenceHook`
   (`src/agenthicc/subagents/pool.py:607-640`).

If you want a hook that applies everywhere, register it alongside these, not by
adding a `Live`-style side channel.

`LaurenToolHookAdapter` exists to wrap an object that already implements the
three lauren-ai hook methods so it can be used anywhere an agenthicc hook is
expected.

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `ValueError: Unsupported hook stage` | Used `pre_execute`/`post_execute`/`on_error` | The stages are `before`, `after`, `error` |
| `ValueError: Hook path must be module:attribute` | Used a dotted path | Use `module:attribute` |
| `TypeError: ... is not a lauren-ai ToolHook` | Dotpath points at something else | Subclass `ToolHook`/`LifecycleHook` |
| `ImportError` from `load_hook_from_dotpath` | Module not importable in this process | Make the module importable from the working directory |
| A sibling hook does not see your input change | Same-stage hooks run concurrently | Rely only on documented ordering, or use separate stages |
| State leaks between tool calls | Per-call state kept on `self` | Use `ctx.state` |
| Config hook never fires | Value is carried but no consumer reads that key | Wire the key into the lifecycle point that owns it |

---

## Key points

- `src/agenthicc/tools/hooks.py` adapts configuration and tests to lauren-ai's
  canonical `ToolHook`; lauren-ai performs dispatch.
- Three stages only: `before_tool_call`, `after_tool_call`, `on_tool_error`.
- Decisions come from factories: `proceed` / `modify` / `abort`,
  `proceed` / `replace`, `reraise` / `suppress_with`.
- `LifecycleHook` is a compatibility alias, not a separate ABC.
- Hooks are shared singletons — keep per-call data in `ctx.state`.
- Dotpaths use a colon: `module:attribute`.
- Same-stage hooks run concurrently under `asyncio.gather`.
- `[hooks]` keys are flattened to dotted strings on
  `AgenthiccConfig.hooks`; the layer carries them, a consumer interprets them.
- For policy, prefer the built-in `ToolCapabilityGate` and `ApprovalGate` over
  writing a new gate from scratch.
