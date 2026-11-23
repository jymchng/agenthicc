---
title: "SUBAGENT-17: Non-blocking subagents in workflow phases"
status: Implemented
version: 1.0.0
date: 2026-11-23
priority: P0
related_prds:
  - SUBAGENT-16
---

# SUBAGENT-17: Non-blocking subagents in workflow phases

## Problem

When an interactive workflow phase calls `spawn_subagents`, its workers can
run in parallel while the parent agent waits for the pool. The parent has
independent work it could do, but the provider cannot receive its next
request until the spawn tool returns. User input queued during the tool batch
also waits for that boundary. The reported `make_book` Research screen shows
this state with eight active workers, an open **Spawn Subagents** call, and
**Ctrl+B background pool**. The same rule applies to any workflow phase,
including Generate phases that expose `spawn_subagents`.

## Diagnosis

Source inspected at `3b4be2b`, the merge of SUBAGENT-16.

1. SUBAGENT-16 deliberately kept workflow spawns foreground until phases
   could enforce collection before transition. `current_spawn_mode()`
   treats any bound workflow run ID as a phase and the resolver vetoes
   background mode. Initial and resumed TUI workflow execution also clear the
   turn notice source. Removing either check alone leaves the other blocker.
   [SUBAGENT-16, Q2](SUBAGENT-16-non-blocking-subagents-by-default.md),
   [Spawn-mode resolver](../../src/agenthicc/subagents/spawn_mode.py),
   [Initial workflow binding](../../src/agenthicc/runners/tui_session.py#L5942),
   [Resume binding](../../src/agenthicc/runners/tui_session.py#L6689).
2. In foreground mode the spawn tool usually awaits the aggregate pool result.
   Detach and parent clarification can return earlier. The provider runner
   awaits the entire tool batch, so a completed sibling tool and queued user
   input can still be held behind the spawn call. In background mode the spawn
   tool already has a durable admission receipt that can release the batch
   while workers are live. The screenshot is consistent with this logical
   wait. It does not establish event-loop starvation or a worker read stall.
   [Spawn dispatch](../../src/agenthicc/subagents/tool.py#L766),
   [Tool-batch boundary](../../src/agenthicc/runners/agent_turn.py#L1792).
3. The existing idle subagent wake routes into a direct agent turn.
   `run_turn()` suppresses workflow dispatch when it receives a subagent wake
   key. The current notice source also claims by conversation. Simply enabling
   it in a phase could deliver results to a sibling phase or to a different
   workflow frame. An eligible phase needs a delivery route tied to its own
   invocation. [Wake scheduling](../../src/agenthicc/runners/tui_session.py#L5677),
   [Direct wake dispatch](../../src/agenthicc/runners/tui_session.py#L5778),
   [Journal notice source](../../src/agenthicc/subagents/delivery.py#L148).

The current mode unit tests and the direct-session integration test for a
sibling tool returning while workers run passed locally (15 cases). The unit
tests currently require workflow spawns to wait. The reported live session was
not replayed. [Mode tests](../../tests/unit/test_spawn_mode.py),
[Direct continuation test](../../tests/integration/test_subagent_background_default.py#L80).

## Required behavior

An eligible interactive phase gets a pool ID as soon as admission is durable.
It can run other tools, respond to queued user input, or yield while waiting
for its workers. Once a worker needs clarification or the pool finishes, the
runtime delivers a notice to that phase. The parent collects results using
`collect_subagent_results` and can then satisfy the phase's ordinary completion
requirements. This requires neither a new user message nor Ctrl+B.

The first release covers declarative phases in the default runner and phases
using the shared `CodePlanRunner` turn path. Enable background by default
where the phase and its host meet the requirements below. Verify both a
declarative Generate phase and the reported `make_book` Research path. Apply
the same contract to custom runners when they register phase delivery and
transition handling. For example, `create_workflow` has a separate Generate
runner with a scoped tool allowlist. Preserve that allowlist.
[Default runner](../../src/agenthicc/workflows/default/runner.py#L510),
[Shared phase turn](../../src/agenthicc/workflows/code_plan/runner.py#L737),
[Scoped Generate turn](../../src/agenthicc/workflows/create_workflow/runner.py#L2001).

Direct-session spawns retain SUBAGENT-16 behavior. Headless, session-service,
worker, and unsupported phase runners keep their existing foreground behavior
where they cannot provide a safe continuation. The operator can select
`subagents.spawn_mode = "foreground"` for eligible phases as well. Tool
permissions, worker limits, deadlines, approvals, and worktree isolation
retain their existing contracts.

## Implementation

### Resolve mode from actual delivery capability

Replace the workflow veto in the shared resolver with an explicit turn
delivery contract. For phase background execution, require the operator's
background setting, a supervised pool owner, a durable journal and phase
memory route, a registered continuation with a collect-before-transition
guard, and the phase's existing access to the required communication tools.
Clarification must either be answerable under the phase's current tool
policy or governed by the existing fail policy.

Use one effective mode decision for the prompt, tool description, runtime
dispatch, and detach availability. Provide the effective session subagent
settings at the workflow runner boundary. Both shared workflow paths omit
the `agent_config` passed by the direct TUI path today, so removing the
workflow veto alone would risk falling back to defaults in a phase. Keep
worker and phase allowlists authoritative. Expose a short reason if a phase
uses foreground despite the background setting. The model-facing tool
arguments stay the same.
[Mode settings](../../src/agenthicc/config.py#L1236),
[Worker tool registration](../../src/agenthicc/runners/agent_turn.py#L1964).

### Give the phase durable ownership of each pool

Tie each admitted pool to its conversation, workflow run, stack frame, and
phase invocation. Include the branch for a parallel phase and distinguish
re-entry from resume. Each provider turn can get a new ID, while continuation
and checkpoint resume keep the same phase invocation ID. Record ownership
with durable pool admission before returning the receipt. A failed admission
leaves no outstanding dependency. Reuse the existing supervisor and
conversation journal for the live task and pool lifecycle. Older pool records
without phase ownership keep their existing delivery behavior.

Bind a phase-filtered notice source at the shared agent turn boundary on
initial entry and resume. Give delivery and collection the same ownership
scope. Record completion and clarification notices with stable IDs and
acknowledge them after durable insertion. Only lifecycle metadata goes into
the notices. Worker findings come through the existing collection tool.
Consume and record the terminal collection receipt at the committed tool
boundary. A status check on a live pool leaves it outstanding.
[Pool journal](../../src/agenthicc/memory/journal.py),
[Collection tool](../../src/agenthicc/subagents/communication.py#L1123).

### Keep the workflow owner in charge of continuation

While a provider turn is active, deliver a notice after the entire tool
exchange has been committed. Observe the existing queue ordering and defer
delivery at an exhausted provider boundary. If the agent yields with
uncollected pools, park that phase at a checkpointable boundary. With a live
pool and no input or notice, waiting issues zero provider requests and spends
zero workflow retries. Describe this yield behavior in the phase's dynamic
prompt so the agent can do independent work first and then wait without
polling.

Resume a parked phase through the same workflow owner, with its original
frame, phase context, mode, tools, and memory. Feed queued input at safe
boundaries under the existing session rules. Have the TUI's scheduler
recognize a parked phase and signal its continuation without dispatching a
new direct turn or a second workflow run. A durable pending-notice check
recovers missed hints. Keep a single writer for each provider history.
Parallel phase branches qualify only if they have a safe memory and delivery
route. A branch without that route falls back to foreground.
[Workflow owner](../../src/agenthicc/runners/workflow_handle.py),
[Checkpoint model](../../src/agenthicc/workflows/checkpoint.py).

### Gate transition before side effects

For a participating phase, allow completion, approval requests that advance
the workflow, review outcomes, implicit next/end, and frame return only after
all of its admitted pools have terminal collection recorded. A failed,
cancelled, or interrupted pool has a collectable terminal outcome. Seeing the
outcome lets the phase apply its existing failure or retry policy. Collection
provides no approval or waiver of artifact validation. Explicit abandonment
settles outstanding pools through the workflow owner.

Check the gate before any transition callback stages a receipt, sets a
completion event, or changes workflow state. A transition tool in the same
provider batch as a spawn or collection must safely return a recoverable
`subagents_pending` result before side effects. Reserve pending admissions
before parallel dispatch or serialize these controls at a defined safe
boundary. A subsequent tool call may retry the transition after collection
commits and all existing phase checks pass. Use the phase declaration and
runtime transition contracts for this gate. For Research, `submit_research`
must still validate durable notes and coverage before entering Assets. The
read-only Researcher returns findings for the parent to incorporate into
those files. [Research submission](../../src/agenthicc/workflows/make_book/runner.py#L837),
[Researcher allowlist](../../src/agenthicc/subagents/types.py#L297).

### Preserve operator control and recovery

Show an admitted pool independently of the completed spawn call. Distinguish
an active parent, a phase parked for results, results ready to collect, and a
phase paused by the operator. Keep the existing controls applicable to each
state. Manual detach in a participating phase uses the same ownership,
collection gate, and delivery requirements as automatic backgrounding.
Advertise Ctrl+B only when the destination can receive the pool.

While a workflow is paused, a session-owned live pool can finish and record
its result, and phase delivery waits for explicit resume. On process restart,
reconcile live records using existing interrupted-pool handling, then resume
the owning phase with its pending notices and collection state. Abandon or
reset must settle owned pools before releasing the frame. Guard handoff if
the receiving host cannot honor the phase delivery contract. Preserve both
worker and workflow timeout enforcement. If the workflow deadline expires
while parked, use the existing timeout and checkpoint path with the phase
ownership intact.

## Acceptance criteria and test plan

Use gated worker transports and the normal session entry points. Assert
causal ordering against explicit admission, commit, and worker-release
barriers. Run tests without provider credentials or a live corpus.

| ID | Acceptance criterion | Required automated test |
| --- | --- | --- |
| A1 | The shared mechanism releases an eligible phase's batch while workers run. | Integration through the TUI and real tool dispatch for declarative Generate and `make_book` Research. Block workers behind a barrier, include a sibling read, and capture the next provider request and queued input while the barrier is still closed. Assert the durable receipt, complete tool exchange, and absence of any worker result. |
| A2 | A pool's question or terminal outcome reaches its owning phase. | Integration for active delivery, parked delivery without a user message, and delivery after a lost scheduler hint. Assert the original frame, phase invocation, provider history, mode, and tool schemas. Verify a question can be answered and a terminal pool collected. Assert zero provider calls and no attempt increments while parked without input. |
| A3 | Phase progression requires committed terminal collection. | Unit and integration tests for simultaneous spawn/transition in both list orders, simultaneous collect/transition, a rejected spawn, and multiple pools with one still running. Cover explicit completion, review/rejection, implicit next/end, and frame return. Assert no premature receipt, event, artifact mutation or checkpoint advance. In Research, collect all pools, reject missing chapter notes, then accept the fixed manifest and advance once. |
| A4 | Phase ownership and permissions are enforced. | Unit resolver matrix and runner integration for initial and resumed phases, nested frames, parallel peers, and re-entry into the same named phase. Assert that claims and collection cannot cross those scopes. Test missing access to collect/answer, an unsupported memory route, and a restricted custom phase. Assert operator foreground settings reach each shared runner and the Researcher tool list stays read-only. |
| A5 | Lifecycle and notice delivery survive pause, interruption, and replay. | Checkpoint and TUI integration covering pause before completion, completion before resume, process restart with live and terminal pools, duplicate hints, interruption between notice insertion and acknowledgement, and interruption around collection commit. Assert one durable receipt, no duplicate provider-visible notice, no automatic worker relaunch, and no transition until all outstanding outcomes are handled. Include timeout, abandon/reset, and unsupported handoff. |
| A6 | Session controls accurately show who is waiting and what the operator can do. | TUI end-to-end test for immediate spawn completion, live worker rows, parked phase, results ready, and pause/resume. Exercise Ctrl+B on an eligible foreground pool and assert the same phase receives the outcome. Verify queued user messages are processed once and in order across notices and resume. |
| A7 | Existing spawn modes and worker governance continue to work. | Keep SUBAGENT-16 direct idle/busy wake tests and the headless, service, and explicit foreground wait tests. Replace the blanket phase-foreground assertion with supported and unsupported phase cases. Verify cancellation, admission limits, elapsed deadlines, tool capability checks, and parallel mutating-worker isolation on the shared dispatch path. |

Gate implementation on targeted unit, integration, and TUI end-to-end tests,
the CI test matrix, and the repository-wide branch coverage gate
(`fail_under = 81.5` at the studied revision). Cover the changed resolver,
phase gate, and continuation branches above 90%. Publish the operator
behavior in the subagents and workflow guides, and describe the phase
ownership and transition boundary in the architecture documentation.
