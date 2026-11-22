---
title: "WFC-06: create_workflow — an agent activates it, completes it, and the new workflow is trusted, reloaded and usable"
status: Proposed
version: 2.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P1
related_prds: [WFC-00, WFC-01, WFC-02, WFC-05, WFC-11, WFC-14, prd-147, prd-154, prd-258]
tags: [workflows, authoring, reload, create_workflow]
---

# WFC-06 — `create_workflow`: activate, complete, trust, reload, use

## 1. Summary

Make `create_workflow` a first-class **call frame**: an agent enters it with
`enter_workflow`, it runs `design → generate → validate → summarize`, and when it
finishes the session — with no approval step — commits and trusts the validated
package (WFC-11), reloads the workflow registry (WFC-05), pops the frame
(WFC-01) and tells the agent the new workflow is available, so the agent can
immediately `enter_workflow` it.

## 2. Problem (current source)

| Fact | Where |
|---|---|
| `create_workflow` is a built-in with a lease-based authoring transaction: the package is generated **at its canonical path** `.agenthicc/workflows/<name>/` while a pending marker (`.agenthicc-pending.json`) makes the loader skip it. | `workflows/create_workflow/authoring.py` (`canonical_workflow_path`, `prepare_authoring`, `PENDING_MARKER_NAME`); `workflows/loader.py:122-140` |
| Generation is path-scoped to the lease's package. | `workflows/create_workflow/generation_tools.py:232` |
| Validation is deterministic and the runner overrides an agent `approve_workflow` when it failed; approval is bound to a package fingerprint and evidence id. | `phase_tools.py:288-345`; `runner.py` `validation_evidence` |
| **Commit already trusts the validated bytes with no human decision.** `finalize_authoring` records `trust.trust(<file>, kind="workflow")` for every `.py` file of the validated package, then removes the marker. The only human approval in the run is for *replacing an existing* workflow. This series keeps that behaviour and generalises it to tools and skills (WFC-11). | `authoring.py:1187-1216`; `runner.py:1071-1090` |
| The trust call runs **inside the runner**, so it cannot serve a worker that built the package in a worktree (trust is bound to a path; the worktree is temporary), and it runs before — not in order with — any reload. | `runner.py:1595-1660` |
| Nothing reloads the registry when the run completes. The operator must reload by hand (`_reload_workflows`, `tui_session.py:4230`), and the session is left selecting `create_workflow` itself (WFC-01 F1). | `tui_session.py` |
| Generated workflows declare no `invocation` metadata (WFC-02), so even once loaded an agent could not discover or enter them. | — |

## 3. Goals

1. `create_workflow` is `agent_callable` and returns a typed `WorkflowReturn`
   whose artifact list names the new (or updated) workflow package.
2. The new package is committed and trusted automatically, by the session's
   publish step (WFC-11 §5.2), not by the runner — so the same step serves
   foreground runs and subagent workers, and runs in the order commit → reload →
   deliver.
3. The session reloads workflows (WFC-05) **before** delivering the return, so
   the parent's resume turn can name a workflow that is already loaded.
4. Generated workflows carry a complete `WorkflowInvocation` and are valid
   children for WFC-01/02/04 from the start.
5. The selector is never left on `create_workflow`.

## 4. Non-goals

- Package layout, the design/generate/validate/summarize machine, and the
  workflow-specific validation rules (unchanged).
- The generic scoping, verdict and repair rules, owned by WFC-14.
- Any approval step for trust (removed by the WFC-11 decision).
- Letting a workflow modify a built-in.

## 5. Design

### 5.1 Entry

`CreateWorkflow.invocation = WorkflowInvocation(agent_callable=True,
subagent_eligible=True, publishes=("workflow",), when_to_use="Create or update
a reusable workflow package for this project", returns="The new workflow's
name, phases, and whether it was published and loaded")`. It is not
`interactive`. The only human prompt it can raise is the existing
*Replace Existing Workflow* approval, an overwrite guard for a package the
manifest does not record as authored (WFC-11 §5.4).

### 5.2 Keep the lease; move commit and trust to the session

The existing lease already gives what staging needs: the half-written package is
at its canonical path, invisible to every loader (pending marker), scoped
writers, a fingerprint, backup and restore. It is **kept**. What changes is
*who* calls the commit, and when:

| Step | Today | After this PRD |
|---|---|---|
| Validation approved, fingerprint bound | runner | runner (unchanged), using WFC-14's verdict and budget |
| `finalize_authoring` (trust every `.py`, recheck fingerprint, remove marker) | **runner**, immediately | **session publish step** (WFC-11 §5.2), immediately after its gate under the default `auto` policy |
| Validation failed, run failed or abandoned | `restore_authoring(lease)` | unchanged |
| Policy `stage_only` | n/a | the run completes with the artifact `staged`; the lease and marker stay; `/authored approve <run-id>` runs `finalize_authoring`, `/authored reject <run-id>` runs `restore_authoring` |

`finalize_authoring` is unchanged internally — it still rechecks the validated
fingerprint, trusts the exact bytes, and removes the marker atomically — but its
only caller becomes `store.commit` in the session's publish step. The runner's
finalize phase now ends at "validation approved, fingerprint recorded, return
built". A package still under its marker is reported by the loader as
`pending_marker` (WFC-05), never as loaded.

Moving the call, rather than leaving it in the runner, is what makes three
things true at once: one call site for trust (WFC-11 A9), correct trust for a
package a worker built in a worktree (the store imports the files into the main
tree and trusts them at the main-tree path), and a deterministic order — commit,
reload, deliver.

### 5.3 Return payload

The `summarize` phase produces:

```python
WorkflowReturn(
    status="completed",
    summary="Created workflow 'release_notes' (3 phases)…",
    outputs={"workflow": "release_notes", "phases": "draft,review,publish"},
    artifacts=(PublishedArtifact(kind="workflow", name="release_notes",
                                 files=(…), digest="…", state="staged", …),),
)
```

### 5.4 Pop handler

On `stack.complete` for a return carrying artifacts, the session:

1. Runs the WFC-11 §5.2 publish step. Under `auto`: gate → `store.commit`
   (`finalize_authoring`) → `ReloadService.reload({"workflows"},
   reason="workflow_return")` → provenance → notice. Under `stage_only` it stops
   after the gate with the artifact `staged`.
2. Rewrites the artifact states and appends `ReloadReport.summary()` and, for
   each loaded workflow, its `when_to_use` and callability.
3. Delivers (WFC-01 §5.4).

The synthetic parent turn therefore reads, for example:
*"create_workflow completed. Published, trusted and loaded: release_notes
(agent-callable). Call enter_workflow('release_notes', …) to use it."* A
`stage_only` run says it is staged and names `/authored approve <run-id>`; it
does not claim availability.

### 5.5 Generated workflows are well-formed children

`create_workflow/validation.py` and the design/generate templates require every
generated `WorkflowPlugin` to declare:

- `invocation.when_to_use` (non-empty, ≤ 200 characters) and `returns`;
- `agent_callable` — generated **false** unless the user's intent explicitly asks
  for agent use; honoured as soon as the package is trusted;
- `interactive` truthfully, derived from whether any phase registers a
  human-gate tool;
- a `build_params` that rejects unknown keys.

A package whose `invocation` is missing or contradicts its phases fails
validation (reported through WFC-14 findings).

### 5.6 Contract adoption

`create_workflow` adopts the WFC-14 modules and deletes its private copies of
the writer scoping, the repair counter (`repair_cycles`/`_max_repair_cycles`
become a persisted `RepairBudget`) and the verdict override. Workflow-specific
validation rules stay where they are.

### 5.7 Name collisions and updates

Publishing refuses a name that matches a built-in workflow (`name_conflict`).
Replacing a package the manifest records as authored is an update: the previous
version is kept for rollback (WFC-11 §5.6) and no prompt is raised. Replacing a
package the manifest does not know keeps the existing *Replace Existing
Workflow* approval before any file changes. A package *currently on the stack*
keeps its materialised class; the next entry uses the new class, and resuming an
old-fingerprint checkpoint follows the existing `PLUGIN_INCOMPATIBLE` path.

## 6. Requirements

- **FR-1** After a completed run under the default policy, `registry.get(name)`
  returns the new class **before** the parent's resume turn starts, with no
  prompt shown.
- **FR-2** A run whose validation fails, or is abandoned, leaves the registry
  unchanged and no trust record for the candidate; the return says why.
- **FR-3** `trust.trust` is not called anywhere in `workflows/create_workflow/`
  except inside `finalize_authoring`, and `finalize_authoring` is called only
  from `store.commit` (a test asserts both).
- **FR-4** The stack never retains `create_workflow` after it completes, fails or
  is abandoned.
- **FR-5** Running as a subagent (WFC-04) produces the package inside the
  worker's worktree; the session imports, commits and trusts it at the
  main-tree path (WFC-11 §5.1).
- **FR-6** `_restore_create_workflow_authoring` (`tui_session.py:33`) and its
  checkpoint twin continue to guarantee that abandoning a run restores or
  discards the candidate.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | A scripted agent calls `enter_workflow("create_workflow", intent)`; on completion — with no prompt — the next resume turn lists the new workflow as trusted, loaded and callable. |
| A2 | The agent then calls `enter_workflow("<new name>")` successfully in the same session with no manual reload. |
| A3 | Policy `stage_only`: after the run the package is present under its pending marker, is absent from `registry.names()`, and has **no** trust record; `/authored approve <run-id>` then trusts and loads it. |
| A4 | Policy `stage_only` with `/authored reject <run-id>`: no new workflow, no trust record, `restore_authoring` leaves the tree as it was. |
| A5 | A run whose deterministic validation has an error commits and trusts nothing and the return says so. |
| A6 | A generated package without `invocation.when_to_use` fails validation. |
| A7 | Publishing a package named `code_plan` fails with `name_conflict`. |
| A8 | After completion `stack.depth == 0` and the base workflow is unchanged. |
| A9 | Aborting mid-generate leaves no candidate residue and no loaded workflow. |
| A10 | The FR-3 static test passes. |
| A11 | A package built by a subagent worker in a worktree is trusted at the main-tree path and loads; no trust entry names a worktree path. |

## 8. Test plan

- Unit: invocation validation rules, name-conflict, return construction, the
  FR-3 static scan.
- Integration: real registry + `ReloadService` + `ExtensionTrust` override;
  default policy, `stage_only` approve and reject, validation failure, abandon,
  worker import (`tests/integration/test_create_workflow_return.py`).
- The existing authoring lease, scoping and validation tests keep running;
  those that now test the shared contract move to WFC-14's tests.
- E2E: WFC-13 S3.

## 9. Documentation and cleanup

`docs/guides/workflows.md`, `docs/guides/custom-workflows-and-config.md`,
`skills/authoring-workflows/`, `llms-full.txt`. The runner-side call to
`finalize_authoring` is removed. The prompt-only bootstrap skill
`create-workflow` (`skills/bootstrap.py:310`) is deleted in the same change
because the workflow supersedes it (PRD-147 already treats the workflow as the
canonical entry); an unmodified seeded copy is removed by the bootstrap's
marker mechanism and a user-edited copy is left and reported.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Auto-trusting agent-generated code. | It is today's `create_workflow` behaviour, kept by decision and now given the WFC-11 §5.4 controls (digest-bound trust, runner-owned verdict, call-time capability gate, provenance and rollback, opt-in `stage_only`). |
| Moving the trust call changes where a failure surfaces. | The session's publish step is all-or-nothing with restore (WFC-11 §5.2) and reports the failure in the return. |
| Generated `agent_callable=True` workflows widen the agent's reach. | Default false; every entry still passes WFC-02 checks and the Safe-mode approval; the workflow's own phases run under their ceilings. |
