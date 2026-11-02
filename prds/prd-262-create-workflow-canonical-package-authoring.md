---
title: "PRD-262: Author create_workflow Packages at Their Canonical Project Path"
status: Proposed
version: 0.1.0
created: 2026-11-01
scope: "create_workflow generation target, path-scoped writes, package discovery visibility, validation, and resume"
related_prds:
  - PRD-250 # Code quality evaluation and extension trust hardening
  - PRD-258 # Lightweight, record-only transitions for generated workflows
tags:
  - workflows
  - create-workflow
  - authoring
  - paths
  - discovery
  - trust
---

# PRD-262 — Author `create_workflow` Packages at Their Canonical Project Path

## 1. Summary

Make the `create_workflow` generation agent create its package directly at the
canonical project path derived from the approved workflow name:

```text
./.agenthicc/workflows/<workflow-name>/
```

The package must be written there during GENERATE, not first created under
`.agenthicc/workflows/.drafts/` and copied or renamed after validation. The
runner must derive the path from the authorized workspace root and approved
name, scope generation writes to that package, and record completion against
that exact path without accepting a model-selected destination.

Direct authoring at the discoverable path must not make partial or rejected code
loadable. Add a run-owned pending marker and explicit collision/recovery rules;
the normal workflow registry must ignore a package while it is being authored.
Only after deterministic validation, the validation-phase approval, and a
rechecked package fingerprint may the runner finalize trust and make the package
visible to normal discovery.

## 2. Evidence-backed current state

`CreateWorkflowRunner._generate()` derives its runtime target with
`_draft_path()`, which calls `build_draft_path()`. `draft.py` places each run at
`.agenthicc/workflows/.drafts/<run_id>/<workflow_name>/`. The generation system
prompt supplies that path as `[TARGET PATH]`, labels the canonical destination
`[PUBLISHED PATH — DO NOT WRITE HERE]`, and tells the agent to create the exact
run-owned draft directory. Its chunked-writing instructions repeat that it must
never write directly into the published workflow directory.

The phase tool contract reinforces the same requirement:
`mark_generation_complete(summary, path)` tells the agent to write to the draft
path, accepts a model-supplied path, and the runner stages a reported package
into the run-owned draft before validation. On validation approval,
`publish_draft()` atomically replaces the canonical package, then the runner
records trust and updates `generated_path` to the published path.

There is a contradictory instruction in `definition.py`: its GENERATE
`system_prompt_override` tells the agent to write to
`.agenthicc/workflows/<name>/`. In practice, the runner's phase system prompt,
explicit target-path block, write-step instructions, and transition-tool
docstring all direct generation to `.drafts`; the definition prompt alone does
not control the actual output path.

This staging behavior is intentional in the current design. The registry scans
immediate workflow-directory children, skips names beginning with `.`, and
requires a `runner.py` package; `.drafts` is hidden from discovery. Run-owned
staging also isolates simultaneous runs, keeps incomplete and rejected
candidates out of the active package path, permits manifest/fingerprint checks,
and lets publication replace a whole package atomically while preserving a
recoverable backup. Consequently, a successful run should end at the canonical
path, but an in-progress, rejected, interrupted, or failed run may leave its
candidate under `.drafts` for resume or inspection.

## 3. Problem

The agent is not choosing `.drafts` accidentally. The effective runner prompt and
transition contract explicitly require it, despite the contradictory phase
definition. If the product requirement is that the agent author directly under
the canonical directory, changing only the phase definition or adding another
prompt sentence will not work: the runner, target derivation, write boundary,
completion tool, validation manifest, commit path, discovery behavior, and tests
must agree on that path.

A naive prompt-only change would be unsafe. The canonical directory is scanned
by normal discovery. A package may be incomplete while the agent writes it, an
existing trusted workflow may be replaced, and another run or `/workflows reload`
may observe the target concurrently. The implementation must meet the requested
path while preserving the current guarantees against partial loading, implicit
overwrite, cross-run interference, stale validation, and trust before approval.

## 4. Goals

1. Make the canonical project package directory the actual generation destination.
2. Ensure every generation prompt, target-path field, writer, completion signal,
   validator, checkpoint, and user-facing path identifies the same directory.
3. Prevent the normal workflow registry from importing or registering a package
   while a create_workflow run owns it.
4. Prevent model-selected paths, writes outside the one package, and overlapping
   authoring runs from changing the target.
5. Preserve collision approval, whole-package integrity, validation-fingerprint
   binding, trust timing, and resume behavior.
6. Remove the draft-copy/publish path as a second generation route; do not add a
   compatibility fallback that continues generating into `.drafts`.

## 5. Non-goals

- Changing the canonical project workflow directory or workflow naming rules.
- Changing the package format, `runner.py` entry-point contract, design approval,
  validation checks, smoke harness scope, or workflow execution interface.
- Making failed or unvalidated candidate code executable.
- Changing the general extension trust model or automatically trusting a
  package before the existing validation approval boundary.
- Requiring generated files to be assembled in a separate candidate directory
  before being copied into the canonical directory; that would not satisfy this
  PRD's generation-path requirement.
- Migrating or adopting old abandoned `.drafts` packages. Existing hidden draft
  trees remain undiscovered; the new implementation must neither import nor
  silently publish them.

## 6. Product contract

### 6.1 Canonical destination

The runner derives one normalized `workflow_name` during approved design and one
canonical target from the session's authorized workspace root:

```text
<workspace-root>/.agenthicc/workflows/<workflow_name>/
```

The user-facing and agent-facing relative path is
`.agenthicc/workflows/<workflow_name>/`. Do not derive it from process CWD when
the session workspace has a different authorized primary root. Reject invalid
names, path separators, traversal, symlinked components, and any target that
resolves outside the authorized workspace.

The agent creates `runner.py` and all package siblings directly inside this
canonical directory. It must not create the package in `.drafts`, a temporary
workflow directory, a user-global directory, or an agent-selected alternate
path and then copy it into place.

### 6.2 Scoped generation and completion

The runner supplies the canonical target as structured phase context and uses a
writer bound to that exact directory. It may expose the minimum directory, file
write/append, and read operations needed to create and inspect the package; each
operation must enforce workspace authorization, reject symlinks and traversal,
and reject paths outside the current target. The ordinary session tool bundle
must not let the generation agent bypass this write scope.

Replace the path-bearing completion handoff with a runner-bound completion
signal (for example, `mark_generation_complete(summary)`). The runner already
knows the only permitted target; a model-supplied path is not authority. The
signal succeeds only after the runner verifies that the expected target contains
a complete package with `runner.py`, a bounded regular-file inventory, and no
symlinked entries. If a path argument is retained for provider-schema reasons,
it must exactly equal the runner-derived canonical target and must not affect
which path is scanned.

Keep the chunked-writing guidance, but describe `path/runner.py` as the canonical
target and explicitly require:

- `.agenthicc/workflows/<workflow_name>/runner.py` as the entry point;
- sibling helpers and tools inside that same package;
- no `.drafts` path and no second package copy; and
- the exact canonical path in completion, validation, checkpoint, and final
  output.

The GENERATE phase definition, runner system prompt and reminder, first-turn
message, transition-tool description, state/context documentation, and
create_workflow authoring docs must use this same contract. Remove contradictory
published-path-versus-draft instructions.

### 6.3 Pending ownership and discovery visibility

Before exposing a writer, the runner atomically creates a pending-generation
marker inside the canonical target. The marker binds the workspace-relative
package path, workflow name, run ID, and lifecycle state. It is created before
any generated Python file can be written and updated only by runner-owned code.

The normal workflow registry must skip a package with a valid pending marker,
regardless of whether `runner.py` exists or any file currently has a matching
trust record. The marker check happens before import or registration. Malformed,
symlinked, or foreign-run markers fail closed and do not authorize generation or
loading. Once finalization commits, the runner removes the marker atomically; a
failure to remove it leaves the package undiscovered rather than partially
published.

Validation and the generated-workflow smoke harness need a separate explicit
path for the exact run-owned pending package. That path must retain the existing
static checks and bounded fake-harness contract, must not weaken normal registry
checks, and must not make a pending package visible to `/workflows reload`.

### 6.4 Collision, concurrency, and recovery

Before generation:

- If the target does not exist, create it under the pending marker and claim it
  for this run.
- If the target contains a pending marker for another run, do not write, reset,
  validate as this run, publish, or remove it. Return a clear conflict and an
  actionable resume/cleanup instruction.
- If a completed workflow already occupies the target, require an explicit user
  decision before replacement. The original package must remain recoverable and
  must not be modified before that decision.

For approved replacement, preserve the original package and trust records in a
run-bound recovery transaction before placing the pending marker and writing the
new package. If generation, validation, approval, or finalization fails, keep the
candidate undiscoverable and provide an explicit resume, restore, or discard path.
A rollback must restore the exact original bytes and its existing digest-bound
trust remains valid only for those restored bytes. Never auto-delete a package
owned by another run or preserve trust for changed bytes.

A checkpoint must persist the canonical target, workspace identity, run-owned
marker identity, replacement decision/backup identity when applicable, current
package manifest/fingerprint, and validation evidence identity. Resume verifies
all identities and current bytes before continuing. It cannot claim another
run's marker, reset its attempt state, or treat a changed package as already
validated.

### 6.5 Validation and finalization

The validator reads and validates the package at the canonical target, not a
copied draft. It binds its report to the exact canonical path and a digest of the
whole package manifest. Any changed, added, removed, or replaced file invalidates
the report and requires validation again.

Do not remove the pending marker or add workflow trust when generation completes
or when deterministic checks alone pass. Finalization requires both a passing,
current report and the existing validation-phase approval. Before the marker is
removed, re-scan the package and verify the same manifest fingerprint. Record
trust only for the exact validated Python-file bytes, then remove the marker so
normal discovery can load the package. If any step fails, retain the marker and
fail closed.

The final summary reports `.agenthicc/workflows/<workflow_name>/` as the package
location and describes whether the package was committed, remains pending, was
restored, or was discarded. It must not tell the user to copy the package from
`.drafts`.

## 7. Functional requirements

### FR-1 — One canonical generation path

Replace `_draft_path()` as the GENERATE destination with the runner-derived
canonical target. Remove prompt text, tool descriptions, staging adapters, and
fallback branches that tell the model to generate under `.drafts` or move a
model-reported path into a draft. The completion signal does not accept an
arbitrary artifact path.

### FR-2 — Exact-path writer

Provide an operation-boundary-enforced writer for the run-owned canonical
package. Reject writes outside the package, alternate filenames/roots,
absolute paths, `..`, symlinked components, and path changes after authorization.
Use atomic per-file replacement where supported; keep the pending marker until
all file writes, validation, approval, and finalization are complete.

### FR-3 — Discovery exclusion

Update project and user workflow directory scanning to skip packages with an
active create_workflow pending marker before attempting trust checks or imports.
Test that a reload during a partially written package performs no import and
registers no workflow. Do not implement exclusion by changing the canonical
package name or by putting the generated package in a different directory.

### FR-4 — Collision and transaction handling

Add canonical-target preflight, explicit replacement approval, run ownership,
serialized access to a workflow name, and recoverable replacement/rollback.
Simultaneous runs for the same name cannot write to the same package. A stale
marker is never silently discarded or adopted; recovery must verify its run and
checkpoint identity and require the appropriate user action.

### FR-5 — Canonical validation and smoke checks

Run the existing deterministic validator and smoke contract against the exact
canonical package path under an explicit pending-candidate validation boundary.
The normal loader must remain unable to discover/import a pending candidate.
Recheck the complete package fingerprint between validation approval and
finalization; changed bytes cannot be trusted or committed under the old report.

### FR-6 — Checkpoint and terminal outcomes

Persist marker, target, backup, manifest, and validation identities as typed,
JSON-safe context. Validate cross-field invariants on restore. A missing or
mismatched marker/manifest is a failure, not a reason to default to the draft
path, adopt another run, or mark the package complete. Resume continues the
same canonical target and run identity.

### FR-7 — Remove the superseded draft-generation route

Delete create_workflow generation code whose sole purpose is staging the
current run's candidate under `.drafts` and promoting it to the canonical
package. Remove or redesign draft-only manifest APIs and tests rather than
keeping two generation paths. Retain only recovery/backup mechanisms needed for
approved replacement; these do not become alternate candidate-generation
locations. Old `.drafts` contents remain ignored and are not auto-migrated.

### FR-8 — Documentation

Update the GENERATE phase reference, workflow authoring guide, failure/resume
instructions, and LLM documentation when public symbols or behavior change. State
that agents build directly at `.agenthicc/workflows/<workflow-name>/`, explain
pending visibility, collision approval, and how to resume or recover. Remove
instructions that present `.drafts` as the expected generation destination.

## 8. Acceptance criteria

1. The generation prompt and first-turn request name exactly
   `.agenthicc/workflows/<approved_name>/`; neither contains a `.drafts` target
   or tells the agent not to write to the canonical path.
2. The model's directory/file-write calls create `runner.py` and sibling modules
   under the canonical target from the first write. No stage-then-copy behavior
   occurs.
3. A model-supplied path cannot redirect completion, validation, or checkpoint
   identity; a path-bearing completion signal, if retained, rejects every path
   except the exact canonical target.
4. Attempts to write outside the canonical package, through a symlink, or into
   another workflow are rejected at the tool boundary.
5. A project/user workflow reload during partial generation, repair, or
   validation does not import or register the pending package, including when an
   existing trusted workflow is being replaced.
6. A same-name concurrent run cannot alter or adopt another run's target; a
   stale or malformed marker fails closed with an actionable recovery message.
7. Existing-target replacement requires explicit approval and can restore the
   original package byte-for-byte after a failed or abandoned replacement.
8. A rejected candidate remains at the canonical target under its pending marker
   for repair/resume and is never loaded or trusted while pending.
9. Editing, adding, removing, or replacing any package file after validation
   approval invalidates the report and prevents final trust/visibility.
10. Only a successfully validated and approved, fingerprint-matching package
    becomes visible to the normal registry; the marker is removed only after
    exact-file trust is recorded.
11. Checkpoint/resume preserves the same target and marker identity; it cannot
    fall back to `.drafts`, reset ownership, or accept stale validation evidence.
12. Successful final output names the canonical package path. Failed and
    interrupted output accurately reports pending, restored, or discarded state.
13. No generation code, prompt, test, or current documentation retains a
    `.drafts` compatibility generation branch; abandoned legacy draft directories
    remain invisible.

## 9. Test plan

- Prompt/contract tests asserting all generation instructions, reminders,
  transition schemas, and dynamic target blocks use the canonical directory and
  do not instruct the model to write to `.drafts`.
- Path-scoped writer tests for exact target success and traversal, absolute path,
  alternate target, symlink, and cross-workflow write rejection.
- Registry tests proving pending-marker packages are skipped before import, even
  when a matching trust entry exists, and become discoverable only after a
  successful marker removal and exact-byte trust.
- End-to-end fake-provider tests that write a package directly to the canonical
  path, complete, validate, approve, and assert there was no staging/copy step.
- Interrupted-write and reload tests proving partial packages cannot be loaded.
- Collision, explicit replacement, same-name concurrent-run, stale-marker,
  rollback, and byte-for-byte original-restoration tests.
- Fingerprint tests for changed, added, removed, symlinked, and replaced package
  files between generation, validation, approval, checkpoint, and finalization.
- Checkpoint round trips and malformed-state tests at GENERATE, VALIDATE,
  pending failure, approved replacement, and committed states.
- Regression tests for repair loops, run-owned cleanup, validation/smoke failure,
  trust-store integrity, and the final canonical path reported to the user.

## 10. Rollout

1. Define the canonical target, pending marker, transaction ownership, and
   checkpoint schema.
2. Add discovery exclusion and direct pending-package validation support before
   changing generation writes.
3. Add the path-scoped writer, collision preflight, replacement backup, and
   rollback behavior.
4. Change all generation prompts and transition contracts to write directly to
   the canonical target; remove model-supplied path authority.
5. Replace draft staging/publication with manifest verification and
   marker-protected finalization; update resume, docs, and tests.
6. Verify no active create_workflow instruction or generation branch still
   directs agents to `.drafts`.

## 11. Security and operational considerations

- The canonical package path is discoverable. A path-only prompt is not a safety
  boundary; pending-marker enforcement must be checked by the normal registry
  before import.
- The pending marker protects discovery visibility, not against arbitrary code
  execution by validation. Preserve the existing static prechecks, bounded smoke
  contract, workspace authorization, and validation error handling; do not
  broaden validation execution or trust as a side effect of moving the path.
- Digest-bound trust applies to exact Python bytes. Marker removal, replacement
  rollback, and trust updates must not create a window in which changed,
  unvalidated content is loadable.
- Do not place secrets in generated source, marker metadata, backup names, or
  checkpoint payloads. Keep user-visible failure details and manifests bounded.
