---
title: "PRD-261: Bounded Repair and Provider-Turn Budgets for make_agenthicc_tool"
status: Proposed
version: 0.1.0
created: 2026-11-01
scope: "make_agenthicc_tool rejection loops, phase-transition retries, and resumable attempt accounting"
related_prds:
  - PRD-170 # Reliable workflow resume and durable recovery
  - PRD-173 # Recoverable workflow errors and failure checkpoints
  - PRD-184 # Preserve workflow phase after errors
  - PRD-198 # Bounded recovery retries
  - PRD-259 # Deterministic make_agenthicc_tool validation
  - PRD-260 # Scoped generation and reviewed publication

tags:
  - workflows
  - retries
  - budgets
  - resume
---

# PRD-261 — Bounded Repair and Provider-Turn Budgets for `make_agenthicc_tool`

## 1. Summary

Bound the complete `make_agenthicc_tool` lifecycle, including repeated
VALIDATE → GENERATE rejection cycles. The runner currently caps how many
`run_phase()` calls each phase makes while waiting for its transition tool, but
it does not cap the outer repair loop: every validation rejection returns to
GENERATION and starts another full phase cycle. A model can therefore keep the
workflow, provider usage, and file mutations alive indefinitely by repeatedly
rejecting and regenerating.

Add an explicit repair counter and a finite overall phase-invocation budget.
Persist both in the typed checkpoint so resume cannot reset the budget. When the
budget is exhausted, fail with the last deterministic validation report and a
clear next action; do not start another provider turn automatically.

## 2. Evidence-backed current state

`MakeToolRunner` uses `_MAX_ATTEMPTS = 5` in `_analyze()`, `_generate()`, and
`_validate()`. Each value bounds only the local loop that waits for a phase
handoff. When `_validate()` receives `reject_tool()`, it returns
`MakeToolState.GENERATE`. The outer `run()`/`resume()` loop then enters another
generation and validation cycle without tracking how many repairs have
occurred.

`tests/unit/test_website_workflow_coverage.py::test_make_agenthicc_tool_full_driver_and_repair_loop`
exercises one rejection followed by approval. The no-transition tests prove a
single phase-local cap, but no test repeatedly rejects and proves termination.

This differs from the base runner's bounded-recovery guidance: a finite
transition-attempt loop is not a finite end-to-end workflow when an outer
state-machine edge can repeat forever.

## 3. Problem

A persistent validator rejection, provider behavior, or maliciously shaped
agent response can create an unbounded loop. Each loop can consume additional
model turns and rewrite the candidate; when generation writes directly to the
active plugin directory, it can also repeat side effects. Even after generation
is moved to staging (PRD-260), unbounded retries remain a cost, latency, and
operational reliability defect.

Resume must preserve the same budget as the interrupted run. Resetting counters
on each resume would turn the nominal cap into an unlimited sequence across
process restarts.

## 4. Goals

1. Bound both transition-seeking retries and the total validation-repair cycle.
2. Make the maximum number of `run_phase()` invocations and agent subturns
   derivable from constants, not model behavior.
3. Persist repair and invocation counters in checkpoint state.
4. Never reset or increase a budget on resume.
5. Stop cleanly with the latest deterministic diagnostics when no budget remains.
6. Preserve the existing workflow error-finalization and recovery ownership.

## 5. Non-goals

- Retrying transient provider, transport, or checkpoint errors; PRDs 173, 184,
  and 198 own those recovery semantics.
- Adding a generic retry framework or changing other workflow runners.
- Treating an agent-reported approval as a substitute for PRD-259 validation.
- Automatically opening a fresh run after a repair budget is exhausted.
- Introducing a user-configurable unbounded retry setting.

## 6. Retry and budget contract

### 6.1 Finite repair cycles

Define a hard default of at most **three complete repair cycles** after the initial
generation. A repair cycle is one `reject_tool()` decision followed by an
opportunity to regenerate and revalidate. The initial generation and validation
are not counted as repairs.

After the third repair candidate fails validation, the runner must transition to
`FAILED` without entering GENERATE again. The context and final failure report
must include the final deterministic diagnostics, the repair count, and a
concise instruction that the user may inspect the staged candidate and start a
new workflow after revising the request or tool plan.

The limit is a fixed policy constant for this workflow in the first release. Do
not add a TOML setting or model-supplied field that can raise it.

### 6.2 Bounded transition attempts and aggregate invocation budget

Retain a finite per-phase handoff retry limit. Define it separately from the
repair-cycle limit so the names and diagnostics cannot conflate:

- `MAX_TRANSITION_ATTEMPTS`: maximum calls to `run_phase()` for one state entry
  while waiting for its required control transition;
- `MAX_REPAIR_CYCLES`: maximum reject-and-regenerate cycles across the run; and
- `MAX_PHASE_INVOCATIONS`: derived hard ceiling for all phase calls in one run,
  including repeated state entries.

The aggregate ceiling must be calculated from the phase topology and limits and
checked before each provider call. It must not be reset when a phase is
re-entered. Every invocation increments the durable counter before the provider
turn starts, so a crash or cancellation cannot make an already-started call
free in the resumed budget.

Keep existing per-invocation `max_turns` limits. Document the resulting upper
bound on agent subturns as the sum of each phase's configured maximum times its
maximum number of invocations. Tests must assert that all execution paths stay
within that bound, including repeated prose-only turns and repeated validation
rejections.

### 6.3 Resume-safe accounting

Add typed context fields for repair cycles used, total phase invocations used,
and the last deterministic validation report identity. Encode them in the
checkpoint payload. On restore:

- reject negative, non-integer, boolean-as-integer, or over-limit counters;
- reject a state/counter combination that could exceed the remaining budget;
- preserve the exact counters rather than defaulting them to zero when required;
- ensure the report identity still matches the candidate digest before it can
  guide a repair or approval; and
- fail closed if an older or malformed payload lacks the required accounting
  fields. Do not add a legacy decoder that resets the counters.

### 6.4 Budget exhaustion behavior

Budget exhaustion is a normal, explicit terminal workflow result, not an
exception that retries itself. It must:

- set `MakeToolState.FAILED` and a stable failure reason;
- retain the last report and repair count for inspection;
- avoid calling generation, validation, or finalization again;
- avoid publishing or trusting a candidate; and
- leave the session-level workflow finalizer responsible for terminal status and
  durable failure handling, consistent with `BaseWorkflowRunner`.

A later user-initiated workflow run starts a new budget and a new run identity.
A resume continues the existing budget.

## 7. Functional requirements

### FR-1 — Explicit counters

Replace the ambiguous `_MAX_ATTEMPTS` usage with clearly named constants and
persisted counters. Increment the repair counter only after an accepted
validation rejection; increment the invocation counter before every
`run_phase()` call.

### FR-2 — Check before call

Check the aggregate budget before starting every phase invocation. If exhausted,
return a terminal failure without contacting the provider. Count prose-only
turns and failed transition attempts because they still consume provider
resources.

### FR-3 — Rejection limit

When validation rejects a candidate and repairs remain, increment the repair
counter and return to GENERATE. When no repairs remain, fail immediately and
retain the rejection report. Do not rely on the validation agent to stop itself.

### FR-4 — Durable resume

Persist counters before phase calls and transitions using the workflow handle's
existing durable context/checkpoint path. Restoring must not re-run an already
counted phase invocation or reset a rejected-repair count. Integrate with the
state-specific context validation in PRD-259 and artifact identity in PRD-260.

### FR-5 — User-visible result

The final failure output must report a bounded summary of the last deterministic
failure and distinguish "repair budget exhausted" from "phase did not call its
transition tool." It must not print full generated source, prompts, secrets, or
unbounded tool output.

### FR-6 — No arbitrary budget mutation

The model-facing transition tools cannot accept retry counts, budgets, or next
states. Only runner-owned transitions and persisted typed context change these
counters.

## 8. Acceptance criteria

1. One validation rejection followed by a passing candidate completes with a
   repair count of one.
2. Four consecutive validation rejections (the initial candidate plus three
   repaired candidates) terminate in `FAILED` without a fifth generation; the
   latest diagnostics remain available.
3. A phase that returns prose without calling its transition tool terminates
   after the configured transition-attempt limit.
4. The aggregate invocation ceiling holds when every phase consumes its full
   transition-attempt limit and every validation cycle is rejected.
5. Every provider phase call is counted before it begins, including a call that
   raises or is interrupted.
6. Resume after one or more repairs preserves the counts and cannot obtain a
   fresh repair or invocation allowance.
7. A checkpoint with missing, malformed, negative, over-limit, or
   success-shaped impossible counters is rejected before provider execution.
8. Budget exhaustion causes no further provider/tool call, no publication, and
   no trust-store mutation.
9. A new user-initiated run receives a new run ID and a fresh, bounded budget.
10. Existing transient-error recovery and workflow-finalizer ownership remain
    unchanged.

## 9. Test plan

- Unit tests for counter increments, per-state transition exhaustion, repair
  exhaustion, and exact aggregate maxima.
- Runner tests for repeated rejection, repeated prose-only responses, and
  mixed failures that consume both budgets.
- Checkpoint round-trip tests at ANALYZE, GENERATE, VALIDATE, and exhausted
  FAILED states.
- Resume tests proving counts persist and no already-counted phase is silently
  replayed outside the documented recovery policy.
- Integration test with a fake provider that repeatedly rejects until the hard
  cap; assert finite calls and no publication/trust changes.
- Regression tests for ordinary one-repair success and the last allowed repair
  succeeding.

## 10. Rollout

1. Add typed counters and a deterministic aggregate budget calculation.
2. Enforce the repair cap on the validation rejection edge.
3. Persist counters before calls and validate them on restore.
4. Add repeated-rejection, no-transition, and interruption/resume coverage.
5. Update workflow documentation to state the finite repair limit and how to
   restart after inspecting the failure.

## 11. Operational considerations

- The limit is about workflow progress, not provider retry policy. Transport
  retries remain governed by the shared runner contract and must not duplicate
  already-counted phase entry accounting.
- Keep counters, failure labels, and reports bounded and JSON-safe.
- Any future policy change that raises the maximum must update the documented
  upper bound and regression tests in the same change.
