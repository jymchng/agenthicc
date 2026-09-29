---
title: "PRD-204: Goal runner and detached orchestration for Agenthicc"
status: Implemented
repository: jymchng/agenthicc
depends_on:
  - PRD-141
  - PRD-150
  - PRD-170
  - PRD-171
  - PRD-203
---

# PRD-204 — Goal runner and detached orchestration for Agenthicc

## 1. Summary

Add a goal-oriented entry point and a durable run projection to Agenthicc.
Users should be able to start work with:

```bash
agenthicc --goal "Add OAuth authentication to the API"
agenthicc --goal "Add OAuth authentication to the API" --detach
```

The first form opens an attached Agenthicc experience and submits the goal as
the initial user intent. The second form creates a durable goal run, starts its
main Agenthicc session through the existing background-session supervisor, and
returns without waiting for completion.

The run must remain inspectable and recoverable after the invoking terminal or
CLI process exits. Users should be able to discover and control it through
Agenthicc itself:

```bash
agenthicc runs
agenthicc runs show RUN_ID
agenthicc agents --run RUN_ID
agenthicc attach RUN_ID
agenthicc runs cancel RUN_ID
```

The feature is an adaptation of the detached multi-agent orchestration
proposal to Agenthicc's existing ownership boundaries. It must reuse:

* `BackgroundStore` and `BackgroundSupervisor` for local process execution;
* `BackgroundSession` for durable process/session lifecycle;
* `WorkflowRunHandle` and workflow checkpoints for workflow state;
* the session journal and session-service projection for conversation state;
* `SessionOwnerLease` for single-live-owner attachment;
* PRD-203 `ParallelCoordinator` and `WorktreeManager` for isolated workers;
* the existing `agents`/`jobs` TUI manager as a compatible control surface.

It must not create a second agent loop, a second session journal, a second
background process manager, or a second implementation of Git worktrees.

## 2. Current Agenthicc baseline

This PRD starts from the following current contracts. They are dependencies,
not requirements to duplicate.

| Existing capability | Canonical implementation | Reuse requirement |
| --- | --- | --- |
| Background process lifecycle | `src/agenthicc/background/supervisor.py` | Detached goal execution must launch through `BackgroundSupervisor`, not an untracked `Popen`. |
| Durable background registry | `src/agenthicc/background/store.py` | A goal run must link to the existing main and worker session records. |
| Background heartbeat and stale detection | `background/worker.py`, `BackgroundSupervisor.recover_stale()` | Run projections must surface stale/orphaned sessions instead of claiming they are running. |
| Background TUI and `agenthicc agents` | `src/agenthicc/tui/workspace/background_manager.py`, `cli/commands/background.py` | Preserve the current manager and add run-aware filtering/details. |
| Foreground handoff | `BackgroundSupervisor.attach_foreground()` | Attach must stop/release the background owner before opening a foreground TUI. |
| Workflow execution | `src/agenthicc/runners/headless.py`, `workflows/` | The main goal session must run the selected workflow through the existing runner. |
| Workflow durability | `WorkflowRunHandle`, workflow checkpoints, workflow recovery | Reattach/resume must use existing checkpoint validation and topology rules. |
| Session ownership | `src/agenthicc/runners/session_owner.py` and related lease code | No two TUI/background processes may write the same session concurrently. |
| Client-neutral projection | PRD-150 session service | Run/agent state may be exposed through the existing projection; no parallel API runtime. |
| Worker agents | PRD-203 `ParallelCoordinator`, `WorktreeManager`, `spawn_worker_agents` | Worker records must remain linked to the goal run and isolated worktrees. |
| Tool and capability policy | `tools/capabilities.py`, `WorkspaceAccessPolicy`, approval services | Detached execution must inherit the same security and approval policy as attached execution. |

### 2.1 Current gaps this PRD addresses

The repository currently has durable background sessions and an `agents`
manager, but it does not yet provide all of the following as one coherent
goal-run product:

1. a global `--goal` entry point;
2. a distinct durable run identity for a complete goal, separate from a
   background session ID;
3. a run-level projection that joins the main session, worker sessions,
   tasks, worktrees, Git state, and attention conditions;
4. direct `attach`, `runs`, and run-filtered `agents` CLI commands;
5. a first-class attached-to-detached handoff for an active goal;
6. run-aware JSON output and stable exit-code behavior;
7. recovery that reconciles run records with background sessions and Git
   worktree manifests without recreating completed workers.

The existing `agenthicc agents` command remains valid. In the current product
it opens the background-session manager and is an alias of `jobs`; this PRD
extends that surface rather than silently replacing it.

## 3. Problem

Agenthicc is currently easiest to use while the initiating terminal remains
attached. Background sessions exist, but a user must use the lower-level
background/job interface and session IDs. There is no canonical operation for:

```text
natural-language goal
    → durable run identity
    → main Agenthicc session
    → optional worker sessions/worktrees
    → integration and verification
    → inspectable final result
```

Without a run-level identity:

* a terminal can disappear while the user loses the logical task identity;
* several sessions belonging to one goal cannot be presented as one unit;
* the existing `agents` manager cannot distinguish a main goal session from
  unrelated background work or worker sessions;
* attachment requires knowledge of a background session ID rather than the
  goal's identity;
* run status cannot reliably combine workflow state, worker state, and Git
  integration state;
* process recovery and worktree recovery are separate operations from the
  user's point of view.

## 4. Goals

### G1 — Goal-based entry point

Support `agenthicc --goal GOAL` as a canonical way to start an Agenthicc
operation. The goal is persisted before the first provider call.

### G2 — Attached execution

Run the goal in the normal TUI/session architecture. The initial goal is
submitted exactly once; reopening or resuming the session must not submit a
duplicate initial goal.

### G3 — Detached execution

Support `agenthicc --goal GOAL --detach`. The command must return after the
run and main session have been durably accepted, without waiting for the goal
to finish.

### G4 — Durable run identity

Every goal invocation creates a globally unique local `run_id`, preferably in
the form `run_<opaque-id>`. The run identity is independent of:

* the foreground session ID;
* the main background session ID;
* worker session IDs;
* workflow run/checkpoint IDs;
* Git branch names.

Those identifiers must be linked, never substituted for one another.

### G5 — Run-level observability

The user can discover a run after the original CLI process exits and can see:

* goal and repository;
* lifecycle status;
* main session and workflow phase;
* workers and their sessions;
* tasks and dependencies;
* worktrees and branches;
* integration/verification state;
* attention conditions;
* latest activity and timestamps.

### G6 — Reattachment

`agenthicc attach RUN_ID` reconnects to an existing run without creating a
new run or duplicate main/worker sessions.

### G7 — Safe lifecycle controls

Users can inspect, cancel, resume/retry where the underlying session contract
allows it, and preserve potentially useful worktrees. No cancellation or
cleanup operation may silently delete unintegrated changes.

### G8 — Worker/run integration

Worker agents summoned through the universal `spawn_worker_agents` tool must
be discoverable under the parent goal run. Their existing PRD-203 worktree,
branch, session, and Git evidence remain authoritative.

### G9 — Terminal independence

The terminal is a client of a run, not the owner of run state. Closing the
terminal or exiting the initiating CLI must not make a healthy detached run
untrackable.

## 5. Non-goals

The first release does not provide:

* distributed or remote execution;
* a cloud Agenthicc service;
* Kubernetes/container orchestration;
* multi-machine worker scheduling;
* a web dashboard;
* unrestricted concurrent mutation of a primary worktree;
* automatic semantic resolution of arbitrary Git conflicts;
* a new general-purpose daemon independent of `BackgroundSupervisor`;
* a second conversation store or event stream for the same session;
* a guarantee that a machine reboot can resume a process that no longer
  exists. The persisted run must remain visible and recoverable/attentionable.

## 6. Product terminology

### 6.1 Goal

The user-provided natural-language request. It is immutable for the run's
identity but may be supplemented by clarification messages in the shared
session conversation.

### 6.2 Run

The durable product-level lifecycle record for one goal invocation. A run
joins the main session, optional worker orchestration(s), workflow state,
Git/integration state, events, and final outcome.

### 6.3 Main agent

The Agenthicc session that owns goal interpretation, workflow execution,
worker dispatch, integration decisions, and verification. In detached mode it
is represented by one `BackgroundSession` with `role="main"`.

### 6.4 Worker agent

An Agenthicc background session created by the main agent for a task. A worker
uses the existing PRD-203 worktree/branch contract and must never write the
main worktree directly.

### 6.5 Session

The existing conversation/provider/workflow execution identity. A run may
reference one main session and many worker sessions; a session must never be
silently shared by two live owners.

### 6.6 Workflow run

The existing typed workflow execution/checkpoint identity. It is linked from
the goal run and remains the source of truth for phase and checkpoint state.

## 7. CLI contract

### 7.1 Start an attached goal

```bash
agenthicc --goal "Add rate limiting to the public API"
```

Behavior:

1. validate configuration and the repository before creating durable state;
2. create the run record and initial main-session identity;
3. start the normal TUI in the current repository;
4. submit the goal once as the initial user intent;
5. preserve the ordinary TUI commands, approval flow, workflow checkpoints,
   and shared conversation memory;
6. update the run record as the session progresses and on exit.

`--goal GOAL` is exactly the CLI shorthand for selecting the registered
`goal_flow` workflow and submitting `GOAL` as its initial user input (the same
operation as `/workflow goal_flow` followed by submitting `GOAL`). It must not be
combined with `--workflow`; the ordinary `--workflow NAME` option remains
available for non-goal TUI and headless starts. The `goal_flow` registration is
validated before the run is created, so an invalid installation fails without
leaving a ghost run.

### 7.2 Start a detached goal

```bash
agenthicc --goal "Implement OAuth login and integration tests" --detach
```

Required sequence:

```text
validate repository/configuration
  → allocate run_id
  → persist RunCreated
  → create/link main BackgroundSession
  → launch through BackgroundSupervisor
  → persist accepted process/session identity
  → print run summary
  → exit without waiting for completion
```

The command must not fork an untracked process directly. The returned success
means “durable execution accepted”, not “the future goal will succeed”. A
provider failure, later worker failure, or eventual run failure must not change
the detached-start exit code after successful acceptance.

### 7.3 Detached output

Human-readable output should be concise:

```text
Agenthicc detached run started

Run ID:  run_01JABC123
Goal:    Implement OAuth login and integration tests
Status:  running
Main:    <main-session-id>

Track:   agenthicc agents --run run_01JABC123
Attach:  agenthicc attach run_01JABC123
```

`--json` is supported for detached start and returns a stable object:

```json
{
  "run_id": "run_01JABC123",
  "status": "running",
  "detached": true,
  "main_session_id": "...",
  "workflow_name": "goal_flow"
}
```

Secrets, raw prompts, provider credentials, environment variables, and full
conversation contents must not appear in this output.

### 7.4 Run listing and inspection

The adapted command surface avoids conflicting with the existing top-level
`run` command:

```bash
agenthicc runs
agenthicc runs list [--status STATUS] [--json]
agenthicc runs show RUN_ID [--json]
agenthicc runs cancel RUN_ID
agenthicc runs resume RUN_ID
```

`agenthicc runs` is an alias for `agenthicc runs list`.

The existing `agenthicc jobs` command remains available for low-level
background-session management. `runs` is a goal-level projection and must not
silently expose unrelated background jobs as goal runs.

### 7.5 Agent listing

Preserve the current interactive manager:

```bash
agenthicc agents
```

Add run-aware modes:

```bash
agenthicc agents --run RUN_ID
agenthicc agents --run RUN_ID --json
```

Without `--run`, the current background-session manager behavior remains
backwards compatible. With `--run`, the view shows the main agent, worker
agents, task, process/session, workflow phase, worktree, branch, Git status,
heartbeat freshness, and attention state.

### 7.6 Attach

```bash
agenthicc attach RUN_ID
```

Attach resolves the run's main session and then uses the existing
`BackgroundSupervisor.attach_foreground()` plus `_run_tui_session(resume_id=...)`
handoff. It must:

* claim the run/session owner before opening the TUI;
* stop the background main worker before starting foreground execution;
* reuse the existing conversation journal and workflow checkpoint;
* reconnect the run projection and event stream;
* refuse a second live owner with a clear recovery instruction;
* never create a new run or duplicate worker sessions.

### 7.7 Cancellation and resume

```bash
agenthicc runs cancel RUN_ID
agenthicc runs resume RUN_ID
```

Cancellation requests the main supervisor to stop the main session and
propagates cancellation to active child worker sessions where the run record
identifies them. It preserves run metadata, event history, worker branches,
and dirty/unintegrated worktrees.

Resume/retry must use the existing background and workflow resume contracts.
It must reconcile first and then continue from the saved checkpoint/session
journal. It must not restart a completed workflow phase or recreate a worker
whose durable task/worktree identity already exists.

## 8. Run data model

Introduce a small run-level domain model under a new canonical package such as
`src/agenthicc/runs/`. The exact module names may follow repository convention,
but ownership must remain separate from `background/`, `workflows/`, and the
reactive TUI.

### 8.1 `GoalRun`

Minimum durable fields:

```text
run_id
goal
repository
repository_root
base_commit
main_branch
status
created_at
started_at
completed_at
last_updated_at
main_session_id
main_agent_id
workflow_name
workflow_run_id
detached
exit_code
result_summary
failure_reason
attention_reasons
orchestration_ids
```

Optional non-secret metadata:

```text
provider
model
configuration_fingerprint
git_remote_name
token_usage_summary
estimated_cost_summary
```

Never persist API keys, resolved secret values, complete environment maps, or
unredacted provider requests.

### 8.2 `RunAgentRecord`

Each main or worker projection includes:

```text
agent_id
run_id
role
task_id
session_id
process_id
workflow_run_id
status
worktree_id
worktree_path
branch
base_commit
head_commit
started_at
completed_at
last_heartbeat_at
last_activity_at
current_phase
current_operation
attention_reason
```

The record is a projection/link, not a second authority for background
session status or Git state. Background session and worktree records remain
authoritative for their respective fields.

### 8.3 Persistence

Use an append-only, locked, atomically durable local store consistent with the
existing `BackgroundStore` and session storage contracts. The default location
is:

```text
~/.agenthicc/runs/
```

The store must support:

* create and fold a run from events;
* atomic updates under a process lock;
* recovery after a partial write;
* redacted public projections;
* filtering by status, repository, and age;
* retention without deleting active or unintegrated work;
* deterministic tests with an injected temporary root.

Run records may reference the existing background event log and PRD-203
manifest paths, but they must retain enough identity to locate those records
after restart.

## 9. Run lifecycle

The public run state is deliberately coarser than individual session states:

```text
CREATED
  → STARTING
  → PLANNING
  → RUNNING
  → WAITING
  → NEEDS_ATTENTION
  → INTEGRATING
  → VERIFYING
  → COMPLETED
```

Terminal/error states:

```text
FAILED
CANCELLED
```

The state machine must permit recovery transitions without erasing history:

```text
FAILED/NEEDS_ATTENTION/LOST → STARTING or RUNNING
WAITING                    → RUNNING or NEEDS_ATTENTION
RUNNING                    → CANCELLED through an explicit cancellation path
```

Run state is derived from authoritative child evidence using deterministic
precedence. For example, an active child with a fresh heartbeat may keep the
run `RUNNING`; a missing child process becomes `NEEDS_ATTENTION` or `FAILED`
according to the recovery policy; an integration conflict is never rendered as
`COMPLETED`.

## 10. Main-agent execution

### 10.1 Attached mode

The attached main agent runs through the existing TUI/session boundary. Goal
startup must preserve:

* `SessionContext` and one workspace access policy;
* one conversation ID and journal;
* existing mode/approval behavior;
* workflow prompt-cache contracts;
* workflow checkpoints and phase receipts;
* existing user question and interruption behavior.

The run store receives lifecycle events from the session/workflow boundary,
not from scraping rendered TUI text.

### 10.2 Detached mode

The detached main agent runs through the existing headless/background adapter:

```text
GoalRunManager
  → BackgroundSupervisor.submit(...)
  → background.worker
  → runners.headless / selected workflow
  → existing journal/checkpoint/session stores
```

The run manager records the main background session ID before returning. The
background worker publishes heartbeats and updates its existing
`BackgroundSession`; a run projection reconciler folds this into `GoalRun`.

No detached goal may depend on the parent CLI's asyncio loop, TTY, stdin,
Rich Live object, or in-memory callbacks.

### 10.3 Default workflow

`--goal` always selects `goal_flow`. A goal invocation combined with
`--workflow NAME` is rejected during argument validation, before repository
validation or run creation. The ordinary `--workflow NAME` option remains
available for non-goal TUI and headless starts. `goal_flow` must be registered
and compatible with the current repository/session configuration before a goal
run is created.

## 11. Worker agents and PRD-203 integration

The main agent may call the universal `spawn_worker_agents` tool. The run
manager must make the relationship explicit:

```text
GoalRun
  ├── main BackgroundSession
  ├── ParallelManifest / orchestration
  │     ├── task
  │     ├── worker BackgroundSession
  │     ├── WorktreeRecord
  │     └── branch/base/head evidence
  └── integration/verification result
```

Required adaptations to PRD-203:

1. record the parent `run_id` on the worker session/manifest projection;
2. expose worker records through the run projection and `agents --run`;
3. preserve the existing one-task/one-session/one-worktree invariant;
4. retain the immutable base commit and serialized main-branch integration;
5. cancel workers through their existing supervisor/session controls;
6. reconcile manifests and Git worktrees before resuming a run;
7. never recreate a completed worker from conversational text alone.

The existing `spawn_worker_agents` tool remains available to every normal
workspace-scoped workflow. This PRD adds run correlation and lifecycle
projection; it does not create a second worker tool.

## 12. Agent and process supervision

### 12.1 Reuse the current supervisor

`BackgroundSupervisor` is the local supervisor for MVP. It already provides
submission, PID tracking, cancellation, foreground handoff, retry/resume,
heartbeat-backed stale detection, and per-project/global limits.

Do not add a separate run daemon in the first implementation.

### 12.2 Heartbeats and stale state

The run projection must use the existing heartbeat and stale thresholds. It
must surface:

```text
worker process missing
heartbeat expired
session owner lost
worktree missing
workflow checkpoint unavailable
```

An active status without fresh authoritative evidence must not be displayed as
healthy `RUNNING` indefinitely.

### 12.3 Main/worker identity

Use stable explicit identity links:

```text
run_id
main_agent_id / main_session_id
worker agent_id / worker session_id
parent_session_id
task_id
worktree_id
workflow_run_id
```

Session IDs and process IDs are operational identities, not user-facing run
IDs. A process restart may change the process ID without changing the run,
agent, task, session, or worktree identity.

## 13. Attach, detach, and ownership

### 13.1 Attach safety

Before opening a run in the TUI:

1. resolve the run record;
2. reconcile the main background session and workflow checkpoint;
3. acquire the existing session owner lease;
4. perform `attach_foreground()` if the main session is active in the
   background;
5. open the existing session with `resume_id`;
6. subscribe the UI to the run projection/event stream.

If another live owner exists, return a `run_already_claimed`-style diagnostic
with the owner, PID/host where safe, and the exact `attach`/close-process
recovery options. Never start a second writer.

### 13.2 Detach from an attached session

Add an explicit `/detach` command for a run-backed foreground session. `Ctrl+D`
may remain a terminal EOF/exit behavior and is not the only detach mechanism.

The handoff must:

1. stop accepting new foreground input;
2. persist the current journal/checkpoint boundary;
3. release the foreground owner only after the background request is durable;
4. launch or hand off the same main session through `BackgroundSupervisor`;
5. record `RunDetached`;
6. close the TUI without cancelling the run.

Failure during handoff must leave one clear owner and a recoverable run; it
must never create concurrent foreground/background writers.

## 14. Run projection and event model

### 14.1 Events

The run store should support events equivalent to:

```text
RunCreated
RunStarted
RunDetached
RunAttached
RunWaiting
RunNeedsAttention
RunCompleted
RunFailed
RunCancelled

MainSessionLinked
WorkflowLinked

AgentCreated
AgentStarted
AgentWaiting
AgentReady
AgentCompleted
AgentFailed
AgentLost
AgentCancelled

TaskLinked
WorktreeLinked
WorktreeReconciled

IntegrationStarted
IntegrationCompleted
IntegrationConflict
VerificationStarted
VerificationPassed
VerificationFailed
```

Event payloads contain IDs, bounded summaries, statuses, timestamps, Git
metadata, and redacted error categories. They must not contain credentials or
unbounded provider messages.

### 14.2 Projection

The projection should answer:

```text
What is the run doing?
Why is it waiting or blocked?
Which agents exist?
Where is each agent working?
What Git changes exist?
What must the user do next?
Can this run be safely attached/resumed/cancelled?
```

The TUI, CLI, and future session-service clients consume this projection.
They must not each fold background/worktree/session state differently.

## 15. TUI integration

The existing background manager remains the compatibility surface for
`agenthicc agents` and `agenthicc jobs`. Extend it with a run-aware view rather
than replacing it with a new application.

### 15.1 Run list

The run list should display:

```text
RUN                    STATUS       AGENTS   AGE   ATTENTION
run_01JABC123          RUNNING         3     12m   —
run_01JABC456          COMPLETED       2      1h   —
run_01JABC789          NEEDS_ATTENTION 4      3h   conflict
```

### 15.2 Run detail

Selecting a run shows:

* goal and repository;
* main session/workflow/current phase;
* agent table and heartbeat freshness;
* task and dependency state;
* worktree/branch/base/head and dirty state;
* integration queue/conflicts;
* verification result;
* bounded recent events;
* actions allowed by current mode and ownership.

### 15.3 Agent detail

Selecting an agent shows its durable session, process, task, worktree, branch,
Git summary, current operation, latest activity, and available actions. Raw
secrets and unbounded logs are excluded; a bounded artifact/log path may be
shown where policy permits.

## 16. Git and repository policy

Before creating a run:

* verify Git is available and the path is a repository;
* resolve repository root, current branch, and base commit;
* record dirty-state policy explicitly;
* refuse ambiguous repository identity;
* do not write a goal run record if validation fails.

The first implementation should default to the PRD-203 safety policy:

> A dirty coordinator worktree is rejected for multi-worker orchestration
> unless the user explicitly chooses a supported snapshot/continuation policy.

Attached single-agent goal execution may preserve the existing user workflow,
but any worker dispatch must still obey PRD-203's clean immutable-base rule.

Run-level Git state is derived from `WorktreeManager`, `ParallelManifest`, and
the actual repository—not from agent prose.

## 17. Concurrency and resource limits

Distinguish:

```text
agent/process execution concurrency
    ≠
primary-branch integration concurrency
```

Reuse the existing settings for the first implementation:

```toml
[background]
max_workers = 2
max_workers_per_project = 2

[execution]
max_parallel_tasks = 4
```

The implementation must define how these limits compose. At most one
integration operation may mutate a primary branch at a time. Multiple worker
worktrees may execute concurrently subject to background and workflow limits.

Future limits such as max concurrent goal runs, CPU, and memory are out of
scope for the first release but the run model must leave room for them.

## 18. Security and privacy

Detached execution inherits the exact provider, workspace, network,
capability, approval, and secret-resolution policy of the initiating session.

Requirements:

* do not persist resolved API keys or secret headers;
* do not persist complete environment maps;
* redact public run/session projections using the existing session-export
  redaction contract;
* keep run/event files user-readable only where existing storage policy
  permits, preferably mode `0600`;
* prevent a run from attaching to a repository/worktree outside its recorded
  workspace policy;
* keep `dangerously_skip_permissions` explicit and invocation-scoped;
* do not weaken Safe/Plan capability gates to make detached execution work;
* preserve owner-lease and approval semantics after attach/resume;
* preserve unintegrated worktrees on cancellation or failed integration.

Git worktrees are isolation for repository state, not a process security
sandbox. The existing Agenthicc workspace/network/tool policies remain the
security boundary.

## 19. Recovery requirements

On every `runs`, `agents`, `attach`, `resume`, or run-aware TUI refresh:

1. load the run record;
2. reconcile linked background sessions and stale PIDs/heartbeats;
3. reconcile workflow checkpoints and session ownership;
4. reconcile linked PRD-203 manifests with `git worktree list --porcelain`;
5. derive attention conditions;
6. persist only evidence-backed status changes;
7. never recreate completed task/session/worktree identities automatically.

Recovery outcomes include:

```text
healthy active
waiting for approval/input
stale/orphaned main session
lost worker
missing worktree
workflow checkpoint mismatch
integration conflict
completed but unintegrated
completed
failed
cancelled
```

If a process is gone but its conversation/checkpoint/worktree is intact, the
run remains inspectable and offers an explicit resume/retry/recover action.
If the worktree contains dirty or unintegrated changes, recovery must preserve
it and surface the path.

## 20. JSON and exit-code contracts

All run/agent inspection commands should support `--json` with stable,
redacted schemas. Human-readable output may evolve; machine-readable keys may
not be removed without a compatibility policy.

Suggested top-level run projection:

```json
{
  "run_id": "run_01JABC123",
  "goal": "Add OAuth authentication",
  "repository_root": "/workspace/app",
  "status": "running",
  "main_session_id": "...",
  "workflow_name": "goal_flow",
  "workflow_run_id": "...",
  "agents": [],
  "worktrees": [],
  "attention": [],
  "created_at": 0.0,
  "last_updated_at": 0.0
}
```

Start command exit codes:

```text
0  detached/attached startup accepted
2  invalid CLI arguments
3  repository/configuration/workflow validation failure
1  unexpected startup failure
```

For `--detach`, eventual run failure must not alter the already-returned
startup code. Inspection/control commands return nonzero for invalid IDs,
ownership conflicts, or failed control operations.

## 21. Acceptance criteria

### Goal entry and startup

- [ ] `agenthicc --goal GOAL` starts one attached goal run and submits GOAL
      once.
- [ ] `agenthicc --goal GOAL --detach` returns a durable `run_id` without
      waiting for goal completion.
- [ ] Invalid repository/configuration/workflow input fails before creating a
      ghost run.
- [ ] `--goal GOAL` always selects registered `goal_flow` and rejects a
      competing `--workflow NAME` before creating a run.
- [ ] Detached startup supports the documented human and JSON output.

### Persistence and identity

- [ ] Every goal invocation has a durable run record.
- [ ] Run, main session, workflow, worker session, task, worktree, and process
      identities are linked explicitly.
- [ ] Run state survives termination of the initiating CLI process.
- [ ] Run storage is locked, redacted, crash-tolerant, and testable under a
      temporary root.

### Tracking and controls

- [ ] `agenthicc runs` lists goal runs, not unrelated background sessions.
- [ ] `agenthicc runs show RUN_ID` exposes the complete bounded run projection.
- [ ] `agenthicc agents --run RUN_ID` shows main and worker agents.
- [ ] Existing `agenthicc agents` and `agenthicc jobs` behavior remains
      compatible.
- [ ] `agenthicc runs cancel RUN_ID` stops active children and preserves useful
      artifacts/worktrees.

### Attach/detach

- [ ] `agenthicc attach RUN_ID` reconnects to the existing main session.
- [ ] Attach does not create a duplicate run, session, worker, or workflow.
- [ ] Attach performs the existing foreground handoff and owner claim.
- [ ] A live owner conflict is reported clearly and leaves the existing owner
      untouched.
- [ ] `/detach` hands an attached run to the existing background supervisor
      without losing journal/checkpoint state.

### Workers and Git

- [ ] Every worker summoned by the main goal appears in the run projection.
- [ ] Each worker retains PRD-203's unique session, task, branch, worktree,
      immutable base commit, and Git evidence.
- [ ] Worker completion is derived from session/Git evidence, not prose.
- [ ] Primary-branch integration is serialized and conflict-preserving.
- [ ] Cancellation and recovery never silently delete dirty or unintegrated
      worker worktrees.

### Recovery and observability

- [ ] Missing worker processes become `LOST`/attentionable rather than staying
      indefinitely `RUNNING`.
- [ ] Stale heartbeats are visible in the run/agent projection.
- [ ] A terminated main process can be inspected and resumed/retried according
      to the existing checkpoint/session contract.
- [ ] Run status is reconstructible from durable run state plus authoritative
      background/workflow/Git state.
- [ ] TUI and CLI render the same projected state.

### Security and quality

- [ ] Detached execution preserves workspace, capability, network, approval,
      and secret policies.
- [ ] Public output and persisted metadata contain no resolved credentials.
- [ ] Unit tests cover run IDs, lifecycle transitions, validation, redaction,
      projection, and invalid control operations.
- [ ] Integration tests cover supervisor handoff, process loss, restart,
      workflow resume, worker correlation, and Git reconciliation.
- [ ] End-to-end tests cover attached goal, detached goal, list, inspect,
      attach, cancel, and worker visibility.

## 22. Implementation phases

### Phase 0 — Archaeology and contracts

Document the current parser, CLI registry, session construction, background
worker, workflow runner, owner lease, session service, and PRD-203 boundaries.
Define the run event/projection schema before changing CLI behavior.

### Phase 1 — Goal entry point

Add `--goal`, `--detach`, and `--json` parsing with validation. Implement
attached goal submission without changing normal no-argument TUI startup.

### Phase 2 — Run store and projection

Add `GoalRun`, run events/store, redacted projection, lifecycle reducer, and
links to existing session/workflow identities. No new process runtime is
permitted.

### Phase 3 — Detached main session

Create the run record and submit its main session through
`BackgroundSupervisor`. Add durable accepted-start output, stale reconciliation,
and terminal process independence.

### Phase 4 — CLI control plane

Add `runs`, `attach`, `runs cancel`, `runs resume`, and run-filtered `agents`
while preserving `jobs`. Add JSON schemas and stable exit codes.

### Phase 5 — Worker/run correlation

Extend the PRD-203 projection and `spawn_worker_agents` boundary with run IDs.
Join worker sessions, tasks, manifests, worktrees, branches, and Git evidence
in the run projection.

### Phase 6 — TUI run view and `/detach`

Add run list/detail/agent views to the existing background manager and implement
an owner-safe foreground/background handoff for `/detach`.

### Phase 7 — Recovery and verification

Exercise process termination, terminal closure, stale heartbeat, main crash,
worker crash, merge conflict, missing worktree, checkpoint mismatch, and
machine-restart simulations. Add docs and release gates.

## 23. Test plan

### Unit

* run ID validation and uniqueness;
* run lifecycle transition/reducer rules;
* event-log folding and crash-tolerant reads;
* redaction and bounded field limits;
* projection joins and status precedence;
* CLI argument conflicts and exit-code mapping;
* worker/run correlation;
* attention-condition derivation;
* attach/detach ownership decisions.

### Integration

* detached startup with a temporary `BackgroundStore` and supervisor;
* accepted start after the parent CLI exits;
* background heartbeat and stale-session reconciliation;
* attach foreground handoff and duplicate-owner rejection;
* workflow journal/checkpoint rehydration;
* PRD-203 manifest/worktree/worker projection joins;
* cancellation preserving dirty worktrees;
* Git integration serialization and conflict preservation;
* session-service projection of a run.

### End to end

Use a temporary Git repository and deterministic fake provider/transport to
verify:

1. attached `--goal` runs once;
2. detached `--goal` returns immediately with a run ID;
3. `runs`, `runs show`, and `agents --run` display the run;
4. the original process can terminate while the background worker continues;
5. `attach` resumes the same main session without duplicate workers;
6. worker dispatch appears under the same run and retains worktree isolation;
7. cancellation, process loss, conflict, and resume are visible and safe.

## 24. Documentation requirements

Update in the same implementation:

* `README.md` CLI and lifecycle sections;
* a guide such as `docs/guides/goal-runs.md`;
* `docs/guides/background-sessions.md` for the run/session boundary;
* `docs/reference/storage.md` for run persistence and retention;
* `docs/reference/cli.md` for flags, commands, JSON, and exit codes;
* `docs/guides/parallel-workers.md` for run correlation;
* `llms.txt`/`llms-full.txt` for public run APIs;
* this PRD index and implementation evidence.

Documentation must state clearly that:

```text
Run state       is not process state
Session state   is not run state
TUI state       is not durable orchestration state
Git evidence    is not agent prose
```

## 25. Open decisions

These decisions must be resolved during Phase 0 and recorded before
implementation:

1. JSONL run events versus a small SQLite run store. The default recommendation
   is JSONL to align with `BackgroundStore`, unless projection queries require
   SQLite.
2. Resolved: `--goal` always selects the registry-validated `goal_flow`
   workflow. A configurable workflow alias is intentionally out of scope for
   this entry point; ordinary `--workflow` starts remain available separately.
3. Whether `/detach` hands off the existing foreground session ID or creates a
   new background execution ID linked to the same run. The recommendation is
   one session identity with an atomic owner handoff where possible.
4. The exact retention policy for completed runs, which must never remove
   active or unintegrated worktree evidence.
5. Whether `agenthicc run` eventually gains a `show` subcommand or remains the
   existing one-shot/background command. The initial adapted interface uses
   `agenthicc runs` to avoid a breaking collision.

## 26. Definition of done

The feature is complete when a user can run:

```bash
agenthicc --goal "Build feature X" --detach
```

close the terminal, later run:

```bash
agenthicc agents
agenthicc runs show <run-id>
agenthicc attach <run-id>
```

and recover the same durable Agenthicc run, including main-agent state,
workflow checkpoint, worker sessions, tasks, worktrees, Git changes, events,
tests, integration state, and attention conditions—without creating duplicate
owners or silently deleting useful work.

The terminal is a temporary view into a persistent Agenthicc run.
