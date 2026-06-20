# AGENTS.md — Agent guidance for agenthicc


## LLM Environment Variables

Agenthicc needs an LLM provider API key to run agents. Set before launching:

| Provider | Environment Variable | Notes |
|----------|---------------------|-------|
| Anthropic (default) | `ANTHROPIC_API_KEY` | `export ANTHROPIC_API_KEY="sk-ant-..."` |
| OpenAI | `OPENAI_API_KEY` | Also set `execution.provider = "openai"` in config |
| Ollama | — | No key needed; set `execution.provider = "ollama"` |

The default model is `claude-opus-4-6`. Override with `--set execution.model=claude-sonnet-4-6`
or in `.agenthicc/agenthicc.toml`:

```toml
[execution]
model = "claude-sonnet-4-6"
```

## File ownership

| Path | Owns what |
|---|---|
| `src/agenthicc/__init__.py` | Package root; top-level re-exports |
| `src/agenthicc/kernel/state.py` | `AppState` (frozen), all domain dataclasses (`Intent`, `Workflow`, `WorkflowNode`, `Task`, `AgentInstance`, `ToolRegistration`, `SecurityPolicy`, `SystemSettings`); copy-on-write `with_*` helpers |
| `src/agenthicc/kernel/events.py` | `Event` dataclass (create, from_dict, to_dict); `Effect`; `EffectType` enum |
| `src/agenthicc/kernel/reducer.py` | `root_reducer`; `_HANDLERS` dict; every per-event `_reduce_*` function; `ReducerFn` type alias |
| `src/agenthicc/kernel/processor.py` | `EventProcessor` (MPSC queue, `run()` loop, `emit()`, `drain()`, `subscribe()`); `EffectExecutor` protocol; `NoOpEffectExecutor`; `restore_from_log()` |
| `src/agenthicc/runtime/pool.py` | `AgentPool` (FIFO idle queue, `add()`, `acquire()`, `release()`); `AgentRecord` dataclass |
| `src/agenthicc/runtime/comm_tools.py` | `CommunicationTools`: `agent_spawn`, `agent_send_message`, `task_create`, `task_assign`, `workflow_modify`, `application_log`, `application_ui_update`, `tool_define`, `hook_register` |
| `src/agenthicc/runtime/scheduler.py` | `Scheduler`: Semaphore-bounded dispatch; picks ready DAG nodes; assigns agents |
| `src/agenthicc/workflow/dag.py` | `DAGNode`, `DAG`; `detect_cycle` (iterative DFS); `topological_sort`; `ready_nodes()` |
| `src/agenthicc/workflow/intent.py` | `IntentPlanner`: parses intent text → `WorkflowNode` spec list |
| `src/agenthicc/workflow/executor.py` | `WorkflowExecutor`: drives a `Workflow` through its DAG; re-dispatches newly ready nodes |
| `src/agenthicc/workflow/modify.py` | `WorkflowModifier`: thin wrapper around `CommunicationTools.workflow_modify` with pre-validation |
| `src/agenthicc/tools/base.py` | `ToolBase` ABC; `ToolResult` dataclass (`ok`, `value`, `error`, `duration_ms`) |
| `src/agenthicc/tools/http.py` | `agenthicc_http_client()` — shared async HTTP client; `configure(timeout_s)` — set at startup from `ToolSettings.http_timeout_s`; `is_network_error(exc)` — classifies network/timeout exceptions |
| `src/agenthicc/tools/executor.py` | `ToolExecutor`: name lookup, sandbox call, hook orchestration, `ToolCallStarted` / `ToolCallComplete` events |
| `src/agenthicc/tools/hooks.py` | `LifecycleHook` ABC; `HookRegistry` (`(entity_type, stage)` → list); `HookRunner` (`asyncio.gather`); `LaurenToolHookAdapter`; `load_hook_from_dotpath` |
| `src/agenthicc/tools/sandbox.py` | `ToolSandbox`: `ResourceLimits`; CPU/memory enforcement (`resource.setrlimit`); path allow-list check |
| `src/agenthicc/memory/layers.py` | `SessionMemoryLayer` (LRU+TTL), `ProjectMemoryLayer` (SQLite KV + artifacts), `GlobalMemoryLayer` (user-wide SQLite); `SessionEntry`, `ArtifactRecord`; `MemoryTier` |
| `src/agenthicc/memory/router.py` | `MemoryRouter`: routes get/set/delete to the right tier; all-tiers fallback for get |
| `src/agenthicc/memory/vector.py` | `VectorIndex`: sqlite-vec wrapper; `upsert()`, `nearest()` |
| `src/agenthicc/tui/transcript.py` | `TranscriptModel`, `AgentTurnEntry`, `ToolCallEntry`, `ToolCallState`; `SPINNER_FRAMES`; `render()`; `diff_lines()` |
| `src/agenthicc/tui/app.py` | `build_app()`, `run_headless()`, `render_frame_ansi()`; `MENU_COMMANDS`; `detect_slash_command()` |
| `src/agenthicc/tui/events.py` | `TUIEventAdapter`: translates kernel `AppState` diffs into `TranscriptModel` mutations |
| `src/agenthicc/api/server.py` | `create_app(processor, api_key)`: FastAPI lifespan, `POST /v1/intents`, `GET /v1/intents/{id}`, `GET /v1/state/summary`, `WS /v1/ws` |
| `src/agenthicc/config.py` | `AgenthiccConfig` + sub-dataclasses; `load_config()`; `deep_merge()`; `to_system_settings()`; `to_security_policy()` |
| `src/agenthicc/security.py` | `build_policy_from_config()`: `SecuritySettings` → kernel `SecurityPolicy` |
| `src/agenthicc/tui/terminal/backend.py` | `TerminalBackend` Protocol; `get_backend()` factory — the only permitted `os.name` branch |
| `src/agenthicc/tui/terminal/posix_backend.py` | `PosixBackend` — wraps `cbreak_reader.raw_mode` / `read_key`; owns all POSIX terminal setup |
| `src/agenthicc/tui/terminal/windows_backend.py` | `WindowsBackend` — exclusive owner of all `msvcrt` calls; no other file may import `msvcrt` |
| `src/agenthicc/tui/cbreak_reader.py` | `Key` enum (canonical — imported by 11 files); `raw_mode(fd)`; `read_key(fd)` — used only by `PosixBackend` |
| `src/agenthicc/tui/terminal_caps.py` | `TerminalCapabilities` frozen dataclass; `TerminalCapabilityDetector.detect()` |
| `llms-full.txt` | Full API reference for LLMs — must stay in sync with public symbols |
| `llms.txt` | Short package overview — update when public API changes |
| `tests/conftest.py` | Shared fixtures: `processor`, `pool`, `comm_tools`, `minimal_state`, `running_processor` |
| `tests/unit/test_appstate_reducers.py` | Pure reducer tests (no asyncio) |
| `tests/unit/test_agent_pool.py` | `AgentPool` acquire/release/timeout tests |
| `tests/unit/test_comm_tools.py` | `CommunicationTools` unit tests |
| `tests/unit/test_config.py` | `load_config()` merge and validation tests |
| `tests/integration/test_event_processor.py` | Full emit/drain/subscribe cycle |
| `tests/integration/test_workflow_executor.py` | End-to-end workflow via events |
| `tests/integration/test_executor_with_hooks.py` | `ToolExecutor` + `HookRunner` integration |
| `tests/integration/test_artifact_sharing.py` | `ProjectMemoryLayer` artifact round-trip |
| `tests/e2e/test_agent_runner_e2e.py` | Full session with lauren-ai agent runner |
| `tests/e2e/test_argon2_scenario.py` | End-to-end Argon2 refactor scenario |

---

## By-task lookup

### 1. Adding a new event type

1. Add a frozen dataclass or payload description comment to `kernel/events.py` —
   document what keys `payload` must contain.
2. Add a `_reduce_<event_type>` function in `kernel/reducer.py`:
   ```python
   def _reduce_foo_happened(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
       ...
       return state.with_<entity>(updated), [Effect(EffectType.update_tui, {...})]
   ```
3. Register the handler in the `_HANDLERS` dict at the bottom of `reducer.py`:
   ```python
   "FooHappened": _reduce_foo_happened,
   ```
4. Add a unit test in `tests/unit/test_appstate_reducers.py` that constructs an
   `Event` with the new type and asserts the returned state is correct.
5. Update `llms-full.txt` with a `### FooHappened` section describing the payload.
6. If the event triggers an observable state change, add an `update_tui` effect so
   `TUIEventAdapter` can react.

### 2. Adding a new communication tool

1. Add an `async def <name>(self, ...)` method to `CommunicationTools` in
   `runtime/comm_tools.py`.  Keep it a plain `async` callable — no framework
   decorators — so it can be wrapped by any adapter.
2. Emit the correct event(s) using `await self._emit(event_type, payload)`.
   Return a typed `dict` (never `None`).
3. If the tool needs fresh state (reads after a prior emit), call
   `await self._fresh_state()` to drain the queue first.
4. If the tool is also needed by agents via lauren-ai, add it to the tool schema
   in `tools/base.py` and register it via `ToolRegistered` at startup.
5. Update `llms-full.txt` with the new method signature and return keys.
6. Add a test in `tests/unit/test_comm_tools.py` using the `running_processor`
   fixture; assert the emitted event type and the return dict keys.

### 3. Adding a reducer handler

1. Write a pure function in `kernel/reducer.py`:
   ```python
   def _reduce_bar_updated(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
       old = state.<entities>.get(event.payload["id"])
       if old is None:
           return state, []
       updated = replace(old, field=event.payload["field"])
       return state.with_<entity>(updated), []
   ```
2. Add it to `_HANDLERS`:
   ```python
   "BarUpdated": _reduce_bar_updated,
   ```
3. Add a pure unit test in `tests/unit/test_appstate_reducers.py` — no asyncio
   needed; call `root_reducer(state, event)` directly and assert the new state.
4. If the handler returns `Effect` objects, write a separate integration test that
   starts a processor and verifies the effects are scheduled.

### 4. Adding a lifecycle hook

1. Subclass `LifecycleHook` from `tools/hooks.py` and override any subset of
   `on_before`, `on_after`, `on_error`:
   ```python
   class AuditHook(LifecycleHook):
       async def on_after(self, entity: Any, result: Any, ctx: Any) -> None:
           logger.info("completed %s → %s", entity, result)
   ```
2. Register it programmatically via `HookRegistry.register(entity_type, stage, hook)`,
   or declaratively via TOML:
   ```toml
   [hooks.tool_call]
   after = ["mypackage.hooks:AuditHook"]
   ```
   TOML dotpaths are loaded by `load_hook_from_dotpath()` at startup.
3. If the hook needs to abort execution, return `Rejection(reason="...")` from
   `on_before` — the `HookRunner` will surface it to the caller.
4. Test with a concrete subclass in `tests/integration/test_executor_with_hooks.py`;
   assert that `on_before` rejections prevent execution and `on_after` fires after
   a successful call.

### 5. Extending the TUI

1. Add an event handler in `TUIEventAdapter._handlers` in `tui/events.py`.  The
   handler receives the new `AppState` and updates `TranscriptModel` accordingly:
   ```python
   def _handle_tool_call_complete(self, state: AppState, event_payload: dict) -> None:
       self._model.update_tool_call(
           tool_use_id=event_payload["tool_use_id"],
           state=ToolCallState.SUCCESS,
           duration_ms=event_payload.get("duration_ms"),
       )
   ```
2. If the new event produces visible output, update `TranscriptModel` (and
   `render()`) in `tui/transcript.py` to include the new line format.
3. Add tests in `tests/unit/test_tui_transcript.py` for the `TranscriptModel`
   changes (no terminal required — `render()` returns plain strings).
4. For layout changes, use `render_frame_ansi()` + pyte in `tests/e2e/` to
   verify the rendered output at specific terminal dimensions.

### 6. Adding a new API endpoint

1. Add a route inside `create_app()` in `api/server.py`:
   ```python
   @app.get("/v1/agents")
   async def list_agents(_: None = Depends(require_auth)) -> dict:
       return {"agents": list(processor.get_state().agents.keys())}
   ```
2. Auth is automatic via `Depends(require_auth)` — include it on every new
   endpoint.
3. Add a test in `tests/integration/test_api.py` using `httpx.AsyncClient` with
   the FastAPI `lifespan` context so the processor runs during the test.
4. For WebSocket endpoints, follow the pattern in the existing `/v1/ws` handler:
   subscribe, accept, loop, unsubscribe in `finally`.

### 7. Adding a new scroll-buffer event renderer

New `ConversationEvent` kinds are rendered by registering a function with
`@register_renderer` in `tui/workspace/appender.py` — no edits to
`_render_one()` are needed.

1. Define a module-level function **after** the `ScrollBufferAppender` class
   definition in `tui/workspace/appender.py`:
   ```python
   @register_renderer("my_event")
   def _render_my_event(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
       from rich.markup import escape as _e  # noqa: PLC0415
       text = ev.payload.get("text", "")
       self._console.print(f"  {_e(text)}", markup=True, highlight=False)
   ```
2. Emit the new event kind from `AgentTurnRunner` or wherever appropriate:
   ```python
   ctx.conv_store.append_event("my_event", {"text": "something happened"})
   ```
3. The function receives `self` (the appender instance) and `ev`
   (`ConversationEvent`).  Call `self._flush_group_summary()` at the start if
   the new event should close an open tool group.
4. Add a test that creates a `ScrollBufferAppender`, calls `_flush_batch()`
   with a `ConversationEvent(kind="my_event", ...)`, and asserts the expected
   `console.print()` call.

**Important:** renderers must be defined **after the class closes** in the
file.  Placing a `@register_renderer` function inside the class body or before
the class ends causes it to become a nested function, not a module-level
renderer.

### 8. Adding a new approval overlay kind

New `ApprovalRequest.kind` values are mapped to overlay classes via the
`_overlay_registry` dict in `tui_session._on_approval_change()`.

1. Create a new overlay class (subclass `PromptOverlay` or `Overlay` from
   `tui/workspace/overlay.py`) in `tui/workspace/overlays/`.
2. Add an entry to `_overlay_registry` inside `_on_approval_change()` in
   `runners/tui_session.py`:
   ```python
   _overlay_registry = {
       "plan_review": PlanApprovalOverlay,
       "questions":   QuestionsOverlay,
       "my_kind":     MyNewOverlay,        # ← add here
   }
   ```
3. Import the new class at the top of `_on_approval_change()`.
4. Emit the approval request with the new kind from a tool or agent:
   ```python
   req = ApprovalRequest(kind="my_kind", ...)
   await approval_svc.request_approval(req)
   ```

### 9. Adding a new kernel bridge event handler

The legacy `inject_event()` bridge in `tui/runtime/kernel_bridge.py` dispatches
on `event["type"]` through `_EVENT_HANDLERS`.  New types are registered with
`@register_event_handler`.

1. Define a module-level function **after the `KernelBridge` class** in
   `tui/runtime/kernel_bridge.py`:
   ```python
   @register_event_handler("my_event_type")
   def _handle_my_event(self: KernelBridge, event: dict) -> None:
       value = event.get("value", "")
       self._conv.some_signal.set(value)
   ```
2. The function receives `self` (the `KernelBridge` instance) and `event`
   (the raw dict from the legacy bridge).
3. No changes to `inject_event()` are needed.

### 10. Extending memory tiers

1. Add methods to the appropriate layer class in `memory/layers.py`:
   - `SessionMemoryLayer` for ephemeral in-process data
   - `ProjectMemoryLayer` for per-project persistence
   - `GlobalMemoryLayer` for user-wide persistence
2. Update `MemoryRouter` in `memory/router.py` to route the new method to the
   correct tier (or all tiers, for fan-out operations).
3. Reads must never acquire a lock.  Writes must acquire `self._lock` for the
   owning tier.
4. Test with `tests/integration/test_artifact_sharing.py` as a reference pattern:
   create a temp directory, instantiate the layer, call the new method, assert
   the expected rows in SQLite.

---

## How to add a new slash command

Adding a slash command requires **two independent steps** that are easy to
get wrong separately:

### Step 1 — Register in `BUILTIN_COMMANDS` (makes it visible in the trigger picker)

Every slash command **must** appear in `commands/builtins.py:BUILTIN_COMMANDS`
regardless of where its execution logic lives.  The trigger picker
(`SlashCommandTrigger.get_matches()`) only calls `registry.matches(partial)` —
it is completely unaware of commands that are handled elsewhere.

```python
# commands/builtins.py — add to BUILTIN_COMMANDS list
Command(
    name="/my-command",
    description="One-line description shown in the dropdown right column",
    argument_hint="[optional-arg]",   # shown as usage hint
    group="Built-in",
    handler=_cmd_my_command,          # None is valid — see Step 2 note
),
```

### Step 2 — Provide execution logic

**Option A — Handler in the registry (preferred for stateless commands)**

Write a `_cmd_my_command(ctx: CommandContext) -> bool` function and set it as
`handler=` on the `Command`.  The `CommandDispatcher` calls it automatically
when the user submits `/my-command`.

```python
def _cmd_my_command(ctx: CommandContext) -> bool:
    ctx.console.print("Hello from /my-command!")
    return True   # True = handled
```

**Option B — Intercept in `TUISession.route()` (required for session-stateful commands)**

When the handler needs access to `TUISession` fields (e.g.
`self._workflow_override`, `self._agent_task`), intercept the command in
`route()` *before* `dispatch_slash()` is called, and set `handler=None` in the
registry entry.  The `None` handler is intentional — it signals "display only;
handled elsewhere".

```python
# runners/tui_session.py — TUISession.route()
def route(self, msg: str) -> bool:
    if not msg.startswith("/"):
        return False
    parts = msg.split(None, 1)
    if parts[0] == "/my-command":            # intercept before dispatch_slash
        return self._handle_my_command(parts[1] if len(parts) > 1 else "")
    if self.dispatch_slash(msg):
        ...
```

### The critical lesson

**Registering in `BUILTIN_COMMANDS` and providing execution logic are
independent requirements.**  Missing either one causes a different silent
failure:

| Missing | Symptom |
|---|---|
| Not in `BUILTIN_COMMANDS` | Command works when typed in full but **never appears in the trigger picker dropdown** |
| No handler / not intercepted | Command appears in the picker but **does nothing when submitted** |

The `/workflow` command (PRD-114) was initially missing from `BUILTIN_COMMANDS`
and therefore invisible in the trigger picker even though it executed correctly.

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `TypeError: AgentPool.__init__() got unexpected keyword argument 'max_size'` | `AgentPool` takes no constructor arguments; pool size is set at `Scheduler` level via `Semaphore` | Use `AgentPool()` with no args |
| `KeyError: 'logged'` on `application_log` result | Return dict key is `"accepted"`, not `"logged"` | Use `result["accepted"]` |
| `KeyError: 'ok'` on `workflow_modify` add_node result | Return dict uses `"applied"`, not `"ok"` | Use `result["applied"]` |
| `ValueError: adding node ... would create a cycle in workflow ...` | `workflow_modify(action="add_node")` raises on cycle — does not return a failure dict | Catch `ValueError`; this is the only signal that the add was rejected |
| `TypeError: object NoneType can't be used in 'await' expression` on `pool.add(...)` | `AgentPool.add()` is synchronous — returns `None` | Remove `await`; `pool.add(record)` is a plain call |
| `AssertionError: intent status 'pending' != 'complete'` after emitting events | Processor `run()` task not started; events queue but are never applied | Start `asyncio.create_task(processor.run())` before emitting; use the `running_processor` fixture |
| `TimeoutError` (from `asyncio.wait_for`) inside `drain()` | `drain()` waits for the idle event; if `run()` is not scheduled the queue never drains | Ensure `processor.run()` is launched as a task before calling `drain()` |
| `unknown tool` warning from `AgentRunnerBase` | Constructing `AgentRunnerBase(transport=...)` directly instead of using the factory | Use `_build_runner_for_agent()` which registers the comm tools correctly |
| pyte test: input bar check on `screen.buffer[ROWS - 2]` finds status line, not input | `render_frame_ansi` writes input at `rows` (1-indexed) = `ROWS - 1` (0-indexed) | Change assertion to `screen.buffer[ROWS - 1]` |
| `EmptyQueueError` from `MockTransport` on second LLM turn | `MockTransport` requires a pre-queued response for every LLM round-trip | Add `mock_transport.queue_response(...)` for each expected turn before running the agent |
| `KeyError: 'status'` when checking workflow node | Accessing `node.status` on a plain `dict` instead of a `WorkflowNode` dataclass | Retrieve from `state.workflows[wf_id].nodes[node_id]` which yields a `WorkflowNode` |
| `ValueError: agent '<id>' already registered` from `pool.add()` | Calling `pool.add()` twice for the same `agent_id` | Each agent is added once; `agent_spawn` in `CommunicationTools` already calls `pool.add()` |

---

## Definition of done

A change is complete when ALL of the following pass:

```bash
uv run ruff check src/ tests/          # ruff: 0 errors
uv run ruff format --check src/ tests/ # format: no diffs
uv run mypy src/agenthicc              # mypy: 0 errors
uv run python scripts/check_llms.py   # all public symbols documented in llms-full.txt
uv run pytest tests/ -q               # all tests pass
```

If you add a public symbol, `check_llms.py` will fail — add a `### SymbolName`
section to `llms-full.txt` to fix it.

If you add a new event type, update:
1. `kernel/events.py` — document the payload keys in a comment or docstring
2. `kernel/reducer.py` — add the handler and register in `_HANDLERS`
3. `llms-full.txt` — add the event type section
4. `tests/unit/test_appstate_reducers.py` — add a pure reducer test

If you change a `CommunicationTools` method signature, update:
1. `runtime/comm_tools.py` — the method itself
2. `llms-full.txt` — the method's section
3. Any tests in `tests/unit/test_comm_tools.py` that call the old signature

## HTTP tool safety rules (PRD-108)

- **Never** use `httpx.AsyncClient()` directly in a tool — always use `agenthicc_http_client()` from `tools/http.py`.
- **All tools** that make HTTP calls must catch network errors with `is_network_error(exc)` and return `{"ok": False, "error": f"{type(exc).__name__}: {exc}", "recoverable": True}` — never let a `ReadTimeout` propagate to `_stream()`.
- **Outlook tools** raise `_OutlookNetworkError` in `_get()`/`_post()`; agent_tools.py catches it via `_safe_call()`.
- **Auth calls** (`_exchange_code`, `_refresh`) raise `AuthNetworkError` on timeout — callers must catch it and show a human-readable message.
- **Timeout default**: `ToolSettings.http_timeout_s = 30.0`; `_build_session_context()` calls `tools.http.configure()` after loading config.
- **Connect timeout**: always 10 s regardless of read timeout — set inside `agenthicc_http_client()`.

## Terminal backend rules (PRD-105/106)

- **`get_backend()`** in `tui/terminal/backend.py` is the **only** place that may branch on `os.name` for terminal decisions.
- **No application code** may import `msvcrt`, `termios`, or `tty` directly — all platform-specific terminal calls are confined to `posix_backend.py` and `windows_backend.py`.
- **`Key` enum** lives in `cbreak_reader.py` and stays there — all 11 existing importers use that path; do not create a second definition.
- **`unified_session.run()`** calls `get_backend()` and checks `backend.is_interactive()` before entering `enter_raw_mode()` — if not interactive, it returns cleanly so `TUISession` can cancel tasks normally.
- **`PosixBackend.enter_raw_mode()`** on a non-TTY fd yields without configuring the terminal (passthrough); never crashes.
- **`WindowsBackend.enter_raw_mode()`** is a no-op; `msvcrt.getwch()` already bypasses line buffering.

---

## Key invariants

- `root_reducer` is a pure function — no `await`, no I/O, no global state.
- `AppState` is `frozen=True`; never mutate a field directly.
- `AgentPool.add()` is synchronous; never `await` it.
- `CommunicationTools.workflow_modify(action="add_node")` raises `ValueError` on
  cycle detection — it does **not** return `{"ok": False}`.
- `application_log` returns `{"accepted": True, ...}` — not `"logged"`.
- `application_ui_update` returns `{"queued": True, ...}`.
- `render_frame_ansi` places the input bar at ANSI row `rows` (1-indexed);
  the pyte buffer index for the same row is `rows - 1` (0-indexed).
- `EventProcessor.drain()` requires `run()` to be scheduled as a task first.
- All `LifecycleHook` methods default to no-ops — subclasses override only the
  stages they care about.
- `HookRunner.run_before` returns the first `Rejection` by registration order;
  subsequent hooks still run (via `asyncio.gather`) but their results are ignored.
