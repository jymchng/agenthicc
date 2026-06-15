# agenthicc Tool System

## Two separate `Tool` abstractions — do not confuse them

**agenthicc's `Tool` ABC** (`tools/base.py`) is a dead-end for the actual agent integration. It defines `execute(args, context)` as an abstract method and `ToolResultEnvelope` as its return type. Nothing in the live agent path uses this class. It exists as an agenthicc-internal interface (used by the kernel/tool executor layer, separate from agent turns).

**Lauren-ai's `@tool()` decorator** is what every tool the agent actually calls uses — both built-ins (`tools/fs/agent_tools.py`) and project plugins (`.agenthicc/tools/project_tools.py`).

---

## The `@tool()` decorator (lauren-ai)

Decorating a function with `@tool()` does one thing at import time: it calls `_build_meta()` which inspects the function's signature and stores a `ToolMeta` struct on the callable at `fn.__lauren_ai_tool__`.

`ToolMeta` carries:

| Field | Type | Description |
|---|---|---|
| `name` | `str` | From function `__name__` |
| `description` | `str` | From docstring |
| `parameters` | `ToolSchema` | JSON Schema generated from type annotations |
| `is_async` | `bool` | `inspect.iscoroutinefunction(fn)` |
| `reads_context` | `bool` | True if any parameter is annotated `ToolContext` |
| `context_param_name` | `str \| None` | The actual parameter name (e.g. `ctx`) |
| `requires_confirmation` | `bool` | HITL gate — raises `ToolPendingApprovalSignal` before execution |
| `cache_ttl` | `int \| None` | Result cache lifetime in seconds |
| `pre_hook` | `Callable \| None` | Called before execution |
| `post_hook` | `Callable \| None` | Called after execution |
| `error_hook` | `Callable \| None` | Called on exception |
| `hook_classes` | `tuple[type, ...]` | From `@use_hooks()` |
| `resolved_hooks` | `tuple[Any, ...]` | Populated post-DI |

**Important:** `@tool()` must always be called with parentheses. `@tool` without parentheses raises `DecoratorUsageError`.

**Important:** Do not use `from __future__ import annotations` in files that define `@tool()` functions. The decorator inspects real type annotations at decoration time; `from __future__ import annotations` makes all annotations lazy strings, breaking schema generation.

---

## `ToolContext` — what it is and when a tool gets one

`ToolContext` is a **lauren-ai dataclass**. A tool receives it only if it declares a parameter annotated `ToolContext` (or `Optional[ToolContext]`). `_build_meta()` detects this at decoration time and sets `reads_context=True` and `context_param_name` to the actual parameter name. At execution time, `ToolExecutor._dispatch()` injects it:

```python
if meta.reads_context and meta.context_param_name:
    kwargs[meta.context_param_name] = tool_context
```

### `ToolContext` fields

| Field | Type | What it contains |
|---|---|---|
| `agent_context` | `AgentContext` | The running agent's context (turn number, config, transport, etc.) |
| `tool_use_id` | `str` | The ID the model assigned to this specific tool call |
| `turn` | `int` | Which turn of the agent loop this is (0-based) |
| `metadata` | `dict[str, Any]` | Static metadata from `@set_metadata` on the tool callable |
| `state` | `dict[str, Any]` | Mutable per-call scratch dict (empty at call start) |
| `tool_name` | `str` | Populated at execution time |
| `request` | `Any \| None` | HTTP request object if running in a web context |
| `execution_context` | `Any \| None` | Lauren DI execution context |

`ToolContext.get_metadata(key)` has a two-level fallback: tool-level static metadata first, then agent-level runtime metadata passed at `runner.run(metadata={…})`.

`ToolContext.message_bus` — returns the current run's message bus, if one is wired.

`ToolContext.runner` — returns the active runner driving the current agent turn.

Most agenthicc tools (file system tools, project plugin tools) do **not** declare `ctx: ToolContext` because they don't need it. They just receive their typed arguments directly.

---

## How tools reach the agent

```
build_registry()  (agenthicc/plugins/registry.py)
  └─ collects @tool()-decorated functions from:
       1. AGENT_TOOLS (built-ins: fs, git, exec, outlook, …)          ← lowest precedence
       2. project_plugin_tools (from .agenthicc/tools/*.py)
       3. agent-specific tools (per-agent directory)                   ← highest precedence
  └─ deduplication: by function __name__; last writer wins
  └─ returns ToolRegistry { _by_name: dict[str, callable] }

agent_turn.py:
  @agent_decorator(model=…, system=…)
  @use_tools(*registry.tools)          ← passes all callables to lauren-ai
  class _AgenthiccAgent: pass

  runner.run_stream(_AgenthiccAgent(), text, …)
```

`@use_tools(*fns)` stores the tools in `AGENT_META.tools` as `{name: (callable, ToolMeta)}`.

---

## Execution path (what happens when the model calls a tool)

```
AgentRunnerBase._execute_single_tool(tool_call, ctx, agent)
  │
  ├─ Looks up: _tool_map = getattr(agent, AGENT_META).tools
  │  └─ entry = _tool_map[tool_call.name]  →  (callable, ToolMeta)
  │
  ├─ Extracts static metadata: callable.__lauren_ai_tool_metadata__
  │
  ├─ Builds ToolContext:
  │    agent_context  = ctx               (AgentContext)
  │    tool_use_id    = tool_call.tool_use_id
  │    turn           = ctx.turn
  │    metadata       = static metadata dict
  │    state          = {}
  │
  ├─ Emits "ToolCallStarted" signal → agenthicc reads this via SignalBus
  │
  ├─ Calls ToolExecutor.execute(tool_call, tool_context, tool_map)
  │    │
  │    ├─ HITL gate: if requires_confirmation → raise ToolPendingApprovalSignal
  │    ├─ Cache read: if cache_ttl set, try to return cached result
  │    ├─ Before hooks: pre_hook + global before_tool_call hooks
  │    │
  │    ├─ _dispatch(callable, meta, tool_input, tool_context):
  │    │    ├─ Resolve fn:
  │    │    │    - class form: instantiate, use .run()
  │    │    │    - DI instance: use .run()
  │    │    │    - function: use directly
  │    │    ├─ Build kwargs from tool_input dict
  │    │    ├─ Inject ToolContext if meta.reads_context:
  │    │    │    kwargs[meta.context_param_name] = tool_context
  │    │    └─ Call: await fn(**kwargs)  or  run_in_executor(fn, **kwargs)
  │    │
  │    ├─ After hooks: post_hook + global after_tool_call hooks
  │    ├─ Convert result → ToolResult.ok(content, tool_use_id=…)
  │    └─ Cache write
  │
  ├─ Emits "ToolCallComplete" signal → agenthicc reads success/failure/duration
  │
  └─ Calls agent.on_tool_result(result, ctx)  (lifecycle hook, may modify result)
```

---

## Key constants (lauren-ai)

| Constant | Value | Where stored |
|---|---|---|
| `TOOL_META` | `"__lauren_ai_tool__"` | Attribute on `@tool()`-decorated callables; stores `ToolMeta` |
| `TOOL_METADATA` | `"__lauren_ai_tool_metadata__"` | Attribute storing static metadata dict (from `@set_metadata`) |
| `USE_HOOKS_META` | `"__lauren_ai_tool_hook_classes__"` | Attribute storing hook classes (from `@use_hooks()`) |
| `AGENT_META` | `"__lauren_ai_agent__"` | Attribute on agent classes; stores `AgentMeta` including tool map |

---

## agenthicc `Tool` ABC vs. lauren-ai `ToolContext`

| | agenthicc `Tool.execute(args, context)` | lauren-ai `ToolContext` |
|---|---|---|
| **Used by** | agenthicc kernel tool executor (not agent turns) | Every `@tool()` function that opts in |
| **`context` type** | Plain `dict[str, Any]` | Typed dataclass |
| **Injection** | Always passed | Only when declared in signature |
| **Agent awareness** | No | Yes — `agent_context`, `turn`, `runner` |
| **HITL** | No | Yes — `requires_confirmation` gate |
| **Caching** | No | Yes — `cache_ttl` |
| **Hooks** | No | Yes — `pre_hook`, `post_hook`, `error_hook`, `@use_hooks()` |
| **Status** | Internal / unused by agent path | Live — used by every agent tool call |

They are completely independent. agenthicc's `Tool` ABC was built for the kernel's own tool execution layer. The actual agent tools (everything the LLM calls) use lauren-ai's `@tool()` + `ToolContext` system exclusively.

---

## Writing a tool

### Function-form (stateless, most common)

```python
# No "from __future__ import annotations" — @tool() inspects real annotations
from lauren_ai._tools import tool

@tool()
async def my_tool(query: str, limit: int = 10) -> dict:
    """Short description used as the tool description for the model.

    Args:
        query: The search query.
        limit: Maximum number of results (default 10).
    """
    # ...
    return {"results": [...]}
```

### With ToolContext

```python
from lauren_ai._tools import tool, ToolContext

@tool()
async def my_tool(query: str, ctx: ToolContext) -> dict:
    """Tool that needs agent context."""
    turn = ctx.turn
    agent_cfg = ctx.agent_context.config
    # ...
```

### Class-form (stateful, with DI)

```python
from lauren_ai._tools import tool

@tool()
class MyTool:
    def __init__(self, dep: SomeDependency) -> None:
        self._dep = dep

    async def run(self, query: str) -> dict:
        """Tool description."""
        return await self._dep.search(query)
```

### Project plugin (`.agenthicc/tools/project_tools.py`)

```python
# No "from __future__ import annotations"
import sys
from pathlib import Path
from lauren_ai._tools import tool

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

@tool()
async def my_project_tool(arg: str) -> dict:
    """Tool description."""
    from my_project import do_something
    result = do_something(arg)
    return {"ok": True, "result": result}

TOOLS = [my_project_tool]   # must export this list
```

---

## `ToolRegistry` (agenthicc)

```python
class ToolRegistry:
    _by_name: dict[str, PluginTool]   # PluginTool = Callable[..., Any]

    def register(tool, *, source="unknown") -> None
    def register_many(tools, *, source="unknown") -> None
    def tools -> list[PluginTool]     # insertion-order, last-writer-wins per name
    def names -> list[str]
    def describe() -> str             # Markdown summary for system prompt
```

`build_registry(agent_name, project_plugin_tools, project_dir, user_dir)` — constructs a `ToolRegistry` for one agent turn with built-ins + project + agent-specific tools merged in priority order.

---

## `ToolResult` (lauren-ai)

```python
@dataclass
class ToolResult:
    tool_use_id: str
    content: str | list[Any]   # serialized output
    is_error: bool = False

    @classmethod
    def ok(cls, content: Any, *, tool_use_id: str) -> ToolResult: ...
    @classmethod
    def error(cls, message: str, *, tool_use_id: str) -> ToolResult: ...
```

Non-string content is automatically serialised to JSON. Lists are passed through as-is (for multi-part tool results).
