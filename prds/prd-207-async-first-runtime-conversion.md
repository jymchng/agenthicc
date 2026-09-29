# PRD-207 — Async-First Runtime and Non-Blocking Operations

**Status:** Proposed  
**Type:** Architecture / Runtime / Reliability  
**Repository:** `jymchng/agenthicc`  
**Date:** 2026-09-29  
**Related work:** PRD-138, PRD-140, PRD-148, PRD-149, PRD-150, PRD-151, PRD-169, PRD-170, PRD-171, PRD-176, PRD-182, PRD-203, PRD-204, PRD-206

## 1. Executive summary

Agenthicc already has an asynchronous execution spine: agent turns, workflow
runners, the kernel event processor, session service, MCP lifecycle, and most
agent-facing tools are async. The repository is not yet async-first end to end,
however. Several code paths perform synchronous filesystem, JSONL, SQLite,
subprocess, Git, process-lock, terminal, and provider-adapter operations from
inside the runtime event loop. Other paths expose synchronous public methods
that either block the caller or create nested event loops with `asyncio.run()`.

This PRD defines a repository-wide migration to an **async-first operational
contract**:

> Every operation that can perform blocking I/O, wait for an external actor,
> spawn or inspect a process, acquire a cross-process lock, or execute a
> potentially expensive external integration must be awaitable and must not
> block the Agenthicc event loop. Every runtime-facing operational API must
> have an async contract. Pure deterministic computation remains synchronous by
> design.

The work is not a mechanical conversion of every `def` into `async def`.
Making reducers, dataclass constructors, parsers, validators, and render
functions coroutines would make the architecture harder to use and would not
remove blocking work. The implementation must classify every synchronous
function, migrate blocking work to native async APIs where practical, and use a
bounded thread/process adapter where a dependency cannot be made asynchronous.

The result must have one event-loop boundary at process entry, explicit async
lifecycles, cancellation-safe durability, backpressure, bounded executor
usage, and compatibility shims that cannot silently block an active loop.

## 2. Problem statement

Agenthicc is a long-lived, interactive, durable agent runtime. A single
foreground session may concurrently service:

* provider streaming and retries;
* workflow phase transitions and checkpoints;
* MCP/browser/network tools;
* background-session supervision;
* terminal subprocesses;
* TUI input, animation, overlays, and transcript rendering;
* kernel event reduction and durable persistence;
* session-service HTTP/SSE clients;
* memory, journal, usage, and recovery writes.

Any synchronous operation on the event-loop thread can pause all of those
activities. The user-visible failures include:

* TUI input and animation becoming unresponsive while a file, Git, or process
  operation runs;
* delayed cancellation and `Ctrl+C`/Esc handling;
* missed provider deadlines and avoidable transport timeouts;
* background jobs appearing frozen while a store or supervisor call blocks;
* event and checkpoint durability being coupled to an unrelated slow operation;
* nested `asyncio.run()` failures when a synchronous CLI adapter is called from
  an already-running loop;
* unbounded use of the default executor, causing unrelated sessions to starve;
* partial writes or lock ownership being left ambiguous when cancellation
  occurs during a synchronous persistence operation.

The repository also has several legitimate synchronous boundaries. The current
architecture explicitly requires pure reducers and value-level state
transforms to remain synchronous, and terminal backends must isolate platform
specific raw reads. The problem is therefore not the number of `def` keywords;
it is the absence of a repository-wide, mechanically enforceable distinction
between pure synchronous code and blocking synchronous code.

## 3. Repository study and baseline

The audit covered the complete `src/agenthicc` tree, the workflow registry,
runtime entry points, persistence layers, tools, TUI, background/session
services, worktrees, and test layout. At the time of this PRD:

* `src/agenthicc` contains approximately 283 Python files;
* `tests/` contains approximately 346 test files;
* a repository-wide textual inventory found approximately **3,230 synchronous
  definitions and 912 asynchronous definitions**. This is a directional grep
  count, not a public-API count: it includes nested functions, compatibility
  helpers, tests/examples, and generated/reference code. Phase 0 must replace
  it with an AST inventory and classification report;
* the runtime already uses one async implementation for agent turns, workflow
  `run`/`resume`, MCP lifecycle, filesystem-agent-tool execution, session
  service HTTP handlers, and most TUI control loops;
* the kernel `EventProcessor` is async at its queue/effect boundary but still
  performs synchronous JSONL open/write/flush/snapshot/restore work;
* journals, session-service stores, background stores, workflow checkpoint
  stores, run stores, worktree stores, session logs, process leases, and several
  discovery/configuration paths perform synchronous filesystem or locking work;
* worktree/runs code invokes `subprocess.run()` synchronously; some callers
  compensate with `asyncio.to_thread`, but the contract is not uniform;
* CLI dispatch contains synchronous handlers, an `asyncio.run()` compatibility
  path, and at least one plugin command that calls `subprocess.run()` directly;
* TUI rendering and key decoding contain intentional platform-level sync code,
  while the async workspace adapters already offload blocking key reads;
* memory SQLite layers already use `asyncio.to_thread` for many operations,
  providing a useful adapter pattern but not yet a repository-wide policy;
* `pyproject.toml` targets Python 3.11+, already depends on `anyio`, `httpx`,
  and `aiohttp`, and configures strict-ish mypy and pytest-asyncio auto mode;
* the current full suite baseline before this PRD-only change was 3,995 passed,
  15 skipped, and 4 warnings. The migration must preserve or improve that
  baseline.

The inventory must be rerun after each migration phase. Counts alone are not a
completion criterion: each synchronous definition must have a reason code and
an owner.

## 4. Goals

### G1 — Non-blocking event-loop runtime

No blocking filesystem, process, network, database, lock, sleep, terminal, or
external-library call may execute directly on an Agenthicc event-loop thread.

### G2 — Async operational APIs

Runtime-facing stores, managers, supervisors, tools, lifecycle objects, service
operations, and CLI command execution must expose awaitable methods with clear
return and cancellation contracts.

### G3 — One event-loop boundary

Each process mode has one explicit `asyncio.run()` boundary at process entry.
Internal code must never call `asyncio.run()` to reach an async helper.

### G4 — Durable async persistence

Event logs, conversation journals, checkpoints, background state, session
service events, usage ledgers, and recovery metadata must preserve ordering,
atomicity, fsync policy, idempotency, and crash recovery while allowing the
event loop to service other work.

### G5 — Responsive cancellation and deadlines

Cancellation, timeout, retry, shutdown, and ownership-lease semantics must be
defined at every async boundary and must not be delayed by a synchronous call.

### G6 — Bounded resource usage

Thread and process offloads must use explicit bounded executors or semaphores,
carry context and deadlines, and expose saturation metrics.

### G7 — Compatible migration

Existing workflows, tools, plugins, headless mode, TUI mode, session service,
background jobs, generated workflow artifacts, and supported integrations must
continue to work during the migration. Compatibility shims must be explicit,
documented, and temporary.

### G8 — Verifiable async contract

Static checks, runtime guards, integration tests, cancellation tests, and
latency benchmarks must prevent regressions in which a new synchronous call is
introduced into the async runtime.

## 5. Non-goals

This PRD does not require:

* making pure functions asynchronous merely because they are in an async
  module;
* replacing asyncio with another runtime;
* rewriting third-party libraries that have no async API;
* making CPU-bound Python code magically non-blocking without a worker pool;
* changing the kernel's pure reducer/event model into an async reducer;
* making terminal byte decoders or Rich/Text rendering awaitable;
* adding a new database or message broker;
* distributed execution across machines;
* changing provider retry policy, cache contracts, or workflow topology except
  where required to preserve async lifecycle and cancellation semantics;
* making user-authored synchronous scripts impossible to run. Such scripts
  receive an async `main_async()` contract plus a clearly documented sync CLI
  shim during the compatibility period.

## 6. Definitions and classification rules

### 6.1 Operational function

An operational function performs or initiates I/O, external interaction,
waiting, process management, lock acquisition, persistence, network access,
provider/tool execution, or lifecycle ownership. Operational functions in a
runtime-facing class must be `async def`, or must be a synchronous adapter
explicitly documented as running outside the event loop.

Examples:

```python
async def append(event: SessionEvent) -> None: ...
async def list_sessions(...) -> list[SessionSnapshot]: ...
async def run_command(...) -> CommandOutcome: ...
async def create_worktree(...) -> WorktreeRecord: ...
async def close() -> None: ...
```

### 6.2 Pure synchronous function

A pure synchronous function has no I/O, waiting, mutable external state, or
observable scheduling requirement. It is deterministic for its inputs, or is a
small in-memory value operation whose latency is bounded by the supplied data.

Allowed examples:

* reducer functions and event-to-state transitions;
* dataclass/value-object constructors and `to_dict`/`from_mapping` conversion;
* validation, redaction, schema parsing, and command argument parsing;
* workflow topology resolution and phase metadata calculation;
* transcript line formatting and Rich/Text rendering functions;
* terminal byte decoding after bytes are already available.

Pure sync code must not call an operational sync helper indirectly.

### 6.3 Adapter-bound synchronous function

A synchronous third-party API, OS primitive, or legacy implementation may be
retained behind an explicit adapter. The adapter must be invoked only through a
bounded worker boundary (`asyncio.to_thread`, `anyio.to_thread.run_sync`, an
owned `ThreadPoolExecutor`, or a process pool for CPU-heavy work). The async
caller owns the timeout and cancellation policy; the adapter must not pretend
that a thread interruption cancels the underlying operation.

### 6.4 Construction versus opening

Python constructors cannot await. Objects that need filesystem, database,
process, provider, or network setup must use an async factory:

```python
store = await SessionEventStore.open(root)
manager = await WorktreeManager.open(repository)
session = await SessionRuntime.create(config)
```

`__init__` may validate and assign in-memory values only. Existing sync
constructors may remain as inert compatibility constructors while setup moves
to `open()`/`create()`.

## 7. Current-to-target architecture

### 7.1 Target data flow

```text
CLI / TUI / HTTP / headless entry point
                  │
                  ▼
          one async process boundary
                  │
                  ▼
           async session runtime
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
  AgentTurn   Workflow   Kernel/EventProcessor
        │         │         │
        └────┬────┴────┬────┘
             ▼         ▼
      async tools    async durability
             │         │
     ┌───────┼─────────┼────────┐
     ▼       ▼        ▼        ▼
   HTTP    MCP     files    subprocess/Git
     │       │        │        │
     └───────┴────────┴────────┘
          native async or bounded adapter
```

### 7.2 Ownership rules

* The kernel owns durable event ordering and remains the source of truth.
* Reducers remain pure synchronous functions.
* Runtime components own async lifecycle and must expose `close()`/`aclose()`
  where they own a file, connection, task, process, subscription, executor, or
  lease.
* A caller that starts an async resource is responsible for closing it in a
  `finally`/async context manager path.
* No TUI projection, workflow, tool, or background service may bypass the
  canonical async store to write a parallel state file.
* A worker adapter must not hold a process/thread lock while awaiting unrelated
  work.

## 8. Functional requirements

### FR-001 — Complete AST inventory and classification

Create a Phase-0 inventory tool that walks all first-party Python files under
`src/agenthicc`, scripts, workflow artifacts, and supported plugin surfaces.
For every function/method/constructor containing synchronous code, record:

* fully qualified name and file/line;
* public/private and runtime/test/reference classification;
* whether it is pure, operational, adapter-bound, platform-bound, or generated;
* blocking calls and transitive callees;
* target contract and migration owner;
* planned removal/deprecation version if a sync compatibility shim remains.

The report is committed as a generated audit artifact or reproducible report,
not manually maintained prose.

### FR-002 — Async runtime contract

Define repository-wide typing protocols for operational boundaries, including
as applicable:

```python
class AsyncEventStore(Protocol):
    async def append(self, event: SessionEvent) -> None: ...
    async def read(self, session_id: str, ...) -> list[SessionEvent]: ...

class AsyncCommandRunner(Protocol):
    async def execute(self, request: CommandRequest) -> CommandOutcome: ...

class AsyncResource(Protocol):
    async def close(self) -> None: ...
```

Protocols must describe cancellation, timeout, ordering, and error behavior;
they must not simply annotate an existing blocking implementation as async.

### FR-003 — Single process entry boundary

Add an `async_main()`/`amain()` entry contract for CLI, TUI, headless, and
background worker modes. `__main__.main()` may remain a tiny process-level sync
shim that parses only enough startup arguments and calls one `asyncio.run()`.

Internal modules must not call `asyncio.run()`, create an event loop, or use
`run_until_complete()`. Existing nested-loop paths in CLI dispatch, session
commands, goal runs, workflow smoke helpers, and process shutdown must be
removed or moved outside the active runtime.

### FR-004 — Async durable file substrate

Implement a shared async persistence substrate for append, replay, atomic
replace, fsync, directory fsync, bounded reads, and corruption handling. It may
use a bounded thread adapter initially, but the adapter must be centralized and
observable rather than each module calling `open()`/`os.write()` ad hoc.

It must provide:

* append ordering per stream;
* cross-process serialization;
* atomic replacement for snapshots/indexes;
* cancellation-safe commit points;
* explicit durability levels (`memory`, `flush`, `fsync` where supported);
* bounded record and file sizes;
* replay behavior for truncated final records;
* crash recovery tests.

### FR-005 — Async kernel persistence

Refactor `kernel/processor.py` so `EventProcessor.run`, `emit`, `drain`,
snapshot persistence, and log restoration never open/write/read directly on
the event-loop thread. Preserve reducer synchrony and the existing MPSC queue,
ordering, effect execution, and startup-before-drain invariant.

`restore_from_log` must not be an `async def` containing a synchronous full-file
read. It must use the shared substrate and yield between bounded replay chunks
for large logs.

### FR-006 — Async journals, checkpoints, ledgers, and logs

Migrate the operational methods of:

* `memory/journal.py`;
* conversation/session journals;
* workflow checkpoint stores and recovery projections;
* usage ledger persistence;
* `tui/runtime/session_log.py` and session export;
* durable run and background-session stores;
* process leases and ownership records;
* discovery caches and generated indexes.

Each must have async read/write/open/close methods, explicit serialization, and
tests for interruption during append, replay, compaction, and atomic replace.
Pure serialization and parsing helpers may remain synchronous.

### FR-007 — Async memory and SQLite lifecycle

Make `SessionMemoryLayer`, `ProjectMemoryLayer`, `GlobalMemoryLayer`, and
`MemoryRouter` operational methods consistently async. Constructors must not
open a database or initialize schema. Provide `await layer.open()` and
`await layer.close()`/async context management.

SQLite access must use a documented strategy: an async-capable driver, or a
bounded thread adapter with one connection/transaction ownership rule. No
SQLite call, migration, VACUUM, or lock wait may run directly on the event loop.

### FR-008 — Async background sessions and terminals

Convert background store/supervisor/worker control-plane operations to async
contracts, including list/status/cancel/retry/resume/delete/archive, process
inspection, event persistence, log access, ownership claims, and cleanup.

Subprocesses must use async process APIs where available. Any unavoidable
`/proc`, `os.kill`, or platform-specific call must be isolated and bounded.
Polling must use `await asyncio.sleep()` and be cancellation-aware; no
`time.sleep()` may execute in an async runtime.

Terminal input/output adapters must preserve current platform behavior while
ensuring blocking reads and writes are behind the terminal executor boundary.

### FR-009 — Async Git and worktree operations

Make all operational `runs/` and `worktrees/` methods awaitable:

* resolve repository, branch, HEAD, cleanliness, and base commit;
* create, inspect, validate, integrate, rebase, recover, and remove worktrees;
* persist orchestration manifests and worker results.

Use an async subprocess runner with process-group cancellation, bounded stdout/
stderr, deadlines, and structured `CommandOutcome`. A temporary thread adapter
is acceptable only for a legacy implementation and must not remain the public
contract. Preserve PRD-203 invariants: immutable base commit, worker isolation,
conflict preservation, and safe cleanup.

### FR-010 — Async tools and integrations

Every agent-facing tool must have an async `execute` boundary. Audit and fix:

* filesystem and workspace access;
* command execution and build/server lifecycle;
* Git/worktree tools;
* HTTP/network clients and policy checks;
* MCP connect/disconnect/reload/discovery/tool calls;
* Playwright/CloakBrowser lifecycle and screenshots;
* TUI screenshot and terminal tools;
* skills installation and plugin operations;
* subagent dispatch and result collection.

Sync third-party SDKs may be adapted through the shared bounded executor. Tool
timeouts, retries, approval decisions, result normalization, and idempotency
must remain unchanged unless explicitly documented.

### FR-011 — Async workflow and phase lifecycle

All workflow runner operations, phase entry/exit, checkpoint writes, artifact
publication, agent-turn invocation, approval/question waits, subagent joins,
and worker orchestration must be awaitable. `PhaseSpec`, topology resolution,
context serialization, and pure validation remain sync.

Every workflow must preserve:

* phase state and history across resume;
* transition-only-via-tool-call behavior;
* shared conversation and cache contracts;
* checkpoint-before-boundary semantics;
* resumability after provider/tool/transport errors;
* cancellation and partial-artifact preservation.

Generated workflows from `create_workflow` must receive the same async runner
contract and must not generate blocking phase methods. The authoring prompts and
validation tools must teach and check this contract.

### FR-012 — Async session construction and shutdown

Refactor session creation so resources are opened asynchronously and closed in
reverse ownership order. Session startup must not do synchronous filesystem,
provider, MCP, browser, journal, or registry work after the event loop begins.

Shutdown must:

1. stop accepting new work;
2. cancel child tasks with a deadline;
3. flush durable queues according to durability policy;
4. close provider/MCP/browser/process resources;
5. release session/workflow/process leases;
6. close executor pools and HTTP servers;
7. report unresolved resources without hanging indefinitely.

### FR-013 — Async CLI command dispatch

The CLI registry must expose an `async _call_async(...)` path that awaits
async handlers and runs legacy sync handlers only in an explicit worker
boundary. `main` must call the dispatcher from the single process async
boundary.

Commands that perform I/O or process work must be converted to async handlers.
Argument parsing, help generation, command discovery, and pure formatting may
remain synchronous. Plugin command contracts must declare whether a sync handler
is pure or legacy blocking; unclassified blocking handlers fail validation.

### FR-014 — Async session service and HTTP/SSE transport

`SessionService` must not materialize event streams, metadata, or indexes using
synchronous store calls while holding its async lock. Its async API must await
the store and release the service lock around slow I/O where correctness
permits.

HTTP/SSE handlers must remain async, propagate client disconnect cancellation,
bound stream queues, close subscriptions, and avoid blocking redaction,
serialization, or export on the loop for unbounded payloads. Large projections
must be paginated or offloaded with a size limit.

### FR-015 — TUI responsiveness boundary

Keep pure rendering, layout, key decoding, and state reduction synchronous.
Move all operational TUI handlers—session selection/loading, background store
operations, agent/workflow startup, transcript loading, export, `/tools reload`,
`/loops`, and deletion—to async methods.

The TUI must never block on disk, Git, subprocess, MCP, or session service work.
Blocking terminal backends must remain behind the existing executor boundary.
Overlay input must remain responsive while async work is pending, and all
pending tasks must be cancellable on overlay close or session shutdown.

### FR-016 — Async optional and plugin integrations

Provider adapters, authentication, plugin discovery, skill installation, MCP
configuration writes, browser runtimes, and optional build/book integrations
must declare one of:

* native async implementation;
* bounded sync adapter with timeout/cancellation caveat;
* startup-only synchronous operation executed before the runtime loop;
* pure synchronous operation.

`webbrowser.open`, subprocess-based installers, Pillow/PDF/build operations,
and bundled reference scripts must not be called directly from an active loop.

### FR-017 — Async generated/user-facing builders

Workflow-produced `build_book.py` and equivalent generated builders must expose
an async implementation where they perform process, filesystem, or image I/O:

```python
async def build_book_async(...) -> BuildResult: ...

def main() -> int:
    return asyncio.run(build_book_async(...))
```

The sync `main()` is a process-level convenience shim only. It must not be
called by Agenthicc from an active event loop; Agenthicc invokes the async API.

### FR-018 — Async-safe compatibility policy

For each migrated method, document its compatibility disposition:

* retain sync pure method;
* add async method and deprecate sync operational method;
* replace sync operational method with async method;
* retain sync adapter only for external callers outside a running loop;
* remove after the announced compatibility window.

A sync operational facade called while an event loop is running must fail with
a clear error or require the caller to explicitly offload it. It must never
silently call nested `asyncio.run()` or block the loop.

### FR-019 — Async observability

Record structured metrics/events for:

* operation name, layer, and async/native/adapter classification;
* queue wait and execution duration;
* executor saturation and worker count;
* cancellation, timeout, and deadline overrun;
* persistence commit/flush/fsync duration;
* subprocess start/exit and cleanup;
* event-loop lag and TUI input latency;
* resource leaks at session shutdown.

Sensitive arguments, credentials, prompts, and transcript contents must remain
redacted according to current security rules.

### FR-020 — Async contract guardrails

Add a custom static audit that fails when first-party runtime code introduces:

* `subprocess.run`, `Popen`, `time.sleep`, direct blocking file I/O,
  synchronous SQLite, or blocking socket calls in an async function or async
  call path;
* `asyncio.run`, `run_until_complete`, or event-loop creation below process
  entry;
* an operational method without an async counterpart;
* an unbounded `asyncio.to_thread` use for a classified hot path;
* an async wrapper that merely calls blocking code directly.

The audit must allow explicit suppression only with a reason code and a test.

## 9. Module migration matrix

| Area | Current risk/evidence | Target contract |
|---|---|---|
| `kernel/processor.py` | Async queue/effects with sync log/snapshot I/O | Async persistence substrate; pure reducer unchanged |
| `memory/journal.py` | Sync fold/open/write/fsync methods | Async journal lifecycle and append/replay |
| `memory/layers.py` | Mixed sync constructors and `to_thread` SQLite | Async open/close and consistent async CRUD |
| `session_service/store.py` | Sync locks, JSONL append/replay/index | `AsyncSessionEventStore`; bounded serialization |
| `session_service/service.py` | Async API calls sync store under lock | Await store; lock only protects in-memory state |
| `background/store.py` | Sync event store, locks, artifact moves | Async store and atomic lifecycle methods |
| `background/supervisor.py` | Sync Popen/proc polling/sleep | Async process supervisor and cancellation |
| `background/terminals.py` | Sync persisted terminal cleanup and sleeps | Async terminal lifecycle; platform adapter isolated |
| `runs/*` | Sync JSONL and Git subprocess inspection | Async stores and command runner |
| `worktrees/*` | Sync Git manager/store; some callers offload | Async manager/store with structured outcomes |
| `runners/*` | Mixed journals, leases, cache, session setup | Async lifecycle and one loop boundary |
| `workflows/*` | Async runners with sync artifact/checkpoint helpers | Async operational phase boundary; pure topology sync |
| `tools/*` | Mostly async but mixed subprocess/fs/plugin paths | All tool execution async; bounded adapters only |
| `cli/*` | Sync dispatch and nested `asyncio.run` | Async command dispatch; parse/discovery remain pure sync |
| `tui/*` | Pure rendering mixed with blocking handlers | Async operational handlers; sync render/input decode |
| `session_service/transport.py` | Async HTTP/SSE over mixed store | Fully async projection/stream lifecycle |
| `plugins/*`, `skills/*` | Sync discovery/install/subprocess | Startup async orchestration and async install APIs |
| `workflows/make_book/*` | Sync build/Pillow/reference script | Async builder API plus process-level sync shim |

The implementation team must expand this matrix with every inventory item,
including private helpers that are reachable from an async entry point.

## 10. Async API and lifecycle design

### 10.1 Resource protocols

Prefer async context managers for resources with ownership:

```python
async with await SessionRuntime.open(config) as runtime:
    await runtime.run()
```

For compatibility with existing construction:

```python
store = SessionEventStore(root)       # inert, no I/O
await store.open()
try:
    await store.append(event)
finally:
    await store.close()
```

No constructor may start background tasks, acquire a process lease, open a
file, initialize a schema, connect MCP, or spawn a process.

### 10.2 Bounded sync adapters

Create one shared adapter facility rather than scattering raw `to_thread`:

```python
result = await io_executor.run(
    "session_store.append",
    sync_append,
    deadline=deadline,
    cancellation=CancellationPolicy.SHIELD_COMMIT,
)
```

The facility must provide:

* per-category bounded pools/semaphores;
* context propagation for tracing/session identity;
* admission timeout and queue metrics;
* explicit cancellation semantics;
* exception normalization;
* shutdown/drain behavior;
* no use for unbounded user-controlled CPU work.

Use a process pool only for measured CPU-bound work that releases neither the
GIL nor practical event-loop time, and cap its size per session/runtime.

### 10.3 Locking

* `asyncio.Lock` protects in-memory state within one event loop only.
* Cross-process/file locks remain in a sync adapter or are replaced with an
  async-compatible primitive.
* Never hold a thread/process lock while awaiting unrelated I/O.
* Record the owner and lease expiry before awaiting a slow operation.
* Lock acquisition has a deadline and a cancellation path.

### 10.4 Persistence commit points

Cancellation must not leave callers believing a record was committed when it
was not. Each write API documents its commit point:

```text
prepare → write temp/append → flush → fsync (if selected) → replace/index
                                  │
                         durable commit point
```

If cancellation arrives after the commit point, the operation may complete in
the adapter and return an idempotent result; if it arrives before it, the
operation must report not committed or recoverable. No `CancelledError` may be
swallowed without recording this state.

## 11. Error, cancellation, and timeout contract

### 11.1 Cancellation

`asyncio.CancelledError` must propagate unless a narrowly scoped cleanup block
uses a documented shield. `except Exception` blocks must not be used to convert
cancellation into an ordinary tool/provider/workflow failure.

Every long operation must check cancellation at safe boundaries and close:

* provider streams;
* subprocess pipes/process groups;
* MCP/browser sessions;
* files and database connections;
* event subscriptions;
* worker pools and leases.

### 11.2 Deadlines

Timeouts must use monotonic deadlines passed down from the caller. A child
operation must not reset the parent timeout. Error messages must identify the
operation, elapsed/deadline state, and whether cleanup completed.

### 11.3 Retry behavior

Retries remain owned by the existing provider/tool/workflow policy. The async
migration must not create accidental duplicate retries at the adapter layer.
Operations that can be retried must have idempotency keys or durable
transaction evidence before a retry is allowed.

### 11.4 Shutdown

Shutdown receives a deadline and returns a structured report of clean,
cancelled, timed-out, and orphaned resources. A stuck adapter must not keep a
TUI or CLI process alive forever.

## 12. Cache, workflow, and conversation requirements

The async conversion must not change prompt/cache semantics:

* stable system/tool regions remain stable across turns;
* dynamic phase state remains in the dynamic prompt region;
* one conversation/journal remains the source of truth across workflows;
* tool-call transactions are appended and repaired atomically;
* phase checkpoints are written before a resumable workflow boundary;
* an interrupted provider call retains the prior turn messages and recovery
  marker;
* `resume`, `--continue`, background attach, and session selection rehydrate
  the same durable state.

Async adapters must not reorder assistant/tool messages, publish a checkpoint
before its required artifact is durably recorded, or allow concurrent writes to
the same journal without the existing ownership/serialization guarantee.

## 13. Security and resource controls

The migration must preserve all current capability, workspace, network, and
approval boundaries. In particular:

* moving a filesystem or subprocess operation to a worker thread must not bypass
  `WorkspaceView`, `NetworkGuard`, tool capabilities, or mode policy;
* paths and command arguments must be validated before offload;
* secrets must not be copied into executor labels, logs, task names, or error
  messages;
* subprocesses must use existing process-group and environment policies;
* executor queues must be bounded to prevent a user or subagent from causing
  unbounded thread creation;
* worker/process cleanup must not delete a broad workspace or another session's
  artifacts;
* async shutdown must release leases and not leave a live owner record;
* HTTP/SSE backpressure and payload limits must remain enforced.

## 14. Testing strategy

### 14.1 Inventory and static tests

Add tests for the AST audit and blocking-call guard. Include positive fixtures
for allowed pure sync code, terminal/platform adapters, startup-only code, and
explicitly justified adapters; include negative fixtures for direct blocking
calls in async paths.

### 14.2 Unit tests

Cover:

* async persistence append/replay/atomic replace/fsync policies;
* partial/truncated records and corrupt snapshots;
* ordering under concurrent writers;
* lock admission, timeout, release, and cancellation;
* bounded executor saturation and shutdown;
* subprocess outcome, output bounds, cancellation, process-group cleanup;
* Git/worktree operations, conflicts, stale workers, and safe cleanup;
* async session/journal/checkpoint lifecycle;
* CLI async dispatch and sync compatibility errors;
* pure reducer/topology behavior remaining synchronous and deterministic;
* tool timeout/retry/idempotency and capability enforcement;
* TUI pending-operation cancellation and render/input separation.

### 14.3 Integration tests

Use temporary repositories/directories and isolated event loops to test:

* kernel event processing while a persistence adapter is delayed;
* session-service list/snapshot/stream while events are appended;
* memory/database restart and migration;
* background worker cancel/resume/recovery;
* worktree dispatch, merge, conflict preservation, and rebase;
* MCP/browser/plugin lifecycle shutdown;
* workflow phase transition, checkpoint, provider failure, and resume;
* multiple sessions sharing the runtime without starvation;
* Windows/Linux terminal adapter behavior where CI permits.

### 14.4 End-to-end tests

Verify critical journeys:

1. `agenthicc` starts the TUI and remains input-responsive during delayed I/O.
2. Headless mode starts and exits through one async boundary.
3. A workflow runs, checkpoints, is interrupted, and resumes without phase or
   conversation loss.
4. `/tools reload`, `/loops`, session selection, background-session delete,
   and transcript loading remain responsive.
5. `--goal`, `--continue`, `--resume`, `--detach`, and worker-agent flows do not
   create nested loops or leaked resources.
6. A provider timeout/cancellation preserves durable state and releases leases.
7. An MCP or browser startup failure does not prevent unrelated async tools from
   loading or shutdown from completing.
8. A parallel worktree run integrates successful workers and preserves a
   conflict for explicit resolution.

### 14.5 Responsiveness and performance tests

Define measurable budgets for the default development environment:

* event-loop lag under a delayed persistence operation: no increase beyond the
  configured observation threshold;
* TUI key-to-handler scheduling remains below the agreed interactive budget;
* no unbounded growth of executor queues under concurrent sessions;
* async persistence throughput is no worse than the current baseline beyond an
  agreed regression tolerance;
* startup and shutdown complete within documented budgets;
* large journal replay yields and does not starve provider/TUI tasks.

Benchmarks must report environment and workload; they must not be treated as
functional tests alone.

### 14.6 Existing quality gates

The completed migration must pass:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
uv run nox -s lint typecheck type_audit tests coverage build build_check
uv run mkdocs build --strict
```

The full suite must not regress from the 3,995-passed baseline, and the
coverage gate must remain satisfied.

## 15. Phased implementation plan

### Phase 0 — Inventory and guardrails

* Build the AST classification report.
* Trace every async entry point to blocking callees.
* Add the blocking-call guard and reason-code registry.
* Define the async protocol/type naming rules.
* Measure event-loop lag, TUI latency, startup, persistence, and shutdown
  baselines.

### Phase 1 — Shared async substrate

* Implement bounded I/O and CPU adapter facilities.
* Implement async file/atomic-write/append primitives.
* Define cancellation, deadline, durability, and shutdown result types.
* Add metrics and test fixtures.

### Phase 2 — Durable state

* Migrate kernel processor persistence.
* Migrate journals, checkpoint stores, session logs, usage ledgers, run stores,
  process leases, background stores, and session-service storage.
* Preserve pure reducers and state/event parsing.
* Add crash/restart and concurrent-writer coverage.

### Phase 3 — Process, Git, worktree, and memory boundaries

* Implement async command/process runner.
* Migrate `runs` and `worktrees` managers/stores.
* Migrate background supervisors and terminal lifecycle.
* Complete memory/database async open/close and CRUD.

### Phase 4 — Tools, agents, and workflows

* Normalize all tool execution and integration lifecycles.
* Migrate workflow artifacts, subagents, phase transitions, and checkpoints.
* Update `create_workflow` prompts, annotations, validator tools, and generated
  templates to enforce async operational phases.
* Verify cache, journal, recovery, and ownership contracts.

### Phase 5 — Session, CLI, TUI, and plugins

* Add one async process boundary.
* Refactor CLI dispatch and command handlers.
* Refactor session construction/shutdown and service projections.
* Keep rendering/decoding sync and migrate TUI operational handlers.
* Migrate discovery, skills, MCP configuration, auth, and optional integrations.

### Phase 6 — Compatibility and removal

* Add deprecation warnings and migration docs for sync operational facades.
* Fail sync facades clearly inside active loops.
* Update third-party/plugin interfaces and examples.
* Remove unneeded shims after the documented compatibility window.

### Phase 7 — Release validation

* Run the complete quality matrix and responsiveness benchmarks.
* Perform fault injection at every async boundary.
* Review all reason-code suppressions.
* Update architecture, storage, workflow, contributor, CLI, and extension docs.
* Publish the final inventory showing no unclassified operational sync code.

## 16. Acceptance criteria

The implementation is accepted only when all of the following are true:

### AC-001 — Inventory completeness

The AST report covers all first-party runtime files, scripts, workflow
artifacts, and plugin surfaces. Every sync function has a classification and no
operational function is unowned or unclassified.

### AC-002 — No direct blocking in async paths

The static guard and review find no unjustified direct blocking file, database,
process, network, lock, sleep, or terminal operation reachable from the event
loop. Every suppression names the adapter and test.

### AC-003 — One event-loop boundary

No internal runtime module invokes `asyncio.run`, `run_until_complete`, or
creates a loop. Headless, TUI, CLI, background, and goal modes start through
the documented async entry path.

### AC-004 — Durable state preserved

Event order, journal continuity, checkpoint topology, phase state, workflow
resume, tool-call transaction repair, session ownership, usage accounting, and
crash recovery behave exactly as before or have a documented intentional change.

### AC-005 — Pure kernel remains pure

Reducers, event parsing, state transitions, and topology/value helpers remain
synchronous, deterministic, and directly unit-testable. No reducer performs
I/O or awaits.

### AC-006 — Async persistence semantics

Kernel/session/background/workflow/journal/ledger stores provide async methods,
preserve atomicity and durability semantics, and pass cancellation, corruption,
concurrency, and restart tests.

### AC-007 — Async process and Git semantics

Commands, background processes, Git, and worktrees expose structured async
outcomes, bounded output, deadlines, process-group cancellation, conflict
preservation, and safe cleanup.

### AC-008 — Async workflow contract

All built-in workflows and generated workflows can run, checkpoint, pause,
resume, reject/retry, ask questions, call tools, and cancel without blocking
the event loop. Generated operational methods are async or explicitly use the
shared adapter.

### AC-009 — Tool and plugin contract

Every agent-facing tool has an async execution path, capability checks occur
before offload, and MCP/browser/network/filesystem/process tools pass success,
denial, timeout, cancellation, malformed-input, and retry-idempotency tests.

### AC-010 — CLI/TUI responsiveness

The TUI remains responsive during delayed persistence, process, Git, MCP, and
session operations. CLI commands do not nest event loops. Session attach,
transcript load, `/tools reload`, `/loops`, background actions, and workflow
controls are cancellable.

### AC-011 — Lifecycle closure

Every opened file, connection, task, subprocess, executor, subscription,
browser/MCP client, and lease has an async close path. Fault-injection tests
show no leaked owner, child process, task, or temporary artifact after normal
shutdown or cancellation.

### AC-012 — Bounded offload

No unbounded thread/process creation exists. Saturation, admission timeouts, and
shutdown are observable and tested under concurrent sessions.

### AC-013 — Compatibility

Supported external plugins and user workflows receive a documented migration
path. Sync operational facades either work outside a running loop or fail with
an actionable message; they never block an active loop or call nested
`asyncio.run()`.

### AC-014 — Performance

Event-loop lag, TUI latency, startup, shutdown, journal replay, and persistence
throughput meet the approved budgets and do not regress beyond the agreed
tolerances against the Phase-0 baseline.

### AC-015 — Quality gates

The complete test, coverage, lint, format, mypy, type-audit, docs, package
build, and async static-audit gates pass. The final inventory contains zero
unjustified operational synchronous functions.

## 17. Migration and compatibility examples

### Before

```python
store = SessionEventStore(root)
events = store.all_events(session_id)
```

### After

```python
store = await SessionEventStore.open(root)
try:
    events = await store.all_events(session_id)
finally:
    await store.close()
```

### Pure code remains synchronous

```python
state = reduce_event(state, event)
payload = PhaseContext.to_payload(context)
```

### Process-level sync convenience only

```python
async def main_async(argv: Sequence[str] | None = None) -> int:
    ...

def main() -> int:
    return asyncio.run(main_async())
```

### Explicit legacy adapter

```python
def legacy_list_sessions(...) -> list[SessionSnapshot]:
    if _running_loop():
        raise RuntimeError(
            "legacy_list_sessions() cannot run in an active event loop; "
            "await list_sessions_async() instead"
        )
    return asyncio.run(list_sessions_async(...))
```

The actual implementation may choose another compatibility shape, but it must
make the loop boundary and blocking behavior visible.

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Async wrappers still block | AST guard, call-graph audit, delayed-I/O tests |
| Cancellation during durable write | Explicit commit points, shielded narrow commit, recovery tests |
| Thread-pool starvation | Bounded per-category executors, admission metrics, budgets |
| Changed event ordering | Single-writer ownership, sequence tests, replay tests |
| Nested event loops | One entry boundary and a failing runtime guard |
| Sync plugin breakage | Typed compatibility adapter, warnings, migration guide |
| TUI regressions | Keep render/input decoder pure, async integration tests |
| CPU work still stalls loop | Measurement, process pool for proven CPU hotspots |
| Resource leaks | Async context managers, shutdown reports, fault injection |
| Cache/journal changes | Preserve transaction and prompt contracts; replay tests |
| Platform differences | Dedicated terminal/process adapters and Linux/Windows CI coverage |

## 19. Open decisions to resolve in Phase 0

1. Whether the durable file substrate should use `anyio` file helpers, a
   project-owned bounded thread adapter, or an optional native async file
   dependency. The default must not add a dependency without measurements.
2. Whether SQLite should remain behind a bounded thread adapter or gain an
   optional async driver, based on transaction latency and deployment support.
3. Whether event-loop lag and TUI latency budgets should be hard CI thresholds
   or monitored benchmark thresholds on shared CI hardware.
4. The deprecation window for sync operational facades and plugin handlers.
5. Whether generated builders should depend on Agenthicc's adapter substrate or
   ship a self-contained `asyncio` implementation.
6. Which operations qualify for a process pool after profiling; process pools
   must not be introduced speculatively.

These decisions cannot weaken the core invariant that blocking work does not
run directly on the Agenthicc event loop.

## 20. Definition of done

This PRD is complete when:

* the AST inventory, module migration matrix, and reason-code registry are
  current;
* every operational runtime API is async-first and every blocking dependency is
  native async or behind a bounded, tested adapter;
* pure reducers, parsers, value objects, rendering, and terminal decoding stay
  sync where that is the correct ownership boundary;
* one async process boundary serves CLI, TUI, headless, background, goal, and
  service modes;
* cancellation, timeout, retry, durability, ordering, checkpoint, cache,
  ownership, and cleanup contracts pass fault-injection tests;
* all built-in and generated workflows—including subagents and parallel
  worktree agents—remain resumable and non-blocking;
* the TUI remains responsive during slow I/O and process operations;
* complete unit, integration, E2E, static, type, docs, build, coverage, and
  performance gates pass;
* documentation explains the async contract to contributors, plugin authors,
  workflow authors, tool authors, and users;
* the final report contains no known unclassified or unjustified blocking sync
  operation.

