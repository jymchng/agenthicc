# Glossary

Definitions for the domain terms used across these documents. Every entry ends
with a **Provenance** line naming the symbol and the source location that
defines it, so each term can be checked rather than trusted.

Three conventions worth stating up front:

- **Some names are overloaded.** `Workflow` is both a kernel record and a
  runnable plugin; `ModeRegistry` is two unrelated classes in two packages.
  Where that happens the entry says so explicitly rather than picking one.
- **`path:line` references are relative to the repository root** and point at
  the current tree, not at a release.
- **A provenance line is a claim.** If a symbol here stops matching its source,
  the entry is wrong and must change — the line is the test.

## Kernel

The kernel is an event-sourced store. Nothing mutates `AppState` in place:
every change is a replayable `Event` folded through a pure reducer.

### Kernel

The `agenthicc.kernel` package — the event-sourced `AppState`, the event and
effect types, the reducer, and the processor. It depends on nothing in the CLI,
the TUI, or any workflow, which is what makes replay from a log possible.

**Provenance:** `agenthicc.kernel` · `src/agenthicc/kernel/__init__.py`

### Event

An immutable record of something that happened. Events are the only input the
reducer accepts, and the only thing that can change state, so a session's
history is a list of events rather than a set of mutations.

**Provenance:** `agenthicc.kernel.Event` · `src/agenthicc/kernel/events.py:86`

### Effect

A description of work the outside world must perform as a consequence of an
event — spawning an agent, executing a tool, persisting a snapshot. The reducer
*returns* effects; it never performs them. That split is what keeps the reducer
pure and testable.

**Provenance:** `agenthicc.kernel.Effect` · `src/agenthicc/kernel/events.py:144`

### EffectType

The enum of the seven effect kinds:
`spawn_agent`, `execute_tool`, `update_tui`, `persist_snapshot`,
`emit_signal`, `start_workflow_node`, and `assign_task`.

**Provenance:** `agenthicc.kernel.EffectType` · `src/agenthicc/kernel/events.py:133`

### Reducer

The pure function `(AppState, Event) -> (AppState, list[Effect])`. The
dispatcher is `root_reducer`; per-event-type behaviour lives in the `_HANDLERS`
mapping, which currently holds **20** entries.

**Provenance:** `agenthicc.kernel.root_reducer` · `src/agenthicc/kernel/reducer.py:36`

**Provenance:** `agenthicc.kernel.reducer._HANDLERS` · `src/agenthicc/kernel/reducer.py:378`

### AppState

The whole system state as one immutable dataclass: intents, tasks, workflows,
agents, tool registrations, security policy, and settings. New values are
derived with `AppState.create(...)` and the `with_*` helpers rather than by
assignment.

**Provenance:** `agenthicc.kernel.AppState` · `src/agenthicc/kernel/state.py:143`

### EventProcessor

The component that drives events through the reducer and then hands the
returned effects to an `EffectExecutor`. `drain(timeout=5.0)` runs the queue to
completion.

**Provenance:** `agenthicc.kernel.EventProcessor` · `src/agenthicc/kernel/processor.py:35`

### Intent

A user request, from raw text to terminal status. An intent is the unit the
kernel schedules; it is not the same thing as a workflow run or a task.

**Provenance:** `agenthicc.kernel.Intent` · `src/agenthicc/kernel/state.py:53`

### IntentStatus

The intent lifecycle: `pending`, `validating`, `planning`, `running`,
`complete`, `failed`, `rejected`.

**Provenance:** `agenthicc.kernel.IntentStatus` · `src/agenthicc/kernel/state.py:26`

### Task

A schedulable unit of work assigned to an agent. Tasks hang off intents and are
what the agent pool actually executes.

**Provenance:** `agenthicc.kernel.Task` · `src/agenthicc/kernel/state.py:87`

### Workflow (kernel record)

The kernel's *declaration* of a workflow: its name, nodes, and status. This is
not the runnable plugin — see [Workflow plugin](#workflow-plugin) below. The
kernel record is what the TUI and the registry display.

**Provenance:** `agenthicc.kernel.Workflow` · `src/agenthicc/kernel/state.py:76`

### WorkflowNode

One node in a kernel workflow record, with its own `NodeStatus`. Nodes are the
unit the processor advances.

**Provenance:** `agenthicc.kernel.WorkflowNode` · `src/agenthicc/kernel/state.py:64`

### AgentInstance

A running agent in the kernel's view: its role, state, and identity.
Distinguish it from a *subagent*, which is an ephemeral worker (see
[Subagent](#subagent)).

**Provenance:** `agenthicc.kernel.AgentInstance` · `src/agenthicc/kernel/state.py:99`

### AgentStatus

The lifecycle of an `AgentInstance`.

**Provenance:** `agenthicc.kernel.AgentStatus` · `src/agenthicc/kernel/state.py:46`

### ToolRegistration

The kernel's record that a tool exists and is callable, including its
capability annotations. Registration is what makes a tool visible to the
executor.

**Provenance:** `agenthicc.kernel.ToolRegistration` · `src/agenthicc/kernel/state.py:110`

### SecurityPolicy

The kernel-level security settings — permission rules and the configured
mode — that the runtime consults before a call is allowed.

**Provenance:** `agenthicc.kernel.SecurityPolicy` · `src/agenthicc/kernel/state.py:127`

### SystemSettings

Kernel-wide tuning, including `snapshot_every_n_events` (default **100**),
which bounds how much history must be replayed to rebuild state.

**Provenance:** `agenthicc.kernel.SystemSettings` · `src/agenthicc/kernel/state.py:133`

### restore_from_log

Rebuilds an `AppState` by folding a persisted event log back through the
reducer. Because the reducer is pure and effects are descriptions, replay is
deterministic.

**Provenance:** `agenthicc.kernel.restore_from_log` · `src/agenthicc/kernel/processor.py:191`

## Workflows

### Workflow plugin

A runnable, multi-phase process — `goal_flow`, `code_plan`, and so on —
implemented as a class derived from `BaseWorkflowRunner` and described by
`WorkflowPlugin`. There are **8** built-in workflows.

**Provenance:** `agenthicc.workflows.WorkflowPlugin` · `src/agenthicc/workflows/plugin.py:457`

### WorkflowRegistry

The ordered name-to-workflow map built by `build_workflow_registry()`.

**Provenance:** `agenthicc.workflows.WorkflowRegistry` · `src/agenthicc/workflows/registry.py:22`

### Phase

One step of a workflow, declared as a `PhaseSpec` with a `PhaseRole` and named
transitions. A phase is the granularity at which progress is checkpointed, so
"which phase am I in" is a durable question with a durable answer.

**Provenance:** `agenthicc.workflows.PhaseSpec` · `src/agenthicc/workflows/plugin.py:226`

### Checkpoint

A persisted workflow boundary. `WorkflowCheckpointTopology` records which phase
transitions are legal, and `topology_from_phase_specs()` derives it from the
phase declarations. A checkpoint that does not sit on a legal boundary is
rejected by `checkpoint_phase_boundary()` rather than resumed.

**Provenance:** `agenthicc.workflows.WorkflowCheckpointTopology` · `src/agenthicc/workflows/checkpoint.py:39`

### Workflow loader

The two entry points that produce workflow classes: `load_builtin_workflows()`
for the shipped ones and `load_python_workflows()` for project-defined modules.

**Provenance:** `agenthicc.workflows.load_python_workflows` · `src/agenthicc/workflows/loader.py:100`

### goal_flow

The reference workflow for this kind of work: it clarifies an intent, decides an
ordered goal list, then alternates implement and verify phases per goal. Its
goal list is dynamic — `append_goal` and `insert_goal` can extend it mid-run.

**Provenance:** `agenthicc.workflows.GoalFlowRunner` · `src/agenthicc/workflows/goal_flow/runner.py:747`

## Modes and security

### Mode

A named operating profile that patches the system prompt and filters tools —
`Safe`, `Plan`, and `Yolo` ship built in.

**Provenance:** `agenthicc.modes.Mode` · `src/agenthicc/modes/mode.py:30`

### ModeRegistry (two of them)

There are two classes with this name, in different packages:

- `agenthicc.modes.registry.ModeRegistry` — the plain ordered registry used by
  the `modes` package. Unknown names yield `None`.
- `agenthicc.tui.runtime.mode_manager.ModeRegistry` — the TUI runtime registry,
  whose `resolve()` **raises** `UnknownModeError`.

They are not interchangeable; a doc that says "ModeRegistry.resolve raises" is
describing the second one.

**Provenance:** `agenthicc.modes.registry.ModeRegistry` · `src/agenthicc/modes/registry.py:12`

**Provenance:** `agenthicc.tui.runtime.mode_manager.ModeRegistry` · `src/agenthicc/tui/runtime/mode_manager.py:64`

### ModeManager (two of them)

The matching pair of managers:

- `agenthicc.modes.manager.ModeManager` — wraps the `modes` registry.
- `agenthicc.tui.runtime.mode_manager.ModeManager` — the TUI one, whose
  `set_by_name()` returns `None` for an unknown *or internal* mode instead of
  raising.

**Provenance:** `agenthicc.modes.manager.ModeManager` · `src/agenthicc/modes/manager.py:11`

**Provenance:** `agenthicc.tui.runtime.mode_manager.ModeManager` · `src/agenthicc/tui/runtime/mode_manager.py:378`

### UnknownModeError

Raised by the TUI runtime's `ModeRegistry.resolve()` when a name matches no
selectable mode. It lives in the TUI runtime, not in `agenthicc.modes`.

**Provenance:** `agenthicc.tui.runtime.mode_manager.UnknownModeError` · `src/agenthicc/tui/runtime/mode_manager.py:35`

### ToolCapability

The declared capability of a tool: `READ`, `WRITE`, `EXECUTE`, `GIT_READ`,
`GIT_WRITE`, `NETWORK`, `SEARCH`, `CONTROL`, or `UNDECLARED`. Declaring
capabilities is what lets a mode gate a tool without knowing what it does.

**Provenance:** `agenthicc.tools.capabilities.ToolCapability` · `src/agenthicc/tools/capabilities.py:52`

### Capability gate

The check that refuses a tool call whose capabilities are not permitted by the
active mode. A tool with `UNDECLARED` capability is treated as unclassified
rather than trusted.

**Provenance:** `agenthicc.tools.capability_gate` · `src/agenthicc/tools/capability_gate.py`

### Approval

The interactive decision point for a gated call. `ApprovalRequest` and
`ApprovalResponse` carry it; `ApprovalGate` enforces the decision. In headless
runs there is no human, so approvals are resolved by policy.

**Provenance:** `agenthicc.tools.approval.ApprovalGate` · `src/agenthicc/tools/approval.py:158`

### Workspace scope

The allow-list of directories a tool may touch, distinct from workspace
*access*, which is whether a given path inside that scope may be written. An
out-of-scope path is refused before it is ever resolved.

**Provenance:** `agenthicc.tools.workspace_access.WorkspaceScope` · `src/agenthicc/tools/workspace_access.py:78`

### Sandbox

The resource envelope a tool call runs inside: a `WorkspaceView` for the file
system, a `NetworkGuard`, and `ResourceLimits` for CPU and memory.

**Provenance:** `agenthicc.tools.sandbox.ToolSandbox` · `src/agenthicc/tools/sandbox.py:121`

## Sessions and background work

### Session

One conversation, persisted as an append-only JSONL log plus an index entry.
A session can be resumed, which is why the log is a log and not a snapshot.

**Provenance:** `agenthicc.tui.runtime.session_log.create_session_id` · `src/agenthicc/tui/runtime/session_log.py:47`

### Session lease

The lock that stops two processes from driving the same session at once.
`SessionAlreadyActiveError` is the refusal; `InterProcessLock` is the underlying
mechanism.

**Provenance:** `agenthicc.runners.session_lease.SessionAlreadyActiveError` · `src/agenthicc/runners/session_lease.py:164`

### Background session

A session detached from the terminal and driven by a separate worker process.
It is addressed by an id and inspected later, so it needs an approval path and
a durable status that do not depend on a TUI being open.

**Provenance:** `agenthicc.background.model.BackgroundSession` · `src/agenthicc/background/model.py:117`

### SessionStatus

The background lifecycle: `QUEUED`, `STARTING`, `RUNNING`, `WAITING_APPROVAL`,
`WAITING_INPUT`, `RETRYING`, `CANCELLING`, `COMPLETED`, `FAILED`, `CANCELLED`,
`ORPHANED`, `ARCHIVED`, `DELETED`. Note that this enum lives in
`agenthicc.background.model` — there is no `SessionStatus` in
`agenthicc.sessions`.

**Provenance:** `agenthicc.background.model.SessionStatus` · `src/agenthicc/background/model.py:11`

### Worker

The child process that runs one background session: `main()` is its entry
point, and `BackgroundApprovalService` is how it asks for a decision when one
is needed.

**Provenance:** `agenthicc.background.worker.BackgroundApprovalService` · `src/agenthicc/background/worker.py:93`

### Supervisor

The parent-side component that starts, tracks, and reaps workers, and that owns
the queue of `BackgroundRequest`s.

**Provenance:** `agenthicc.background.supervisor.BackgroundSupervisor` · `src/agenthicc/background/supervisor.py:50`

### Terminal

A background-managed shell session with a durable record and a bounded output
tail. `TerminalStore` persists it under `~/.agenthicc/background/terminals`;
`TerminalManager` drives the live process.

**Provenance:** `agenthicc.background.terminals.TerminalManager` · `src/agenthicc/background/terminals.py:506`

### Session service

The optional client-neutral transport that lets a non-TUI client drive a
session over a stable protocol.

**Provenance:** `agenthicc.session_service.SessionService` · `src/agenthicc/session_service/service.py:110`

## Agents and subagents

### Role

An agent's declared function. The seven built-in roles are `planner`,
`executor`, `reviewer`, `explorer`, `verifier`, `human`, and `auto`. Phases
name a role, which is how a workflow says who should do the work.

**Provenance:** `agenthicc.agents.builtin.BUILTIN_AGENT_DEFINITIONS` · `src/agenthicc/agents/builtin.py:89`

### Subagent

A short-lived delegated worker spawned to run a bounded task inside its own
context, then folded back as an aggregate. Distinct from a *background
session*, which is a whole detached session.

**Provenance:** `agenthicc.subagents.SubagentTask` · `src/agenthicc/subagents/pool.py:222`

### Subagent pool

The concurrency-limited collection that owns pending and running subagents and
produces a single `AggregatedResult`.

**Provenance:** `agenthicc.subagents.pool.SubagentPoolState` · `src/agenthicc/subagents/pool.py:206`

## Tools

### Tool

A callable the agent may invoke, registered with a schema and capability
annotations and executed through `AgenthiccToolExecutor`.

**Provenance:** `agenthicc.tools.executor.AgenthiccToolExecutor` · `src/agenthicc/tools/executor.py:189`

### Hook

A lifecycle callback that observes or vetoes tool activity, registered in the
`HookRegistry` and run by the `HookRunner`.

**Provenance:** `agenthicc.tools.hooks.HookRegistry` · `src/agenthicc/tools/hooks.py:42`

### MCP server

An external tool provider reached over the Model Context Protocol.
`McpSessionManager` owns connection state, and a server may be optional
(isolated on failure) or required (fail-closed).

**Provenance:** `agenthicc.tools.mcp_manager.McpSessionManager` · `src/agenthicc/tools/mcp_manager.py:238`

### Project tool

A tool defined inside the user's project rather than shipped with agenthicc.
`make_agenthicc_tool` is the workflow that scaffolds one.

**Provenance:** `agenthicc.workflows.MakeToolRunner` · `src/agenthicc/workflows/make_agenthicc_tool/runner.py:342`

## Memory

### Memory tier

One of three scopes: `SESSION`, `PROJECT`, and `GLOBAL_`. Each tier is a
separate layer with its own store, and a write is routed to a tier rather than
taken globally.

**Provenance:** `agenthicc.memory.layers.MemoryTier` · `src/agenthicc/memory/layers.py:41`

### Memory router

The component that decides which tier a read or write belongs to.

**Provenance:** `agenthicc.memory.router.MemoryRouter` · `src/agenthicc/memory/router.py:33`

### Journal

The durable conversation record used to recover an interrupted turn.
`journal_path_for()` gives its location, and `ConversationJournal` reads and
writes it.

**Provenance:** `agenthicc.memory.journal.ConversationJournal` · `src/agenthicc/memory/journal.py:199`

### Semantic index

The vector-backed similarity store behind memory search, with a dependency-free
fallback so search still works without a vector backend.

**Provenance:** `agenthicc.memory.vector.SemanticIndex` · `src/agenthicc/memory/vector.py:101`

## CLI and commands

### Entry point

`main()` in `agenthicc/__main__.py`. It parses CLI arguments and then
dispatches to exactly one of three paths: a registered subcommand, the headless
runner, or the TUI runner.

**Provenance:** `agenthicc.__main__.main` · `src/agenthicc/__main__.py:37`

### Headless mode

Running without a TUI: the same session machinery with approvals resolved by
policy. Selected by `--headless`.

**Provenance:** `agenthicc.runners.headless` · `src/agenthicc/runners/headless.py`

### TUI

The default interactive surface. Its runtime helpers — mode management, session
logging, and replay — live under `agenthicc.tui.runtime`.

**Provenance:** `agenthicc.runners.tui_session` · `src/agenthicc/runners/tui_session.py`

### Slash command

A `/name` command entered in the prompt. The built-ins live in
`BUILTIN_COMMANDS`; project and skill-provided commands join the same
`UnifiedCommandRegistry`.

**Provenance:** `agenthicc.commands.builtins.BUILTIN_COMMANDS` · `src/agenthicc/commands/builtins.py:892`

### Command dispatcher

The component that resolves a parsed slash command against the registry and
executes it, applying the busy policy when a previous command is still running.

**Provenance:** `agenthicc.commands.dispatcher.CommandDispatcher` · `src/agenthicc/commands/dispatcher.py:11`

### Busy policy

The rule for what happens when a command arrives while another is still in
flight — run it, queue it, or refuse it.

**Provenance:** `agenthicc.commands.busy_policy` · `src/agenthicc/commands/busy_policy.py`

### Cassette

A recorded provider exchange, replayed instead of a live call when
`--record_cassette` is in effect. The default location is
`~/.agenthicc/cassettes`.

**Provenance:** `agenthicc.cli.parser.parse_cli` · `src/agenthicc/cli/parser.py:102`

## Skills

### Skill

A packaged capability delivered as a `SKILL.md` file with typed frontmatter.
The loader validates the frontmatter, so an unknown field is a diagnostic
rather than a silent omission.

**Provenance:** `agenthicc.skills.loader.SkillDef` · `src/agenthicc/skills/loader.py:88`

### Skill diagnostic

A structured report from the loader — an unknown key, a missing name, a
duplicate — surfaced instead of failing quietly. This is why a malformed skill
is visible at load time.

**Provenance:** `agenthicc.skills.loader.SkillDiagnostic` · `src/agenthicc/skills/loader.py:63`

### Bootstrap skill

A skill shipped with agenthicc that provides slash commands — including
`/create-tools` and `/create-commands`, which exist as bootstrap skills rather
than as `BUILTIN_COMMANDS` entries.

**Provenance:** `agenthicc.skills.bootstrap` · `src/agenthicc/skills/bootstrap.py`

## Usage and accounting

### Usage ledger

The record of token and cost accounting for a run, including the quality and
cost-status enums that mark a row as estimated or unpriced.

**Provenance:** `agenthicc.runners.usage_ledger.UsageRecord` · `src/agenthicc/runners/usage_ledger.py:163`

### Durable idempotency ledger

The ledger that makes a side effect replay-safe: a repeated effect with the
same id is recognised and not performed twice.

**Provenance:** `agenthicc.runners.durable_ledger.DurableIdempotencyLedger` · `src/agenthicc/runners/durable_ledger.py:46`

### Recovery projection

The component that turns an interrupted tool call into a stable projection for
the TUI and the headless transcript, so recovery is rendered rather than
narrated.

**Provenance:** `agenthicc.runners.recovery_projection.ToolRecoveryProjector` · `src/agenthicc/runners/recovery_projection.py:10`

## Verifying this page

Every provenance line above is machine-checkable:

```bash
PYTHONPATH=src python /tmp/check_nav_pages.py
```

The checker imports each dotted symbol, confirms each `path:line` exists, and
– where both are given – confirms the cited line actually mentions the symbol.

## Related

- [Documentation fact base](reference/fact-base.md) — the full provenance
  register behind the reference pages.
- [Architecture diagram](guides/architecture-diagram.md) — how these packages
  fit together.
- [Troubleshooting index](reference/troubleshooting-index.md) — symptom-first
  navigation.
