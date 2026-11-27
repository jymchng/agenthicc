---
title: "SUBAGENT-19: Background session handoff with live subagents"
status: In verification
version: 1.2.0
date: 2026-11-24
priority: P0
related_prds:
  - SUBAGENT-08
  - SUBAGENT-11
  - SUBAGENT-15
  - SUBAGENT-16
  - SUBAGENT-17
  - SUBAGENT-18
  - prd-141
  - prd-227
  - prd-245
---

# SUBAGENT-19 — Background session handoff with live subagents

## Problem

A user running `goal_flow` in Yolo tried to hand the session to the background
service while five Executor subagents were running. The TUI replied
“Collect the workflow phase's subagents before handing this session off.”
The parent had already delegated the work. The user wanted to leave the
terminal while the parent and workers continued, then inspect or reattach
to the same session. Waiting for collection defeats that use case.

A second report showed the failure in an ordinary session with no workflow.
Five Explorers were running, `/bg` printed
`Successfully backgrounded · 504b8eec-a43 (queued)`, and every Explorer row
then read `parent_cancelled`. The command had been accepted, and the work it
was meant to preserve was destroyed. No guard stopped it, because only
workflow-phase pools were checked. The invariant for every alias and every
session kind is: **a handoff never cancels a live worker.** It either leaves
those worker attempts running under the service, or it is refused with a typed
reason before anything is cancelled, persisted or launched. It never reports
success and then settles the workers as cancelled.

An accepted `/detach`, `/bg`, or `/background` must return control to the
terminal with those worker attempts still running. The service must manage the
same session. The owning phase must receive the existing pools' updates,
questions, and outcomes, and collect them before advancing. The operation
must preserve running worker identity and work already performed.

## Current behavior and diagnosis

Cancellation origin for the second report: the legacy handoff cancels the
foreground turn (`task.cancel()` in `background/integration.py`), and a
cancelled `spawn_subagents` call makes `SubagentPool.run` request an abort of
every worker with reason `parent_cancelled`. The TUI guard consulted only
workflow-phase pools, so an ownerless direct-session pool passed it. The
terminal row `Successfully backgrounded` is written after the foreground turn
finishes unwinding, which is why it appeared above the cancelled rows.

Source baseline is `e2cc244`, the merge of SUBAGENT-18. The reported screen is
the user observation. The code and automated regression establish the guard
that produced the message.

1. The TUI checks for a pool call in the active batch and for any uncollected
   pool owned by the current or recoverable workflow run. Both the command
   entry point and the deferred handoff path call this guard. A terminal pool
   awaiting collection can also trigger the refusal. `goal_flow` uses the
   shared CodePlan turn path, where SUBAGENT-17 binds scoped continuation and
   collection. [TUI handoff guard](../../src/agenthicc/runners/tui_session.py#L4668),
   [GoalFlowRunner](../../src/agenthicc/workflows/goal_flow/runner.py#L947),
   [Guard regression](../../tests/integration/test_workflow_phase_subagents.py#L631).
2. The existing foreground handoff persists a request, requests cancellation
   of the foreground turn, releases its session lease, and starts a separate
   background worker. Cancellation is asynchronous. Its finalizers may still
   be unwinding when the new process opens the session. An ordinary tool
   exchange already has a deferred handoff boundary, but there is no transfer
   protocol for a live pool. [Handoff bridge](../../src/agenthicc/background/integration.py),
   [Background supervisor](../../src/agenthicc/background/supervisor.py#L1129),
   [Current handoff ordering](../../docs/guides/background-sessions.md).
3. The live pool consists of local tasks, a broker, and a continuation handle.
   TUI teardown closes its task supervisor and cancels tasks that miss the
   close deadline. The journal keeps lifecycle and result evidence for
   recovery. Opening a resumed session currently reconciles live records as
   interrupted. The receiving background worker selects a checkpoint and runs
   it through the headless workflow path. Neither the journal's pool ID nor a
   recovered phase invocation gives that new process the original tasks.
   [Spawn dispatch](../../src/agenthicc/subagents/tool.py#L765),
   [TUI teardown](../../src/agenthicc/runners/tui_session.py#L7172),
   [Pool recovery](../../src/agenthicc/memory/journal.py#L1472),
   [Resume initialization](../../src/agenthicc/runners/tui_session.py#L1618),
   [Background workflow dispatch](../../src/agenthicc/background/worker.py#L2529).
4. SUBAGENT-17 already protects the workflow transition independently. The
   phase host can park in its original coroutine and wake on input or a
   scoped notice. The phase gate reads committed terminal collection receipts.
   This feature needs to preserve that host and gate when the terminal leaves.
   [Phase host](../../src/agenthicc/runners/workflow_phase_subagents.py),
   [Phase gate](../../src/agenthicc/subagents/phase.py),
   [Committed receipts](../../src/agenthicc/memory/journaled.py#L602),
   [SUBAGENT-17](SUBAGENT-17-non-blocking-workflow-subagents.md).
5. Today, reattaching a running background session stops the worker before
   opening the TUI. Live round-trip handoff therefore needs a supported attach
   path that reconnects to the same runtime.
   [Current foreground attach](../../src/agenthicc/background/supervisor.py#L1443).

Nine targeted baseline tests passed for the aliases, tool boundary, and
workflow phase exit behavior. [Handoff boundary tests](../../tests/unit/test_background_handoff_waits_for_tool.py),
[Phase exit tests](../../tests/integration/test_workflow_phase_subagents.py#L631).

## Operator experience

- Submit any handoff alias from an active parent turn, a phase parked for its
  pools, a direct session with background pools, or a session whose operator
  paused a workflow phase. A handoff from a paused phase leaves that phase
  paused. Its pools can finish and record their outcomes. Delivery to the
  phase resumes when the operator explicitly resumes it.
- The TUI reports that handoff is pending while the service validates the
  request. On durable acceptance it identifies the same session and summarizes
  the live pool and worker counts. The terminal exits once. The session is
  discoverable through the background hub and `agenthicc agents`. The service
  distinguishes an active parent, a parent waiting for subagents, results
  awaiting collection, a paused phase, and an operator interaction.
- A healthy handoff completes while workers are still behind a test barrier.
  A blocking `spawn_subagents` call may continue waiting inside the runtime
  under the explicit foreground spawn setting. The terminal can leave during
  that wait. A pending approval or question is accessible through the
  background inbox without requiring the original terminal to remain open.
- Every worker that finishes notifies the parent agent, whether or not a
  terminal is attached. The notice (`subagent-worker:{pool}:{task}:{attempt}`)
  names the worker, how it ended and how many of its pool have finished, and
  says so when the worker completes the pool. It never carries what the worker
  wrote. An idle parent is woken by it, a running parent receives it at its
  next tool boundary, and workers that finish together share one wake. The
  parent reads a finished worker's result with `collect_subagent_results`,
  which returns the finished workers' results while the rest of the pool
  runs; only a terminal collection counts toward a phase's gate. A blocking
  `spawn_subagents` call is the exception: its parent is waiting for the
  aggregate and its workers send no notices. The notices and the parent's
  replies are part of the session transcript.
- The agents hub transcript shows the session's subagents: the pool and its
  workers starting, each worker's result or error, any stop request with its
  source and reason, the pool's outcome, and the notice the parent received.
  The notice is never shown as an operator message.
- A terminal-owned session (background agents disabled, or no local controller
  transport) with live subagent pools refuses the command with the reason
  `runtime_not_hosted`. Nothing is cancelled and no background record is
  written. A finished pool that only awaits collection does not block it.
- Inspecting the background session shows the same pool and worker IDs,
  attempt numbers, progress, and transcript available before handoff.
  Reattach through the normal session entry point to operate the live
  session again. Stop, abort, reset, and abandon retain their explicit
  effects on the relevant run or workers.
- A failed request displays the specific reason and the supported recovery
  action. A refused handoff with a verified attached runtime leaves the
  terminal usable. An ambiguous response leads to reconciliation against
  the existing request before accepting another handoff or a new controller.

## Scope

The required release covers local interactive sessions and the local
background service. It includes direct turns and shared workflow phase
runners, with real `goal_flow` as the primary reproduction. Declarative
Generate and `make_book` Research exercise the other shared paths. It
covers several concurrent pools, lineage-scoped descendants where supported,
and mutating workers using isolated worktrees. Both spawn modes retain their
parent-facing semantics. Background-disabled installations retain their
existing availability behavior.

A live handoff requires a session already running under the supported runtime
owner. Existing checkpoint formats and completed sessions remain readable.
A session already running in the old terminal-owned process during an upgrade
continues there and receives an actionable eligibility result. Its historical
pool IDs still resolve via the existing recovery path after that process stops.
Cross-host task migration and resumption of an arbitrary Python task after
process death are beyond the local live-handoff contract.

## Proposed design

### 1. Host the session runtime independently of its terminal

Start a service-supervised session runtime for supported interactive sessions
before their first provider turn. It owns the session lease, conversation
writer, event processor, active workflow stack and checkpoint claim, provider
runner, pool registry and brokers, task supervisor, and resources used by its
tools. Keep the terminal's input, rendering, and local view lifecycle in the
TUI. Route agent input and control requests to the runtime and stream bounded
events back into the existing reactive view. Use the background service to
supervise and discover the runtime. Use the session lease as the authority
for its writer identity.

This placement preserves a live Python task, transport request, clarification
future, and worktree lock across terminal exit. A transfer of serialized pool
descriptors to a new worker would require restarting at least some live work
and replaying side effects. The normal live path adopts the existing runtime
under background management and keeps its execution identity. The separate
process launch and checkpoint resume path remains available for sessions
without a live hosted runtime.

Consolidate the turn and workflow control boundary used by the TUI and the
service. The hosted path must run the original workflow continuation,
including its phase-specific prompts, callbacks, mode, tool registrations,
and memory route. A handoff uses a control message to that runtime. It cannot
be implemented by issuing the historical user intent to the headless workflow
runner, changing phase options, or moving a task into a second registry.

Treat the runtime's process and terminal lifetime separately. It must survive
graceful TUI exit, client termination after commit, and loss of the terminal
process group. Keep live tool resources hosted for their ordinary lifetime.
For an operation requiring the attached physical terminal, register the
specific dependency and either bridge it to the service's input route or
return a typed handoff blocker while it remains unsafe. Pool activity, a
pending tool exchange, and worktree cleanup are insufficient reasons by
themselves to block a handoff.

### 2. Adopt the runtime through the existing background control plane

Add an adoption operation within `BackgroundSupervisor` and the existing
store and dispatch boundaries. The durable adoption request carries an
idempotency key, session ID, verified runtime and lease identity, expected
controller generation, and expected background attempt, dispatch, and
lifecycle revisions. Store only bounded correlation and routing metadata in
the registry. Use the authenticated runtime channel for a live ownership
proof. Follow existing protected launch and config storage for recovery.

Use a two-stage protocol with one durable commit point for controller
authority.

| Stage | Required behavior |
| --- | --- |
| Resolve | Authenticate the current controller and verify the exact runtime and session lease. Admit only an eligible background target. Use the canonical capacity and dispatch lock. A live attached runtime consumes a background slot when adoption commits. If a slot is unavailable, report it while the attached runtime and its workers remain active. |
| Prepare | Persist one request and reserve the slot under expected-field checks. The runtime confirms which input and operator interaction sequence it has accepted. Old-controller commands racing the cutover either commit below the sequence boundary or return a retryable stale-controller result. Before commit, support a bounded rollback that releases the reservation. |
| Commit | Publish the new background controller generation for this runtime in the durable store. Register the runtime as the session's active background attempt under the same session ID. Retain the runtime's existing session lease and writer. The same runtime acknowledges or reconciles that committed generation before granting the new controller write access. |
| Acknowledge | The TUI observes the committed disposition or queries it by handoff ID after response loss. It finishes the foreground view and returns to the shell. Background actions and projections use the committed attempt and generation. Repetition returns the same accepted result. |
| Reattach | Authenticate an attach request against the live attempt. Acquire an attached controller generation, restore the view from a cursor, and release background capacity according to the store contract. The runtime and its tasks retain their owner. Existing non-hosted attach and recovery operations continue to follow their own lease contracts. |

The runtime keeps one writer throughout. A control-plane restart must
reconcile from the durable commit before admitting commands. The attach
transition uses the same runtime proof and generation fence. The store still
recognizes intentional cancellation separately from foregrounding. An
unverifiable runtime identity uses the existing fail-closed process identity
rules, including PID reuse and unavailable process probes. A stale generation,
reply, attach, or finalizer cannot mutate the current background attempt.
[Existing session lease](../../src/agenthicc/runners/session_lease.py),
[Attempt fencing and claim](../../src/agenthicc/background/store.py#L1404),
[PRD-245](../prd-245-recoverable-background-handoff-after-process-kill.md).

### 3. Preserve phase, tool, and conversation semantics

Keep the same workflow run, stack frame, phase invocation, branch ownership,
provider history, configuration, resolved provider profile, effective mode,
remaining parent deadline, and tool exchange. If a worker is in a provider
request, tool call, retry backoff, question wait, or cleanup, its task
continues from that state. Keep pool lineage, admission slots, absolute worker
deadlines, attempt counters, progress clocks, worktree identities, pending
artifact publication, and usage attribution.

The same runtime remains responsible for admission during handoff. A spawn
racing detach has one outcome. A durable admission stays in its owning phase
and appears in the service view under its original pool ID. A refused or
uncommitted admission contributes no phantom pool. The runtime completes the
original tool exchange once and appends notices after its commit. On an
exhausted provider boundary, it retains notices for a legal continuation.
An explicit foreground spawn returns the existing aggregate or clarification
result to the existing parent. A background spawn returns its running receipt
as soon as admission is durable.

Reroute activity hints to the hosted continuation. Use runtime ownership and
provider-history readiness to control notice delivery. The TUI's pending
handoff row is a projection of controller state. Keep notice scope, stable
message IDs, append-before-acknowledge ordering, and lost-hint reconciliation.
A parked runtime spends zero provider calls while awaiting workers and input.
A question, progress update, or worker finish wakes only its owner. A finished
worker's result is readable at once. The result of the worker that completes
its pool is announced when the pool's task is collectable, including after
required worktree cleanup. Original phase gates,
artifact checks, and approval rules still control progression. A transition
in the same batch as a spawn or collection still returns
`subagents_pending`. Partial, cancelled, interrupted, or failed terminal
outcomes remain collectable under the existing contracts.

Keep protocol handoff state out of provider history. Forward operator input
through the existing FIFO and acceptance receipts. Transfer accepted input
still held by a client to the owner before acknowledging cutover, or require
runtime acknowledgement before describing input as accepted. Preserve order
across the cutover and deduplicate by message ID. Local commands and unsent
drafts retain their client semantics. A disconnected terminal's cleanup
releases its views and subscriptions. The runtime closes its own resources
only at its actual lifecycle boundary.
[Notice protocol](../../src/agenthicc/subagents/delivery.py),
[Background input inbox](../../src/agenthicc/background/input_inbox.py),
[Worker isolation](../SUBAGENT-15-worktree-isolated-parallel-implementation.md).

### 4. Route operator interaction and retain policy

Publish live approval and question requests to the existing background
interaction inbox when the terminal relinquishes control. Preserve the
client's accepted response, the request ID and revision, the owning worker
or parent turn, and the original expiration. A response on either surface
races through one resolution fence. A delayed terminal response at the old
generation must be rejected unless it was accepted before cutover. Worker
clarifications from `ask_parent` retain their pool-scoped broker route to
the parent agent. An actual operator interaction reached through that route
is handled by the operator inbox.

Carry the runtime's actual policy and configuration across the change.
Service attachment grants only the operator control the session already
exposes. Preserve mode, tool and skill visibility, explicit per-role and
depth restrictions, path scope, sandboxing, network checks, approval
requirements, and usage limits. Keep runtime identity verification and
control transport local and authenticate both endpoints. Use bounded typed
requests, same-user access controls, replay fencing, and protected
credentials. Retain secrets and worker outputs in their existing protected
storage. Project safe IDs, statuses, counts, and reason codes in the hub.
[Background interaction inbox](../prd-227-background-approval-and-input-inbox.md),
[Tool parity and restrictions](SUBAGENT-18-parent-tool-parity-for-all-subagent-types.md).

### 5. Handle failure explicitly

| Situation | Expected result |
| --- | --- |
| Background service unavailable, disabled, or at capacity | The current controller retains authority. The runtime and admitted workers continue. The command reports the condition and permits a fresh request. |
| Persistence, validation, or reservation failure before commit | Roll back an uncommitted preparation according to its request ID and preserve the runtime. A retry cannot duplicate reservations, attempts, or recognized input. |
| Client exits or loses the response around commit | Look up the durable disposition by request ID. For a committed handoff, the runtime is service-managed and continues with its original lease. For a preparation with no committed owner change, apply the attached-runtime reconnect and close policy. |
| Service restarts with the runtime alive | Reconnect to the same authenticated runtime and rebuild the projection from its durable and live state. Reconcile pending inputs, approvals, notices, and the controller generation. |
| Runtime process dies | Use verified process identity and owner fencing to release stale claims and mark the corresponding background attempt recoverable. Preserve journaled effects and terminal results. Settle genuinely lost workers as interrupted, ensure owned resources are accounted for, and resume the original checkpoint only via the existing authorized recovery path. |
| Runtime or worker identity is uncertain | Surface an actionable recovery state. Preserve the live fence until identity or permitted recovery is established. |
| Operator cancels, resets, or abandons | Settle the addressed execution scope and its resources before releasing it. Preserve bounded evidence and the cause of termination. Detach, attach, and reconcile do not share the cancellation action. |
| Parent or worker deadline expires during or after handoff | Apply the existing deadline behavior using the remaining time. Keep the original phase checkpoint and the terminal evidence available for collection or recovery. |

## Acceptance criteria and verification

Use gated scripted transports and subprocesses through the production
entry points. A worker is demonstrably live when the command is issued and
after the terminal client has exited. For the primary scenario, compare the
runtime identity, pool ID, worker ID, worker attempt, provider request
sequence, and a durable side-effect marker before handoff, after client exit,
and after release. A successful status projection alone does not establish
execution continuity.

| ID | Acceptance criterion | Required automated evidence |
| --- | --- | --- |
| A1 | The reported `goal_flow` journey succeeds with live Executors. | TUI subprocess test with five gated Executors in a real implementation phase. Have the Executors perform a verifiable action and hold them at a provider barrier. Invoke `/detach`, observe the TUI exit and the service show five running workers, then release workers. Assert identical session, goal run, frame, invocation, pool, worker attempts, and runtime process. Assert one committed collection and one normal phase advance. |
| A2 | Handoff works during a parent turn and while parked. | Parameterize all aliases over streaming, parked, a phase with uncollected terminal outcomes, and a direct session with ownerless pools. Cover an explicit foreground spawn and handoff during pool admission, alongside another tool call. In live cases, hold at least one worker past acknowledgement. Include a terminal-only case. Assert a single original tool exchange and zero duplicate provider requests or result blocks. |
| A3 | Handoff preserves the phase's normal progression guard. | Real declarative Generate and `make_book` Research tests with multiple pools. Mix spawn or collection with transition in both tool orders. Assert `subagents_pending`, one committed receipt per terminal pool, and stable scope. Give Research missing chapter notes after collection, then supply the notes and assert its normal transition advances once. Include a nested frame return and a repeated phase name. |
| A4 | The service presents and controls the same live session. | TUI and service E2E with status snapshots before and after detach and reattach. Assert the same IDs, rows, progress clocks, and transcript cursor. Return once to the original shell. Confirm an attached view can send input and control the live session, with only one controller holding the current generation. |
| A5 | Operator and parent communications survive cutover. | Integration with a queued message, pending approval, pending operator question, and a pool's direct update or `ask_parent`. Race responses from the terminal and inbox. Assert ordered input, one winning response per request, stable IDs and deadlines, and delivery only in the owning phase. Drop and duplicate activity hints. Check append-before-ack replay and zero parked provider polling. |
| A6 | Request and lease fencing make the handoff idempotent. | Unit transition matrix with the store's real compare-and-set contract. Race repeated aliases, capacity reservation, detach and attach, dispatch restart, stale PID/token, and stale finalizer. Assert one writer, one committed controller generation, one background attempt, and one slot per adoption. |
| A7 | Loss of the client or service does not replay a live pool. | Subprocess fault injection before prepare, after durable prepare, after commit before acknowledgement, after acknowledgement, and during service restart. Verify the correct durable disposition and inspect real worker continuity after the client is gone. Inject a verified runtime death separately and prove one recovery decision, truthful interrupted outcomes, and zero automatic relaunch of lost workers. |
| A8 | Policies and resources remain in force after adoption. | Compare effective mode and tool schemas before and after handoff, including an explicitly restricted role and a denied and allowed write. Gate two mutating workers in separate worktrees with a dirty parent snapshot. Verify unchanged worktree identities, one apply per accepted patch, durable artifacts, correct usage, retained retry/backoff counters, and original deadline enforcement. Include a nested pool where the existing lineage policy allows one. |
| A9 | Pause, cancel, reset, abandon, and timeout remain distinct. | Through the service and after live reattach, cover explicit pause before detach, operator resume after workers finish, worker cancel, run abort, phase reset, and deadline expiry. Assert checkpoint and pool lifecycle evidence, scope-limited cleanup, and reason codes. |
| A10 | Ineligible and ordinary sessions retain their documented behavior. | Regression cases for a live legacy terminal-owned pool, disabled service, exhausted capacity, unavailable local transport, a terminal-bound operation, old checkpoints, and an ordinary session without active pools. Assert typed refusal or supported handoff, single session identity, and the existing dead-worker recovery behavior. |
| A11 | The CLI and TUI remain responsive at cutover. | Exercise a gated provider request and multiple live workers while the client confirms handoff. Assert causal completion with gates held, a single confirmation, accessible status, and no false cancelled or completed row. Measure warmed handoff time as time spent in local routing, persistence, and acknowledgement. Test on supported platforms using their real control transport and process identity adapters. |
| A12 | A handoff never cancels a live worker in an ordinary session. | Subprocess PTY test: five gated Explorers in a session with no workflow, with the parent yielded, mid provider request, and inside an explicit foreground spawn. Issue each alias. Assert the same runtime, pool and worker IDs, no `subagent_worker_result` or cancellation record and no `parent_cancelled` anywhere before release, and five successful results after. In-process test: a terminal-owned session refuses with `runtime_not_hosted` and leaves the workers, the store and the handoff state untouched. |
| A13 | Every subagent notifies the parent when it finishes, and the operator can see it. | Release five Explorers one at a time with the terminal gone. After each release assert one parent request whose last message is that worker's notice (label, `k/5`, and `the pool is complete` only on the fifth), no worker text in it, one acknowledgement, and no extra provider request. After the first notice assert that `collect_subagent_results` on the still-running pool returns that worker's result and not the others'. Assert every notice is in the transcript page a reattached terminal replays, and that the agents hub transcript lists the pool, each worker result and five `parent notified` rows, with no operator row for a notice. A foreground spawn sends no notices. A worker retry notifies again for its new attempt only, and a pool that ends without running a worker notifies for that worker. |

The rollout gate requires the applicable unit, integration, and E2E matrix,
including the existing SUBAGENT-16 through SUBAGENT-18 and background recovery
regressions. Require more than 90% changed line-and-branch coverage and keep
the configured repository-wide coverage floor, currently 81.5%. Exercise
timing with barriers and fake clocks in correctness tests. Measure elapsed
latency separately under a documented healthy local setup. Track handoff
acceptance, precise refusal codes, duplicate/stale control rejection,
time from prepare to acknowledgement, pool continuity, and recovery
outcomes using bounded identifiers. Healthy handoff latency must remain
independent of worker duration.

## Automated evidence

The hosted variants run the original workflow or turn and use the local
controller protocol to transfer control. The PTY cases start the public CLI
and observe the owner and provider trace from outside its process.

| Criterion | Test coverage |
| --- | --- |
| A1 | [Real goal workflow and five gated Executors](../../tests/e2e/test_live_subagent_handoff.py) |
| A2 | [Handoff aliases and live, parked, foreground and terminal pool states](../../tests/e2e/test_live_subagent_handoff.py), [admission and a sibling tool exchange](../../tests/integration/test_workflow_phase_subagents.py) |
| A3 | [Generate batch ordering, pool limits, MakeBook notes, nested return and phase re-entry](../../tests/integration/test_workflow_phase_subagents.py) |
| A4 | [CLI status, live reattachment and subsequent operator input](../../tests/e2e/test_live_subagent_handoff.py), [controller generation fencing](../../tests/integration/test_workflow_phase_subagents.py) |
| A5 | [Pending question, ordered provider input and lost acknowledgement](../../tests/e2e/test_live_subagent_handoff.py), [approval and scoped worker questions](../../tests/integration/test_workflow_phase_subagents.py) |
| A6 | [A single lease, duplicate requests, stale generations and concurrent capacity reservations](../../tests/unit/test_live_handoff_store.py) |
| A7 | [Controller loss at the durable transitions and verified runtime death](../../tests/e2e/test_live_subagent_handoff.py) |
| A8 | [Dirty worktree snapshots, isolated edits, mode capabilities, worker deadlines and transport retries](../../tests/integration/test_workflow_phase_subagents.py) |
| A9 | [Live cancellation](../../tests/e2e/test_live_subagent_handoff.py), [pause, resume, reset, abandon and expiry](../../tests/integration/test_workflow_phase_subagents.py) |
| A10 | [Capacity and transport failure](../../tests/e2e/test_live_subagent_handoff.py), [legacy phase refusal and terminal-bound operations](../../tests/integration/test_workflow_phase_subagents.py) |
| A11 | [Shell return with provider gates held](../../tests/e2e/test_live_subagent_handoff.py), [bounded authenticated control and native process identity](../../tests/unit/test_hosted_transport.py) |
| A12 | [Five Explorers survive every alias, parent state and foreground spawn](../../tests/e2e/test_live_subagent_handoff.py), [terminal-owned refusal and hosted detach, in process](../../tests/integration/test_direct_session_handoff.py) |
| A13 | [A notice per Explorer with the terminal gone, and a partial collect](../../tests/e2e/test_live_subagent_handoff.py), [one wake per worker and transcript replay](../../tests/integration/test_direct_session_handoff.py), [two workers, two wakes in a phase](../../tests/integration/test_workflow_phase_subagents.py), [per-attempt delivery](../../tests/unit/test_subagent_resume.py), [hub transcript rows](../../tests/unit/test_agents_hub_subagent_transcript.py) |

## Implementation sequence

1. Add the red regression for the reported `goal_flow` journey. Capture the
   existing direct and workflow ownership behavior as contract tests.
2. Extract the live runtime owner and foreground client boundary. Prove one
   lease and one journal writer. Start supported interactive runtimes under
   service supervision before any pool is admitted. Test attached-only
   parity for input, tools, approvals, cancellation, and progress.
3. Implement the store-backed adoption and reattach handshake. Exercise all
   CAS, crash, persistence, and capacity cases before routing a live detach
   through it. Use the existing recovery path for a verified dead runtime.
4. Route scoped notices and operator interactions across controller changes.
   Wire the hub and TUI projections to canonical state. Replace the
   collect-before-handoff refusal for hosted sessions when this path can
   accept them. Keep the collect-before-transition gate in the workflow.
5. Run the production subprocess tests and the complete CI gate. Update the
   background-session, workflow, subagent, architecture, and storage guides
   with the new owner, readiness states, and recovery contract. Publish the
   rollout from the earliest session entry point that can host the runtime
   with its complete tool and policy context.

The first-prompt performance probe measures the hosted architecture: the
readiness events fire in the runtime process, so the probe instruments every
process of its tree and takes wall time from the probe's launch.

The feature is complete when an operator can close the foreground client
through any handoff alias while an eligible workflow pool is genuinely live,
the same worker attempts finish under background management, and the
original phase durably collects their outcomes before it advances.
