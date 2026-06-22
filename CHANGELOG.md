# Changelog

All notable changes to `agenthicc` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Removed — Kernel runtime trio (PRD-128)

- Deleted the unwired `agenthicc.runtime` package — `CommunicationTools`, `AgentPool`,
  `AgentRecord`, and `Scheduler` (`runtime/comm_tools.py`, `runtime/pool.py`,
  `runtime/scheduler.py`). This was the original PRD-01/PRD-03 kernel agent-execution
  layer, superseded by lauren-ai's `AgentRunnerBase` and the `agenthicc.workflows`
  runners. It had zero production consumers: nothing live emitted the events it
  produced, every live `EventProcessor` uses `NoOpEffectExecutor` (so its effects were
  discarded), and `AppState.agents` / `AppState.tasks` were read by no live code. Only
  its own unit/integration tests instantiated it.
- Removed the five tests that exercised the trio: `test_agent_pool.py`,
  `test_comm_tools.py`, `test_scheduler.py`, `test_mcp_connect.py`, and
  `test_runtime_cycle.py`.
- Documentation sync: removed all references to the trio (and to the already-removed
  PRD-116 `agenthicc.workflow` package that lingered alongside it) from CLAUDE.md,
  AGENTS.md, CONTRIBUTING.md, README.md, `llms.txt`, `llms-full.txt`, the `docs/`
  site (deleted `reference/communication-tools.md` and `guides/agents.md`), and the
  shipped reference skills (deleted `skills/writing-agents`; cleaned
  `skills/testing-agenthicc` and `skills/extending-with-hooks`).
- **Deferred to a follow-up (Phase 2):** pruning the now-orphaned kernel reducer
  branches (`_agent_spawn_request`, `_task_created`, `_task_assigned`,
  `_workflow_node_added/removed`, `_agent_status_changed`), the
  `EffectType.spawn_agent` / `assign_task` / `start_workflow_node` members, the
  `AppState.agents` / `AppState.tasks` fields + `AgentInstance` / `Task` dataclasses,
  and the now-inert `agent_pool_size` / `max_parallel_tasks` config fields.

---

## [0.1.0] — 2025-01-01

### Added — Kernel

- `AppState` immutable dataclass — single source of truth for all runtime state
- `EventProcessor` — multi-producer single-consumer asyncio queue; applies pure reducers
  sequentially; append-only JSON-lines event log; periodic snapshot; subscriber notification
- `root_reducer` — pure `(AppState, Event) → (AppState, list[Effect])` with handlers for
  12 event types: `IntentCreated`, `IntentStatusChanged`, `AgentSpawnRequest`,
  `AgentStatusChanged`, `WorkflowCreated`, `WorkflowNodeAdded`, `WorkflowNodeRemoved`,
  `WorkflowNodeStatusChanged`, `TaskCreated`, `TaskAssigned`, `ToolRegistered`,
  `HookRegistered`
- `restore_from_log()` — replays the JSON-lines event log for crash recovery;
  tolerates corrupt trailing lines from mid-write crashes
- Copy-on-write `AppState.with_*()` helpers for zero-mutation state updates
- `Effect` / `EffectType` descriptors for side-effectful actions

### Added — Workflow Engine

- `find_ready_nodes()` — O(n) scan returning all dispatchable pending nodes
- `detect_cycle()` — 3-colour iterative DFS cycle check; used before every node addition
- `topological_sort()` — Kahn's algorithm; raises `CycleError` on cycles
- `DAGExecutor` — concurrent node dispatch via `asyncio.Semaphore`, double-dispatch
  prevention, auto-skip of nodes whose dependencies failed, `run_workflow()` drives
  to terminal state via subscriber-based event loop
- `IntentParser` — regex/heuristic goal extraction (polite-prefix stripping, entity
  extraction, deadline/priority constraint detection)
- `IntentValidator` — capacity check against `max_concurrent_intents`
- `StaticPlanner` — JSON task-array → `NodeSpec` list with single-node fallback
- `LlmPlanner` — runner-injected planner with JSON parse + fallback
- `WorkflowModifier` — atomic node add/remove with DAG integrity guarantees

### Added — Agent Runtime

- `AgentPool` — `asyncio.Queue`-based idle/busy pool with `acquire()` / `release()`
- `Scheduler` — assigns oldest pending task to idle agent via kernel events
- `CommunicationTools` — 9 async methods covering all PRD inter-agent communication:
  `agent_spawn`, `agent_send_message`, `task_create`, `task_assign`,
  `workflow_modify` (with inline cycle guard), `application_log`, `application_ui_update`,
  `tool_define`, `hook_register` — every method emits kernel events, never mutates state

### Added — Tool Execution Layer

- `Tool` ABC with `execute(args, ctx) → Any`
- `ToolResultEnvelope` dataclass — unified ok/error result with duration_ms
- `LifecycleHook` ABC — `on_before → Rejection`, `on_after`, `on_error → RecoveryAction`
- `RecoveryAction` enum: `RETRY`, `FALLBACK`, `ESCALATE`, `SKIP`
- `HookRegistry` and `HookRunner` — parallel hook gather via `asyncio.gather`
- `LaurenToolHookAdapter` — bridges agenthicc hooks into lauren-ai `ToolHook` protocol
- `load_hook_from_dotpath()` — importlib-based dynamic hook loading
- `AgenthiccToolExecutor` — permission → before-hooks → `asyncio.wait_for` execution →
  after-hooks → error-hooks (RETRY honored once) → ToolCallStarted/Complete events
- `execute_parallel()` — fan-out up to `tool_call_budget`, extras get budget_exceeded
- `WorkspaceView` — `os.path.realpath`-based path-prefix sandbox; blocks traversal,
  symlink escape, and absolute escapes
- `NetworkGuard` — domain allow-list with subdomain matching

### Added — Memory Architecture

- `SessionMemoryLayer` — in-process LRU+TTL cache (CPython-safe lock-free reads,
  serialised writes via `asyncio.Lock`)
- `ProjectMemoryLayer` — SQLite key-value store (namespaced) + content-addressed artifact
  table (sha256 hex artifact IDs)
- `GlobalMemoryLayer` — user-wide SQLite at `~/.agenthicc/global.db`
- `MemoryRouter` — tier routing with permission checker; `read`, `write`,
  `publish_artifact`, `read_artifact`
- `SemanticIndex` — cosine-similarity bag-of-words vector search (zero extra deps)

### Added — TUI

- `TranscriptModel` — pure Python rendering model: labeled agent turn blocks,
  tool call entries with `ToolCallState` (pending → running → success/failure),
  Braille spinner frames, cost/token footer
- `diff_lines()` — `difflib.SequenceMatcher`-based line diff (keep/add/remove)
- `TUIEventAdapter` — maps kernel events to `TranscriptModel` mutations;
  `subscribe_to()` + `sync()` for incremental replay; `consume(queue)` for live feed
- `render_frame_ansi()` — positions input bar unconditionally on the last terminal row
  (`rows`); menu overlay anchored above status line; never displaces input bar
- `build_app()` — prompt_toolkit `Application` with `HSplit` layout; slash-command
  detection; `ConditionalContainer` menu overlay; headless JSON-lines fallback
- `INPUT_PROMPT = "> "` — consistent input prefix across TUI and headless

### Added — Configuration & Security

- `load_config()` — deep-merge TOML from `agenthicc.toml` + `~/.agenthicc.toml`;
  user config overrides project config; graceful missing-file handling
- `AgenthiccConfig` — typed dataclass hierarchy: `ExecutionSettings`, `MemorySettings`,
  `SecuritySettings`, `ApiSettings`, `ToolSettings`
- `PermissionChecker` — fnmatch pattern matching against `SecurityPolicy.permission_rules`;
  fail-closed default; path-prefix and network-domain condition matching

### Added — Headless API

- `create_app()` — FastAPI application with lifespan-managed `EventProcessor`
- `POST /v1/intents` — submit intent text; returns `intent_id`
- `GET /v1/intents/{intent_id}` — poll status; 404 on unknown
- `GET /v1/state/summary` — agent/workflow/intent counts + last event type
- `WebSocket /v1/ws` — streams state summary JSON on every kernel event
- API key authentication via `Authorization: Bearer` header

### Added — lauren-ai Integration

- Signal bridge pattern: `SignalBus` handlers convert `lauren_ai` lifecycle signals
  (`ModelCallStarted`, `ToolCallStarted`, `ToolCallComplete`, `AgentRunComplete`)
  into agenthicc kernel events
- `_build_runner_for_agent()` from `lauren_ai.testing` used to resolve tools and
  connect runners in tests
- Three E2E tests using real `AgentRunnerBase` + `MockTransport` against the kernel

### Added — Tests

- 276 tests across 26 files (15 unit, 7 integration, 4 e2e)
- Hypothesis property-based testing for reducer purity and determinism
- pyte vt100 emulator TUI tests verifying input bar position on last row
- FastAPI `TestClient` + WebSocket integration tests
- Full Argon2 refactor E2E scenario: 3 parallel agents, test failure, debugger recovery

---

[Unreleased]: https://github.com/agenthicc/agenthicc/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/agenthicc/agenthicc/releases/tag/v0.1.0
