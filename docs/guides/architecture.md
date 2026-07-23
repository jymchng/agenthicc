# Architecture

## Runtime overview

```text
CLI parser
   │
   ▼
session context ───────────────┐
   │                           │
   ▼                           ▼
TUISession                 EventProcessor
   │                           │
   ├─ reactive TUI AppState    ├─ Event queue
   ├─ Workspace                 ├─ root_reducer
   ├─ UnifiedInputSession       ├─ frozen kernel AppState
   └─ WorkflowConfig            ├─ effects/subscribers
                                └─ JSONL persistence
                                     │
                                     ▼
                             workflow/agent turns
                                     │
                       capability-gated tools and memory
```

The headless runner uses the kernel directly and currently handles stdin
intent submission. The TUI runner adds the larger session graph: configuration,
registries, durable memory, approvals, workflow selection, and rendering.

## Kernel

`kernel.AppState` is a frozen dataclass containing intents, workflows, tasks,
agents, registered tools/hooks, security policy, settings, and session id.
`Event` is the serialized input to the pure reducer:

```text
await processor.emit(event)
        │
        ▼
asyncio.Queue → root_reducer(state, event)
        │
        ├─ new immutable state
        ├─ Effect descriptors
        ├─ subscriber snapshots
        └─ optional JSONL event + snapshot
```

`EventProcessor` is multi-producer/single-consumer. Always schedule
`processor.run()` before `emit()` and await `drain()` before reading the state.
`restore_from_log()` replays valid JSONL entries and skips malformed entries
according to the current recovery policy.

## State boundary

The TUI has a second `AppState` in `tui/conversation_store.py`. It is a reactive
container, not a replacement for the kernel model. It contains:

- conversation turns and scroll events;
- token/cost/frame signals;
- input buffer and paste state;
- active runtime mode;
- overlay and approval state;
- workflow progress and transient notifications.

`TUISession` and workflow runners emit kernel events for durable domain changes
and update reactive signals for immediate presentation. This split avoids
putting terminal-only state in the event log, but it must remain explicit. A
new feature should state which model is authoritative and how restart/replay
behaves. Consolidating or formalizing this boundary is PRD-138 P0.3.

## Workflow and agent path

1. A submitted message is routed through `TUISession`.
2. The active mode and optional `/workflow` override select a workflow.
3. A `WorkflowPlugin` exposes phase specifications and a runner.
4. A phase selects an agent role, model override, tool capabilities, and
   transition policy.
5. `AgentTurnRunner` supplies memory, mentions, skills, tools, approval, retry,
   and durable idempotency context to lauren-ai.
6. Tool and workflow events update the kernel and reactive presentation.
7. Completion, rejection, error, or interruption determines the next phase or
   resume plan.

The built-in modes are Auto, Plan, Ask, Review, Safe, and Debug. The built-in
agent roles include planner, executor, reviewer, explorer, verifier, human,
and auto.

## Tool and security path

Tools can be class-based `Tool` objects or lauren-ai decorated callables. The
runtime combines:

- `ToolCapability` metadata and mode filters;
- `PermissionChecker` and per-agent `AgentCapabilityScope`;
- `WorkspaceView` path resolution and symlink escape prevention;
- `NetworkGuard` domain allow-list checks;
- approval services and overlays;
- timeout/retry/error handling;
- shared HTTP timeout configuration for network integrations.

The default posture is fail-closed. A new tool needs capability metadata,
approval expectations, an error contract, and tests for denied and malformed
calls.

## Persistence layers

The session can write a kernel event log, conversation event log, durable
conversation journal, project/global memory databases, a workspace file cache,
and test cassettes. They have different owners and recovery guarantees; see
the [storage reference](../reference/storage.md). Do not describe all of them
as one event log.

## Architectural improvement priorities

The current high-value risks are explicit in
[PRD-138](https://github.com/agenthicc/agenthicc/blob/main/prds/prd-138-repository-improvement-roadmap.md): document the
state bridge, decide whether a server API is supported, unify workflow sources
of truth, define processor failure/backpressure semantics, and add storage
migrations and observability.

## Background-session boundary

The `agenthicc.background` package is a control-plane adapter, not a second
agent runtime. `BackgroundSupervisor` owns worker leases, bounded process
creation, cancellation, stale detection, and control requests. A worker builds
the normal session through `runners/session_context.py`, then delegates direct
turns to the canonical agent-turn runner or workflows to
`runners/headless.py`.

`BackgroundStore` owns only the rebuildable lifecycle index. Kernel events,
conversation events, workflow phase state, approvals, and memory remain under
their existing owners. The manager TUI reads the index and renders safe
metadata; it does not mutate frozen kernel state directly. `/bg`, `/background`,
`agenthicc jobs`, and `agenthicc agents` all enter this same boundary.
