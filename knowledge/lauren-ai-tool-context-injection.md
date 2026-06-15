# lauren-ai: ToolContext Injection and AppState Embedding

## ToolContext parameter injection

Lauren-ai detects `ToolContext` parameters at **decoration time** in `_build_meta()` using `typing.get_type_hints()` (resolves forward references and handles `from __future__ import annotations`).

### What is recognised

| Annotation | Recognised? |
|---|---|
| `ctx: ToolContext` | Yes — exact type match |
| `ctx: Optional[ToolContext]` | Yes — string fallback (`"ToolContext" in ann_str`) |
| `ctx: ToolContext \| None` | Yes — same string fallback |
| Any parameter name | Yes — name doesn't matter; `ctx`, `tool_ctx`, `context`, etc. all work |
| `ctx: MySubclass(ToolContext)` | **No** — subclasses not auto-recognised |

### Detection code (`_tools/__init__.py` lines 524–553)

```python
reads_context = False
context_param_name: str | None = None
sig = inspect.signature(entry)
_hints = typing.get_type_hints(entry, globalns=_globalns, include_extras=False)
for param_name, param in sig.parameters.items():
    ann = _hints.get(param_name, param.annotation)
    if ann is ToolContext:                  # exact match
        reads_context = True
        context_param_name = param_name
        break
    if "ToolContext" in str(ann):           # Optional / | None fallback
        reads_context = True
        context_param_name = param_name
        break
```

### Injection code (`_executor.py` lines 489–496)

```python
kwargs = dict(tool_input)
if meta.reads_context and meta.context_param_name:
    kwargs[meta.context_param_name] = tool_context
```

### Hook execution order

```
1. ToolCallContext built          (state={}, metadata=static)
2. before_tool_call hooks fire    ← can write to ctx.state
3. ToolContext injected into tool
4. Tool executes                  ← reads ctx.state / ctx.metadata
5. after_tool_call hooks fire     ← can read modified ctx.state
   on_tool_error fires on exc
```

All hooks fire correctly regardless of what is stored in `ctx.state` or `ctx.metadata`.

---

## ToolContext fields

```python
@dataclass
class ToolContext:
    agent_context:     Any                    # AgentContext — running agent's context
    tool_use_id:       str                    # provider-assigned tool call ID
    turn:              int                    # 0-based agentic loop iteration
    request:           Any | None = None      # HTTP request (web context only)
    execution_context: Any | None = None      # lauren framework ExecutionContext
    metadata:          dict[str, Any] = {}    # static metadata from @set_metadata()
    state:             dict[str, Any] = {}    # mutable per-call scratch — ALWAYS starts empty
    tool_name:         str = ""               # populated at execution time
```

### `ToolCallContext` (hooks only)

`ToolCallContext` is a subclass of `ToolContext` with two extra fields used exclusively by `ToolHook` methods:

```python
@dataclass
class ToolCallContext(ToolContext):
    tool_name:  str = ""
    tool_input: dict[str, Any] = {}
```

Regular `@tool()` functions receive `ToolContext`. Hooks receive `ToolCallContext`.

---

## Can ToolContext carry AppState?

### `metadata` vs `state`

| Field | Pre-populated? | Best for |
|---|---|---|
| `metadata` | Yes — static data from `@set_metadata()` on the callable | Read-only tool config |
| `state` | **No — always `{}`** | Per-call mutable scratch; the right place for injected objects |

### `runner.run(metadata={...})` — what it does and does NOT do

`runner.run()` and `runner.run_stream()` accept `metadata: dict[str, Any] | None`. This goes into **`AgentContext.metadata`**, not directly into `ToolContext.metadata`.

Tools access it via:
```python
ctx.get_metadata(key)   # two-level: tool-level static → agent-level runtime
```

`ToolContext.metadata` itself contains only static metadata from `@set_metadata()` decorators.

**There is no `set_context()`, `with_context()`, or similar mechanism on the runner.**

### The correct pattern: inject via a global `ToolHook`

```python
class AppStateInjectorHook(ToolHook):
    """Injects AppState into ctx.state before every tool call."""

    def __init__(self, app_state: AppState) -> None:
        self._app_state = app_state

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        ctx.state["app_state"] = self._app_state
        return BeforeToolHookDecision.proceed()

# Register as a global hook when building the runner:
global_hooks = [AppStateInjectorHook(app_state)]
runner = AgentRunnerBase(transport, global_hooks=global_hooks, ...)
```

Tools that need `AppState` declare `ctx: ToolContext` and read it:

```python
@tool()
async def read_conversation(ctx: ToolContext, query: str) -> dict:
    conv = ctx.state["app_state"].conversation
    return {"turns": conv.turn_count(), "model": conv.model_name()}
```

Tools that don't need it omit `ctx` entirely — hooks still fire for them.

---

## ToolHook interface

```python
class ToolHook:
    async def before_tool_call(
        self, ctx: ToolCallContext
    ) -> BeforeToolHookDecision:
        return BeforeToolHookDecision.proceed()

    async def after_tool_call(
        self, result: Any, ctx: ToolCallContext
    ) -> AfterToolHookDecision:
        return AfterToolHookDecision.proceed()

    async def on_tool_error(
        self, exc: Exception, ctx: ToolCallContext
    ) -> ErrorToolHookDecision:
        return ErrorToolHookDecision.reraise()
```

### Decision types

| Method | Return type | Options |
|---|---|---|
| `before_tool_call` | `BeforeToolHookDecision` | `proceed()`, `abort(result)`, `modify(new_input)` |
| `after_tool_call` | `AfterToolHookDecision` | `proceed()`, `replace(result)` |
| `on_tool_error` | `ErrorToolHookDecision` | `reraise()`, `suppress_with(result)` |

---

## DI for class-form tools

Class-form tools are auto-decorated with `@injectable(scope=Scope.SINGLETON)` at decoration time, making them participate in the lauren DI container:

```python
@tool()
class MyTool:
    def __init__(self, dep: SomeDependency) -> None:
        self._dep = dep      # injected by DI container

    async def run(self, query: str) -> dict:
        return await self._dep.search(query)
```

This is the primary DI mechanism for complex tools. There is no equivalent for function-form tools — they must use `ToolContext` or closure capture for any dependencies.

---

## Summary

| Question | Answer |
|---|---|
| Is ToolContext injection consistent for all hook paths? | Yes — injection happens between before-hooks and dispatch; all hooks fire regardless |
| Does `Optional[ToolContext]` work? | Yes |
| Do subclasses of ToolContext work? | No — only exact `ToolContext` or string containing `"ToolContext"` |
| Is `ToolContext.state` pre-populated? | No — always `{}` |
| Can `runner.run(metadata={...})` reach `ToolContext.metadata`? | Indirectly via `ctx.get_metadata(key)`, not directly |
| Is there a `set_context()` / `with_context()` mechanism? | No |
| Best way to embed AppState in every tool call? | Global `ToolHook` that writes to `ctx.state["app_state"]` in `before_tool_call` |
| Do hooks fire correctly when arbitrary objects are in `ctx.state`? | Yes — hooks don't inspect state contents |
