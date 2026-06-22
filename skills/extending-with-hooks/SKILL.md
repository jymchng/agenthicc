---
skill: extending-with-hooks
version: 1.0.0
tags: [hooks, lifecycle, recovery, audit, rate-limiting]
summary: Implement and register LifecycleHooks for audit logging, rate limiting, and recovery via config dotpaths.
---

# Skill: Extending with Hooks

## When to use this skill

Use this skill when you need to:
- Add audit logging, rate limiting, or approval gates to tool executions
- Implement retry or fallback recovery on tool errors
- Register hooks statically via `agenthicc.toml` or dynamically via `hook_register`
- Test hook behaviour in isolation

---

## LifecycleHook ABC

```python
from abc import ABC, abstractmethod
from agenthicc.tools.hooks import LifecycleHook, RecoveryAction

class LifecycleHook(ABC):
    async def pre_execute(self, tool: Tool, inputs: dict) -> dict:
        """Called before execution. Return (possibly modified) inputs."""
        return inputs

    async def post_execute(self, tool: Tool, inputs: dict, result: Any) -> Any:
        """Called after successful execution. Return (possibly modified) result."""
        return result

    async def on_error(
        self, tool: Tool, inputs: dict, error: Exception
    ) -> RecoveryAction:
        """Called when execution raises. Return how to recover."""
        return RecoveryAction.abort
```

Only override the stages you need — the base class provides no-op defaults.

---

## RecoveryAction values

| Value | Behaviour |
|---|---|
| `RecoveryAction.retry` | Re-execute the tool (up to `Tool.max_retries` times) |
| `RecoveryAction.fallback` | Call `Tool.fallback` if defined; else abort |
| `RecoveryAction.abort` | Raise the original exception wrapped in `ToolResultEnvelope.error` |
| `RecoveryAction.ignore` | Return `None` result, no error recorded |

---

## HookRegistry and HookRunner

```python
from agenthicc.tools.hooks import HookRegistry, HookRunner

registry = HookRegistry()

# Register by dotted import path
registry.register(
    entity_type="file_write",   # tool name or "*" for all tools
    stage="pre_execute",
    handler_dotpath="myapp.hooks.FileWriteAuditHook",
)
registry.register(
    entity_type="*",
    stage="on_error",
    handler_dotpath="myapp.hooks.RetryHook",
)

runner = HookRunner(registry)
```

`HookRunner` loads each handler class by dotpath using `importlib.import_module`
and instantiates it once per `HookRunner` instance. All hooks for a `post_execute`
or `on_error` stage run in parallel via `asyncio.gather`. Pre-hooks run sequentially
because each one may modify the inputs passed to the next.

---

## TOML configuration

Register hooks statically in `agenthicc.toml`:

```toml
[hooks.file_write.pre_execute]
handlers = ["myapp.hooks.FileWriteAuditHook", "myapp.hooks.PathSanitizer"]

[hooks.file_write.on_error]
handlers = ["myapp.hooks.RetryHook"]

[hooks."*".pre_execute]
handlers = ["myapp.hooks.RateLimitHook"]
```

These are loaded by `load_config` and flattened into
`AgenthiccConfig.hooks: dict[str, list[str]]` with dotted keys like
`"file_write.pre_execute"`.

---

## Complete audit + recovery example

```python
# myapp/hooks.py
from __future__ import annotations

import logging
import time
from typing import Any

from agenthicc.tools.base import Tool
from agenthicc.tools.hooks import LifecycleHook, RecoveryAction

logger = logging.getLogger(__name__)


class FileWriteAuditHook(LifecycleHook):
    """Log every file_write call with timing and outcome."""

    def __init__(self) -> None:
        self._start: dict[str, float] = {}

    async def pre_execute(self, tool: Tool, inputs: dict) -> dict:
        self._start[inputs.get("path", "?")] = time.monotonic()
        logger.info(
            "file_write starting",
            extra={"path": inputs.get("path"), "tool": tool.name},
        )
        return inputs  # unchanged

    async def post_execute(self, tool: Tool, inputs: dict, result: Any) -> Any:
        elapsed = time.monotonic() - self._start.pop(inputs.get("path", "?"), 0)
        logger.info(
            "file_write success",
            extra={
                "path": inputs.get("path"),
                "elapsed_ms": round(elapsed * 1000, 1),
            },
        )
        return result

    async def on_error(
        self, tool: Tool, inputs: dict, error: Exception
    ) -> RecoveryAction:
        logger.error(
            "file_write failed: %s",
            error,
            extra={"path": inputs.get("path"), "tool": tool.name},
        )
        return RecoveryAction.abort


class RetryOnTimeoutHook(LifecycleHook):
    """Retry on asyncio.TimeoutError up to tool.max_retries times."""

    async def on_error(
        self, tool: Tool, inputs: dict, error: Exception
    ) -> RecoveryAction:
        import asyncio
        if isinstance(error, asyncio.TimeoutError) and tool.max_retries > 0:
            logger.warning("timeout on %s — retrying", tool.name)
            return RecoveryAction.retry
        return RecoveryAction.abort


class FallbackOnNetworkHook(LifecycleHook):
    """Fall back to a cached result when the network is unavailable."""

    async def on_error(
        self, tool: Tool, inputs: dict, error: Exception
    ) -> RecoveryAction:
        if "Connection" in type(error).__name__ and tool.fallback is not None:
            logger.warning("network error on %s — using fallback", tool.name)
            return RecoveryAction.fallback
        return RecoveryAction.abort


class RateLimitHook(LifecycleHook):
    """Simple token-bucket rate limiter for all tools."""

    def __init__(self, max_per_second: float = 10.0) -> None:
        self._tokens = max_per_second
        self._max = max_per_second
        self._last = time.monotonic()

    async def pre_execute(self, tool: Tool, inputs: dict) -> dict:
        import asyncio
        now = time.monotonic()
        elapsed = now - self._last
        self._tokens = min(self._max, self._tokens + elapsed * self._max)
        self._last = now
        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) / self._max
            logger.debug("rate limiter waiting %.3fs for %s", wait, tool.name)
            await asyncio.sleep(wait)
            self._tokens = 0.0
        else:
            self._tokens -= 1.0
        return inputs
```

### Wire it up

```python
from agenthicc.tools.hooks import HookRegistry, HookRunner
from agenthicc.tools.executor import AgenthiccToolExecutor
from agenthicc.security import PermissionChecker
from agenthicc.kernel import SecurityPolicy

registry = HookRegistry()
registry.register("file_write", "pre_execute",  "myapp.hooks.FileWriteAuditHook")
registry.register("file_write", "post_execute", "myapp.hooks.FileWriteAuditHook")
registry.register("file_write", "on_error",     "myapp.hooks.FileWriteAuditHook")
registry.register("*",          "on_error",     "myapp.hooks.RetryOnTimeoutHook")
registry.register("*",          "pre_execute",  "myapp.hooks.RateLimitHook")

runner = HookRunner(registry)
checker = PermissionChecker(SecurityPolicy())
executor = AgenthiccToolExecutor(permission_checker=checker, hook_runner=runner)

# Execute a tool through the full pipeline
from agenthicc.tools.base import Tool

async def write_file(path: str, content: str) -> str:
    with open(path, "w") as f:
        f.write(content)
    return f"wrote {len(content)} bytes"

tool = Tool(
    name="file_write",
    description="Write text to a file",
    parameters_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    fn=write_file,
    timeout_seconds=10.0,
    max_retries=2,
)

envelope = await executor.execute(
    tool=tool,
    inputs={"path": "/workspace/out.txt", "content": "hello"},
    tool_use_id="tc-001",
    agent_id="worker-1",
)
print(envelope.result, envelope.error, envelope.duration_ms)
```

---

## LaurenToolHookAdapter

Wrap an existing lauren-ai hook object to reuse it in the Agenthicc pipeline:

```python
from agenthicc.tools.hooks import LaurenToolHookAdapter
from lauren_ai.hooks import MyLaurenHook

adapter = LaurenToolHookAdapter(MyLaurenHook())
# adapter implements LifecycleHook — register via registry or pass directly
```

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `ImportError` in `HookRunner` | `handler_dotpath` module not on `sys.path` | Ensure the module is importable from the process's working directory |
| `AttributeError` in `HookRunner` | Class name in dotpath is wrong | Double-check the class name after the last `.` |
| Hook `pre_execute` modifies inputs but next hook doesn't see changes | Pre-hooks run sequentially — each receives the output of the previous | Ensure `pre_execute` returns the modified dict |
| `RecoveryAction.retry` loops forever | `Tool.max_retries = 0` (default) | Set `max_retries > 0` on the `Tool` to enable retries |
| `RecoveryAction.fallback` has no effect | `Tool.fallback` is `None` | Provide a `fallback` callable on the `Tool` |

---

## Key points

- Override only the hook stages you need — base class has safe no-op defaults.
- `pre_execute` must return the inputs dict (modified or unchanged).
- Pre-hooks run **sequentially**; post/error hooks run **in parallel** via `asyncio.gather`.
- `RecoveryAction.retry` only works when `Tool.max_retries > 0`.
- `RecoveryAction.fallback` only works when `Tool.fallback` is not `None`.
- Static hooks in `agenthicc.toml` and dynamic `hook_register` both write to
  `AppState.hooks`; both are replayed on `restore_from_log`.
- `LaurenToolHookAdapter` bridges lauren-ai hooks with no code changes to the
  underlying hook class.
