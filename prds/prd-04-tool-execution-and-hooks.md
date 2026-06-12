---
id: PRD-04
title: "Tool Execution Layer and Lifecycle Hooks"
status: draft
version: "0.1.0"
created: 2025-06-01
updated: 2025-06-01
authors:
  - platform-team
reviewers:
  - agents-team
  - security-team
tags:
  - tools
  - hooks
  - execution
  - sandboxing
  - lifecycle
related_prds:
  - PRD-01  # Agent Runtime
  - PRD-02  # Workflow Engine
  - PRD-03  # Intent and Routing
---

# PRD-04: Tool Execution Layer and Lifecycle Hooks

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Architecture and Design](#3-architecture-and-design)
4. [Data Structures and Interfaces](#4-data-structures-and-interfaces)
5. [Implementation Plan](#5-implementation-plan)
6. [Tests](#6-tests)
7. [Configuration Reference](#7-configuration-reference)
8. [Open Questions](#8-open-questions)

---

## 1. Executive Summary

The Tool Execution Layer is the lowest-level runtime component of the AgentHicc
platform. It is responsible for dispatching tool calls issued by agents, enforcing
per-call permissions, isolating side-effects inside a sandbox, and surfacing a
structured lifecycle through which every other subsystem -- auditing, rate limiting,
observability, human-in-the-loop review, and recovery -- can observe and mutate
execution without coupling to dispatcher internals.

This document specifies:

- The `Tool` ABC: the contract every callable capability must implement, including
  name, description, JSON-Schema parameters, and an `execute` method.
- `ToolContext`: the rich per-call context injected into every execution and hook.
- Parallel tool execution: `asyncio.gather`-based fan-out bounded by
  `tool_call_budget`.
- Permission enforcement: pre-execution check against the agent's declared
  capabilities and per-tool access rules.
- Sandboxing: `WorkspaceView` (path-prefix enforcement), network allow-list, and
  resource limits via `asyncio.wait_for`.
- `ToolRegistration` in `AppState`.
- `LifecycleHook` ABC and the full hook execution model across every entity level:
  Intent, Workflow, WorkflowNode/Task, AgentInstance, and ToolCall.
- Hook registration via TOML config, the `hook_register` dynamic tool, and plugin
  entry-points (`agenthicc.hooks`).
- The `RecoveryAction` enum: `RETRY`, `FALLBACK`, `ESCALATE`, `SKIP`.
- Mapping to the existing `lauren_ai._tools._executor` implementation, including
  `ToolHook`, `BeforeToolHookDecision`, `AfterToolHookDecision`,
  `ErrorToolHookDecision`, `ToolCallContext`, and `ToolMeta`.

The design delivers predictable, auditable tool execution with zero changes needed
to existing tool implementations when hooks are added, updated, or removed.

---

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal |
|---|------|
| G1 | Provide a single, typed execution path for all tool calls regardless of origin (LLM tool-use, workflow step, user command). |
| G2 | Allow before/after/error hooks at five entity levels without modifying tool code. |
| G3 | Guarantee that global hooks wrap per-tool hooks (outer-to-inner ordering). |
| G4 | Short-circuit execution on the first `Rejection` from any `on_before` hook. |
| G5 | Enforce filesystem, network, and CPU sandboxing without relying on kernel namespaces. |
| G6 | Enable parallel fan-out of independent tool calls within a single agent turn. |
| G7 | Support declarative hook configuration via TOML and dynamic registration at runtime. |
| G8 | Expose plugin hook discovery via `importlib.metadata` entry-points. |
| G9 | Provide deterministic retry / fallback / escalate recovery from hook-caught errors. |
| G10 | Serialize all `ToolResult` values to JSON-serialisable structures for storage and replay. |

### 2.2 Non-Goals

| # | Non-Goal |
|---|----------|
| NG1 | Kernel-level sandboxing (seccomp, cgroups, Docker). This is a deployment-layer concern. |
| NG2 | Distributed tracing propagation format (covered by PRD-05). |
| NG3 | LLM prompt construction or tool-description injection into context windows. |
| NG4 | Tool versioning and schema migration. |
| NG5 | Authorization policy language (handled by the IAM layer atop the permission check). |
| NG6 | Human-in-the-loop UI (hooks can trigger it; the UI itself is out of scope). |

---

## 3. Architecture and Design

### 3.1 Component Overview

```
+---------------------------------------------------------------------+
|                          Agent Turn                                  |
|                                                                      |
|  LLM response ---> ToolCallBatch(tool_calls=[...])                  |
|                         |                                            |
|                         v                                            |
|              +----------------------+                               |
|              |    ToolExecutor       |                               |
|              |  (lauren_ai._tools   |                               |
|              |    ._executor)        |                               |
|              +----------+-----------+                               |
|                         |  asyncio.gather (parallel_tool_calls)     |
|            +------------+------------+                              |
|            v            v            v                              |
|     _run_one()   _run_one()   _run_one()                            |
|            |                                                         |
|            v                                                         |
|   +-----------------------------------------+                       |
|   |          Single Tool Call Pipeline       |                       |
|   |                                          |                       |
|   |  1. Build ToolCallContext                |                       |
|   |  2. Permission check                     |                       |
|   |  3. Run global on_before hooks           |                       |
|   |  4. Run per-tool on_before hooks         |                       |
|   |       -- first Rejection --> abort -->  |                       |
|   |  5. Sandbox entry (WorkspaceView, etc.)  |                       |
|   |  6. asyncio.wait_for(tool.execute(...))  |                       |
|   |  7. Sandbox exit                         |                       |
|   |  8. Run per-tool on_after hooks          |                       |
|   |  9. Run global on_after hooks            |                       |
|   |  10. Serialize ToolResult                |                       |
|   |  (on error: on_error hooks -> Recovery)  |                       |
|   +-----------------------------------------+                       |
+---------------------------------------------------------------------+
```

### 3.2 Hook Execution Model

Hooks are ordered **global-wraps-per-tool** (outermost first, innermost last),
analogous to WSGI middleware. The `on_before` stage fires in declaration order;
the `on_after` stage fires in **reverse** declaration order so that the first hook
registered is also the last to see the result (stack discipline).

```
Declaration order: [GlobalAuditHook, GlobalRateLimitHook, ToolSpecificHook]

on_before execution order:
  GlobalAuditHook.on_before      --->
  GlobalRateLimitHook.on_before  --->
  ToolSpecificHook.on_before     ---> tool.execute()

on_after execution order (reverse):
  ToolSpecificHook.on_after      --->
  GlobalRateLimitHook.on_after   --->
  GlobalAuditHook.on_after

on_error execution order (same as on_before):
  GlobalAuditHook.on_error       --->
  GlobalRateLimitHook.on_error   --->
  ToolSpecificHook.on_error
```

#### 3.2.1 Parallel Hook Execution within a Stage

Within a single stage (`on_before`, `on_after`, `on_error`) all hooks that do not
depend on each other's output are run concurrently via `asyncio.gather`.  The
gather is wrapped in a short-circuit coroutine: as soon as any hook returns a
`Rejection`, execution of the remaining hooks in that gather is cancelled and the
rejection propagates up immediately.

```
Stage: on_before
  asyncio.gather(
    hook_a.on_before(ctx),
    hook_b.on_before(ctx),
    hook_c.on_before(ctx),
  )
  |
  +- hook_a returns None  (passes)
  +- hook_b returns Rejection("rate limit")  --> cancel hook_c, abort call
  +- hook_c  (cancelled)
```

#### 3.2.2 Hook Entity Levels

```
Entity Level     Hook Type               Example Hooks
-----------------------------------------------------------------
Intent           IntentLifecycleHook     intent_audit, intent_guard
Workflow         WorkflowLifecycleHook   workflow_timer, workflow_quota
WorkflowNode     NodeLifecycleHook       node_retry_policy
Task             TaskLifecycleHook       task_sla_alert
AgentInstance    AgentLifecycleHook      agent_telemetry
ToolCall         ToolHook                tool_audit, tool_sandbox_check
-----------------------------------------------------------------
```

### 3.3 Sandbox Architecture

```
+------------------------------------------------------------+
|                      Sandbox Envelope                       |
|                                                             |
|  +-----------------+   +------------------------------+   |
|  |  WorkspaceView   |   |      NetworkGuard            |   |
|  |                  |   |                              |   |
|  |  root: /agents/  |   |  allow_list: ["api.ex.com"]  |   |
|  |  <agent_id>/ws/  |   |  block: all others           |   |
|  |                  |   |                              |   |
|  |  open(path) -->  |   |  httpx_hook intercepts       |   |
|  |  resolve &       |   |  outgoing requests           |   |
|  |  prefix check    |   +------------------------------+   |
|  +-----------------+                                        |
|                                                             |
|  +------------------------------------------------------+  |
|  |              ResourceLimiter                          |  |
|  |                                                       |  |
|  |  asyncio.wait_for(coro, timeout=tool_meta.timeout_s)  |  |
|  |  memory_limit checked via resource.getrusage()        |  |
|  +------------------------------------------------------+  |
+------------------------------------------------------------+
```

### 3.4 ToolResult Serialization

Every `ToolResult` is reduced to a JSON-serialisable dict before being stored in
agent memory or sent back to the LLM. The serialisation contract is:

```json
{
  "tool_use_id": "toolu_01XYZ",
  "tool_name":   "read_file",
  "ok":          true,
  "content":     "<tool output string or structured object>",
  "error":       null,
  "metadata": {
    "duration_ms": 42,
    "cache_hit":   false,
    "hook_events": []
  }
}
```

### 3.5 Permission Enforcement

Before any hooks fire, the executor performs a synchronous permission check:

```
ToolExecutor._run_one()
  |
  +--> PermissionChecker.check(agent_ctx.capabilities, tool_name)
         |
         +--> capability in agent_ctx.capabilities?    YES --> proceed
         |
         +--> tool_meta.requires_confirmation?         YES --> inject HumanConfirmationHook
         |
         +--> permission denied                        --> ToolResult.error("Permission denied")
```

Permission rules are declared per-agent in the agent manifest and stored in
`AgentContext.capabilities` as a frozenset of capability strings.  The
`PermissionChecker` maps tool names to required capability strings via a registry
table configurable in TOML under `[tools.permissions]`.

---

## 4. Data Structures and Interfaces

All type annotations use Python 3.12+ syntax (`type X = ...`, PEP 695).

### 4.1 Tool ABC

```python
# agenthicc/tools/base.py
from __future__ import annotations

import abc
from typing import Any, ClassVar

from pydantic import BaseModel

from agenthicc.tools.context import ToolContext
from agenthicc.tools.result import ToolResult


class Tool(abc.ABC):
    """Abstract base class every AgentHicc tool must implement.

    Subclasses declare their JSON Schema via a Pydantic ``Args`` inner
    class; the framework generates the tool description from it automatically.
    """

    #: Stable identifier used in tool_use messages and registry keys.
    name: ClassVar[str]

    #: Human-readable description forwarded to the LLM in the system prompt.
    description: ClassVar[str]

    #: Pydantic model whose JSON Schema describes the accepted arguments.
    #: The framework validates incoming ``args`` against this before calling
    #: ``execute``.
    Args: ClassVar[type[BaseModel]]

    @abc.abstractmethod
    async def execute(
        self,
        ctx: ToolContext,
        args: dict[str, Any],
    ) -> ToolResult:
        """Execute the tool.

        Parameters
        ----------
        ctx:
            Rich per-call context injected by the executor.  Contains the
            agent context, sandbox references, metadata, and state bag.
        args:
            Validated argument dict (values already parsed against
            ``self.Args``).

        Returns
        -------
        ToolResult
            Structured result.  Always use ``ToolResult.ok(...)`` or
            ``ToolResult.error(...)`` constructors.
        """
        ...

    def parameters_schema(self) -> dict[str, Any]:
        """Return the JSON Schema for this tool's parameters.

        Default implementation derives the schema from ``self.Args``.
        Override to provide a hand-crafted schema.
        """
        return self.Args.model_json_schema()
```

### 4.2 ToolContext

```python
# agenthicc/tools/context.py
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from agenthicc.agent.context import AgentContext
from agenthicc.sandbox.workspace import WorkspaceView
from agenthicc.sandbox.network import NetworkGuard


@dataclass(slots=True)
class ExecutionContext:
    """Low-level execution handles injected into every tool call."""

    workspace: WorkspaceView
    """Filesystem sandbox scoped to the agent's workspace root."""

    network: NetworkGuard
    """Network sandbox enforcing the agent's allow-list."""

    timeout_seconds: float = 30.0
    """Wall-clock budget for this specific call."""


@dataclass(slots=True)
class ToolCallContext:
    """Full per-call context available to tools and hooks.

    This mirrors ``lauren_ai._tools._executor.ToolCallContext`` and extends
    it with sandbox handles.
    """

    # ---- Identity --------------------------------------------------------
    tool_use_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    """Unique ID for this invocation, matching the LLM's tool_use block id."""

    tool_name: str = ""
    """Stable tool identifier, matches ``Tool.name``."""

    # ---- Call inputs -----------------------------------------------------
    tool_input: dict[str, Any] = field(default_factory=dict)
    """Raw argument dict before Pydantic validation."""

    request: Any = None
    """Original LLM request object that triggered this call (if available)."""

    # ---- Agent state -----------------------------------------------------
    agent_context: AgentContext | None = None
    """The parent agent's full context (memory, config, identity)."""

    turn: int = 0
    """Zero-based turn index within the current agent run."""

    # ---- Sandbox ---------------------------------------------------------
    execution_context: ExecutionContext | None = None
    """Sandbox handles; None when sandboxing is disabled (test mode)."""

    # ---- Extension bags --------------------------------------------------
    metadata: dict[str, Any] = field(default_factory=dict)
    """Freeform metadata for hooks to attach observability data."""

    state: dict[str, Any] = field(default_factory=dict)
    """Mutable state bag for hooks to pass data between stages."""


# Expose the lauren_ai alias so existing code continues to import from here.
ToolContext = ToolCallContext
```

### 4.3 ToolResult

```python
# agenthicc/tools/result.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolResult:
    """Unified result type for all tool executions.

    Always construct via the class-method constructors ``ok`` and ``error``.
    """

    content: Any
    """Successful output.  Must be JSON-serialisable."""

    error: str | None = None
    """Error message.  Set iff ``is_error`` is True."""

    is_error: bool = False
    """True when the call failed; False on success."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Hook-attached metadata (duration, cache info, etc.)."""

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def ok(cls, content: Any, **metadata: Any) -> "ToolResult":
        """Return a successful result."""
        return cls(content=content, metadata=dict(metadata))

    @classmethod
    def error(cls, message: str, **metadata: Any) -> "ToolResult":
        """Return an error result."""
        return cls(
            content=None,
            error=message,
            is_error=True,
            metadata=dict(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "ok": not self.is_error,
            "content": self.content,
            "error": self.error,
            "metadata": self.metadata,
        }
```

### 4.4 ToolMeta

```python
# agenthicc/tools/meta.py
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agenthicc.tools.hooks import ToolHook


@dataclass
class ToolMeta:
    """Metadata attached to a registered tool.

    Maps to ``lauren_ai._tools._executor.ToolMeta``.
    """

    name: str
    """Must match ``Tool.name``."""

    requires_confirmation: bool = False
    """If True, a human-in-the-loop hook will be injected automatically."""

    cache_ttl: float | None = None
    """Seconds to cache successful results; None disables caching."""

    cache_key_fn: Callable[[dict[str, Any]], str] | None = None
    """Optional function to derive the cache key from raw args."""

    timeout_seconds: float = 30.0
    """Execution timeout enforced via ``asyncio.wait_for``."""

    # ---- Hook slots on ToolMeta (per-tool hooks) -------------------------
    pre_hook: ToolHook | None = None
    post_hook: ToolHook | None = None
    error_hook: ToolHook | None = None

    # ---- Resolved (merged global + per-tool) hooks ----------------------
    resolved_hooks: list[ToolHook] = field(default_factory=list)
    """Populated by the registry; do not set manually."""

    # ---- Context injection config ---------------------------------------
    reads_context: bool = False
    """If True the executor will inject a populated ToolContext."""

    context_param_name: str = "ctx"
    """Name of the parameter that receives the ToolContext."""

    is_async: bool = True
    """True when ``Tool.execute`` is a coroutine function."""
```

### 4.5 LifecycleHook ABC

```python
# agenthicc/hooks/base.py
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import Any


class RecoveryAction(enum.Enum):
    """Actions an ``on_error`` hook can suggest to the executor."""

    RETRY = "retry"
    """Re-execute the tool/step with the same arguments."""

    FALLBACK = "fallback"
    """Use a pre-registered fallback tool/step instead."""

    ESCALATE = "escalate"
    """Bubble the error up to the calling workflow or human."""

    SKIP = "skip"
    """Suppress the error and continue as if the call returned empty."""


@dataclass(slots=True)
class Rejection:
    """Returned by ``on_before`` to prevent execution."""

    reason: str
    """Human-readable explanation surfaced in audit logs and error results."""

    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class RecoveryDirective:
    """Returned by ``on_error`` to guide the executor's recovery logic."""

    action: RecoveryAction
    fallback_tool: str | None = None
    """Only meaningful when ``action == RecoveryAction.FALLBACK``."""

    retry_delay_seconds: float = 0.0
    """Only meaningful when ``action == RecoveryAction.RETRY``."""

    metadata: dict[str, Any] | None = None


class LifecycleHook(abc.ABC):
    """ABC for all lifecycle hooks regardless of entity level.

    Concrete hooks inherit from the entity-specific subclass (e.g.
    ``ToolHook``, ``AgentLifecycleHook``) rather than this class directly.
    """

    priority: int = 0
    """Lower value = earlier in ``on_before``, later in ``on_after``."""

    @abc.abstractmethod
    async def on_before(self, ctx: Any) -> Rejection | None:
        """Called before the entity executes.

        Return a ``Rejection`` to abort; return ``None`` to allow.
        """
        ...

    @abc.abstractmethod
    async def on_after(self, result: Any, ctx: Any) -> None:
        """Called after the entity executes successfully."""
        ...

    @abc.abstractmethod
    async def on_error(
        self,
        exc: BaseException,
        ctx: Any,
    ) -> RecoveryDirective | None:
        """Called when the entity raises an exception.

        Return a ``RecoveryDirective`` to suggest recovery; return ``None``
        to let the executor use its default handling.
        """
        ...
```

### 4.6 ToolHook Protocol

```python
# agenthicc/hooks/tool_hook.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agenthicc.tools.context import ToolCallContext
from agenthicc.tools.result import ToolResult


@dataclass(slots=True)
class BeforeToolHookDecision:
    """Decision returned by a hook's ``before_tool_call`` method.

    Maps to ``lauren_ai._tools._executor.BeforeToolHookDecision``.
    """

    _aborted: bool = False
    _abort_result: ToolResult | None = None
    _modified_input: dict[str, Any] | None = None

    @classmethod
    def allow(
        cls,
        modified_input: dict[str, Any] | None = None,
    ) -> "BeforeToolHookDecision":
        return cls(_modified_input=modified_input)

    @classmethod
    def abort(cls, result: ToolResult) -> "BeforeToolHookDecision":
        return cls(_aborted=True, _abort_result=result)

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def abort_result(self) -> ToolResult | None:
        return self._abort_result

    @property
    def modified_input(self) -> dict[str, Any] | None:
        return self._modified_input


@dataclass(slots=True)
class AfterToolHookDecision:
    """Decision returned by a hook's ``after_tool_call`` method.

    Maps to ``lauren_ai._tools._executor.AfterToolHookDecision``.
    """

    _replacement: ToolResult | None = None

    @classmethod
    def passthrough(cls) -> "AfterToolHookDecision":
        return cls()

    @classmethod
    def replace(cls, result: ToolResult) -> "AfterToolHookDecision":
        return cls(_replacement=result)

    @property
    def replacement(self) -> ToolResult | None:
        return self._replacement


@dataclass(slots=True)
class ErrorToolHookDecision:
    """Decision returned by a hook's ``on_tool_error`` method.

    Maps to ``lauren_ai._tools._executor.ErrorToolHookDecision``.
    """

    _suppressed: bool = False
    _fallback: ToolResult | None = None

    @classmethod
    def propagate(cls) -> "ErrorToolHookDecision":
        return cls()

    @classmethod
    def suppress(cls, fallback: ToolResult) -> "ErrorToolHookDecision":
        return cls(_suppressed=True, _fallback=fallback)

    @property
    def suppressed(self) -> bool:
        return self._suppressed

    @property
    def fallback(self) -> ToolResult | None:
        return self._fallback


@runtime_checkable
class ToolHook(Protocol):
    """Structural protocol every tool-level hook must satisfy.

    Maps to ``lauren_ai._tools._executor.ToolHook``.
    """

    async def before_tool_call(
        self,
        ctx: ToolCallContext,
    ) -> BeforeToolHookDecision:
        """Called before the tool executes; may abort or modify input."""
        ...

    async def after_tool_call(
        self,
        result: ToolResult,
        ctx: ToolCallContext,
    ) -> AfterToolHookDecision:
        """Called after successful execution; may replace the result."""
        ...

    async def on_tool_error(
        self,
        exc: BaseException,
        ctx: ToolCallContext,
    ) -> ErrorToolHookDecision:
        """Called when ``tool.execute`` raises; may suppress the error."""
        ...
```

### 4.7 WorkspaceView (Filesystem Sandbox)

```python
# agenthicc/sandbox/workspace.py
from __future__ import annotations

import os
from pathlib import Path


class WorkspaceEscapeError(PermissionError):
    """Raised when a tool attempts to access a path outside the workspace."""


class WorkspaceView:
    """Filesystem view that enforces a path-prefix boundary.

    All path arguments are resolved to absolute paths and checked against
    ``root`` before any I/O is allowed.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str | Path) -> Path:
        """Resolve *path* relative to the workspace root and check prefix.

        Raises
        ------
        WorkspaceEscapeError
            If the resolved path is outside the workspace root.
        """
        candidate = (self._root / path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise WorkspaceEscapeError(
                f"Path escape attempt: {candidate!r} is outside "
                f"workspace root {self._root!r}"
            ) from exc
        return candidate

    def open(self, path: str | Path, mode: str = "r", **kwargs):
        """Open a file inside the workspace."""
        safe_path = self.resolve(path)
        return safe_path.open(mode, **kwargs)

    def listdir(self, path: str | Path = ".") -> list[str]:
        safe_path = self.resolve(path)
        return os.listdir(safe_path)
```

### 4.8 NetworkGuard

```python
# agenthicc/sandbox/network.py
from __future__ import annotations

import re
from urllib.parse import urlparse


class NetworkBlockedError(PermissionError):
    """Raised when a tool attempts to contact a blocked host."""


class NetworkGuard:
    """Enforces an allow-list of hosts/patterns for outbound network calls.

    Intended to be injected into tools via ``ExecutionContext.network``.
    Tools that make HTTP calls should call ``guard.check(url)`` before
    initiating the request, or use the provided ``httpx`` event hook.
    """

    def __init__(
        self,
        allow_list: list[str],
        *,
        allow_localhost: bool = False,
    ) -> None:
        self._patterns = [re.compile(p) for p in allow_list]
        self._allow_localhost = allow_localhost

    def check(self, url: str) -> None:
        """Assert that *url* is on the allow-list.

        Raises
        ------
        NetworkBlockedError
            If the host is not covered by any allow-list pattern.
        """
        host = urlparse(url).netloc.split(":")[0]
        if self._allow_localhost and host in ("localhost", "127.0.0.1", "::1"):
            return
        if not any(p.fullmatch(host) for p in self._patterns):
            raise NetworkBlockedError(
                f"Outbound request to {host!r} is not on the network allow-list."
            )
```

### 4.9 ToolRegistration and AppState

```python
# agenthicc/state.py (excerpt)
from __future__ import annotations

from dataclasses import dataclass, field

from agenthicc.tools.base import Tool
from agenthicc.tools.meta import ToolMeta
from agenthicc.hooks.tool_hook import ToolHook


@dataclass
class ToolRegistration:
    """A tool paired with its metadata, stored in AppState."""

    tool: Tool
    meta: ToolMeta


@dataclass
class AppState:
    """Singleton application state shared across the process."""

    tools: dict[str, ToolRegistration] = field(default_factory=dict)
    """Map from tool name to registration."""

    global_hooks: list[ToolHook] = field(default_factory=list)
    """Hooks applied to every tool call, outermost-first."""

    def register_tool(
        self,
        tool: Tool,
        meta: ToolMeta | None = None,
    ) -> None:
        if meta is None:
            meta = ToolMeta(name=tool.name)
        self.tools[tool.name] = ToolRegistration(tool=tool, meta=meta)

    def get_tool(self, name: str) -> ToolRegistration:
        try:
            return self.tools[name]
        except KeyError:
            raise LookupError(f"No tool registered with name {name!r}") from None
```

### 4.10 ToolExecutor

```python
# agenthicc/tools/executor.py  (also maps to lauren_ai._tools._executor)
from __future__ import annotations

import asyncio
from typing import Sequence

from agenthicc.hooks.tool_hook import (
    AfterToolHookDecision,
    BeforeToolHookDecision,
    ErrorToolHookDecision,
    ToolHook,
)
from agenthicc.tools.context import ToolCallContext
from agenthicc.tools.meta import ToolMeta
from agenthicc.tools.result import ToolResult
from agenthicc.state import AppState


def use_hooks(*hooks: ToolHook):
    """Decorator that attaches per-tool hooks to a ``Tool`` subclass."""

    def decorator(cls):
        existing: list[ToolHook] = getattr(cls, "_per_tool_hooks", [])
        cls._per_tool_hooks = list(hooks) + existing
        return cls

    return decorator


class ToolExecutor:
    """Dispatches tool calls with full hook lifecycle support.

    Maps to ``lauren_ai._tools._executor.ToolExecutor``.
    """

    def __init__(
        self,
        app_state: AppState,
        *,
        parallel_tool_calls: bool = True,
        tool_call_budget: int = 8,
    ) -> None:
        self._state = app_state
        self._parallel = parallel_tool_calls
        self._budget = tool_call_budget

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_batch(
        self,
        calls: Sequence[tuple[str, dict]],
        base_ctx: ToolCallContext,
    ) -> list[ToolResult]:
        """Execute a batch of tool calls, optionally in parallel."""
        capped = list(calls[: self._budget])
        if self._parallel and len(capped) > 1:
            return list(
                await asyncio.gather(
                    *[self._run_one(name, args, base_ctx) for name, args in capped]
                )
            )
        results = []
        for name, args in capped:
            results.append(await self._run_one(name, args, base_ctx))
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_one(
        self,
        tool_name: str,
        raw_args: dict,
        base_ctx: ToolCallContext,
    ) -> ToolResult:
        reg = self._state.get_tool(tool_name)
        ctx = ToolCallContext(
            tool_name=tool_name,
            tool_input=raw_args,
            agent_context=base_ctx.agent_context,
            turn=base_ctx.turn,
            request=base_ctx.request,
            execution_context=base_ctx.execution_context,
        )
        all_hooks: list[ToolHook] = (
            self._state.global_hooks
            + getattr(reg.tool.__class__, "_per_tool_hooks", [])
        )

        # 1. on_before (global then per-tool)
        before_result = await self._run_before_hooks(all_hooks, ctx)
        if before_result is not None and before_result.aborted:
            return before_result.abort_result  # type: ignore[return-value]
        if before_result and before_result.modified_input:
            ctx.tool_input = before_result.modified_input

        # 2. Execute with sandbox timeout
        try:
            timeout = reg.meta.timeout_seconds
            result = await asyncio.wait_for(
                reg.tool.execute(ctx, ctx.tool_input),
                timeout=timeout,
            )
        except BaseException as exc:
            return await self._run_error_hooks(all_hooks, exc, ctx)

        # 3. on_after (per-tool then global, reversed)
        result = await self._run_after_hooks(list(reversed(all_hooks)), result, ctx)
        return result

    async def _run_before_hooks(
        self,
        hooks: list[ToolHook],
        ctx: ToolCallContext,
    ) -> BeforeToolHookDecision | None:
        decisions = await asyncio.gather(
            *[h.before_tool_call(ctx) for h in hooks]
        )
        for d in decisions:
            if d.aborted:
                return d
        # Apply last modified_input that is not None
        last_mod = None
        for d in decisions:
            if d.modified_input is not None:
                last_mod = d.modified_input
        if last_mod is not None:
            return BeforeToolHookDecision.allow(modified_input=last_mod)
        return BeforeToolHookDecision.allow()

    async def _run_after_hooks(
        self,
        hooks: list[ToolHook],
        result: ToolResult,
        ctx: ToolCallContext,
    ) -> ToolResult:
        for hook in hooks:
            decision: AfterToolHookDecision = await hook.after_tool_call(result, ctx)
            if decision.replacement is not None:
                result = decision.replacement
        return result

    async def _run_error_hooks(
        self,
        hooks: list[ToolHook],
        exc: BaseException,
        ctx: ToolCallContext,
    ) -> ToolResult:
        for hook in hooks:
            decision: ErrorToolHookDecision = await hook.on_tool_error(exc, ctx)
            if decision.suppressed and decision.fallback is not None:
                return decision.fallback
        return ToolResult.error(str(exc))
```

---

## 5. Implementation Plan

Tasks are ordered by dependency.  Each task references the lauren-ai types it
builds on or introduces.

### Phase 1 -- Core Types (Sprint 1)

| Task | Description | Lauren-AI Reference |
|------|-------------|---------------------|
| T1.1 | Implement `Tool` ABC in `agenthicc/tools/base.py` | -- |
| T1.2 | Implement `ToolResult` with `ok()`/`error()` / `is_error` / `to_dict()` | `ToolResult` in `lauren_ai._tools._executor` |
| T1.3 | Implement `ToolCallContext` dataclass with all fields | `ToolCallContext` in `lauren_ai._tools._executor` |
| T1.4 | Implement `ToolMeta` dataclass including all hook slots | `ToolMeta` in `lauren_ai._tools._executor` |
| T1.5 | Implement `ToolRegistration` and extend `AppState` | -- |

### Phase 2 -- Hook Protocol (Sprint 1)

| Task | Description | Lauren-AI Reference |
|------|-------------|---------------------|
| T2.1 | Implement `BeforeToolHookDecision` with `allow()`/`abort()` | `BeforeToolHookDecision` |
| T2.2 | Implement `AfterToolHookDecision` with `passthrough()`/`replace()` | `AfterToolHookDecision` |
| T2.3 | Implement `ErrorToolHookDecision` with `propagate()`/`suppress()` | `ErrorToolHookDecision` |
| T2.4 | Define `ToolHook` Protocol (runtime-checkable) | `ToolHook` protocol |
| T2.5 | Implement `RecoveryAction` enum and `LifecycleHook` ABC | -- |
| T2.6 | Implement `@use_hooks()` decorator | `@use_hooks()` in executor |

### Phase 3 -- Sandbox (Sprint 2)

| Task | Description | Notes |
|------|-------------|-------|
| T3.1 | Implement `WorkspaceView` with `resolve()`/`open()`/`listdir()` | Raises `WorkspaceEscapeError` |
| T3.2 | Implement `NetworkGuard` with pattern allow-list | Raises `NetworkBlockedError` |
| T3.3 | Implement `ExecutionContext` and wire into `ToolCallContext` | |
| T3.4 | Write sandbox unit tests (escape attempts, blocked hosts) | See section 6.3 |

### Phase 4 -- Executor (Sprint 2)

| Task | Description | Lauren-AI Reference |
|------|-------------|---------------------|
| T4.1 | Implement `ToolExecutor._run_one` sequential path | `ToolExecutor` in `lauren_ai._tools._executor` |
| T4.2 | Add `asyncio.gather` parallel batch path with budget cap | `parallel_tool_calls` config |
| T4.3 | Wire `asyncio.wait_for` timeout from `ToolMeta.timeout_seconds` | |
| T4.4 | Integrate before/after/error hook pipeline in `_run_one` | |
| T4.5 | Implement global hook list on `AppState` / `AgentRunnerBase.global_hooks` | `global_hooks` on `AgentRunnerBase` |

### Phase 5 -- Hook Registration (Sprint 3)

| Task | Description | Notes |
|------|-------------|-------|
| T5.1 | Parse `[tools.hooks]` TOML config section and instantiate hook classes | See section 7 |
| T5.2 | Implement `hook_register` dynamic tool (registers hooks at runtime) | |
| T5.3 | Implement `importlib.metadata` entry-point discovery for `agenthicc.hooks` | Plugin support |
| T5.4 | Document hook priority ordering and write ordering tests | See section 6.1 |

### Phase 6 -- Entity-Level Hooks (Sprint 3)

| Task | Description |
|------|-------------|
| T6.1 | Define `IntentLifecycleHook` ABC and wire into intent dispatch |
| T6.2 | Define `WorkflowLifecycleHook` ABC and wire into workflow runner |
| T6.3 | Define `NodeLifecycleHook` / `TaskLifecycleHook` ABCs |
| T6.4 | Define `AgentLifecycleHook` ABC and wire into `AgentRunnerBase` |

---

## 6. Tests

### 6.1 Unit Tests: Hook Ordering and Rejection

```python
# tests/unit/test_hook_ordering.py
"""
Unit tests verifying:
  - Global hooks wrap per-tool hooks (outer-first for on_before,
    reverse for on_after).
  - First Rejection in on_before short-circuits remaining hooks.
  - Error suppression via ErrorToolHookDecision.suppress().
  - Input mutation via BeforeToolHookDecision.allow(modified_input=...).
"""
from __future__ import annotations

import asyncio

import pytest

from agenthicc.hooks.tool_hook import (
    AfterToolHookDecision,
    BeforeToolHookDecision,
    ErrorToolHookDecision,
)
from agenthicc.tools.base import Tool
from agenthicc.tools.context import ToolCallContext
from agenthicc.tools.executor import ToolExecutor
from agenthicc.tools.meta import ToolMeta
from agenthicc.tools.result import ToolResult
from agenthicc.state import AppState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _PassthroughHook:
    """Hook that records calls and passes everything through."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.before_calls: list[str] = []
        self.after_calls: list[str] = []

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        self.before_calls.append(ctx.tool_name)
        return BeforeToolHookDecision.allow()

    async def after_tool_call(
        self, result: ToolResult, ctx: ToolCallContext
    ) -> AfterToolHookDecision:
        self.after_calls.append(ctx.tool_name)
        return AfterToolHookDecision.passthrough()

    async def on_tool_error(
        self, exc: BaseException, ctx: ToolCallContext
    ) -> ErrorToolHookDecision:
        return ErrorToolHookDecision.propagate()


class _RejectingHook:
    """Hook that always rejects."""

    def __init__(self, reason: str = "blocked") -> None:
        self.reason = reason
        self.called = False

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        self.called = True
        return BeforeToolHookDecision.abort(
            ToolResult.error(f"Rejected: {self.reason}")
        )

    async def after_tool_call(
        self, result: ToolResult, ctx: ToolCallContext
    ) -> AfterToolHookDecision:
        return AfterToolHookDecision.passthrough()

    async def on_tool_error(
        self, exc: BaseException, ctx: ToolCallContext
    ) -> ErrorToolHookDecision:
        return ErrorToolHookDecision.propagate()


class _EchoTool(Tool):
    name = "echo"
    description = "Returns its input"

    class Args:
        @staticmethod
        def model_json_schema():
            return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, ctx: ToolCallContext, args: dict) -> ToolResult:
        return ToolResult.ok(args.get("text", ""))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app_state():
    state = AppState()
    state.register_tool(_EchoTool(), ToolMeta(name="echo"))
    return state


@pytest.fixture(autouse=True)
def clear_per_tool_hooks():
    """Ensure per-tool hooks don't leak between tests."""
    _EchoTool._per_tool_hooks = []
    yield
    _EchoTool._per_tool_hooks = []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_global_before_hook_runs_before_per_tool_hook(app_state):
    """Global hook on_before fires before per-tool hook on_before."""
    order: list[str] = []

    class GlobalHook(_PassthroughHook):
        async def before_tool_call(self, ctx):
            order.append("global_before")
            return await super().before_tool_call(ctx)

    class PerToolHook(_PassthroughHook):
        async def before_tool_call(self, ctx):
            order.append("per_tool_before")
            return await super().before_tool_call(ctx)

    app_state.global_hooks = [GlobalHook("global")]
    _EchoTool._per_tool_hooks = [PerToolHook("per_tool")]

    executor = ToolExecutor(app_state, parallel_tool_calls=False)
    ctx = ToolCallContext()
    asyncio.get_event_loop().run_until_complete(
        executor._run_one("echo", {"text": "hi"}, ctx)
    )

    assert order.index("global_before") < order.index("per_tool_before"), (
        f"Expected global_before before per_tool_before, got: {order}"
    )


def test_global_after_hook_runs_after_per_tool_hook(app_state):
    """Global hook on_after fires AFTER per-tool hook on_after (reverse order)."""
    order: list[str] = []

    class GlobalHook(_PassthroughHook):
        async def after_tool_call(self, result, ctx):
            order.append("global_after")
            return AfterToolHookDecision.passthrough()

    class PerToolHook(_PassthroughHook):
        async def after_tool_call(self, result, ctx):
            order.append("per_tool_after")
            return AfterToolHookDecision.passthrough()

    app_state.global_hooks = [GlobalHook("global")]
    _EchoTool._per_tool_hooks = [PerToolHook("per_tool")]

    executor = ToolExecutor(app_state, parallel_tool_calls=False)
    ctx = ToolCallContext()
    asyncio.get_event_loop().run_until_complete(
        executor._run_one("echo", {"text": "hi"}, ctx)
    )

    # on_after is reversed: per_tool fires first, then global
    assert order.index("per_tool_after") < order.index("global_after"), (
        f"Expected per_tool_after before global_after, got: {order}"
    )


def test_first_rejection_short_circuits(app_state):
    """When global hook rejects, per-tool hook must NOT be called."""
    rejecting = _RejectingHook("rate-limited")
    per_tool_spy = _PassthroughHook("spy")

    app_state.global_hooks = [rejecting]
    _EchoTool._per_tool_hooks = [per_tool_spy]

    executor = ToolExecutor(app_state, parallel_tool_calls=False)
    ctx = ToolCallContext()
    result = asyncio.get_event_loop().run_until_complete(
        executor._run_one("echo", {"text": "hi"}, ctx)
    )

    assert result.is_error
    assert "rate-limited" in (result.error or "")
    # The per-tool hook's before_calls list must be empty
    assert per_tool_spy.before_calls == [], (
        "Per-tool hook should not have fired after rejection"
    )


def test_error_hook_suppression(app_state):
    """on_tool_error hook can suppress an exception and return a fallback."""

    class BrokenTool(Tool):
        name = "broken"
        description = "Always raises"

        class Args:
            @staticmethod
            def model_json_schema():
                return {}

        async def execute(self, ctx, args):
            raise ValueError("boom")

    class SuppressingHook:
        async def before_tool_call(self, ctx):
            return BeforeToolHookDecision.allow()

        async def after_tool_call(self, result, ctx):
            return AfterToolHookDecision.passthrough()

        async def on_tool_error(self, exc, ctx):
            return ErrorToolHookDecision.suppress(
                ToolResult.ok("fallback_value")
            )

    app_state.register_tool(BrokenTool(), ToolMeta(name="broken"))
    app_state.global_hooks = [SuppressingHook()]

    executor = ToolExecutor(app_state, parallel_tool_calls=False)
    ctx = ToolCallContext()
    result = asyncio.get_event_loop().run_until_complete(
        executor._run_one("broken", {}, ctx)
    )

    assert not result.is_error
    assert result.content == "fallback_value"


def test_hook_can_modify_input(app_state):
    """Before hook can inject/override args via modified_input."""

    class InputMutatingHook:
        async def before_tool_call(self, ctx):
            return BeforeToolHookDecision.allow(
                modified_input={**ctx.tool_input, "text": "mutated"}
            )

        async def after_tool_call(self, result, ctx):
            return AfterToolHookDecision.passthrough()

        async def on_tool_error(self, exc, ctx):
            return ErrorToolHookDecision.propagate()

    app_state.global_hooks = [InputMutatingHook()]

    executor = ToolExecutor(app_state, parallel_tool_calls=False)
    ctx = ToolCallContext()
    result = asyncio.get_event_loop().run_until_complete(
        executor._run_one("echo", {"text": "original"}, ctx)
    )

    assert result.content == "mutated"


def test_after_hook_can_replace_result(app_state):
    """After hook with replace() overrides the tool's original result."""

    class ResultReplacingHook:
        async def before_tool_call(self, ctx):
            return BeforeToolHookDecision.allow()

        async def after_tool_call(self, result, ctx):
            return AfterToolHookDecision.replace(ToolResult.ok("replaced"))

        async def on_tool_error(self, exc, ctx):
            return ErrorToolHookDecision.propagate()

    app_state.global_hooks = [ResultReplacingHook()]

    executor = ToolExecutor(app_state, parallel_tool_calls=False)
    ctx = ToolCallContext()
    result = asyncio.get_event_loop().run_until_complete(
        executor._run_one("echo", {"text": "original"}, ctx)
    )

    assert result.content == "replaced"
```

### 6.2 Unit Tests: Parallel Execution and Budget Cap

```python
# tests/unit/test_parallel_execution.py
"""
Tests for asyncio.gather fan-out and tool_call_budget enforcement.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from agenthicc.tools.base import Tool
from agenthicc.tools.context import ToolCallContext
from agenthicc.tools.executor import ToolExecutor
from agenthicc.tools.meta import ToolMeta
from agenthicc.tools.result import ToolResult
from agenthicc.state import AppState


class _SlowTool(Tool):
    name = "slow"
    description = "Sleeps for 0.1 s"

    class Args:
        @staticmethod
        def model_json_schema():
            return {}

    async def execute(self, ctx, args):
        await asyncio.sleep(0.1)
        return ToolResult.ok("done")


@pytest.fixture
def state_with_slow():
    state = AppState()
    state.register_tool(_SlowTool(), ToolMeta(name="slow"))
    return state


def test_parallel_execution_is_faster_than_sequential(state_with_slow):
    """4 slow tools in parallel should finish in ~0.1 s, not ~0.4 s."""
    executor = ToolExecutor(
        state_with_slow, parallel_tool_calls=True, tool_call_budget=8
    )
    calls = [("slow", {})] * 4
    ctx = ToolCallContext()

    start = time.monotonic()
    results = asyncio.get_event_loop().run_until_complete(
        executor.execute_batch(calls, ctx)
    )
    elapsed = time.monotonic() - start

    assert len(results) == 4
    assert all(not r.is_error for r in results)
    # Should finish well under 0.3 s if truly parallel
    assert elapsed < 0.3, f"Parallel execution took {elapsed:.3f}s, expected < 0.3s"


def test_sequential_execution_respects_order(state_with_slow):
    """With parallel=False, calls are sequential and take cumulative time."""
    executor = ToolExecutor(
        state_with_slow, parallel_tool_calls=False, tool_call_budget=8
    )
    calls = [("slow", {})] * 3
    ctx = ToolCallContext()

    start = time.monotonic()
    asyncio.get_event_loop().run_until_complete(executor.execute_batch(calls, ctx))
    elapsed = time.monotonic() - start

    # Should take at least 0.3 s (3 x 0.1 s)
    assert elapsed >= 0.29, f"Sequential execution took {elapsed:.3f}s, expected >= 0.29s"


def test_budget_cap_limits_calls(state_with_slow):
    """Only the first tool_call_budget calls are executed."""
    executor = ToolExecutor(
        state_with_slow, parallel_tool_calls=True, tool_call_budget=2
    )
    calls = [("slow", {})] * 5
    ctx = ToolCallContext()

    results = asyncio.get_event_loop().run_until_complete(
        executor.execute_batch(calls, ctx)
    )

    assert len(results) == 2, f"Expected 2 results with budget=2, got {len(results)}"


def test_single_call_does_not_use_gather(state_with_slow):
    """A batch of 1 must not use asyncio.gather (no overhead)."""
    executor = ToolExecutor(
        state_with_slow, parallel_tool_calls=True, tool_call_budget=8
    )
    ctx = ToolCallContext()

    results = asyncio.get_event_loop().run_until_complete(
        executor.execute_batch([("slow", {})], ctx)
    )
    assert len(results) == 1
    assert not results[0].is_error


def test_timeout_raises_error():
    """Tool that exceeds timeout_seconds returns ToolResult.error."""

    class InfiniteLoopTool(Tool):
        name = "infinite"
        description = "Never returns"

        class Args:
            @staticmethod
            def model_json_schema():
                return {}

        async def execute(self, ctx, args):
            await asyncio.sleep(9999)
            return ToolResult.ok("never")

    state = AppState()
    state.register_tool(
        InfiniteLoopTool(),
        ToolMeta(name="infinite", timeout_seconds=0.05),
    )
    executor = ToolExecutor(state, parallel_tool_calls=False)
    ctx = ToolCallContext()

    result = asyncio.get_event_loop().run_until_complete(
        executor._run_one("infinite", {}, ctx)
    )

    assert result.is_error
    # asyncio.TimeoutError message should be present
    assert result.error is not None
```

### 6.3 Integration Tests: Audit Hook and Sandbox Escape Prevention

```python
# tests/integration/test_audit_and_sandbox.py
"""
Integration tests:
  1. AuditHook logs every tool call name and result status.
  2. WorkspaceView prevents path traversal escapes.
  3. NetworkGuard blocks unlisted hosts.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from agenthicc.hooks.tool_hook import (
    AfterToolHookDecision,
    BeforeToolHookDecision,
    ErrorToolHookDecision,
)
from agenthicc.tools.base import Tool
from agenthicc.tools.context import ToolCallContext
from agenthicc.tools.executor import ToolExecutor
from agenthicc.tools.meta import ToolMeta
from agenthicc.tools.result import ToolResult
from agenthicc.state import AppState
from agenthicc.sandbox.workspace import WorkspaceView, WorkspaceEscapeError
from agenthicc.sandbox.network import NetworkGuard, NetworkBlockedError


# ---------------------------------------------------------------------------
# Audit hook integration
# ---------------------------------------------------------------------------

class AuditHook:
    """Audit hook that appends to an in-memory log."""

    def __init__(self) -> None:
        self.log: list[dict] = []

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        self.log.append({"event": "before", "tool": ctx.tool_name})
        return BeforeToolHookDecision.allow()

    async def after_tool_call(
        self, result: ToolResult, ctx: ToolCallContext
    ) -> AfterToolHookDecision:
        self.log.append(
            {
                "event": "after",
                "tool": ctx.tool_name,
                "ok": not result.is_error,
            }
        )
        return AfterToolHookDecision.passthrough()

    async def on_tool_error(
        self, exc: BaseException, ctx: ToolCallContext
    ) -> ErrorToolHookDecision:
        self.log.append(
            {"event": "error", "tool": ctx.tool_name, "exc": str(exc)}
        )
        return ErrorToolHookDecision.propagate()


class _PingTool(Tool):
    name = "ping"
    description = "Returns pong"

    class Args:
        @staticmethod
        def model_json_schema():
            return {}

    async def execute(self, ctx, args):
        return ToolResult.ok("pong")


class _ErrorTool(Tool):
    name = "err_tool"
    description = "Always errors"

    class Args:
        @staticmethod
        def model_json_schema():
            return {}

    async def execute(self, ctx, args):
        raise RuntimeError("deliberate error")


def test_audit_hook_logs_successful_calls():
    audit = AuditHook()
    state = AppState()
    state.register_tool(_PingTool(), ToolMeta(name="ping"))
    state.global_hooks = [audit]

    executor = ToolExecutor(state, parallel_tool_calls=False)
    ctx = ToolCallContext()
    asyncio.get_event_loop().run_until_complete(
        executor.execute_batch([("ping", {}), ("ping", {})], ctx)
    )

    # 2 calls x (1 before + 1 after) = 4 log entries
    assert len(audit.log) == 4
    assert audit.log[0] == {"event": "before", "tool": "ping"}
    assert audit.log[1] == {"event": "after", "tool": "ping", "ok": True}
    assert audit.log[2] == {"event": "before", "tool": "ping"}
    assert audit.log[3] == {"event": "after", "tool": "ping", "ok": True}


def test_audit_hook_logs_errors():
    audit = AuditHook()
    state = AppState()
    state.register_tool(_ErrorTool(), ToolMeta(name="err_tool"))
    state.global_hooks = [audit]

    executor = ToolExecutor(state, parallel_tool_calls=False)
    ctx = ToolCallContext()
    asyncio.get_event_loop().run_until_complete(
        executor._run_one("err_tool", {}, ctx)
    )

    error_events = [e for e in audit.log if e["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["tool"] == "err_tool"
    assert "deliberate error" in error_events[0]["exc"]


# ---------------------------------------------------------------------------
# WorkspaceView sandbox tests
# ---------------------------------------------------------------------------

def test_workspace_view_blocks_relative_traversal():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceView(tmpdir)
        with pytest.raises(WorkspaceEscapeError):
            workspace.resolve("../../etc/passwd")


def test_workspace_view_blocks_absolute_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceView(tmpdir)
        with pytest.raises(WorkspaceEscapeError):
            workspace.resolve("/etc/passwd")


def test_workspace_view_allows_nested_valid_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceView(tmpdir)
        safe = workspace.resolve("subdir/file.txt")
        assert str(safe).startswith(tmpdir)
        assert "etc" not in str(safe)


def test_workspace_view_blocks_symlink_escape():
    """Symlink inside workspace pointing outside must be blocked."""
    import os
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceView(tmpdir)
        # Create a symlink inside the workspace that points outside
        link_path = Path(tmpdir) / "evil_link"
        link_path.symlink_to("/tmp")
        with pytest.raises(WorkspaceEscapeError):
            workspace.resolve("evil_link/../../../etc/passwd")


def test_workspace_view_open_writes_inside_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = WorkspaceView(tmpdir)
        with workspace.open("hello.txt", "w") as f:
            f.write("hello world")
        assert (Path(tmpdir) / "hello.txt").read_text() == "hello world"


# ---------------------------------------------------------------------------
# NetworkGuard tests
# ---------------------------------------------------------------------------

def test_network_guard_blocks_unlisted_host():
    guard = NetworkGuard(allow_list=[r"api\.example\.com"])
    with pytest.raises(NetworkBlockedError):
        guard.check("https://evil.com/steal-data")


def test_network_guard_allows_exact_listed_host():
    guard = NetworkGuard(allow_list=[r"api\.example\.com"])
    guard.check("https://api.example.com/v1/data")  # must not raise


def test_network_guard_allows_localhost_when_enabled():
    guard = NetworkGuard(allow_list=[], allow_localhost=True)
    guard.check("http://localhost:8080/health")  # must not raise


def test_network_guard_blocks_localhost_by_default():
    guard = NetworkGuard(allow_list=[])
    with pytest.raises(NetworkBlockedError):
        guard.check("http://localhost:8080/health")


def test_network_guard_allows_wildcard_subdomain_pattern():
    guard = NetworkGuard(allow_list=[r".*\.anthropic\.com"])
    guard.check("https://api.anthropic.com/v1/messages")
    with pytest.raises(NetworkBlockedError):
        guard.check("https://api.openai.com/v1/chat")
```

### 6.4 End-to-End Test: Hook-Triggered Debugger Agent

```python
# tests/e2e/test_debugger_agent_on_error.py
"""
E2E test: when a tool raises, an on_error hook spawns a debugger agent
that captures the traceback and returns diagnostic metadata.

This test validates the contract between the hook system, the executor,
and the agent-spawning API.  The debugger agent is a minimal stub that
records what it received.
"""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass, field
from typing import Any

import pytest

from agenthicc.hooks.tool_hook import (
    AfterToolHookDecision,
    BeforeToolHookDecision,
    ErrorToolHookDecision,
)
from agenthicc.tools.base import Tool
from agenthicc.tools.context import ToolCallContext
from agenthicc.tools.executor import ToolExecutor
from agenthicc.tools.meta import ToolMeta
from agenthicc.tools.result import ToolResult
from agenthicc.state import AppState


# ---------------------------------------------------------------------------
# Stub debugger agent infrastructure
# ---------------------------------------------------------------------------

@dataclass
class DebuggerAgentRun:
    """Records a single debugger invocation."""

    tool_name: str
    traceback: str
    context_snapshot: dict[str, Any]
    diagnostic: str = "stub diagnosis: no issues found"
    recommendations: list[str] = field(default_factory=list)


_debugger_runs: list[DebuggerAgentRun] = []


async def _spawn_debugger_agent(
    tool_name: str,
    exc: BaseException,
    ctx: ToolCallContext,
) -> DebuggerAgentRun:
    """Simulate spawning a child debug agent to analyse a failure.

    In production this would call AgentRunnerBase.run() with a debug
    prompt constructed from the exception and context snapshot.
    """
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    run = DebuggerAgentRun(
        tool_name=tool_name,
        traceback=tb,
        context_snapshot={
            "tool_input": ctx.tool_input,
            "turn": ctx.turn,
            "tool_use_id": ctx.tool_use_id,
        },
        recommendations=["Check tool input validation", "Review timeout settings"],
    )
    _debugger_runs.append(run)
    return run


# ---------------------------------------------------------------------------
# Hook that spawns the debugger on error
# ---------------------------------------------------------------------------

class DebuggerSpawnHook:
    """on_error hook that spawns a debug agent and suppresses the error."""

    async def before_tool_call(self, ctx):
        return BeforeToolHookDecision.allow()

    async def after_tool_call(self, result, ctx):
        return AfterToolHookDecision.passthrough()

    async def on_tool_error(self, exc, ctx):
        run = await _spawn_debugger_agent(ctx.tool_name, exc, ctx)
        # Suppress the original error; return the diagnostic as the result
        return ErrorToolHookDecision.suppress(
            ToolResult.ok(
                {
                    "debugger_diagnosis": run.diagnostic,
                    "tool": run.tool_name,
                    "recommendations": run.recommendations,
                },
                debugger_invoked=True,
                original_error=str(exc),
            )
        )


# ---------------------------------------------------------------------------
# Tool under test
# ---------------------------------------------------------------------------

class _FragileTool(Tool):
    name = "fragile"
    description = "Raises if input contains 'fail'"

    class Args:
        @staticmethod
        def model_json_schema():
            return {"type": "object", "properties": {"msg": {"type": "string"}}}

    async def execute(self, ctx, args):
        if "fail" in args.get("msg", ""):
            raise RuntimeError(f"Deliberate failure: {args['msg']}")
        return ToolResult.ok(f"ok: {args.get('msg', '')}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_debugger_runs():
    _debugger_runs.clear()
    yield
    _debugger_runs.clear()


@pytest.fixture
def state_with_fragile():
    state = AppState()
    state.register_tool(_FragileTool(), ToolMeta(name="fragile"))
    state.global_hooks = [DebuggerSpawnHook()]
    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_debugger_agent_spawned_on_tool_failure(state_with_fragile):
    """When fragile tool fails, debugger hook suppresses error and returns diagnosis."""
    executor = ToolExecutor(state_with_fragile, parallel_tool_calls=False)
    ctx = ToolCallContext(turn=3)

    result = asyncio.get_event_loop().run_until_complete(
        executor._run_one("fragile", {"msg": "please fail now"}, ctx)
    )

    # The error should be suppressed; we get a diagnostic result instead
    assert not result.is_error, f"Expected suppressed error, got: {result.error}"
    assert result.metadata.get("debugger_invoked") is True
    assert result.content["tool"] == "fragile"
    assert "stub diagnosis" in result.content["debugger_diagnosis"]
    assert len(result.content["recommendations"]) > 0

    # The debugger agent must have been called exactly once
    assert len(_debugger_runs) == 1
    run = _debugger_runs[0]
    assert run.tool_name == "fragile"
    assert "Deliberate failure" in run.traceback
    assert run.context_snapshot["turn"] == 3


def test_no_debugger_on_success(state_with_fragile):
    """Successful tool execution does not trigger the debugger hook."""
    executor = ToolExecutor(state_with_fragile, parallel_tool_calls=False)
    ctx = ToolCallContext()

    result = asyncio.get_event_loop().run_until_complete(
        executor._run_one("fragile", {"msg": "all good"}, ctx)
    )

    assert not result.is_error
    assert result.content == "ok: all good"
    assert _debugger_runs == [], "Debugger should not have been invoked on success"


def test_multiple_failures_spawn_independent_debugger_runs(state_with_fragile):
    """Each failing tool call in a parallel batch spawns its own debugger run."""
    executor = ToolExecutor(
        state_with_fragile, parallel_tool_calls=True, tool_call_budget=4
    )
    ctx = ToolCallContext()

    calls = [
        ("fragile", {"msg": "fail-1"}),
        ("fragile", {"msg": "fail-2"}),
        ("fragile", {"msg": "success"}),
    ]
    results = asyncio.get_event_loop().run_until_complete(
        executor.execute_batch(calls, ctx)
    )

    # Two failures -> two debugger runs
    assert len(_debugger_runs) == 2

    # The success call should not have triggered debugger
    success_results = [r for r in results if not r.is_error and
                       not r.metadata.get("debugger_invoked")]
    assert len(success_results) == 1
    assert success_results[0].content == "ok: success"


def test_debugger_result_contains_original_error_message(state_with_fragile):
    """Suppressed result metadata must include the original exception text."""
    executor = ToolExecutor(state_with_fragile, parallel_tool_calls=False)
    ctx = ToolCallContext()

    result = asyncio.get_event_loop().run_until_complete(
        executor._run_one("fragile", {"msg": "fail: index out of range"}, ctx)
    )

    assert result.metadata.get("original_error") is not None
    assert "fail: index out of range" in result.metadata["original_error"]
```

---

## 7. Configuration Reference

All configuration is in TOML format under the `[tools]` top-level key.

```toml
# agenthicc.toml  --  Tool Execution Layer configuration

# ---------------------------------------------------------------------------
# Global executor settings
# ---------------------------------------------------------------------------
[tools]
parallel_tool_calls = true
tool_call_budget    = 8
default_timeout_s   = 30.0

# ---------------------------------------------------------------------------
# Per-tool overrides
# ---------------------------------------------------------------------------
[tools.overrides.read_file]
timeout_s            = 10.0
requires_confirmation = false
cache_ttl_s          = 60.0

[tools.overrides.execute_code]
timeout_s            = 120.0
requires_confirmation = true
cache_ttl_s          = 0      # never cache

[tools.overrides.web_search]
timeout_s            = 15.0
requires_confirmation = false

# ---------------------------------------------------------------------------
# Global hooks  (apply to EVERY tool call)
# ---------------------------------------------------------------------------
[tools.hooks.global]
# Each entry is a dotted Python import path to a class implementing ToolHook.
# Hooks are applied in the order listed (first = outermost).
hooks = [
  "agenthicc.hooks.audit:AuditHook",
  "agenthicc.hooks.rate_limit:RateLimitHook",
  "agenthicc.hooks.telemetry:OtelTracingHook",
]

# Constructor kwargs are passed as a table keyed by the hook's import path.
[tools.hooks.global."agenthicc.hooks.rate_limit:RateLimitHook"]
calls_per_minute = 60
burst            = 10

[tools.hooks.global."agenthicc.hooks.audit:AuditHook"]
log_level    = "INFO"
include_args = false   # PII guard: omit tool arguments from audit log

# ---------------------------------------------------------------------------
# Per-tool hooks  (applied inside global hooks)
# ---------------------------------------------------------------------------
[tools.hooks.per_tool.execute_code]
hooks = [
  "agenthicc.hooks.sandbox:SandboxEscapeGuardHook",
  "agenthicc.hooks.confirm:HumanConfirmationHook",
]

[tools.hooks.per_tool.web_search]
hooks = [
  "agenthicc.hooks.network:NetworkAllowListHook",
]

[tools.hooks.per_tool.web_search."agenthicc.hooks.network:NetworkAllowListHook"]
allow_list = ["api.openai.com", "duckduckgo.com", "scholar.google.com"]

# ---------------------------------------------------------------------------
# Sandbox settings
# ---------------------------------------------------------------------------
[tools.sandbox]
workspace_root       = "/agents/{agent_id}/workspace"
network_allow_list   = [
  "api\\.openai\\.com",
  "api\\.anthropic\\.com",
]
allow_localhost      = false

# ---------------------------------------------------------------------------
# Permission mapping: tool name -> required capability string
# ---------------------------------------------------------------------------
[tools.permissions]
read_file       = "fs:read"
write_file      = "fs:write"
execute_code    = "code:execute"
web_search      = "net:search"
send_email      = "comms:email"
hook_register   = "admin:hooks"

# ---------------------------------------------------------------------------
# Plugin hook discovery
# ---------------------------------------------------------------------------
[tools.plugins]
# Entry-point group scanned at startup.
# Third-party packages declare hooks via:
#   [project.entry-points."agenthicc.hooks"]
#   my_audit_hook = "my_package.hooks:MyAuditHook"
entry_point_group = "agenthicc.hooks"
disabled_plugins  = []   # list plugin entry-point names to skip
```

### 7.1 Hook Class Loading

At startup, `agenthicc.bootstrap` loads hooks in the following order:

1. **Entry-point discovery** -- scan `agenthicc.hooks` group via
   `importlib.metadata.entry_points(group="agenthicc.hooks")`.
2. **TOML global hooks** -- load `[tools.hooks.global]` list in declaration order.
3. **TOML per-tool hooks** -- load per-tool lists and attach to `ToolMeta.resolved_hooks`.
4. **Runtime `hook_register` calls** -- appended at the back of `AppState.global_hooks`.

### 7.2 `hook_register` Dynamic Tool

When an agent needs to register a hook without restarting the process it can call
the `hook_register` built-in tool:

```json
{
  "tool": "hook_register",
  "args": {
    "hook_class": "my_package.hooks:TemporaryAuditHook",
    "scope": "global",
    "priority": 10,
    "kwargs": {}
  }
}
```

The executor will import the class via `importlib.import_module`, instantiate it
with the provided `kwargs`, and insert it into `AppState.global_hooks` at the
position determined by `priority`.  This operation requires the `admin:hooks`
capability in the agent's permission set.

### 7.3 Plugin Entry-Point Declaration (pyproject.toml)

Third-party packages that provide hooks should declare them in `pyproject.toml`:

```toml
[project.entry-points."agenthicc.hooks"]
my_rate_limiter = "my_package.hooks:MyRateLimiterHook"
my_pii_scrubber = "my_package.hooks:PiiScrubberHook"
```

On install, these are discovered automatically by `importlib.metadata` and
instantiated with zero-argument constructors unless overridden in TOML config.

---

## 8. Open Questions

| # | Question | Owner | Status |
|---|----------|-------|--------|
| OQ1 | Should `asyncio.gather` for parallel hooks use `return_exceptions=True` to allow independent hooks to continue past a peer's exception, or should the first exception hard-cancel remaining hooks? The current design cancels remaining on first `Rejection` but not on hook implementation errors. | platform-team | Open |
| OQ2 | `RecoveryAction.RETRY` -- who owns the retry counter and back-off state? Should it live in `ToolCallContext.state`, in the executor, or in a separate `RetryPolicy` object on `ToolMeta`? | platform-team | Open |
| OQ3 | Does `WorkspaceView` need to handle symlinks that point outside the workspace root? `Path.resolve()` follows symlinks, so a symlink to `/etc/passwd` inside the workspace resolves correctly and is rejected. An explicit test for dangling symlinks should be added. | security-team | In-progress |
| OQ4 | Should `NetworkGuard` be injected at the `httpx` transport level (transparent for all tools using `httpx`) or should tools be required to call `guard.check()` explicitly? The transport approach is more robust but harder to test in isolation. | platform-team | Open |
| OQ5 | `ToolMeta.cache_ttl` -- should caching be implemented inside `ToolExecutor` (cross-call, shared cache) or inside each tool (private cache)? A shared executor-level cache risks leaking results between agents with different permission sets. | platform-team | Open |
| OQ6 | `hook_register` tool -- should it require a capability flag in the agent's permission set? Allowing arbitrary agents to register global hooks is a significant privilege escalation vector. Current proposal: require `admin:hooks` capability. | security-team | Open |
| OQ7 | How should `AgentLifecycleHook` and `WorkflowLifecycleHook` share context with `ToolHook`? Should there be a unified `HookContext` supertype, or keep contexts separate and pass references? | platform-team | Open |
| OQ8 | The `on_before` hooks for a given stage run via `asyncio.gather` (parallel). If hook A modifies `ctx.tool_input` and hook B reads it, hook B sees the unmodified version because both run concurrently. Is that acceptable, or do we need a sequential-within-stage option? | platform-team | Open |
| OQ9 | Plugin entry-point hooks loaded at startup are global for the entire process lifetime. Should there be a mechanism to unload/disable a plugin hook at runtime without restart? | platform-team | Open |
| OQ10 | `ToolResult.to_dict()` currently serialises `content` as-is. Should we enforce that `content` is `json.dumps`-able at construction time (better error messages) or lazily at serialisation time (allows non-serialisable types for in-memory use cases)? | platform-team | Open |

---

*End of PRD-04*
