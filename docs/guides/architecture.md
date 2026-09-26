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

The client-neutral session service sits beside this runtime as the shared
coordination and projection boundary:

```text
TUI / headless / CLI / HTTP-SSE / IDE adapter
                    │ typed commands, snapshots, event cursors
                    ▼
          session_service.SessionService
                    │ kernel event subscription + owning service events
                    ▼
          EventProcessor + workflow/background/terminal owners
```

`SessionService` owns the client-facing snapshot, command idempotency ledger,
capability projection, durable event cursor, replay boundary, and bounded
subscriptions. It does not replace frozen kernel `AppState`, the reducer,
workflow runners, or the existing session journals. The loopback transport
(`LocalSessionServer`) and `HttpSessionClient` are adapters over this same
in-process service; installing agenthicc does not start a listening socket.

The headless runner uses the kernel directly and currently handles stdin
intent submission. The TUI runner adds the larger session graph: configuration,
registries, durable memory, approvals, workflow selection, and rendering.

## Progressive startup

Session construction is staged by runners.startup.StartupCoordinator. The
owner lease and security-sensitive configuration/workspace decisions happen
before durable session mutation. The session service then opens its bounded
metadata projection and restores only the selected session; it does not replay
unrelated JSONL histories merely because a new TUI was opened. The minimum
kernel, built-in command registry, workspace, and input panel can render a
first frame while project extensions and optional integrations load in tracked
background phases.

The coordinator is observational, not a second runtime or persistence layer.
Every deferred task receives the existing session-owned context and is
cancelled/awaited when the runner closes. An agent operation waits at its
declared dependency boundary: for example, a direct turn waits for extension
discovery before exposing project tools, and an MCP-dependent operation waits
for the MCP catalog. A failed optional phase is shown as degraded and does not
block unrelated local work; required resources retain their fail-closed
semantics.

Built-in workflows and agents expose lazy descriptors for names and mode
metadata. Their implementations are materialized only when selected. The
fresh-session TUI defers user/project Python extension imports until after the
first frame, then replaces the contents of the session-owned registries in
place so existing WorkflowConfig and AgentsRegistry references remain valid.
This preserves phase state, prompt/cache contracts, conversation identity,
memory, AGENTS.md instructions, tool policy, and checkpoints.

See the startup guide for readiness states, /startup, cache repair, benchmark
usage, and optional integration behavior.

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
- token/cost/activity/frame signals; activity timing includes prompt waits for
  telemetry but does not publish a visible timer tick while a prompt owns the
  terminal; the animation frame is intentionally quiet while idle or paused;
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

At the provider boundary, `AgentTurnRunner` passes one memory instance through
the shared `lauren-ai` transaction API. `run()` and `run_stream()` validate
before every provider request, execute a full assistant call batch, commit one
canonical result exchange, and validate again. Cancellation, queued input,
workflow checkpoints, headless execution, and generated workflows use this
same boundary; agenthicc does not maintain a second provider-specific result
correlator. Journaled memory persists repair before control returns to the TUI,
and malformed history is reported as a safe structured diagnostic.

Streaming recovery has one additional boundary: a logical user turn contains
one or more provider steps, and each step may have multiple network attempts.
`lauren-ai` emits step-start, retry, interruption, and commit receipts. The
session adapter persists them and advances the safe memory cursor only after a
valid assistant/tool exchange is committed. A transport error in step N may
discard only the uncommitted attempt N; it cannot restore the snapshot from
before step N-1. This keeps the journal fold, the reactive transcript, and the
next provider request aligned after a late failure. Unsupported older runners
are not placed inside a destructive whole-stream retry boundary.

For workflow turns, `AgentTurnRunner` also applies the shared prompt contract:
the stable system policy and deterministic stable-tool schemas form the reusable
prefix, while phase instructions, artifacts, questions, answers, and summaries
are appended as dynamic context. The contract records redacted fingerprints and
an epoch in the conversation journal/checkpoint; it never exposes provider
credentials or raw endpoint URLs. Provider-specific cache support is detected at
the boundary, so unsupported providers retain correct execution semantics
without being reported as cache hits.

### Subagent policy and communication boundary

`spawn_subagents` snapshots the parent turn's effective runtime policy into a
frozen `SubagentExecutionPolicy`. Each worker receives a fresh reactive state,
fresh short-term memory, the exact workspace scope, and a child-local
capability/approval adapter. The role allow-list and parent-visible tool list
are additional ceilings. No worker shares the parent's transcript or mutable
mode signal.

`AgentMessageBroker` is owned by one pool and is the only parent/child/peer
communication route. It validates pool membership, bounds payloads and
mailboxes, journals delivery and question lifecycle events, and keeps
clarification continuation non-reentrant: `ask_parent` returns a structured
pending result, `answer_subagent` resolves the correlated future, and
`collect_subagent_results` receives the still-running pool later. Message
payloads are untrusted context and cannot modify policy or grant tools. The
broker's journal fold is the recovery boundary for pending messages and
questions; a missing worker becomes an explicit terminal/orphaned outcome.

The canonical selectable modes are Safe, Plan, and Yolo, in that cycle order.
Safe asks for approval before side effects, Plan hard-blocks side effects, and
Yolo preserves the unrestricted Auto behavior. Auto, Guard, Ask, and Review are
non-displayed compatibility aliases; Replay is internal-only and Debug is not
silently granted any permissions. The built-in agent roles include planner,
executor, reviewer, explorer, verifier, human, and auto.

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

Terminal subprocesses are a child resource of that control plane. The
session-scoped `background.TerminalManager` owns only process groups it
created, stores bounded redacted records under the terminal registry, and is
consumed by `tools/exec` for explicit `background=true` requests. A workflow
phase may declare `terminal_wait_policy = "background"`; the declaration
changes the default tool wait policy without embedding a shell command in the
workflow. Cancelling a detached parent asks the terminal registry to stop its
exact child groups before the parent session is finalized.

The PRD-151 execution contract is shared by foreground and owned-terminal
paths: `CommandOutcome` derives success only from a zero exit, records the
deadline owner and cleanup result, and distinguishes finite command exit from
service readiness. `wait_terminal` is observational and never kills a process
because its observer timed out; explicit stop controls own termination.

## Inspecting the architecture from a shell

Every claim on this page can be re-derived from the installed package without
starting a session. This snippet is read-only and runs in about a second:

```bash
PYTHONPATH=src python - <<'PY'
import dataclasses
import agenthicc.kernel as kernel
from agenthicc.kernel.reducer import _HANDLERS

print("kernel exports:", len(kernel.__all__))
print("AppState fields:", [f.name for f in dataclasses.fields(kernel.AppState)])
print("reducer handlers:", len(_HANDLERS))
print("EffectType members:", [e.name for e in kernel.EffectType])
PY
```

```text
kernel exports: 22
AppState fields: ['session_id', 'run_id', 'intents', 'workflows', 'tasks', 'agents', 'tools', 'hooks', 'snapshot_index', 'settings', 'policy', 'agent_types']
reducer handlers: 20
EffectType members: ['spawn_agent', 'execute_tool', 'update_tui', 'persist_snapshot', 'emit_signal', 'start_workflow_node', 'assign_task']
```

Two facts worth pinning down while you are here. `kernel.AppState` lives in
`src/agenthicc/kernel/state.py` and is *not* the same object as the reactive
`tui/conversation_store.py` `AppState`; the field list above is the kernel one.
And the reducer dispatches on the string `event_type`, not on the `EffectType`
enum — `EffectType` describes the *effects the reducer emits*, which is why the
two lists above do not line up one-to-one.

To see which process-level resources a running session has opened, use the
startup report instead of importing internals:

```bash
PYTHONPATH=src python -m agenthicc --headless < /dev/null
```

```text
{"status": "ready", "mode": "headless", "session_id": "4c783bb1ff244a66bf3243d28aead0da"}
```

The output is one JSON object per line, so it pipes cleanly into `jq` or any
line-oriented consumer. `session_id` changes on every run; the keys and the
`"status": "ready"` value do not, because the headless runner answers `--help`
and `--version` before command discovery and emits this readiness record before
reading stdin. The process exits 0 even with no input, which is what makes it a
safe smoke test.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AttributeError` when reading `AppState` fields | You imported the reactive TUI `AppState` from `tui/conversation_store.py` instead of the frozen kernel one | Import from `agenthicc.kernel` (or `agenthicc.kernel.state`) and check `AppState.__module__` |
| `processor.emit()` seems to do nothing | `EventProcessor.run()` was never scheduled, or the state was read before `drain()` | Schedule `await processor.run()` before the first `emit()` and `await processor.drain()` before reading state |
| Effects are emitted but nothing happens | An `Effect` is a descriptor; something has to consume it | Pair the reducer with an effect executor such as `EffectExecutor` or `NoOpEffectExecutor` |
| Mermaid blocks render as literal text | `mkdocs.yml` has no mermaid `custom_fences` entry | Keep diagrams as balanced ```text ASCII blocks, as this page does |
| A code fence swallows the following paragraphs | A nested fence of the same length; CommonMark closes on the first matching ``` | Never nest same-length fences — close the outer block first |
| `restore_from_log()` returns less state than expected | Malformed trailing JSONL lines are skipped by the recovery policy | Inspect the log tail; the durable prefix is replayed and corrupt entries are dropped |

### The state did not change after an event

State transitions happen only inside the reducer. Mutating the object you hold a
reference to cannot work: `AppState` is a frozen dataclass, so assignment raises
`dataclasses.FrozenInstanceError`. Read the new state returned by the processor,
not the one you passed in.

### Two `AppState` classes look identical

They are different types with the same name. Confirm which one you have:

```bash
PYTHONPATH=src python -c "import agenthicc.kernel as k; print(k.AppState.__module__)"
```

```text
agenthicc.kernel.state
```

If your output names a `tui` module, you imported the reactive store.

### A background or workflow operation never becomes ready

Startup is staged by `runners.startup.StartupCoordinator`, and an operation
waits at its declared dependency boundary. A failed *optional* phase is reported
as degraded without blocking unrelated local work; a failed *required* resource
keeps its fail-closed behavior. See the [startup guide](startup.md) for the
readiness states and the `/startup` report.
