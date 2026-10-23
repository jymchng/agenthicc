# Changelog

All notable changes to `agenthicc` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed — required quality gates (PRD-250 WS-1)

- One CI workflow, `.github/workflows/ci.yml`, runs every gate — lint and
  format, mypy, the type ratchet, `llms_check`, a strict docs build, unit and
  integration tests on Python 3.11–3.13, E2E and performance tests, a Windows
  job for the lock, filesystem and terminal code, and branch-and-subprocess
  coverage — and ends in one aggregate **Required checks** job.
  `scripts/apply_branch_protection.sh` makes that check and code-owner review
  required on `main`; it is not applied automatically.
- Releases re-run that CI for the tagged commit, require the tag to name the
  package version, and check that the built wheel's `agenthicc --version`
  reports it. `agenthicc --version` now reads the installed distribution's
  version instead of a literal.
- `mypy src/agenthicc` is clean. Six bundled workflow runners that subclass
  `CodePlanRunner` carry a module-level `override` suppression with an owner
  (PRD-250 WS-4.2) and an expiry date that `type_audit` enforces.
- `scripts/type_audit.py` holds the baseline in both directions — a stale
  baseline fails until it is lowered with `--update` — rejects a pull request
  that raises a baseline value (`--baseline-ref`), and counts module-level
  `# mypy:` suppressions. The baseline was lowered to the measured debt, and
  `getattr` calls fell from 716 to 500 through typed access.
- Nox sessions run on the uv backend, so each tool runs at its declared
  version inside the session's virtualenv; previously a session could run a
  copy that happened to be on `PATH`, or none at all on a clean runner. The two
  `dev` dependency sets are one `dev` extra that also declares `twine` and the
  book/screenshot test dependency; core/extra duplicates were removed.
- Coverage measures branches and CLI/worker subprocesses, and the floor is
  `[tool.coverage.report] fail_under`. Pre-commit hooks run the project's own
  ruff, mypy, type ratchet and unit tests. `.github/CODEOWNERS` owns the
  quality baselines and gate definitions.
- Tests that start a fresh interpreter — a CLI command or a detached worker —
  take their wall-clock ceiling from `tests/cli_process.py`, and each test's
  `pytest-timeout` covers all of its waits. The 15- and 30-second ceilings they
  replace expired under traced coverage on a loaded host, and some longer
  ceilings could never apply under the 60-second default. The coverage session
  gives a test without its own mark three times that default, because tracing
  roughly triples CPU time.

### Fixed — verified defects (PRD-250 D-01–D-10)

- Project-memory SQLite connections are closed; fire-and-forget tasks are owned
  by a `SupervisedTasks` supervisor that logs failures and drains on shutdown;
  the kernel processor keeps no unbounded event list (use `add_sink`), rejects
  undeclared events with a published `KernelEventRejected`, counts dropped
  notifications, writes its log off the event loop, drains with
  `Queue.join()`, and stops after the events already emitted (cancelling
  `run()` now always stops it, including on Python 3.11); every store uses one
  fail-closed `InterProcessLock`; plugin
  dependencies are read statically so each plugin imports once; reactive
  subscriber failures are logged with the signal name and `Computed.dispose()`
  releases subscriptions; bugbear and async findings are fixed and the `B`,
  `ASYNC`, `RUF006` and `RUF100` rules are enabled.
- A workflow checkpoint now restores nested `design_tokens` losslessly, and the
  base reconstruct-site runner declares the re-entry hooks it calls.

### Security — trust gaps (PRD-250 S-01–S-03)

- Skill `` !`command` `` placeholders are never executed when a skill loads; the
  agent is asked to run each command through its command tool, where approvals,
  the mode ceiling and the sandbox apply.
- Every Python extension loader — tools, commands, workflows, modes, agents and
  CLI plugins — imports a project file only when `~/.agenthicc/trust.json`
  holds a record of its current SHA-256. Manage trust with `agenthicc trust
  list`, `add` and `revoke`. The old CLI-only trust manifest and audit file are
  removed.
- Settings that nothing read were deleted instead of left inert, including the
  `[api]`, `[hooks]` and `[storage]` sections, `[memory]`'s storage keys,
  `security.sandbox_mode` and most `[plugins]` keys; a test asserts every
  remaining field has a reader. The undeclared `execution.base_system_prompt`
  and mention-budget reads were removed.

### Added — hermetic cassette replay

- `run_headless_replay(..., hermetic=True)` replaces side-effecting built-in
  tools with schema-identical stubs inside the tool registry
  (`override_builtin_tools`, `hermetic_builtin_groups`) and owns a durable
  workflow handle, so a recorded `goal_flow` campaign replays as a regression
  test without running any recorded command.

### Added — session-scoped recurring `/loop` prompts (PRD-193)

- Added the interactive `/loop [interval] <prompt-or-registered-command>`
  scheduler with idle-safe dispatch, one pending iteration while busy, and no
  concurrent turns.
- Added `/loop status`, `/loop pause`, `/loop resume`, and `/loop stop`, with
  finite lifetime, bounded failure backoff, replacement, and explicit resume
  semantics after process restart.
- Persisted loop definitions under each session's `loop.json` with atomic
  owner-only writes; scheduled turns reuse the normal conversation,
  checkpoint, approval, permission, and security paths.
- Documented configuration, command syntax, session storage, troubleshooting,
  headless behavior, and operational limits across the README and MkDocs usage
  and reference pages.
- Added `/loops`, a table-based schedule-job manager with immediate run-now,
  owner-safe deletion, keyboard navigation, and delete confirmation.

### Changed — queued input during tool execution

- Ordinary messages queued while the agent is running are now delivered after
  the current tool batch completes, before the next model request. Queued slash
  commands and skills retain FIFO local routing when the run becomes idle.

### Documentation — repository map and improvement roadmap

- Rewrote the README, contributor/agent guidance, and MkDocs landing page to
  match the current Rich workspace, headless stdin runner, kernel, workflows,
  tools, and persistence layers.
- Added configuration, security, extensions, testing, CLI, storage, and
  contributor documentation, plus PRD-138 covering the remaining repository
  improvements and implementation/documentation drift.
- Marked the historical REST API and prompt-toolkit TUI references as planned
  or compatibility material until their source packages exist again.
- Added an evidence-backed current repository-state reference, refreshed
  PRD-138's baseline, reconciled the hook/executor adapter documentation, and
  added orphaned guides and references to the MkDocs navigation.

### Added — Configurable session memory budget

- `[execution] session_memory_max_tokens` (default 32000) sets the token budget of
  the session conversation memory (`ShortTermMemory`).  Settable from
  `agenthicc.toml` and the CLI (`--set execution.session_memory_max_tokens=N`), and
  honoured by both the interactive session and the workflow runners.

### Added — Conversation durability & resumable execution (PRD-129)

- **Phase 1 — idempotent tools.** A turn-scoped `IdempotencyLedger` (lauren-ai)
  replays a tool result across a transport-retry rollback instead of re-executing
  it, so side-effecting tools (`write_file`, `run_bash`, `git_commit`) apply once.
  Replay is rollback-scoped (a `promote`/`committed`/`pending` model), so a
  legitimate repeat call within one attempt still runs live; replayed results are
  rekeyed to the current `tool_use_id`.
- **Fixed** a provider 400 (`tool_result ... must have a corresponding tool_use
  block`) introduced by the first-cut ledger: it deduped a repeat `(name,input)`
  call within a single attempt and replayed a stale `tool_use_id`.
- **Phase 2 — durable journal.** `ShortTermMemory` is now a projection of an
  append-only, `fsync`-ed `ConversationJournal`; every transition is durable, so a
  crash mid-turn no longer loses the in-flight turn, and `--resume` folds the
  journal back into memory. Removed the superseded SQLite `conversation_store.py`.
- **Phase 3 — resumable execution.** Turn-lifecycle markers + a
  `DurableIdempotencyLedger` + a `RunCoordinator` detect a turn interrupted by a
  hard crash and re-drive it from where it left off, replaying already-completed
  tools. Defers to the PRD-94 workflow resume when a workflow run is in progress.

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
