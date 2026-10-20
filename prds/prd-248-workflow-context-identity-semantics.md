---
title: "PRD-248: Make Workflow Recovery Context-Identity Checks Schema-Aware"
status: Implemented
version: 1.1.0
date: 2026-10-20
repository: jymchng/agenthicc
priority: P1
related_prds: [prd-154, prd-170, prd-174, prd-188, prd-213]
tags: [workflow-recovery, checkpoints, context-codec, create-workflow, identity]
---

# PRD-248 — Make Workflow Recovery Context-Identity Checks Schema-Aware

## 1. Executive summary

Resuming a saved `create_workflow` run can fail before the workflow runner is
invoked:

```text
Cannot resume the latest workflow run:
ValueError: workflow recovery is unavailable for run '…':
context_identity_mismatch: saved workflow context has a different workflow name
```

The recovery validator compares `getattr(restored_context, "workflow_name")`
to the checkpoint's owning workflow name for every context type. That assumes
every field named `workflow_name` identifies the workflow that owns the run.
This is false for `CreateWorkflowContext`: its `workflow_name` means the slug
of the custom workflow currently being authored. For example, a checkpoint
owned by `create_workflow` may correctly contain `workflow_name="tailor_resume"`.
Recovery mistakes that target slug for the current workflow identity and
rejects an otherwise valid checkpoint.

The source contracts establish the defect independently: the authored slug and
run-owner name have different meanings, yet recovery compared them by field
name. A session ID supplied during implementation did not corroborate this
specific incident: its readable event projection showed a `goal_flow` run at
`verify_goal`, with tool-history recovery followed by cancellation, rather than
`create_workflow` at `generate`. The private session journal was not readable
under the environment's file permissions and was not accessed by bypassing
them. The regression case is therefore constructed from the repository's
documented context contracts, not claimed as a replay of that session.

This PRD requires recovery identity validation to use explicit, schema-aware
ownership metadata rather than interpreting arbitrary domain fields by name.
It must continue rejecting genuine run/workflow identity mismatches while
allowing the built-in authoring context—and custom contexts with unrelated
`workflow_name` fields—to resume without altering their data.

## 2. Incident and impact

The reported incident is a run paused in the `generate` phase of
`create_workflow`. The workflow is responsible for authoring a different
workflow package, and its generated draft artifacts are stored in a path
associated with the authored target slug. After continuation/resume, recovery reports
`context_identity_mismatch` and marks the run unavailable instead of passing
the restored context to `CreateWorkflowRunner.resume()`.

This is a local checkpoint-validation failure, not a provider error, model
response, corrupt artifact, or proof that the workflow's generated files were
lost. The identity check fails before the workflow can continue. It can strand
valid authoring work and make the user believe the checkpoint is corrupt.

## 3. Evidence and root cause

### 3.1 Current validation behavior

In `src/agenthicc/runners/workflow_recovery.py`,
`WorkflowRecoveryCoordinator._validate_recovery()`:

1. loads the workflow implementation identified by
   `checkpoint.workflow_name`;
2. restores the typed context via `context_from_payload()`; and
3. reads `restored.workflow_name` with `getattr()` and rejects any non-empty
   value different from `checkpoint.workflow_name`.

The checkpoint's `workflow_name` is the workflow that owns and executes the
run. The restored context's same-named attribute is not guaranteed to have
that meaning.

### 3.2 Conflicting field semantics

The relevant built-in contracts are:

| Value | Meaning |
|---|---|
| `WorkflowCheckpoint.workflow_name` | Owning workflow whose runner, plugin fingerprint, and phase topology govern this run. |
| `WorkflowContext.workflow_name` | Owning workflow identity in the generic declarative context. |
| `CreateWorkflowContext.workflow_name` | Slug of the new workflow being authored; it may intentionally differ from `create_workflow`. |
| Custom context field named `workflow_name` | Plugin-defined domain data; its identity meaning is unspecified unless the plugin explicitly declares one. |

`CreateWorkflowContext` documents its field as the lower-snake-case slug of
the workflow being authored. The built-in context codec serializes and
restores this field unchanged. `CreateWorkflowRunner` assigns it from the
approved design and uses it to construct the draft path. The field must not be
rewritten to `create_workflow` merely to satisfy recovery validation; doing so
would corrupt the authored workflow identity and could redirect later writes.

### 3.3 Session-specific evidence and limits

The session ID supplied during implementation does not match the incident
described by this PRD. Its accessible session-service event projection records
`goal_flow` at `verify_goal`, a recovery event with status `repaired`, and a
subsequent cancellation/pause. It does not record a `create_workflow` run or
the reported context-name diagnostic. The separate private conversation
journal was permission-restricted and was not read or modified. Accordingly,
that session is not evidence for this defect. The checkpoint payload from the
reported incident also remains unavailable for direct inspection. The PRD
therefore treats the source-level semantic collision as the verified root
cause and uses typed-context unit, disk-backed integration, and process-restart
tests to exercise the exact supported state.

### 3.4 Why existing checks are not enough

The checkpoint already carries canonical ownership data and recovery validates
the owning workflow's registry entry, plugin fingerprint, topology, run ID,
phase, conversation identity, and other applicable metadata. Those checks
should remain. The defect is the additional duck-typed comparison against any
restored `workflow_name` attribute. Attribute spelling is not an identity
contract, especially across built-in and plugin-defined context schemas.

## 4. Goals

1. Resume a valid `create_workflow` run even when the authored target slug
   differs from `create_workflow`.
2. Make context ownership validation explicit and typed/schema-aware.
3. Preserve genuine mismatch detection for contexts that explicitly declare
   an owning-workflow identity.
4. Preserve existing context values, phase state, artifacts, draft paths, and
   run identity exactly across checkpoint round-trip and recovery.
5. Keep plugin fingerprint, topology, conversation, run-ID, phase, and claim
   validation fail-closed.
6. Provide a clear diagnostic for real identity conflicts without describing
   valid domain data as corrupt.

## 5. Non-goals

- Renaming `CreateWorkflowContext.workflow_name` or changing its authored-slug
  semantics.
- Rewriting an authored slug to match the owning workflow during migration.
- Disabling workflow identity, run-ID, plugin-fingerprint, or topology checks.
- Automatically discarding, resetting, or replacing the saved run.
- Changing the create-workflow authoring flow, draft directory layout, or
  generated workflow package.
- Broad changes to session ownership or the checkpoint store format unless an
  explicit identity envelope is proven necessary.

## 6. Proposed solution

### 6.1 Establish one canonical owner identity

`WorkflowCheckpoint.workflow_name` remains the canonical identity of the
workflow that owns the run. Recovery MUST select the implementation from that
field and continue validating its registry presence, fingerprint, and
checkpoint topology as it does today.

Context identity MUST NOT be inferred by reading an arbitrary attribute named
`workflow_name`. Instead:

- Built-in generic `WorkflowContext.workflow_name` may be validated as the
  owning workflow because that type's contract explicitly defines it so.
- `CreateWorkflowContext.workflow_name` MUST be treated as authored-workflow
  domain data, never as the current run's owner identity.
- Other built-in contexts without an owner-identity field remain governed by
  checkpoint identity and their typed codec.
- Custom contexts may opt into context-owner validation through an explicit,
  documented checkpoint-context identity contract (for example, a dedicated
  codec-provided `owner_workflow_name` value). A field name alone is not
  sufficient evidence.

The implementation may choose a typed helper/protocol or a versioned context
envelope, but it MUST avoid ambiguous overloading and preserve compatibility
with existing checkpoint payloads. Prefer the narrowest compatible change;
do not add a second source of truth for workflow ownership.

The implementation adds the optional
`WorkflowPlugin.checkpoint_context_owner_workflow_name(context)` hook. Its
default returns the owner only for generic `WorkflowContext`; a custom plugin
may override it to declare its own context's owner identity. Returning `None`
means that the checkpoint owner is authoritative and the typed context has no
separate owner identity.

### 6.2 Preserve all independent recovery guards

The correction MUST NOT weaken checks for:

- checkpoint conversation/session identity;
- context `run_id` where the context contract provides it;
- workflow registry membership and plugin fingerprint;
- topology version/fingerprint/profile/phase names;
- current phase and phase index consistency;
- context restoration/codec validity;
- ownership claims and stale-checkpoint reload; or
- journal cursor and tool-call recovery invariants.

Only the unsupported inference that every `context.workflow_name` is the owner
identity is removed. When an explicit identity contract reports a genuine
mismatch, recovery remains unavailable with a precise error code and keeps the
checkpoint and authored artifacts intact for inspection.

### 6.3 Compatibility and recovery behavior

Existing checkpoints containing `CreateWorkflowContext` MUST be interpreted
using that type's established field meaning; they must not require rewriting.
Old generic `WorkflowContext` checkpoints continue to validate their owner
identity. A custom codec with no explicit identity declaration is restored
under the workflow selected by the checkpoint and is not rejected solely
because one of its domain fields is named `workflow_name`.

If a checkpoint cannot be safely restored for another reason, report that
specific reason. Do not silently start a new `create_workflow` run, select a
different workflow, discard the checkpoint, or move the authored draft.

## 7. Data flow

### Current failing flow

```text
checkpoint.workflow_name = "create_workflow"     # run owner
        │
        ├── checkpoint context restored as CreateWorkflowContext
        │
        └── context.workflow_name = "tailor_resume" # authored target slug
                         │
                         ▼
recovery compares both strings by attribute name
                         │
                         ▼
context_identity_mismatch
                         │
                         ▼
latest workflow run is reported unavailable
```

### Required flow

```text
checkpoint.workflow_name selects and identifies the owning workflow
        │
        ├── validate workflow registration/fingerprint/topology
        ├── restore CreateWorkflowContext with authored slug unchanged
        ├── validate run ID, phase, conversation and explicit identity contracts
        └── claim and resume the same run at its saved phase
```

## 8. Acceptance criteria

| ID | Criterion |
|---|---|
| 248.1 | A `CreateWorkflowContext` whose authored `workflow_name` differs from the owning `create_workflow` workflow is considered identity-valid when all canonical checkpoint and context invariants pass. |
| 248.2 | Recovery preserves the authored workflow slug byte-for-byte and resumes the same run without rewriting the field or draft path. |
| 248.3 | A generic `WorkflowContext.workflow_name` that explicitly differs from the checkpoint owner is still rejected with `context_identity_mismatch`. |
| 248.4 | A custom context's incidental/domain `workflow_name` field is not treated as owner identity unless its declared codec contract explicitly says it is. |
| 248.5 | An explicitly declared custom owner identity that genuinely conflicts with `WorkflowCheckpoint.workflow_name` is rejected and reported precisely. |
| 248.6 | Context `run_id`, conversation identity, plugin fingerprint, workflow topology, phase, phase index, and ownership-claim checks remain enforced. |
| 248.7 | Existing `CreateWorkflowContext` checkpoints can be read without migration or destructive rewrite. |
| 248.8 | A process-restart E2E test authors a differently named workflow, pauses in `generate`, resumes the same run, and verifies that the authored slug, existing draft, phase, and run ID are retained. |
| 248.9 | Failed recovery remains non-destructive: the system does not silently start a replacement workflow, discard the checkpoint, or alter generated artifacts. |
| 248.10 | User-facing diagnostics distinguish a true owner-identity mismatch from an unrelated context-domain field. |

## 9. Test plan

### Unit tests

- Recover `CreateWorkflowContext(workflow_name="tailor_resume")` under the
  `create_workflow` owner and assert it remains unchanged.
- Retain a negative test proving an owner mismatch in generic
  `WorkflowContext` is rejected.
- Add a custom context with a business field named `workflow_name` and prove
  it does not trigger owner validation by convention alone.
- Add a custom context with explicit owner metadata and test both matching and
  mismatching identities.
- Verify genuine run-ID, conversation-ID, plugin-fingerprint, topology,
  current-phase, phase-index, and claim mismatches still fail closed.
- Round-trip a partially completed `CreateWorkflowContext` with design,
  artifacts, `workflow_name`, generated path, state `GENERATE`, and resumable
  phase metadata; assert every field is preserved.

### Integration tests

- Persist and reload a checkpoint for `create_workflow` whose target slug is
  different from the owner; inspect and rehydrate it through
  `WorkflowRecoveryCoordinator` using the registered workflow and the original
  `SessionConversation`.
- Assert no checkpoint revision or authored draft is mutated just by inspect
  or failed validation.
- Verify a genuinely mismatched generic context remains visible as invalid and
  cannot be resumed.

### End-to-end test

Simulate a process restart after `create_workflow` reaches `generate` for an
authored workflow with a distinct slug. Rebuild the session, discover the
latest checkpoint, resume it, and verify the same run continues at `generate`
with its existing draft and authored slug. Assert no new run starts and no
design phase is repeated.

## 10. Rollout and documentation

- Implement the recovery comparison and context identity contract without
  changing checkpoint ownership fields or existing authored slugs.
- Keep old checkpoint payloads readable; add migration only if a versioned
  envelope is necessary, and make that migration additive and non-destructive.
- Update the workflow recovery guide and checkpoint/context codec reference
  with the distinction between run-owner identity and workflow-domain data.
- Add this PRD to `prds/README.md`; when implemented, record exact test and
  release-gate evidence there.

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Removing duck-typed validation could hide real owner mismatches | Validate only through explicit identity contracts; retain checkpoint owner, registry, fingerprint, topology, run-ID, and phase checks. |
| A versioned identity envelope could make older checkpoints unreadable | Preserve current checkpoint schema where possible; otherwise implement additive backward-compatible decoding and test legacy fixtures. |
| Authored slug is accidentally changed to appease recovery | Treat it as immutable domain data and assert exact round-trip/path preservation. |
| Custom plugins relied on implicit attribute checking | Document opt-in identity metadata and provide a clear migration path; do not infer identity from field spelling. |
| Session evidence cannot expose the original checkpoint bytes | Use the reproducible typed-context regression test as primary verification; retain the evidence limitation in this PRD. |

## 12. Definition of done

The PRD is implemented when schema-aware context identity validation allows
valid `create_workflow` authoring checkpoints to resume with their authored
slug unchanged, genuine explicitly declared identity conflicts still fail
closed, process-restart recovery resumes the same run at the saved phase, all
other checkpoint guards remain effective, and the unit/integration/E2E tests
and relevant workflow-recovery documentation are complete.

## 13. Implementation and verification record

Implemented on 2026-10-20. Recovery validates run IDs when the restored schema
provides one and validates workflow-owner identity only through the explicit
plugin hook. The built-in create-workflow slug remains unchanged across
checkpoint restoration and runner resume. Plugin identity-hook failures or
malformed return values produce a stable diagnostic-only recovery result.

Verification completed:

- Unit: `tests/unit/test_workflow_recovery.py` — 25 passed.
- Integration: `tests/integration/test_workflow_recovery_integration.py` — 2 passed.
- E2E: `tests/e2e/test_workflow_resume_recovery_e2e.py` — 4 passed.
- Combined targeted run — 31 passed.
- Full Python 3.13 suite: `uv run pytest tests/ -q` — 4,438 passed,
  16 skipped.
- Full Python 3.12 suite using the isolated Nox interpreter and activated
  environment: 4,435 passed, 18 skipped before the final malformed-owner
  metadata regression was added; the final 31-test targeted recovery suite
  also passes under Python 3.12. `pyproject.toml` now explicitly sets
  pytest `pythonpath = ["src"]`, matching the repository's documented source
  test setup. The Nox wrapper itself could not sync into the shared `.venv`
  because a root-owned installed-file blocked replacement; running from the
  prepared isolated Nox venv avoided changing that shared environment.
- The Nox unit, integration, and E2E sessions each passed on Python 3.13:
  3,945 passed / 14 skipped; 315 passed; 172 passed / 2 skipped.
- `uv run mkdocs build --strict`, the type-safety baseline audit, the
  `llms_check`, and mypy on both changed source modules passed.
- Repository-wide `ruff check` passed. The repository-wide format check still
  reports eight unrelated, clean-worktree files that would be reformatted;
  only the five touched Python files pass the focused format check.
- Repository-wide mypy remains blocked by 240 errors in ten unrelated existing
  files; no errors were reported in the two changed source modules.

The session ID supplied during implementation was inspected only through its
readable event projection; it showed a separate `goal_flow` cancellation and
did not reproduce the `create_workflow` identity mismatch. No private session
data or saved checkpoint was altered.
