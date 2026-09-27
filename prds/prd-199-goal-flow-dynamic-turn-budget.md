---
title: "PRD-199: Dynamic turn budgeting for goal_flow goal-list mutations"
status: Proposed
version: 1.0.0
date: 2026-09-27
scope: "goal_flow append/insert scheduling, aggregate turn accounting, checkpoints, and resume"
related_prds:
  - PRD-100 # code_plan architecture
  - PRD-111 # workflow parameters
  - PRD-126 # transport retry
  - PRD-169 # transaction-safe tool-call conversations
  - PRD-170 # durable workflow recovery
  - PRD-182 # durable mid-turn preservation
  - PRD-184 # preserve active workflow phase after errors
  - PRD-185 # dynamic goal-list mutation
  - PRD-194 # runtime goal-flow phase control
  - PRD-198 # bounded provider-error recovery retries
tags:
  - goal-flow
  - dynamic-goals
  - turn-budget
  - checkpoints
  - resume
  - observability
---

# PRD-199 — Dynamic turn budgeting for `goal_flow` goal-list mutations

## 1. Executive summary

`goal_flow` can add work during `IMPLEMENT_GOAL` and `VERIFY_GOAL` with
`append_goal()` and `insert_goal()`. The current implementation durably adds
the new `GoalRecord`, but it does not update any workflow-wide turn budget. The
workflow therefore has no explicit aggregate budget to extend, and no durable
record of how many agent sub-turns have been consumed by the growing plan.

This is easy to misdiagnose as an “append did not add turns” bug because the
workflow has several different limits called *turns*:

1. `PhaseSpec.max_turns` / the `max_turns` argument to `run_phase()` limit one
   `lauren-ai` agentic loop invocation.
2. `goal_flow`’s `_MAX_ATTEMPTS = 5` limits how many separate invocations it
   makes while waiting for a required transition tool.
3. `ExecutionSettings.max_agent_turns` limits a direct non-workflow agent turn;
   it is not currently passed as an aggregate budget to `goal_flow`.
4. `ExecutionSettings.turn_timeout_s` is a wall-clock deadline around the
   complete TUI dispatch, not a turn count.
5. `lauren-ai`’s `AgentConfig.max_turns` is reset for every `run_phase()` call;
   it is not a session-wide counter.

The proposed implementation makes this distinction explicit and adds a
workflow-owned, durable budget ledger. The default budget is dynamic: the
fixed clarify/decide/summarize allowances plus an allowance for every goal.
Accepting an appended or inserted goal atomically adds that goal’s future
allowance. Verification retries receive an explicit retry allowance. An
optional hard aggregate ceiling can reject additions or pause at a safe
checkpoint, but it must never silently discard a goal or restart the workflow.

The change preserves the existing per-phase limits, stable goal identities,
conversation/memory sharing, cache contract, and resume semantics. It adds the
missing accounting and diagnostics needed to prove that a growing goal list is
not consuming a fixed budget prematurely.

## 2. Investigation findings

The current source tree was inspected on 2026-09-27. The following behavior is
authoritative and must be preserved or deliberately changed as specified below.

| Evidence | Current behavior | Consequence |
|---|---|---|
| `src/agenthicc/workflows/goal_flow/runner.py:62` | `_MAX_ATTEMPTS = 5` is a retry count for a phase method that has not received its transition tool call | It is not a workflow-wide turn budget |
| `runner.py:1829-1854` and `1859-1884` | `CLARIFY` and `DECIDE_GOALS` each call `run_phase()` up to five times; each call gets a fresh `max_turns` value | Rejection/repair attempts consume additional phase invocations |
| `runner.py:1919-1954` | Each implementation invocation passes `max_turns=25` to `run_phase()` | The 25-turn ceiling is per invocation, not per goal or run |
| `runner.py:1999-2027` and `2077-2154` | Verification passes `max_turns=12`; a failed verification returns to `IMPLEMENT_GOAL` and can repeat without an aggregate ceiling | A goal can consume an unbounded number of implementation/verification cycles unless another limit intervenes |
| `runner.py:1645-1691` | The outer `run()` loop advances until a terminal state and has no total-turn counter or budget check | Appended goals add future work but no corresponding accounting |
| `runner.py:1074-1184` | `_mutate_goal()` validates, inserts a record, increments `goal_list_revision`, checkpoints, and returns success | The mutation is durable, but it changes only plan state, not turn capacity or consumption state |
| `runner.py:435-449` | `next_schedulable()` selects pending/postponed records by list order and skips terminal records | Newly added goals will run, but their cost is not represented in a budget ledger |
| `runner.py:2149-2217` | `GoalFlowWorkflow.phases` declares static `max_turns` values of 10, 6, 25, 12, and 4 | The declarative five-phase topology does not describe the runtime’s repeated per-goal implementation/verification work |
| `src/agenthicc/workflows/config.py:33-64` | `WorkflowConfig.completed_turns` is copied from the TUI’s session turn count | It is telemetry/context for a user turn, not an aggregate workflow sub-turn budget |
| `src/agenthicc/runners/tui_session.py:3027-3032,3219-3240` | `turn_timeout_s` wraps the complete workflow dispatch and `_turn_count` increments once when it exits | A long dynamic workflow can hit time before any count-based guard; this is a different failure mode |
| `src/agenthicc/runners/agent_turn.py:1831-1845` | A new `AgentConfig(max_turns=...)` is created for each `run_stream()` invocation | `lauren-ai` has no automatic cross-phase or cross-goal accumulation |
| installed `lauren-ai 1.6.0`, `_stream_loop()` | `for _turn in range(effective_config.max_turns)` counts one agent-loop iteration inside one stream run | Provider retries within a step are separate from this loop counter |
| `prds/prd-185...` and `docs/guides/workflows.md:338-384` | Dynamic goals already have stable IDs, atomic checkpoints, bounded list size, and resume rules | The new budget must extend the same context/checkpoint rather than introduce another store |

### 2.1 Root cause

There is currently no single concept called “the workflow’s maximum turns.” A
phase-local ceiling is re-created for each phase invocation, while
`ExecutionSettings.max_agent_turns` applies only to direct turns. Consequently:

- appending a goal does not increase a nonexistent aggregate counter;
- it also does not reduce the existing per-phase allowance of other goals;
- each appended goal creates another implementation/verification pair, so total
  work increases even though the visible per-phase number remains 25/12;
- repeated `verify_goal(satisfied=False, ...)` cycles are currently unlimited;
- the user receives no durable `used`, `allocated`, or `remaining` workflow
  value that could explain an apparent exhaustion; and
- a separate timeout or provider retry limit may terminate the outer dispatch
  before the per-phase `max_turns` value is reached.

The implementation must not “fix” this by multiplying
`ExecutionSettings.max_agent_turns` globally, mutating a shared session setting,
or silently restarting `CLARIFY`/`DECIDE_GOALS`. Those actions would affect
direct turns and other workflows and would violate the existing checkpoint
contract.

## 3. Problem statement

When the agent discovers necessary work and appends several goals, the entire
workflow needs a predictable amount of additional agent capacity. Today there
is no way to answer these questions from durable state:

- How many logical agent sub-turns has this workflow consumed?
- How many are allocated to the current plan?
- Did a new goal receive a turn allowance before `append_goal()` reported
  success?
- How much capacity remains for the active goal’s transition and verification?
- Was a failure caused by a per-phase loop limit, a transport retry limit, a
  wall-clock timeout, a context-window guard, or an aggregate workflow budget?
- After `--continue` or `--resume`, should the workflow continue with the same
  budget and active goal rather than recalculate from the original list?

Without these answers, a user can see a workflow stop after dynamic expansion
and reasonably conclude that the appended goals caused premature exhaustion,
even though the runtime has no aggregate accounting and may actually have hit a
different limit.

## 4. Goals

1. Define “workflow turn” precisely and distinguish it from phase invocations,
   provider retry attempts, wall-clock time, and the TUI’s user-turn counter.
2. Track actual logical agent sub-turn consumption across all `goal_flow`
   phases and per-goal cycles.
3. Allocate capacity for every initial goal and atomically add capacity when a
   new goal is appended or inserted.
4. Preserve each existing phase’s local `max_turns` behavior and tool-only
   transitions.
5. Keep dynamic-goal mutation, budget mutation, and checkpoint publication one
   atomic operation: a successful tool response must mean both are durable.
6. Make verification retries explicit and prevent them from unexpectedly
   exhausting a finite aggregate budget.
7. Preserve the same run, conversation, memory, current goal, evidence, and
   budget ledger through interruption, provider errors, process restart,
   `--continue`, and `--resume`.
8. Provide actionable TUI/journal diagnostics that identify the exact limit
   reached and how much capacity remains.
9. Keep the stable prompt/cache contract unchanged; budget and goal state remain
   dynamic context.
10. Add deterministic unit, integration, and E2E coverage for initial plans,
    repeated appends, retries, hard caps, failures, and resume.

## 5. Non-goals

- Changing `ExecutionSettings.max_agent_turns` semantics for direct agent turns.
- Increasing provider SDK retry counts or changing the existing transient and
  irrecoverable error retry policies.
- Treating network retries as fresh logical agent turns.
- Removing the existing per-phase `max_turns` ceilings.
- Making all workflows share one mutable global turn counter.
- Allowing an appended goal to interrupt the active goal or preempt a safe
  phase boundary.
- Silently truncating, deduplicating, deleting, or discarding a goal when a
  budget is insufficient.
- Creating a second conversation store, memory store, journal, or checkpoint
  format outside the existing workflow handle.
- Guaranteeing a provider prompt-cache hit or putting budget counters into the
  stable system-prompt prefix.
- Making an unbounded workflow safe merely by raising an arbitrary hard-coded
  maximum. Bounds must be configurable and observable.

## 6. Terminology and counting contract

### 6.1 Logical agent sub-turn

A **logical agent sub-turn** is one iteration of the `lauren-ai` agentic loop
represented by one `AgentConfig.max_turns` slot in one `run_phase()` invocation.
It begins when the runtime starts a provider step for a loop index and ends when
that step either commits a response/tool exchange or irrecoverably fails.

The ledger must count a logical slot at most once by `(workflow_run_id,
phase_invocation_id, step_index)`. This avoids double charging when the same
step is retried.

### 6.2 What is not a logical sub-turn

- A transient or bounded irrecoverable provider retry of the same step is not a
  second logical sub-turn. It is recorded separately as a provider attempt.
- `append_goal()` and `insert_goal()` are tool calls inside the current logical
  sub-turn; they do not themselves consume a new slot.
- A phase method invocation is not necessarily one sub-turn. One invocation can
  use several `lauren-ai` loop iterations, and the outer method can invoke
  `run_phase()` repeatedly while waiting for its transition tool.
- `WorkflowConfig.completed_turns` and the TUI `_turn_count` are user-turn
  telemetry, not this ledger.
- `turn_timeout_s` is a deadline, not a capacity allowance.

### 6.3 Provider-attempt accounting

The implementation must retain separate counters for provider attempts and
retry categories so diagnostics can say, for example, “workflow budget
exhausted after 84 logical sub-turns; 11 of those steps had transport retry
attempts.” Existing `transport_max_retries`,
`irrecoverable_error_max_retries`, and total-duration/deadline behavior remain
authoritative for provider retries.

## 7. Product behavior

### 7.1 Per-phase ceilings remain local

The current phase ceilings remain the maximum number of logical sub-turns in a
single phase invocation:

| Runtime operation | Current local ceiling | New behavior |
|---|---:|---|
| `CLARIFY` invocation | 10 | unchanged |
| `DECIDE_GOALS` invocation | 6 | unchanged |
| one goal’s `IMPLEMENT_GOAL` invocation | 25 | unchanged |
| one goal’s `VERIFY_GOAL` invocation | 12 | unchanged |
| `SUMMARIZE` invocation | 4 | unchanged |

The phase retry loop remains bounded by a configurable replacement for
`_MAX_ATTEMPTS`, defaulting to 5 for compatibility. Every invocation and every
logical sub-turn is charged to the workflow ledger. A missing transition does
not reset the ledger.

### 7.2 Dynamic plan allowance

The workflow receives an automatically calculated allowance for the current
plan. Let:

```text
control_budget =
    clarify_max_turns * transition_attempt_limit
  + decide_max_turns * transition_attempt_limit
  + summarize_max_turns * transition_attempt_limit

goal_budget =
    (implement_max_turns + verify_max_turns) * transition_attempt_limit

dynamic_allocated_budget =
    control_budget + (number_of_goal_records * goal_budget)
                     + explicitly_reserved_retry_budget
```

This is an upper-bound allowance based on the existing local ceilings, not a
promise that the provider will be called that many times. The ledger also
records actual usage. The default dynamic policy means:

- `finalize_goals()` allocates one goal allowance for every accepted initial
  goal;
- every successful `append_goal()` or `insert_goal()` adds exactly one
  `goal_budget` allowance before it returns success;
- a verification failure that sends the same goal back to implementation
  reserves one additional retry allowance before the next cycle; and
- an appended goal never borrows or reduces capacity already allocated to a
  different goal.

With the current defaults, one normal pass through two goals has a nominal
  dynamic allowance of `10 + 6 + 4 + 2 * (25 + 12) = 94` logical sub-turns.
  A third goal raises the allowance to 131. If every phase invocation reaches
  its five-attempt transition ceiling, the corresponding worst-case allowance
  is `20 + 3 * 185 = 575`. The UI and diagnostics must label these as
  *allowances*, not expected usage.

### 7.3 Optional hard aggregate ceiling

`GoalFlowParams` shall add an explicit `max_workflow_turns` setting:

- `0` means no independent aggregate ceiling; the existing per-phase,
  `max_goals`, retry, provider, timeout, and context-window limits remain in
  force. This is the backwards-compatible default.
- A positive value is a hard ceiling on logical sub-turns for this workflow
  run. It must be validated as a positive integer within a safe implementation
  maximum.

When a hard ceiling is configured:

1. Initial goal finalization rejects a plan whose calculated mandatory
   allowance cannot fit, with a structured error naming the required and
   configured amounts.
2. `append_goal()` / `insert_goal()` atomically reject a new goal if allocating
   its goal allowance would exceed the hard ceiling. The existing plan and
   checkpoint remain unchanged.
3. A verification retry is admitted only when its retry allowance fits. If it
   does not, the runner writes a failure/pause checkpoint at the current goal
   and reports `goal_workflow_turn_budget_exhausted`; it never starts `CLARIFY`
   or `DECIDE_GOALS` again.
4. The runner must preserve at least one available logical slot for the active
   phase’s required transition/tool result when deciding whether to dispatch
   another continuation.
5. The user can change the configured ceiling and resume the same run. Resume
   must reconcile the saved `used` value with the new configured ceiling
   without resetting usage or the plan.

The hard ceiling is intentionally separate from `execution.max_agent_turns`.
Changing the latter must not silently change an existing workflow’s durable
budget.

### 7.4 Verification retry policy

The current implementation can loop from a failed verification back to
implementation without a workflow-wide count. Add
`max_goal_retries` to `GoalFlowParams`:

- `-1` preserves the current unlimited retry behavior, subject to any configured
  `max_workflow_turns`, timeout, provider, and max-goal limits.
- `0` disallows a second implementation/verification cycle after the first
  failed verification and pauses with a durable diagnostic.
- A positive value limits additional cycles per goal.

Each allowed retry receives a budget reservation and is persisted. A failed
verification must not consume a retry allowance twice if the process is
interrupted after the rejection but before the next phase entry.

### 7.5 Budget exhaustion is resumable

Every budget exhaustion path must:

- retain the typed `GoalContext`, active stable goal ID, evidence, files,
  attempts, list revision, budget ledger, and same workflow run ID;
- checkpoint the current phase before exposing the error;
- distinguish `phase_max_turns_exhausted`,
  `workflow_turn_budget_exhausted`, `goal_retry_budget_exhausted`,
  `provider_retry_exhausted`, `turn_timeout`, and `context_window_exhausted`;
- leave the run resumable when the failure is recoverable/configuration-related;
  and
- never convert an exhausted or failed dynamic run into a new initial planning
  run.

## 8. Configuration contract

The exact parser naming may follow existing conventions, but the effective
configuration shall be equivalent to:

```toml
[workflows.goal_flow]
# Existing dynamic-plan safety settings.
max_goals = 1000
max_goal_text_chars = 4096

# New budget settings.
max_workflow_turns = 0       # 0 = no aggregate ceiling; per-phase limits remain
max_goal_retries = -1        # -1 = unlimited, subject to other limits
transition_attempts = 5      # replacement for the current _MAX_ATTEMPTS
```

The default dynamic allowance is derived from the registered `GoalFlowWorkflow`
phase metadata, so a phase ceiling changed in one source of truth changes the
budget calculation as well. If a future configuration exposes per-phase
overrides, the ledger must use the effective runtime values, not stale
declarative defaults.

Invalid values must fail closed with a configuration diagnostic: booleans,
non-integers, zero/negative values where not explicitly permitted, and values
above implementation safety ceilings are rejected. No value may be silently
clamped. Existing valid `max_goals`, goal text, model, and provider settings
remain compatible.

## 9. Durable state and checkpoint schema

Extend `GoalContext` with a typed, JSON-safe budget section. The exact class
names may follow repository conventions, but the durable fields shall be
equivalent to:

```text
GoalTurnBudget
  version: positive integer
  mode: dynamic | hard_cap
  hard_limit: non-negative integer       # 0 means unlimited
  transition_attempt_limit: positive integer
  max_goal_retries: integer >= -1
  control_allowance: non-negative integer
  goal_allowance: non-negative integer
  retry_allowance: non-negative integer
  allocated: non-negative integer
  consumed: non-negative integer
  remaining: derived non-negative integer | null when unlimited
  provider_attempts: non-negative integer
  transient_retry_attempts: non-negative integer
  irrecoverable_retry_attempts: non-negative integer
  by_phase: bounded map[phase_name, non-negative integer]
  by_goal: bounded map[stable_goal_id, non-negative integer]
  last_charged_step_id: string | null
  budget_revision: positive integer
```

Requirements:

- `remaining` is derived from `hard_limit - consumed` for a hard-cap run and is
  not trusted as an independent mutable value.
- Goal additions increase `allocated` and `goal_allowance` in the same
  checkpoint transaction as the new `GoalRecord` and mutation receipt.
- A logical step is idempotently charged by stable run/phase/step identity.
- Budget entries are bounded and do not copy the full conversation into the
  checkpoint. Per-goal maps may be compacted only with an auditable aggregate;
  they must not lose the active goal’s counter.
- Old `goal_list_version` checkpoints without budget fields load with a
  compatibility migration: existing records remain authoritative, `consumed`
  starts at zero or from recoverable journal evidence, and the calculated
  allowance is written at the next safe checkpoint. Migration must not reset
  the phase or regenerate goal IDs.
- A malformed or contradictory budget (negative values, consumed above a hard
  limit, duplicate step receipt, unknown goal ID, or incompatible version) is a
  typed recovery error, never permission to restart planning.

## 10. Runtime dataflow

```text
agent calls append_goal(goal)
        │
        ▼
GoalFlowRunner._mutate_goal()
  validate phase, owner, text, max_goals, and hard-cap capacity
  derive one goal allowance from effective phase ceilings
  construct GoalRecord + GoalMutationReceipt
  increment goal-list and budget revisions together
        │
        ▼
WorkflowRunHandle.attach_context()
  save one atomic checkpoint: records + active cursor + budget ledger
        │ success only
        ▼
tool returns goal_id/index/goal_count/allocated/used/remaining
  current phase continues; no transition event is set
        │
        ▼
run_phase() → _run_agent_turn()
  emit/collect logical step identity and provider-attempt diagnostics
  charge each logical step once
        │
        ▼
GoalFlowRunner records phase/goal usage
  transition succeeds → phase-boundary checkpoint
  verify=false → reserve retry allowance, checkpoint, return to implementation
        │ error/interruption at any point
        ▼
failure checkpoint preserves same run, cursor, records, memory journal,
budget usage, and mutation receipts; --continue/--resume rehydrates all state
```

The stable prompt contract remains unchanged. The current ordered goal list,
budget counters, remaining capacity, and exhaustion diagnostics are dynamic
phase context. They must never be interpolated into the cache-stable prefix or
prepended as a synthetic conversation reset.

## 11. API and implementation requirements

### FR-01 — Canonical budget model

Implement a typed budget model owned by `goal_flow`, with pure functions for
deriving allowances, charging a step idempotently, reserving a new goal, and
reserving a verification retry. These functions must be independently unit
testable and must not perform filesystem/provider I/O.

### FR-02 — Accurate phase usage reporting

Extend the existing phase/turn boundary in a backwards-compatible way so
`GoalFlowRunner` can observe actual logical sub-turn usage. The preferred
contract is an optional usage result/observer on `run_phase()` and
`_run_agent_turn()`; existing callers that ignore the result must continue to
work. Do not infer usage from the TUI user-turn count or from text output.

The observer/result must identify:

- workflow run ID and phase invocation ID;
- logical step indexes charged;
- provider attempts and retry categories;
- completed, interrupted, and failed status; and
- the phase’s effective local `max_turns`.

### FR-03 — Atomic initial allocation

`finalize_goals()` and `GoalContext.initialize_goals()` must calculate and
persist the dynamic allowance for the accepted initial plan. A plan that cannot
fit a configured hard limit is rejected before the initial transition succeeds.

### FR-04 — Atomic append/insert allocation

`append_goal()` and `insert_goal()` must add exactly one goal allowance in the
same transaction as the stable goal record and existing mutation receipt. A
capacity rejection must make no in-memory or durable change and must not signal
a phase transition.

### FR-05 — No budget reset at phase boundaries

Transitioning from implementation to verification, moving to the next goal,
looping after a failed verification, resuming after an exception, and entering
summary must carry the same ledger forward. A new `run_phase()` invocation may
reset its local `ctx.turn` to zero, but it must not reset workflow-level
`consumed` or per-goal usage.

### FR-06 — Safe exhaustion handling

When no logical slot remains under a hard cap, stop at a tool/provider-safe
boundary, checkpoint, and surface a typed error. Do not dispatch a new phase,
discard an accepted goal, or run initial clarification again. A resumed run
after configuration adjustment must continue at the saved active phase.

### FR-07 — Retry separation

Use the existing retry helper and error taxonomy. Transport and irrecoverable
provider retries retain their current bounds and are not mistaken for new
logical agent turns. Their counts are included in diagnostics and checkpoints
but do not silently expand the workflow’s logical-turn hard cap.

### FR-08 — Checkpoint/recovery integration

Use `WorkflowRunHandle` and its existing ownership/revision rules. Budget
updates must participate in the same CAS/claim checks as goal mutations and
phase boundary checkpoints. A process crash between charging and publication
must resolve deterministically from the journal/checkpoint without double
charging or losing the active goal.

### FR-09 — TUI and journal observability

Add bounded structured events and compact TUI notices for:

- budget initialized;
- goal allowance added;
- logical step charged;
- retry allowance reserved;
- budget near exhaustion; and
- budget exhausted.

Every notice must identify the limit category, current phase/goal, used,
allocated, and remaining (or `unlimited`). Do not print full prompts, goal text,
provider credentials, or unbounded receipts.

### FR-10 — Prompt guidance

The dynamic goal-list block must tell the agent that appending/ inserting a goal
adds future work and consumes a bounded allowance when a hard cap is configured.
It must instruct the agent to add only necessary, concrete work and to inspect
the returned capacity fields. It must not encourage splitting one task into
many goals merely to obtain more turns.

### FR-11 — Compatibility

`execution.max_agent_turns` continues to govern direct turns. Existing
`GoalFlowParams` constructors, workflow plugin loading, checkpoint codecs,
`run_phase()` callers, and tests that do not request budget accounting remain
valid. New fields must be appended to public dataclasses where positional
compatibility matters.

## 12. User journeys and examples

### 12.1 Normal dynamic append

1. The agent finalizes two goals; the checkpoint records two goal allowances.
2. It implements goal A and calls `append_goal("add migration regression tests")`.
3. The tool atomically adds record C and one goal allowance, then returns
   `allocated`, `consumed`, and `remaining`.
4. The agent continues goal A; the active goal and phase do not change.
5. After A and B are verified, the scheduler selects C. A and B are not replayed.
6. The final checkpoint contains all goal and budget receipts.

### 12.2 Multiple appends

Five accepted appends add five distinct stable IDs and five allowance increments.
Each is serialized by the existing mutation lock and checkpoint revision. A
crash after the third success restores exactly three additions; it does not
replay the first two or expose the last two as accepted.

### 12.3 Hard-cap rejection

With `max_workflow_turns = 200`, the initial plan consumes/allows too much
capacity for another full goal. `append_goal()` returns
`goal_turn_budget_exhausted` with the required delta and current values. The
goal list, revision, cursor, and checkpoint are unchanged. The agent may finish
existing work or the user may change configuration and resume.

### 12.4 Verification retry

`verify_goal(satisfied=False, ...)` records evidence and reserves one retry
allowance before returning to implementation. If the configured retry limit is
reached, the run pauses at the same stable goal ID with
`goal_retry_budget_exhausted`; it does not re-enter planning.

### 12.5 Provider failure after append

The append checkpoint succeeds and the next provider request returns a
transient/irrecoverable error after its configured retries. Failure finalization
preserves the appended record, budget increment, consumed count, current phase,
and conversation. Resume continues that same goal-flow run, never a fresh
`CLARIFY` or `DECIDE_GOALS` run.

## 13. Acceptance criteria

### AC-01 — Definitions are observable

Tests and diagnostics distinguish logical sub-turns, phase invocations,
provider retries, user turns, timeouts, and context-window failures.

### AC-02 — Per-phase compatibility

With no new configuration, every existing phase uses its current local
`max_turns` value and transition retry behavior. Direct non-workflow turns are
unaffected by goal-flow accounting.

### AC-03 — Initial dynamic allocation

Finalizing `N` valid goals creates `N` stable records and an allowance equal to
the fixed control allowance plus `N * goal_budget`; the value is checkpointed
before the transition is considered successful.

### AC-04 — Append allocation

Each successful `append_goal()` increases the allocated allowance by exactly one
effective goal allowance and returns the new values. It does not alter the
active goal, phase, consumed count, or existing goal evidence.

### AC-05 — Insert allocation

`insert_goal()` has the same budget semantics as append, including insertion
before the active goal and after the last goal. Existing stable IDs and
per-goal counters remain attached to their original records.

### AC-06 — Atomic rejection

Invalid text, invalid index, missing checkpoint ownership, max-goal overflow,
and hard-cap insufficiency leave both typed state and the prior checkpoint
unchanged and do not set a transition event.

### AC-07 — Accurate charge-once accounting

A logical step charged once remains charged once across provider retries,
workflow retries, cancellation recovery, and process restart. A failed attempt
does not erase earlier committed steps.

### AC-08 — Dynamic scheduling

Every accepted new goal receives implementation and verification capacity and is
executed exactly once unless explicitly skipped/deleted. A completed goal is
never replayed because list indexes moved.

### AC-09 — Verification retry accounting

Each unsatisfied verification either reserves a permitted retry allowance and
continues, or pauses with a typed retry-budget diagnostic. The same rejection
cannot reserve twice after resume.

### AC-10 — Hard-cap behavior

Under a configured positive `max_workflow_turns`, the runner never charges more
than the cap. It rejects additions that cannot fit and checkpoints exhaustion
without starting a new planning phase.

### AC-11 — Resume fidelity

After append, insertion, phase retry, provider error, cancellation, process
restart, `--continue`, and `--resume`, the same run restores goal records,
active ID, phase, revision, consumed/allocated values, retry reservations, and
conversation memory.

### AC-12 — Failure taxonomy

The UI and checkpoint diagnostics distinguish per-phase exhaustion, aggregate
budget exhaustion, goal retry exhaustion, provider retry exhaustion, timeout,
and context-window exhaustion.

### AC-13 — Cache contract

Budget counters, dynamic plan rows, and remaining capacity appear only in the
dynamic workflow context. Stable prompt and tool-schema fingerprints remain
unchanged across appends, charges, and resumes where the connection/profile is
unchanged.

### AC-14 — Concurrency and ownership

Concurrent mutation attempts serialize through the existing lock/claim/CAS
path. Each committed mutation has one revision and one allowance increment;
stale owners cannot spend or allocate budget.

### AC-15 — Bounded state and UI

Per-goal counters, phase counters, receipts, event payloads, and TUI notices
remain bounded under the configured `max_goals` and receipt limits. No full
conversation or unbounded goal text is copied into a checkpoint event.

### AC-16 — Backward migration

Existing pre-PRD-199 checkpoints load and resume without changing the phase,
goal IDs, completed records, or conversation. The next safe checkpoint writes
the new budget section and marks its migration version.

### AC-17 — No hidden budget mutation

Changing `ExecutionSettings.max_agent_turns`, `_turn_count`, or
`turn_timeout_s` does not silently change a saved goal-flow budget. Only the
documented `goal_flow` budget settings and explicit dynamic mutations do so.

## 14. Testing strategy

### 14.1 Unit tests

Add clean-slate unit tests for:

- counting one logical step and ignoring duplicate step receipts;
- separating provider retries from logical turns;
- deriving fixed and per-goal allowances from effective phase metadata;
- validating `max_workflow_turns`, `max_goal_retries`, and transition-attempt
  settings;
- initial allocation for zero, one, and many goals;
- append/insert budget deltas and hard-cap rejection;
- retry reservation and idempotent recovery;
- exhaustion diagnostics and remaining-capacity calculation;
- checkpoint payload round-trip, malformed values, version migration, and
  contradictory consumed/limit state;
- preservation of stable prompt fingerprints while dynamic budget text changes;
- compatibility of existing `GoalFlowParams` and `run_phase()` call shapes.

### 14.2 Integration tests

Use a temporary `SessionConversation`, `WorkflowRunHandle`, checkpoint store,
and deterministic fake runner to cover:

- initial plan → append → all goals complete with monotonic budget checkpoints;
- several appends from both implementation and verification;
- phase-local missing-transition retries charged correctly;
- false verification reserving a retry and resuming implementation;
- provider failure after a committed append and same-run resume;
- interruption between budget mutation and tool-result projection;
- owner conflict and stale checkpoint revision;
- TUI/journal event payloads and bounded notices;
- config override changing a hard limit before resume without resetting usage.

### 14.3 End-to-end tests

With the real `GoalFlowRunner`, `AgentTurnRunner`, mock transport, and durable
conversation:

- execute a two-goal plan and append a third during implementation;
- insert a prerequisite before the active goal and prove the active goal is not
  replayed;
- make the transport retry the same provider step and prove it is charged once;
- exhaust a finite aggregate budget and resume after raising the limit;
- exhaust goal retries and prove the checkpoint resumes at the same goal;
- interrupt and resume after multiple accepted mutations;
- prove that `execution.max_agent_turns` affects direct turns only;
- prove no dynamic run resets to `CLARIFY`/`DECIDE_GOALS` after exhaustion or
  provider failure.

### 14.4 Quality gates

The implementation must add/update the relevant unit, integration, and E2E
tests and pass the repository’s source-surface gates:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

Because this feature changes user-visible configuration, workflow state, TUI
diagnostics, and public behavior, implementation must also update the relevant
README/guide, storage reference, public symbol inventories, and a PRD index
entry after the user’s “PRD only” review step is complete.

## 15. Documentation requirements

Document in `docs/guides/workflows.md`:

- the difference between per-phase `max_turns`, direct-turn
  `max_agent_turns`, aggregate goal-flow budget, provider retries, and timeout;
- the dynamic allowance formula and append/insert behavior;
- finite hard-cap configuration and resume-after-capacity-change;
- verification retry behavior; and
- examples of the TUI diagnostics.

Document the serialized budget fields and migration rules in
`docs/reference/storage.md`. Update configuration examples and troubleshooting
guidance in `README.md`, `docs/guides/configuration.md`, and
`docs/usage/12-troubleshooting.md`. Add public budget types or configuration
symbols to `llms-full.txt` and the appropriate `__all__` inventories.

## 16. Security, resilience, and performance

- Never include provider credentials, raw prompts, full conversation messages,
  or unbounded goal text in budget events or checkpoints beyond existing bounded
  context fields.
- Use existing workflow ownership, mutation locks, checkpoint CAS, and journal
  transaction boundaries; do not add an alternate persistence path.
- Charge before publishing a phase result when the step identity is known, but
  make the charge/checkpoint protocol recoverable and idempotent if the process
  dies between those operations.
- Avoid a provider call merely to calculate a budget. Budget derivation is pure
  and local.
- Keep per-step and per-goal lookup O(1) or bounded by configured goal count;
  do not scan the full conversation on every tool call.
- Keep dynamic budget context compact so it does not itself cause context-window
  exhaustion. Render totals and the active goal’s row rather than repeating all
  counters in every prompt when the plan is large.
- Treat configuration and checkpoint values as untrusted input and reject
  contradictory or oversized values.

## 17. Rollout and migration plan

1. Implement the pure budget model and payload migration without changing
   default runtime behavior.
2. Add usage observation at the existing agent-turn boundary and verify it with
   deterministic fake transports.
3. Integrate initialization, append/insert, retry, phase boundary, and failure
   checkpoints.
4. Add TUI/journal diagnostics and dynamic prompt guidance.
5. Enable dynamic accounting with `max_workflow_turns = 0` by default, preserving
   current unlimited aggregate behavior while making capacity visible.
6. Offer finite hard caps as an opt-in configuration and document their resume
   semantics.
7. Run the full test matrix and quality gates before changing any default hard
   limit.

## 18. Decision summary

The investigation does **not** support changing a shared global `max_turns`
value whenever `append_goal()` is called. That would conflate direct turns,
phase-local loops, and dynamic workflow scheduling. The supported solution is a
`goal_flow`-owned dynamic budget ledger:

- per-phase ceilings remain local and unchanged;
- initial and appended goals receive explicit future allowances;
- actual logical sub-turns are charged once and persisted;
- verification retries are budgeted separately;
- provider retries and wall-clock deadlines remain separate policies; and
- every failure or exhaustion path checkpoints the same run and active phase.

This directly solves the original concern: adding goals no longer competes with
an invisible fixed aggregate budget, and when a configured hard ceiling really
is reached, the user receives an exact, resumable explanation instead of a
premature or apparently unexplained workflow stop.
