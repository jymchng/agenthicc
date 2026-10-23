---
title: "PRD-249: Resolve workflow checkpoint phases through typed context semantics"
status: Proposed
version: 1.0.0
date: 2026-10-20
repository: jymchng/agenthicc
priority: P1
related_prds: [prd-170, prd-184, prd-186, prd-248]
tags: [workflow-recovery, checkpoints, phase-cursor, dynamic-workflows, create-workflow]
---

# PRD-249 — Resolve Workflow Checkpoint Phases Through Typed Context Semantics

## 1. Executive summary

A valid resumable run can be rejected with:

```text
Cannot resume 'c5b1090424c14628accfac1ac2c418bf':
checkpoint_phase_mismatch: checkpoint phase does not match the saved workflow context
```

The immediate cause is a mismatch between two different concepts that recovery
currently treats as equivalent:

- the workflow's **active phase cursor**, such as `research_company`; and
- the workflow context's **coarse state-machine state**, such as `ACTIVITY`.

The supplied session's event projection identifies workflow `tailor_resume`,
run `c5b1090424c14628accfac1ac2c418bf`, paused after user cancellation at
`research_company` (checkpoint revision 12). Subsequent recovery discovery
reported zero recoverable runs and listed this run as invalid. The session
readiness projection reported provider history ready but workflow recovery
blocked. The exact checkpoint JSON is not present in this checkout, so its
serialized fields could not be independently inspected. However, the installed
`tailor_resume` implementation makes the reported phase mismatch reproducible
from its documented context schema and Agenthicc's current validator.

`WorkflowRecoveryCoordinator._validate_recovery()` first looks for
`restored.current_phase`. If it is absent, it uses
`restored.state.name.lower()` as the context phase. `TailorContext` has no
`current_phase` field. Its state is `TailorState.ACTIVITY` for every planned
activity, while `current_activity` derives the actual phase from
`activity_names` and `activity_cursor`. Thus a context executing
`research_company` can legitimately restore as `state=ACTIVITY` and
`current_activity=research_company`. Recovery instead compares checkpoint
phase `research_company` with the literal enum name `activity` and rejects the
checkpoint.

Cancellation exposed the invalid recovery record; it is not itself the cause.
This PRD defines a typed, topology-aware phase-identity contract so recovery
can distinguish canonical phase identity from an implementation's internal
state-machine state, while still failing closed on genuine phase or topology
corruption.

## 2. Incident evidence and limits

### 2.1 Evidence from the supplied session

The readable session-service projection contains:

| Evidence | Recorded value |
|---|---|
| Session | `7adf6422-6812-4ffd-893a-1b64d72e7504` |
| Workflow | `tailor_resume` |
| Run | `c5b1090424c14628accfac1ac2c418bf` |
| Last paused phase | `research_company` |
| Pause reason | `user_cancelled` |
| Checkpoint revision | `12` |
| Recovery discovery | `count=0`, run listed in `invalid_run_ids` |
| Provider history | `ready` |
| Workflow readiness | `blocked` / not resumable |

The projection confirms that the run was saved at a named activity phase and
then rejected during recovery. It does not itself contain the validator's
error code or checkpoint context bytes. The run ID shown above is the exact ID
in the event projection; the screenshot's visually ambiguous `1`/`l` glyph
does not affect the diagnosis.

The confirming session events are `workflow_paused_after_error` (sequence
1149), `workflow_recovery_available` with this run in `invalid_run_ids`
(sequence 1152), and `session_readiness_changed` showing provider history
`ready` but workflow recovery `blocked` (sequence 1153).

### 2.2 Evidence from the workflow implementation

The project at `/root/resume-workflow/tailor-resume` contains a generated
`tailor_resume` runner with these relevant properties:

- `TailorContext.state` is a `TailorState` enum.
- `TailorState.ACTIVITY` is one state that drives several distinct activity
  phases.
- `TailorContext.current_activity` resolves the active activity using the
  planned names and `activity_cursor`.
- `active_phase_names()` builds a dynamic topology containing names such as
  `research_company`; it does not contain the coarse state name `activity`.
- The workflow checkpoint topology resolver reconstructs that active plan
  from the context payload.
- `publish_phase_annotation()` calls `WorkflowRunHandle.update_phase()` at
  phase entry, so entering `research_company` writes a checkpoint whose
  top-level cursor is the named phase while the context codec writes the
  coarse `ACTIVITY` state and its activity cursor.
- The custom checkpoint codec serializes `state`, `activity_names`, and
  `activity_cursor`, but no canonical `current_phase` field.
- On cancellation, `finalize_failure()` calls `sync_context_cursor()`. The
  generic synchronizer cannot map `ACTIVITY` to a topology phase and therefore
  retains the already-published `research_company` handle cursor. Failure
  preparation may set a runtime `current_phase` attribute, but that attribute
  is not part of the custom checkpoint codec and is absent again after restore.

Relevant implementation points are `src/agenthicc/runners/workflow_recovery.py`
(`_validate_recovery`),
`src/agenthicc/runners/workflow_handle.py` (`sync_context_cursor()`,
`finalize_failure()`, and `_prepare_context_for_failure_resume()`),
`src/agenthicc/workflows/phase_lifecycle.py` (`publish_phase_annotation()`),
and the generated workflow's
`.agenthicc/workflows/tailor_resume/runner.py` (`TailorContext`,
`current_activity`, `_drive()`, `checkpoint_boundary()`, and its checkpoint
codec).

The framework's generic state-name fallback therefore compares values from
different namespaces. This is a deterministic false-positive path for a
context in the dynamic ACTIVITY state when the checkpoint phase is one of its
named activities.

### 2.3 Evidence limitation

The private checkpoint file for the supplied session is not available in the
current checkout's session store. This PRD does not claim a byte-for-byte
inspection of that checkpoint. The event projection plus the actual workflow
schema and recovery code establish the failure mechanism, but an implementation
must add a fixture that reproduces the exact `ACTIVITY`/`current_activity`
shape and verify the behavior against real serialized checkpoints where
available.

## 3. Problem statement and root cause

In `src/agenthicc/runners/workflow_recovery.py`, after restoring a context,
recovery currently performs the equivalent of:

```python
context_phase = restored.current_phase
if not context_phase:
    context_phase = restored.state.name.lower()
if checkpoint.current_phase != context_phase:
    reject("checkpoint_phase_mismatch")
```

This assumes that every context's state enum is a one-to-one encoding of the
active phase graph. That assumption is not true for composite or dynamic state
machines.

For the affected workflow, a valid checkpoint can be represented as:

```text
checkpoint.current_phase              = "research_company"
checkpoint.phase_index                = index of "research_company" in the active plan
context.state                         = TailorState.ACTIVITY
context.activity_names[cursor]        = "research_company"
context.current_activity              = "research_company"
```

The validator ignores `current_activity`, falls back to `state.name.lower()`,
and compares:

```text
"research_company" != "activity"
```

The resulting checkpoint can be internally correct: phase entry persisted
`research_company`, while the runner's state machine remained in its generic
`ACTIVITY` state and its persisted cursor still selected `research_company`.
The mismatch is introduced by recovery's interpretation of the restored
context, not by a phase transition to the wrong activity. The user cancellation
causes failure finalization to persist a paused checkpoint and exposes the
problem on the next discovery pass; it does not itself corrupt the cursor.

It then reports `checkpoint_phase_mismatch`, even though the checkpoint phase
and the context's effective active phase agree. Since recovery discovery
validates before offering a run for resume, the user cannot reach the runner's
own resume reconciliation logic, which understands the dynamic activity
cursor.

This is distinct from PRD-186's profile-local phase-index mismatch. In this
incident, topology resolution can identify the active phase graph and the
checkpoint's named cursor; the invalid comparison is between that phase and a
coarse internal state. It also qualifies PRD-170's shorthand invariant
`context state == checkpoint.current_phase`: that equality is valid only when
the context state is explicitly a phase identity, not when it is a broader
execution state.

## 4. Goals

1. Resume a dynamic/composite workflow at the exact checkpoint phase when its
   typed context uses a coarser state than its phase graph.
2. Define an explicit, documented way for a workflow to expose the active
   semantic phase represented by its context.
3. Preserve strict rejection when an explicit context phase contradicts the
   checkpoint phase.
4. Continue validating the checkpoint phase and index against the exact active
   workflow topology, including profile- and plan-filtered topologies.
5. Preserve old checkpoints without a schema-breaking migration.
6. Ensure cancellation, provider errors, and process interruption do not
   rewrite a valid phase cursor to `INIT` or strand a run as non-recoverable.
7. Teach `create_workflow` to generate checkpoint-aware phase identity for
   workflows whose state machine and phase graph differ.

## 5. Non-goals

- Treating every phase mismatch as safe or automatically accepting corrupted
  checkpoints.
- Inferring phase identity from arbitrary attribute names or parsing prompts,
  summaries, phase annotations, or user-visible text.
- Rewriting a saved phase, context state, phase index, or workflow plan merely
  to make recovery pass.
- Restarting the workflow at its first phase, creating a replacement run, or
  replaying completed side effects.
- Weakening plugin fingerprint, topology, run identity, conversation cursor,
  claim ownership, journal, or tool-history checks.
- Changing workflow business logic or forcing all workflows to use one
  specific state-machine representation.

## 6. Proposed solution

### 6.1 Separate phase identity from internal state

Add an explicit workflow checkpoint-context phase resolver, for example:

```python
checkpoint_context_phase_name(
    context: object,
    topology: WorkflowCheckpointTopology,
) -> str | None
```

The exact API should follow the existing `WorkflowPlugin` extension contract.
Its semantics must be:

- Return a canonical phase name only when the context can identify the
  executable phase it represents.
- Return `None` when the context encodes a coarse state that does not itself
  identify one active phase.
- Never treat an arbitrary enum's member name as a phase solely because it is
  an enum.
- A workflow with a direct `current_phase` field may use that field as its
  explicit identity.
- A composite/dynamic workflow may map state plus cursor, e.g.
  `ACTIVITY + activity_cursor -> current_activity`.
- Terminal states with no active phase return `None` and are handled according
  to existing terminal-checkpoint rules.

The default implementation may preserve compatibility for direct state
machines by accepting `state.name.lower()` only when that exact name exists in
the resolved active topology. A coarse state such as `activity`, absent from
the topology, must not be compared as if it were a phase. Built-in and
generated workflows with richer context semantics should override the hook.

For `tailor_resume`, the resolver should return `context.current_activity` when
`context.state` is `TailorState.ACTIVITY`, and map the other state members to
their corresponding canonical phase names. It must use the restored context
and resolved active topology, not module-global mutable state.

### 6.2 Make the checkpoint cursor the durable recovery coordinate

Recovery MUST:

1. Resolve the workflow and the exact active topology from the saved context.
2. Validate that `checkpoint.current_phase` exists in that topology and that
   `checkpoint.phase_index` matches its index.
3. Restore and validate the typed context using the workflow codec.
4. Ask the workflow's phase resolver for a semantic context phase.
5. Compare context and checkpoint phases only when the resolver returns an
   explicit phase identity.
6. Reject an explicit mismatch with `checkpoint_phase_mismatch`; do not infer
   mismatch from a coarse state label outside the topology.
7. Keep the checkpoint bytes and artifacts unchanged on either success or
   failure during inspection.

If no context-phase identity is available, the checkpoint's phase and
topology remain authoritative for framework-level discovery. Before invoking
the provider, the workflow's normal resume reconciliation must still verify
that its typed cursor/artifacts can safely continue at that boundary. If it
cannot, it must return a specific non-destructive recovery error rather than
silently restarting.

### 6.3 Use one phase-identity contract across persistence and recovery

`WorkflowRunHandle.sync_context_cursor()`, failure finalization,
`checkpoint_phase_boundary()`, checkpoint construction, and
`WorkflowRecoveryCoordinator` must not each invent different state-to-phase
rules.

The implementation should centralize phase projection in the workflow
checkpoint contract. At phase entry or a successful transition, the context's
committed state/cursor and the handle's `current_phase` are saved together.
During failure finalization, an unmapped coarse state must not overwrite a
known last-safe named phase with the enum's lowercase name. The handle may
retain the last committed cursor only when the context resolver says that the
context does not encode a competing phase; explicit contradictory phase
metadata remains an error.

### 6.4 Compatibility and diagnostics

- Existing checkpoints remain readable; no checkpoint schema migration is
  required unless implementation evidence proves otherwise.
- Static contexts with `state=RESEARCH` and topology phase `research` continue
  to be compared and mismatches remain fail-closed.
- Dynamic contexts whose state is broader than a phase may use the new hook;
  absent the hook, an unmatched state name is not falsely treated as a phase.
- Diagnostics should report bounded, non-secret values: saved phase, resolved
  context phase (if any), state label, active topology version/profile, and
  phase index. Do not include prompt text or full context payloads.
- Recovery UI should distinguish a true semantic phase conflict from a
  context state that simply does not have one-to-one phase semantics.

### 6.5 Generated workflow authoring

Update `create_workflow` prompts, reference tools, and smoke tests so generated
workflows:

- identify whether their state enum represents phases directly or contains
  composite states;
- expose a checkpoint-context phase resolver for composite/dynamic states;
- derive dynamic phase identity from persisted context fields (such as a
  cursor), not process-local state;
- keep phase topology resolution and phase identity resolution consistent;
- preserve the named phase on cancellation and provider failure; and
- test a serialized context round-trip through recovery before publication.

Do not require every workflow to add a redundant `current_phase` field when
the active phase can be derived unambiguously from its typed state/context.

## 7. Data flow

### Current false-rejection path

```text
tailor_resume enters research_company
        │
        ├── state = TailorState.ACTIVITY
        ├── activity_cursor identifies research_company
        └── checkpoint boundary stores current_phase=research_company
                         │
user cancels; failure finalizer saves resumable checkpoint
                         │
recovery restores TailorContext
        │
        ├── no context.current_phase field
        └── generic fallback derives state.name.lower() = "activity"
                         │
                         ▼
compares "activity" with "research_company"
                         │
                         ▼
false checkpoint_phase_mismatch; run excluded from resume picker
```

### Required phase-aware path

```text
restore typed context + resolve active topology
        │
        ├── checkpoint cursor: research_company @ active index N
        ├── context state: ACTIVITY (coarse execution state)
        └── workflow resolver: current_activity -> research_company
                         │
                         ▼
validate named phase/index/topology and explicit context phase agree
                         │
                         ▼
offer the same run for resume; runner resumes research_company
```

## 8. Acceptance criteria

| ID | Criterion |
|---|---|
| 249.1 | A typed context with coarse state `ACTIVITY`, an activity cursor resolving to `research_company`, and a checkpoint cursor at `research_company` is recoverable when all other checkpoint invariants pass. |
| 249.2 | The same test proves recovery does not compare the literal enum name `activity` with a phase in another namespace. |
| 249.3 | A workflow that explicitly resolves a context phase different from the checkpoint remains rejected with `checkpoint_phase_mismatch`. |
| 249.4 | A direct state-to-phase context continues to reject genuine phase mismatches. |
| 249.5 | A saved phase absent from the active topology, wrong phase index, changed topology fingerprint, or invalid workflow identity remains rejected with its existing specific diagnostic. |
| 249.6 | A workflow with no context-phase resolver and a coarse enum state can be discovered from its valid checkpoint cursor, then must pass its own typed resume reconciliation before provider execution. |
| 249.7 | Cancellation/error finalization preserves the last committed named phase when the typed state is coarse; it does not regress to `INIT`, discard the run, or invent an enum-name phase. |
| 249.8 | Existing checkpoint payloads round-trip without destructive migration. |
| 249.9 | A process-restart integration/E2E case resumes the same dynamic workflow run at `research_company`, preserving activity cursor, plan/topology, artifacts, run ID, and completed phases. |
| 249.10 | The `create_workflow` guidance and generated-workflow smoke test cover a composite state whose executable phase is derived from persisted context. |
| 249.11 | Diagnostics identify saved and semantically resolved phase values without logging prompt, resume, or artifact contents. |
| 249.12 | Provider-history validity remains independent: the supplied run's provider history was already reported ready, and this phase fix does not weaken tool-history validation. |

## 9. Required tests

### Unit

- Reproduce `TailorContext(state=ACTIVITY, activity_cursor=<research_company>)`
  using a minimal typed fixture and dynamic topology.
- Verify default phase resolution does not mistake an unmatched coarse enum
  name for a phase.
- Verify an explicit resolver maps the cursor to the named active phase.
- Verify explicit current-phase mismatch, direct enum mismatch, terminal
  state, and missing topology behavior.
- Verify phase index, topology, fingerprint, run ID, conversation cursor, and
  owner checks remain independent.
- Verify failure finalization retains the correct named phase for a coarse
  state and does not rewrite user artifacts or completed phase records.

### Integration

- Persist a dynamic checkpoint at an activity boundary, close/reopen the
  conversation and checkpoint store, inspect, and rehydrate the same run.
- Exercise a cancellation during an activity and ensure recovery discovery
  reports the run as eligible when the checkpoint and context resolver agree.
- Create a genuine semantic mismatch and ensure it remains visible and
  non-destructive.

### End-to-end

- Simulate process restart for a dynamic/composite workflow paused in
  `research_company`; resume and assert the provider starts with that phase,
  not `intake`, `plan`, or another activity.
- Verify the supplied session pattern: provider history ready, workflow phase
  mismatch is fixed by phase-aware validation, and no new run is created.
- Verify TUI and `/workflow resume` display actionable recovery details for a
  genuine mismatch.

## 10. Rollout and migration

1. Add the phase-identity hook with a compatibility default that recognizes
   explicit `current_phase` and direct state names present in the active
   topology, but treats unmatched coarse state names as non-phase values.
2. Update `create_workflow` references/templates and tests for composite
   state-to-phase mapping.
3. Add a regression fixture matching the `tailor_resume` context semantics.
4. Re-run recovery inspection for existing checkpoints without modifying them.
5. For the affected run, verify the saved context and cursor using the new
   validator, then resume the same run ID. If the checkpoint has a real
   semantic conflict, leave it intact and report it; do not reset it
   automatically.

This change must not alter the journal, provider transcript, or workflow
artifacts during inspection. Recovery claims and provider-history guards remain
unchanged.

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Skipping a comparison could hide a real phase conflict | Skip only when no semantic phase is derivable; always validate checkpoint phase/index against active topology and require workflow resume reconciliation before provider use. |
| Plugin hook behavior differs between checkpoint save and recovery | Centralize phase projection and test it at boundary writes, failure finalization, inspect, and rehydrate. |
| Dynamic topology changes after the checkpoint | Keep topology version/fingerprint/profile validation fail-closed before phase resolution. |
| Generated workflows omit the resolver | Update authoring prompts, examples, annotation docs, and smoke tests; keep a safe compatibility default for existing workflow packages. |
| Existing checkpoint context is genuinely inconsistent | Preserve the checkpoint and provide a specific, non-destructive diagnostic; never rewrite it to the expected phase. |

## 12. Definition of done

The PRD is implemented when phase identity is derived through an explicit,
topology-aware context contract; the `ACTIVITY`/`research_company` false
mismatch is covered by unit, integration, and restart E2E tests; genuine
phase/topology conflicts still fail closed; the same run resumes at its saved
phase without replaying completed work; generated workflows follow the
contract; and all relevant workflow recovery tests and documentation pass.
