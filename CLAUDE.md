# CLAUDE.md — Development guide for agenthicc

## Project overview

`agenthicc` is a state-driven agent operating system for autonomous software
engineering built on top of lauren-ai.  It provides:

- **Event-sourced kernel** — MPSC `asyncio.Queue` feeding a pure `root_reducer`;
  every state change is an appended `Event`; `AppState` is fully immutable.
- **Parallel DAG executor** — intents compile to dependency graphs; ready nodes
  execute concurrently bounded by `max_parallel_tasks`.
- **Tool-only agent communication** — agents never call each other directly;
  all inter-agent signalling goes through `CommunicationTools` methods that emit
  typed kernel events (`agent_spawn`, `task_create`, `workflow_modify`, etc.).
- **Lifecycle hooks** — `LifecycleHook.on_before/on_after/on_error` at intent,
  workflow node, task, agent, and tool-call granularity; loaded from TOML dotpaths.
- **3-tier memory** — session (in-process LRU+TTL), project (SQLite KV + artifacts),
  global (user-wide SQLite).
- **Full-screen TUI** — `prompt_toolkit` HSplit; transcript viewport auto-scrolls;
  input bar always pinned to the last terminal row.


## LLM Configuration

Agenthicc uses lauren-ai for LLM calls. The only required env var is:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."  # for Anthropic Claude (default)
export OPENAI_API_KEY="sk-..."         # for OpenAI (set provider = "openai")
# Ollama needs no key — just have it running locally
```

Override the model:
```bash
agenthicc --set execution.model=claude-sonnet-4-6
# or in .agenthicc/agenthicc.toml:
# [execution]
# model = "claude-sonnet-4-6"
```

## Essential commands

```bash
# Run full test suite (all layers)
uv run pytest tests/ -q

# Run by layer
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q

# Run a single file
uv run pytest tests/unit/test_appstate_reducers.py -v

# Type-check source
uv run mypy src/agenthicc

# Lint + format check
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Check llms-full.txt coverage
uv run python scripts/check_llms.py

# Launch the TUI
uv run agenthicc

# Launch headless (JSON-lines to stdout)
uv run agenthicc --headless
```

## Repository layout

```
src/agenthicc/
  __init__.py              Package root; re-exports top-level symbols

  kernel/
    __init__.py            Re-exports AppState, Event, EventProcessor, all state types
    state.py               Frozen AppState + all domain dataclasses (Intent, Workflow,
                           WorkflowNode, Task, AgentInstance, ToolRegistration,
                           SecurityPolicy, SystemSettings); copy-on-write with_* helpers
    events.py              Event dataclass (event_id, event_type, payload, timestamp,
                           source_agent_id); Effect + EffectType; Event.create / from_dict
    reducer.py             Pure root_reducer: (AppState, Event) -> (AppState, list[Effect]);
                           _HANDLERS dict maps event_type strings to handler functions
    processor.py           EventProcessor: MPSC asyncio.Queue, run() loop, emit(), drain(),
                           subscribe()/unsubscribe(); NoOpEffectExecutor; restore_from_log()

  runtime/
    __init__.py            Re-exports AgentPool, AgentRecord, CommunicationTools, Scheduler
    pool.py                AgentPool: FIFO idle queue + busy dict; add() (sync), acquire()
                           (async, blocks until timeout), release(); AgentRecord dataclass
    comm_tools.py          CommunicationTools: agent_spawn, agent_send_message, task_create,
                           task_assign, workflow_modify, application_log, application_ui_update,
                           tool_define, hook_register; all methods are plain async callables
    scheduler.py           Scheduler: asyncio.Semaphore-bounded task dispatch; picks ready
                           DAG nodes and assigns them to agents via CommunicationTools
    comm_tools.py          (see above)

  workflow/
    __init__.py            Re-exports WorkflowExecutor, IntentPlanner, WorkflowModifier, DAG
    dag.py                 DAGNode, DAG; detect_cycle (iterative DFS); topological_sort;
                           ready_nodes() returns nodes with all deps satisfied
    intent.py              IntentPlanner: parses raw intent text into a list of WorkflowNode
                           specs; wraps the lauren-ai planner agent
    executor.py            WorkflowExecutor: drives a Workflow through its DAG; listens for
                           node-complete effects; re-dispatches newly ready nodes
    modify.py              WorkflowModifier: validates and applies add_node/remove_node;
                           thin wrapper around CommunicationTools.workflow_modify

  tools/
    __init__.py            Re-exports ToolExecutor, HookRegistry, HookRunner, LifecycleHook,
                           ToolSandbox
    base.py                ToolBase ABC; ToolResult dataclass (ok, value, error, duration_ms)
    executor.py            ToolExecutor: looks up tool by name, calls sandbox, runs hooks,
                           emits ToolCallStarted / ToolCallComplete events
    hooks.py               LifecycleHook ABC (on_before/on_after/on_error); HookRegistry
                           (entity_type × stage → list); HookRunner (asyncio.gather);
                           LaurenToolHookAdapter; load_hook_from_dotpath
    sandbox.py             ToolSandbox: ResourceLimits, CPU/memory enforcement via
                           resource.setrlimit; path allow-list check before each call

  memory/
    __init__.py            Re-exports all three layers + MemoryRouter
    layers.py              SessionMemoryLayer (LRU+TTL), ProjectMemoryLayer (SQLite KV +
                           artifact table), GlobalMemoryLayer (user-wide SQLite);
                           ArtifactRecord dataclass; reads never block; writes serialised
                           per tier via asyncio.Lock
    router.py              MemoryRouter: routes get/set/delete to the correct tier based on
                           the MemoryTier enum; convenience all-tiers fallback for get
    vector.py              VectorIndex: sqlite-vec wrapper; upsert / nearest-neighbour query;
                           used for semantic retrieval from project memory

  tui/
    __init__.py            Re-exports build_app, run_headless, render_frame_ansi,
                           TranscriptModel
    transcript.py          TranscriptModel (mutable); AgentTurnEntry, ToolCallEntry,
                           ToolCallState; SPINNER_FRAMES; render() → list[str]; diff_lines()
    app.py                 build_app(): prompt_toolkit Application; HSplit transcript /
                           status / input; FloatContainer menu overlay; detect_slash_command;
                           render_frame_ansi() (offline ANSI frame for pyte e2e tests);
                           run_headless() (JSON-lines stdout mode)
    events.py              TUIEventAdapter: subscribes to the kernel state queue and
                           translates AppState diffs into TranscriptModel mutations

  api/
    __init__.py            Re-exports create_app
    server.py              create_app(processor, api_key): FastAPI app with lifespan;
                           POST /v1/intents, GET /v1/intents/{id},
                           GET /v1/state/summary, WS /v1/ws; optional Bearer auth

  config.py                AgenthiccConfig + sub-dataclasses (ExecutionSettings,
                           ToolSettings, MemorySettings, SecuritySettings, ApiSettings);
                           load_config() merges agenthicc.toml + ~/.agenthicc.toml;
                           deep_merge(); to_system_settings() / to_security_policy()
  security.py              build_policy_from_config(): translates SecuritySettings into
                           a kernel SecurityPolicy (allow/deny/require_confirmation rules)

tests/
  conftest.py              Shared fixtures: processor, pool, comm_tools, minimal_state,
                           running_processor (starts run() as a task)
  unit/
    test_appstate_reducers.py   Pure reducer tests (no asyncio, no processor)
    test_agent_pool.py          AgentPool acquire/release/timeout tests
    test_comm_tools.py          CommunicationTools unit tests with a running processor
    test_config.py              load_config() merge and validation tests
    test_workflow_dag.py        DAG cycle detection, topological sort, ready_nodes
    test_hooks.py               HookRegistry, HookRunner, LaurenToolHookAdapter
    test_tui_transcript.py      TranscriptModel render, diff_lines, spinners
  integration/
    test_event_processor.py     Full emit/drain/subscribe cycle with real processor
    test_workflow_executor.py   End-to-end workflow execution via events
    test_executor_with_hooks.py ToolExecutor + HookRunner integration
    test_artifact_sharing.py    ProjectMemoryLayer artifact store round-trip
  e2e/
    test_agent_runner_e2e.py    Full session with lauren-ai agent runner
    test_argon2_scenario.py     End-to-end Argon2 refactor scenario via TUI/API
```

## Architecture decisions

### 1. MPSC event queue with single-consumer reducer

`EventProcessor` owns a single `asyncio.Queue[Event]`.  Any coroutine can call
`await processor.emit(event)` (producer side); one `run()` task dequeues events
serially, applies `root_reducer`, persists the line to `events.jsonl`, notifies
all subscriber queues, and schedules effects.  Serial consumption means the reducer
is always called from one coroutine — no locking needed on `AppState`.  Full
replay from the log is possible via `restore_from_log()`.

### 2. Frozen AppState with copy-on-write helpers

Every field on `AppState` is immutable (`frozen=True` dataclass).  Mutations
return a new `AppState` sharing unchanged sub-dicts by reference (`dataclasses.replace`
+ spread operator).  The `with_intent`, `with_workflow`, `with_task`, `with_agent`,
`with_tool`, `with_hook` helpers are the only sanctioned write paths.  This means
any snapshot of state is permanently safe to hold; there are no "stale read" bugs.

### 3. Tool-only agent communication

Agents communicate exclusively through `CommunicationTools` methods, which emit
kernel events rather than calling Python directly.  Rationale:

- **Observability** — every inter-agent action is an event in the append-only log.
- **Security** — agents cannot call arbitrary code on each other; only the typed
  tool catalog is accessible.
- **Replay** — a session can be replayed from the event log without re-running
  any agent logic.

Never add Python-level agent-to-agent call paths.  If a new coordination primitive
is needed, express it as a new event type + reducer handler.

### 4. prompt_toolkit HSplit with input bar on last row

`build_app()` lays out `HSplit([transcript_window, status_window, input_window])`.
`render_frame_ansi()` writes the input bar at ANSI row `rows` (1-indexed).  In
pyte tests, the corresponding screen buffer index is `ROWS - 1` (0-indexed).
The menu overlay is a `Float(bottom=2)` inside a `FloatContainer` — it floats
above the status line and **never** displaces the input bar.

**Critical**: when writing pyte tests that check the input bar, assert against
`screen.buffer[ROWS - 1]`, not `screen.buffer[ROWS - 2]`.

### 5. asyncio.Semaphore for task throttling, not threads

`Scheduler` uses `asyncio.Semaphore(max_parallel_tasks)` to bound concurrency.
Rationale: agent runners are `async def` — spawning OS threads would fight the
GIL and add context-switch overhead.  A Semaphore is a single awaitable that
integrates naturally with the event loop, has predictable fairness (FIFO waiters),
and is trivially inspectable in tests.

### 6. SQLite for project/global memory

`ProjectMemoryLayer` and `GlobalMemoryLayer` use `sqlite3` (stdlib).  Rationale:
zero external dependency, file-per-project isolation, atomic writes via WAL mode,
good enough for single-user workloads.  The `vector.py` module wraps `sqlite-vec`
for nearest-neighbour retrieval.  If scale demands Redis or pgvector those are
drop-in replacements — swap `layers.py` without touching `MemoryRouter`.

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `TypeError: object NoneType can't be used in 'await' expression` on `pool.add(...)` | `AgentPool.add()` is synchronous, not a coroutine | Remove `await`; call `pool.add(record)` directly |
| `ValueError: unsupported workflow action 'add'` | `workflow_modify` action parameter must be `"add_node"`, not `"add"` | Use `action="add_node"` or `action="remove_node"` |
| `KeyError: 'logged'` on `application_log` result | The return dict uses `"accepted"`, not `"logged"` | Use `result["accepted"]` |
| `KeyError: 'queued'` on `application_ui_update` result | The return dict uses `"queued"` — check, it should be present | Confirm you are not reading `result["ok"]`; the key is `"queued"` |
| `ValueError: adding node ... would create a cycle` | `workflow_modify(action="add_node")` raises on cycle detection — does not return `ok=False` | Catch `ValueError`; it is the definitive signal that the add was rejected |
| `KeyError: 'ok'` on `workflow_modify` result | `workflow_modify` returns `{"applied": True, ...}`, not `{"ok": True}` | Use `result["applied"]` |
| `TimeoutError` in `processor.drain()` | The `run()` coroutine is not scheduled — `drain()` waits for the idle event which never fires | Start `asyncio.create_task(processor.run())` before emitting events; use the `running_processor` fixture |
| `EventProcessor.drain()` hangs indefinitely | Processor was never started; `_idle` event is set but queue stays populated | Ensure `run()` is running as a task before calling `drain()` |
| pyte test: input bar missing from expected row | Asserting `screen.buffer[ROWS - 2]` instead of `screen.buffer[ROWS - 1]` | `render_frame_ansi` writes input bar at `rows` (1-indexed) = `ROWS - 1` (0-indexed in pyte) |
| `AgentPool.acquire()` blocks forever in tests | No agent was registered before calling `acquire()` | Call `pool.add(AgentRecord(...))` first, or pass `timeout=0.1` and expect `TimeoutError` |
| `AssertionError: intent status is 'pending'` after emitting | Processor not yet running when `drain()` is called | Use `running_processor` fixture (creates `asyncio.create_task(processor.run())`) |

## Conventions

- `from __future__ import annotations` on every source file.
- `asyncio_mode = "auto"` in pytest — every `async def test_*` runs automatically;
  do **not** add `@pytest.mark.asyncio`.
- Mark tests: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.e2e`.
- Reducer handler functions are pure — no `await`, no I/O, no side effects.
  Side effects go in `Effect` objects returned alongside the new state.
- `ruff` for linting + formatting (`line-length = 100`).
- All public symbols must appear in `llms-full.txt`; run `check_llms.py` to verify.


## New Modules (PRD-13..19)
```

  plugin.py                Plugin system (PRD-13): AgenthiccPlugin ABC, PluginRegistry
                           discover/load/reload; register_tool/hook/command/agent_type

  security.py              Permission enforcement (PRD-07 + PRD-19): PermissionChecker,
                           AgentCapabilityScope, ScopeManager; per-agent tool scoping

  skills/
    __init__.py            Skills system (PRD-18): SkillBundle ABC, SkillRegistry,
                           _BUILTIN registry; load/load_all; system_prompt_suffix
    web_search.py          SearchWebTool (Brave API), FetchPageTool (httpx)

  tools/
    fs/__init__.py         14 filesystem tools (PRD-14): read_file, write_file,
                           append_file, delete_file, move_file, copy_file,
                           list_directory, make_directory, file_exists,
                           search_files, grep_files, get_file_info,
                           read_lines, patch_file; FsToolKit factory
    git/__init__.py        11 git tools (PRD-15): git_status, git_diff, git_log,
                           git_show, git_add, git_commit, git_checkout,
                           git_branch, git_stash, git_blame, git_grep; GitToolKit
    exec/__init__.py       5 exec tools (PRD-16): run_bash, run_command,
                           run_python, run_python_expr, run_tests; ExecToolKit
    outlook/__init__.py    9 Outlook/Graph API tools (PRD-17): list/read/send/reply/
                           search/move emails, list_folders, calendar_events,
                           create_event; OutlookToolKit + GraphApiOutlookBackend
```

### Additional Common Pitfalls (PRD-13..19)
| Symptom | Cause | Fix |
|---------|-------|-----|
| `PluginLoadError` on `registry.load(name)` | Plugin not yet discovered | Call `registry.discover()` before `load()` |
| `AgentCapabilityScope.restrict()` expands the allowed set | Expecting union — it's intersection | `restrict()` always returns the MORE restrictive set |
| `ScopeManager.can_spawn()` returns False unexpectedly | Agent already at `max_spawn_depth` | Check `get_depth(agent_id)` vs `scope.max_spawn_depth` |
| `run_bash` timeout leaves zombie processes | `start_new_session=False` | The tool uses `start_new_session=True` and `os.killpg` — verify POSIX platform |
| `WorkspaceView.resolve()` raises `PermissionError` on traversal | Path escapes workspace root | All fs tools catch this and return `{ok: False, error: "permission_denied:..."}` |

## Engineering Discipline

### No legacy code

When implementing a PRD, **remove all code it supersedes**.  Do not keep the
old implementation alongside the new one "for backward compatibility".  This
project is not in production; there are no external consumers of internal APIs.

Concretely:

- If a PRD introduces `WorkflowGraph`, remove `WorkflowDefinition`.
- If a PRD introduces `PhaseNode`, remove `PhaseSpec`.
- If a PRD introduces `DataBus`, remove `WorkflowContext`.
- If a PRD introduces `make_completion_tool`, remove `make_planner_tools` and
  `make_executor_tools`.
- If a PRD introduces a new runner path, remove the old runner path.
- Update every test that references the removed types.

Keeping both paths doubles the surface area, creates `isinstance` branches,
forces `Any` type annotations, and guarantees the old path will rot unnoticed.

**Checklist before marking a PRD complete:**

1. Search for every symbol the PRD replaces and delete them.
2. Remove all `# type: ignore[union-attr]` comments that exist only because of
   legacy union types.
3. Run `grep -rn "OldSymbol" src/` — zero results expected.
4. All tests pass with the new types only.
