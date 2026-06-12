# Architecture

This document explains why Agenthicc is designed the way it is, the layered
structure of the codebase, and how the major subsystems fit together.

---

## Layer diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        User / Operator                          │
│            TUI (prompt_toolkit)  │  Headless API (FastAPI)      │
└─────────────────────┬───────────────────────────────────────────┘
                      │  Event.create("IntentCreated", ...)
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                    EventProcessor (MPSC queue)                  │
│   emit → queue → run loop → root_reducer → AppState snapshot   │
│                           → effects   → EffectExecutor         │
│                           → JSON-lines log (persist=True)       │
│                           → subscriber fan-out                  │
└─────────────────────┬───────────────────────────────────────────┘
                      │  AppState (immutable, frozen)
                      ▼
┌──────────────┬───────────────────┬──────────────────────────────┐
│ DAGExecutor  │ IntentParser      │ WorkflowModifier             │
│ (asyncio     │ IntentValidator   │ (cycle-guarded add/remove)   │
│  Semaphore)  │ StaticPlanner     │                              │
│              │ LlmPlanner        │                              │
└──────┬───────┴───────────────────┴──────────────────────────────┘
       │  CommunicationTools (9 async methods)
       │  All side-effects go through processor.emit(...)
       ▼
┌──────────────────────────┬──────────────────────────────────────┐
│  AgentPool (idle/busy)   │  Scheduler (assign_next)             │
│  AgentRunnerBase         │  SignalBus bridge (lauren-ai)        │
└──────────────────────────┴──────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────┐
│  AgenthiccToolExecutor                                           │
│  PermissionChecker → HookRunner(pre) → execute → HookRunner(post)│
│  asyncio.timeout deadline; retry/fallback on HookRunner(error)   │
└──────────────────────────┬───────────────────────────────────────┘
                           │
               ┌───────────┴────────────┐
               │                        │
               ▼                        ▼
    WorkspaceView (sandbox)    NetworkGuard (domain allow-list)
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  MemoryRouter                                                    │
│  session:  →  SessionMemoryLayer (LRU + TTL, in-process)        │
│  project:  →  ProjectMemoryLayer (SQLite + artifacts)           │
│  global:   →  GlobalMemoryLayer  (SQLite, cross-project)        │
└──────────────────────────────────────────────────────────────────┘
```

---

## Event sourcing

Every change to application state flows through this sequence:

1. A producer calls `await processor.emit(event)`.
2. The event is placed on the `asyncio.Queue`.
3. The single `run()` task dequeues the event and calls
   `new_state, effects = root_reducer(state, event)`.
4. `new_state` replaces `_state`; the event is appended to `_event_log`.
5. If `persist=True`, the event is written as a JSON line to `event_log_path`.
6. `new_state` is pushed to all subscriber queues.
7. Each effect is scheduled as a background `asyncio.ensure_future` task.

**Why event sourcing?**

- **Crash recovery**: `restore_from_log` replays the log line by line to rebuild
  state. Corrupt trailing lines (from a mid-write crash) are skipped.
- **Audit trail**: every inter-agent message, task creation, and tool registration
  is a first-class event in the log. You can replay any point in time.
- **Testability**: `root_reducer` is a pure function — no IO, no async, no side
  effects. Unit tests are synchronous and deterministic.
- **Snapshot compaction**: when `events_since_snapshot` exceeds
  `SystemSettings.snapshot_every_n_events`, a lightweight snapshot is written to
  `snapshot_path`. Long-running sessions do not need to replay the full log.

---

## Why tool-only communication

Agents must communicate exclusively through `CommunicationTools`. Direct object
references are forbidden. This constraint has three benefits:

1. **Auditability**: every inter-agent interaction — message, task creation, spawn
   — emits a kernel event. The full communication graph is in the event log.
2. **Isolation**: agents cannot break each other by holding stale references.
   The only shared mutable state is the kernel queue.
3. **Replay**: because all communication is events, you can replay a run exactly
   and observe what would have happened with different reducers or planners.

---

## DAG execution

The `DAGExecutor` drives a workflow to completion:

1. Call `find_ready_nodes(workflow)` — returns all `pending` nodes whose every
   dependency is `complete`. O(n) scan.
2. Acquire a `Semaphore(max_parallel_tasks)` slot for each ready node.
3. Launch each node as an `asyncio.Task` via `_build_runner_for_agent`.
4. When a task completes (success or failure), release the semaphore slot and
   call `find_ready_nodes` again.
5. Repeat until no nodes are `pending` or `running`.

The `asyncio.Semaphore` is the primary back-pressure mechanism. Set
`max_parallel_tasks` in `agenthicc.toml` or `SystemSettings` to control
concurrency.

**Cycle detection** happens at two points:
- In `WorkflowModifier.add_node` / `CommunicationTools.workflow_modify` (before
  emitting the event).
- In `topological_sort` (used by planners building initial workflow DAGs).

Both use the same three-color iterative DFS from `detect_cycle`.

---

## Memory tiers

The three memory tiers have different performance and durability characteristics:

| Property | Session | Project | Global |
|---|---|---|---|
| Backend | In-process dict | SQLite (WAL) | SQLite (WAL) |
| Durability | Lost on restart | Survives restarts | Survives restarts |
| Speed | ~1µs | ~100µs | ~100µs |
| Scope | Single process run | One project | All projects |
| Eviction | LRU + TTL | Manual `delete` | Manual `delete` |

`MemoryRouter` routes by key prefix so agents use a single interface without
knowing which tier they're reading from. The prefix contract is part of the
agent's domain design — not an infrastructure detail.

---

## TUI update pipeline

The TUI update pipeline is deliberately separated from the kernel:

```
EventProcessor  →  subscriber Queue  →  TUIEventAdapter.consume()
                                                ↓
                                        apply(state) → TranscriptModel mutations
                                                ↓
                                        app.invalidate() → prompt_toolkit re-render
```

`TranscriptModel` is pure Python with no terminal dependencies. It holds the
logical state of the transcript. `render()` converts it to a list of plain strings.
The prompt_toolkit `Application` reads from `TranscriptModel` via
`FormattedTextControl(render_transcript)` — a pull-based model that re-renders
on every `invalidate()` call.

This separation means:
- `TranscriptModel` is fully unit-testable without a terminal.
- `render_frame_ansi` can produce an ANSI snapshot for e2e tests using pyte.
- The TUI can be swapped for a different renderer (e.g. a web UI) without touching
  the kernel.

---

## Security model

Security is layered:

1. **`PermissionChecker`** — evaluated before every tool call. Uses `fnmatch`
   patterns from `SecurityPolicy.permission_rules`. First match wins; default
   action is `"deny"` (fail-closed).
2. **`WorkspaceView`** — all file tool operations are restricted to paths under
   the workspace root. `resolve()` follows symlinks and rejects escapes.
3. **`NetworkGuard`** — all network tool operations check the URL's `netloc`
   against the `allow_list`. An empty list blocks all hosts.
4. **`asyncio.timeout`** in `AgenthiccToolExecutor` — every tool call has a
   deadline (`Tool.timeout_seconds`). Timed-out calls are recorded in the envelope
   but do not crash the executor.
5. **Hook pipeline** — `pre_execute` hooks can reject calls before the tool runs;
   `on_error` hooks can choose `RecoveryAction.abort` to prevent retries on
   security-sensitive failures.

The security model is fail-closed: when no permission rule matches, the default
action is `"deny"`. This means adding a new tool requires an explicit `"allow"`
rule rather than relying on a default-open policy.
