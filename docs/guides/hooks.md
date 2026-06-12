# Lifecycle Hooks

Lifecycle hooks let you intercept every execution stage in agenthicc —
intent validation, workflow node transitions, task execution, agent spawns,
and individual tool calls — before and after they happen, and recover
gracefully from errors.

---

## `LifecycleHook` ABC

All hooks inherit from `LifecycleHook`:

```python
from agenthicc.tools.hooks import LifecycleHook, RecoveryAction, Rejection

class LifecycleHook(abc.ABC):
    async def on_before(self, entity: Any, ctx: Any) -> Rejection | None:
        """Called before the entity executes.  Return a Rejection to abort."""
        return None

    async def on_after(self, entity: Any, result: Any, ctx: Any) -> None:
        """Called after the entity executes successfully."""
        return None

    async def on_error(
        self, entity: Any, error: BaseException, ctx: Any
    ) -> RecoveryAction | None:
        """Called when the entity raises.  May suggest a RecoveryAction."""
        return None
```

All three methods default to no-ops.  Concrete hooks only override the stages
they care about, so a hook that only audits successful completions only needs
to define `on_after`.

---

## `RecoveryAction` enum

`on_error` may return one of four recovery hints:

| Value | Meaning |
|---|---|
| `RecoveryAction.RETRY` | Retry the failed operation (executor decides max retries) |
| `RecoveryAction.FALLBACK` | Substitute a fallback value and continue |
| `RecoveryAction.ESCALATE` | Surface the error to the parent agent or operator |
| `RecoveryAction.SKIP` | Log the error and continue without the result |

Returning `None` from `on_error` means "no suggestion — propagate normally."

### `Rejection` dataclass

`on_before` returns a `Rejection` to prevent execution:

```python
from agenthicc.tools.hooks import Rejection

@dataclass(slots=True)
class Rejection:
    reason: str  # Human-readable explanation logged to the event log
```

---

## `HookRegistry`

`HookRegistry` maps `(entity_type, stage)` pairs to ordered lists of hooks.

```python
from agenthicc.tools.hooks import HookRegistry, LifecycleHook

registry = HookRegistry()
registry.register("tool_call", "before", my_audit_hook)
registry.register("tool_call", "after",  my_metrics_hook)
registry.register("tool_call", "error",  my_recovery_hook)
```

Valid stages are `"before"`, `"after"`, and `"error"`.  Entity types are
free-form strings; the executor passes the type that matches the entity being
processed (e.g. `"tool_call"`, `"task"`, `"agent_spawn"`).

Retrieving all hooks for a stage:

```python
hooks = registry.hooks_for("tool_call", "before")
```

---

## `HookRunner`

`HookRunner` executes all hooks registered for a stage concurrently via
`asyncio.gather`, then combines results.

```python
from agenthicc.tools.hooks import HookRunner, HookRegistry

runner = HookRunner(registry=registry)

# Run before-hooks; returns first Rejection or None
rejection = await runner.run_before("tool_call", tool_entity, ctx)
if rejection is not None:
    # Abort and log rejection.reason
    ...

# Run after-hooks (fire-and-forget pattern; no return value)
await runner.run_after("tool_call", tool_entity, result, ctx)

# Run error-hooks; returns first non-None RecoveryAction
action = await runner.run_error("tool_call", tool_entity, exc, ctx)
if action is RecoveryAction.RETRY:
    # re-run the tool
    ...
```

**Concurrency note**: all hooks at a stage run in parallel.  For
`run_before`, the first `Rejection` in registration order wins even if
multiple hooks reject simultaneously.  For `run_error`, the first non-None
`RecoveryAction` wins.

---

## Registering hooks via `agenthicc.toml`

Static hooks are registered before the kernel starts by listing dotted import
paths in the `[hooks]` table:

```toml
[hooks]
# Format: entity_type.stage = ["dotpath1", "dotpath2", ...]

[hooks.tool_call]
before = ["myproject.hooks:AuditHook"]
after  = ["myproject.hooks:MetricsHook"]
error  = ["myproject.hooks:RetryHook"]

[hooks.agent_spawn]
before = ["myproject.hooks:SpawnGuardHook"]
```

Each string is resolved via `load_hook_from_dotpath`.  Both colon-separated
(`"pkg.module:ClassName"`) and dot-separated (`"pkg.module.ClassName"`) forms
are accepted.  If the resolved attribute is a class it is instantiated with no
arguments; if it is already an instance it is used directly.

---

## Dynamic registration via `hook_register` tool

Agents can register hooks at runtime using the `hook_register` communication
tool:

```python
result = await tools.hook_register(
    entity_type="tool_call",
    stage="before",
    handler_dotpath="myproject.hooks:AuditHook",
)
# {"hook_id": "<hex>", "entity_type": "tool_call", "stage": "before", "registered": True}
```

This emits a `HookRegistered` event to the kernel; the runtime's effect
executor calls `load_hook_from_dotpath` and registers the hook with the live
`HookRegistry`.  Dynamic hooks take effect immediately for all subsequent
executions.

---

## `LaurenToolHookAdapter`

`LaurenToolHookAdapter` wraps any `LifecycleHook` into a lauren-ai
`ToolHook`, bridging the two hook systems transparently:

```python
from agenthicc.tools.hooks import LaurenToolHookAdapter

lauren_hook = LaurenToolHookAdapter(my_lifecycle_hook)
# Pass lauren_hook to a lauren-ai AgentRunnerConfig
```

Stage mapping:

| agenthicc | lauren-ai |
|---|---|
| `on_before` returning `Rejection` | `BeforeToolHookDecision.abort({"ok": False, "error": "rejected: ..."})` |
| `on_before` returning `None` | `BeforeToolHookDecision.proceed()` |
| `on_after` | `AfterToolHookDecision.proceed()` |
| `on_error` returning `RecoveryAction.FALLBACK` | `ErrorToolHookDecision.suppress_with(fallback_value)` |
| `on_error` returning anything else | `ErrorToolHookDecision.reraise()` |

The fallback value for `FALLBACK` is read from `ctx.state["fallback_value"]`
when present.

---

## Complete example: audit hook

This hook logs every tool call and its result to the structured event log:

```python
# myproject/hooks.py

from __future__ import annotations
import time
from typing import Any
from agenthicc.tools.hooks import LifecycleHook, Rejection, RecoveryAction


class AuditHook(LifecycleHook):
    """Append an audit record for every tool call."""

    async def on_before(self, entity: Any, ctx: Any) -> Rejection | None:
        tool_name = entity if isinstance(entity, str) else getattr(entity, "name", str(entity))
        # Block tools on an explicit deny-list
        BLOCKED = {"rm_rf", "drop_table"}
        if tool_name in BLOCKED:
            return Rejection(reason=f"{tool_name!r} is on the deny-list")
        return None

    async def on_after(self, entity: Any, result: Any, ctx: Any) -> None:
        tool_name = entity if isinstance(entity, str) else getattr(entity, "name", str(entity))
        agent_id  = getattr(ctx, "agent_id", None)
        print(
            f"[AUDIT] tool={tool_name!r} agent={agent_id!r} "
            f"result_keys={list(result.keys()) if isinstance(result, dict) else type(result).__name__}"
        )

    async def on_error(self, entity: Any, error: BaseException, ctx: Any) -> RecoveryAction | None:
        tool_name = entity if isinstance(entity, str) else getattr(entity, "name", str(entity))
        print(f"[AUDIT] tool={tool_name!r} FAILED: {type(error).__name__}: {error}")
        return None   # let the executor decide
```

---

## Complete example: error-recovery hook that spawns a debugger

This hook intercepts `ERROR`-level failures on tasks and spawns a debugger
agent to investigate:

```python
# myproject/hooks.py  (continued)

from agenthicc.tools.hooks import LifecycleHook, RecoveryAction
from agenthicc.runtime.comm_tools import CommunicationTools


class DebuggerSpawnHook(LifecycleHook):
    """On task error: escalate and spawn a debugger agent."""

    def __init__(self, tools: CommunicationTools, workflow_id: str) -> None:
        self._tools = tools
        self._workflow_id = workflow_id

    async def on_error(
        self, entity: Any, error: BaseException, ctx: Any
    ) -> RecoveryAction | None:
        task_id  = getattr(entity, "task_id",  None)
        agent_id = getattr(ctx,    "agent_id", None)

        await self._tools.application_log(
            "ERROR",
            f"Task {task_id} failed; spawning debugger agent",
            {"error": str(error), "task_id": task_id, "agent_id": agent_id},
        )

        await self._tools.agent_spawn(
            "debugger",
            config={
                "failed_task_id":  task_id,
                "failed_agent_id": agent_id,
                "error_message":   str(error),
                "workflow_id":     self._workflow_id,
            },
            parent_agent_id=agent_id,
        )

        # Escalate so the orchestrator can decide whether to retry or abort
        return RecoveryAction.ESCALATE
```

Register it dynamically from the orchestrator agent:

```python
# Inside your orchestrator's run() method, after creating the workflow:
await tools.hook_register(
    entity_type="task",
    stage="error",
    handler_dotpath="myproject.hooks:DebuggerSpawnHook",
)
```

Or statically in `agenthicc.toml`:

```toml
[hooks.task]
error = ["myproject.hooks:DebuggerSpawnHook"]
```

Note that for constructor-injected hooks (those requiring `tools` and
`workflow_id`), use dynamic registration with a pre-instantiated object
stored at a module-level attribute, or use a factory function registered via
the dotpath mechanism.

---

## Next steps

- [Writing agents](agents.md) — the `hook_register` communication tool
- [Memory guide](memory.md) — persist hook audit records to project memory
- [Kernel reference](../reference/kernel.md) — `HookRegistered` event and `AppState.hooks`
