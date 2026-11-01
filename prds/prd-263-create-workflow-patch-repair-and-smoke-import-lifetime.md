---
title: "PRD-263: Patch-Oriented Repair and Lifetime-Correct Smoke Imports for create_workflow"
status: Proposed
version: 0.1.0
created: 2026-11-01
scope: "create_workflow rejected-package repair, package-scoped repair tools, repair provenance, and generated-package smoke import lifetime"
related_prds:
  - PRD-170 # Reliable workflow resume and durable recovery
  - PRD-173 # Recoverable workflow errors and failure checkpoints
  - PRD-258 # Lightweight, record-only transitions for generated workflows
  - PRD-262 # Canonical package authoring

tags:
  - workflows
  - create-workflow
  - authoring
  - repair
  - validation
  - smoke
  - imports
---

# PRD-263 — Patch-Oriented Repair and Lifetime-Correct Smoke Imports for `create_workflow`

## 1. Summary

Improve the `create_workflow` repair loop so a validation rejection is repaired
in the existing run-owned pending package, rather than by discarding the
candidate and asking the generator to rebuild it. The repair agent must receive
the previous report and manifest, be able to enumerate and read the package,
and have narrowly scoped operations to replace obsolete files and remove empty
obsolete directories. It must never be given access outside the one pending
package, and it must never be able to alter the runner-owned pending marker.

At the same time, make the generated-workflow smoke harness execute a package
while its temporary package namespace remains registered in `sys.modules`.
This makes valid function-local relative imports (for example,
`from .state import Context` inside a method) work during fake-runtime
execution. The namespace must be removed after the smoke run, including when
candidate code raises, so smoke checks neither retain stale generated modules
nor contaminate a later validation attempt.

The result is a bounded, auditable patch loop: a rejected candidate remains
pending and invisible to normal discovery, its validated starting bytes are
preserved for the repair agent, each repair produces a new manifest, and the
fake runtime observes Python package import semantics that match a real loaded
workflow.

## 2. Evidence-backed current state

`CreateWorkflowRunner._route_repair()` records a rejection reason and returns
`CreateWorkflowState.GENERATE`. The GENERATE phase retains the same
`AuthoringLease`, pending marker, canonical target, prior deterministic report,
and prior `PackageManifest`. `make_generation_writer_tools()` exposes only
`make_directory`, `read_file`, `write_file`, and `append_file`; it has no
bounded way to enumerate the live candidate, delete an obsolete source file,
or remove an empty now-unused directory.

The writer already enforces the important ownership boundary: every path is
normalized as a package-relative POSIX path, checked through the authoring
lease, rejected when it traverses, escapes the package, is a symlink, or names
the pending marker, and authorized through the current workspace policy.
`scan_package()` builds a bounded inventory and validates the presence of
`runner.py`; a successful `mark_generation_complete()` stores the new manifest
before VALIDATE.

`load_python_workflow_modules()` creates a temporary package name, imports
`runner.py` and local siblings under it, then removes every module under that
namespace in its `finally` block before returning the module objects. This is
correct for static inspection because plugin classes retain their definitions,
but it is too short-lived for the smoke harness. The harness receives the
returned plugin, builds its runner, and calls it after the parent package has
already been removed. A function-local relative import in the generated runner
then resolves against a missing temporary parent namespace and can fail even
though the same package would work when loaded normally.

The failure mode misdiagnoses valid generated package structure as an authoring
error. It also encourages a destructive rewrite of a candidate that could have
been repaired by changing one import or helper module.

## 3. Problem

A generated workflow is often multi-module. When validation reports one fault,
throwing away the candidate or only providing whole-file overwrite and append
operations makes repairs unnecessarily large and error-prone. The repair agent
needs to see the exact inventory and contents it is changing, remove stale
modules after reorganizing a package, and preserve good modules instead of
regenerating every file.

Retaining candidate files without an explicit repair contract is not enough.
The next GENERATE turn needs an unambiguous base manifest and deterministic
repair instructions. Out-of-band changes made between validation and repair
must not be silently attributed to the prior report. A partial repair may be
checkpointed, interrupted, and resumed; the runner must preserve the one
candidate transaction rather than creating another target or silently deleting
its contents.

The smoke importer has a separate correctness defect. A package import is not
complete merely because the top-level `runner.py` loaded: Python permits
relative imports at function execution time. Removing the temporary namespace
before the fake runtime starts breaks that language feature and makes smoke
validation less faithful than normal workflow loading.

## 4. Goals

1. Keep every rejected `create_workflow` candidate intact and pending for an
   in-place repair.
2. Give repair turns a complete bounded inventory, read access, atomic
   replacement, and minimally destructive cleanup operations inside only the
   run-owned package.
3. Bind a repair start to the exact prior validation manifest and report so a
   changed candidate cannot be repaired under stale evidence.
4. Record bounded repair provenance across checkpoints without retaining source
   text or unbounded reports.
5. Run generated-package fake runtime checks with a live, private temporary
   package namespace for the whole execution window.
6. Always remove that temporary namespace after smoke execution, success,
   failure, cancellation, or timeout.
7. Preserve pending-marker discovery exclusion, package/trust finalization,
   repair budgets, workspace authorization, and all existing deterministic
   validation rules.

## 5. Non-goals

- Allowing the repair agent to write outside the canonical pending package.
- Allowing any model-facing tool to delete the pending marker, authoring lock,
  replacement backup, workflow root, or another workflow package.
- Deleting a candidate automatically after validation rejection, repair-budget
  exhaustion, smoke failure, or process interruption.
- Changing the policy that all Python import roots are allowed. This PRD fixes
  package-relative import lifetime, not import allow-list policy.
- Sandboxing generated code. Candidate import and smoke execution remain
  in-process and must retain the existing trusted-source warning.
- Treating an agent approval as a replacement for deterministic validation or
  the bounded smoke harness.
- Adding a second draft directory, copy-and-publish route, or compatibility
  reset path.
- Turning package repair into unrestricted filesystem editing or a general
  project refactoring tool.

## 6. Product contract

### 6.1 Rejection preserves the candidate

A validation rejection sets the typed rejection reason, increments the bounded
repair counter, clears any approval/publication outcome, and routes to
GENERATE. It does **not** clear files, replace the candidate directory, reset
its package contents, remove its pending marker, or create a second authoring
lease.

Remove the package-clearing repair routine and every production or test call path
that uses it for workflow repair. A repository-wide symbol search after
implementation must find no remaining package-clearing repair API, export,
documentation entry, or workflow call.

The repair prompt must include:

- the concrete rejection reason;
- the complete previous deterministic validation and smoke report, subject to
  existing bounded report limits;
- the previous package manifest including sorted relative filenames, lengths,
  and digests; and
- an explicit instruction to inspect and patch the existing package first,
  preserving correct files unless a rewrite is required.

The initial generate prompt stays creation-oriented. The repair-specific prompt
must not claim the target is empty or tell the agent to rebuild it from scratch.

### 6.2 Repair-start identity check

Before the first repair provider turn, the runner verifies the existing lease,
pending marker, and workspace root. It re-scans the candidate and requires its
fingerprint to equal the manifest fingerprint recorded by the immediately
preceding validation attempt. If the package is missing, unsafe, or changed,
the runner fails closed without exposing writer tools or starting a repair
turn. Its diagnostic must identify that repair cannot safely begin because the
candidate no longer matches its validation evidence.

On success, record a bounded `RepairAttempt` provenance entry in the typed
context and checkpoint:

```text
repair cycle number
source package fingerprint
validation evidence ID
rejection-reason digest
repair started timestamp
```

The entry contains no source text, prompts, secrets, or model tool arguments.
After `mark_generation_complete()`, add the repaired package fingerprint and
completion timestamp to that same entry. The context keeps at most the current
configured repair budget's worth of entries; malformed, duplicate, impossible,
or over-limit history fails checkpoint restoration.

Once repair writing begins, the current bytes are expected to differ from the
source fingerprint. The final generation completion re-scans the complete
candidate, replaces `ctx.package_manifest` with the new manifest, and requires
an entirely new validation/smoke result before approval. A resumed partial
repair remains allowed while its pending lease is valid; it must preserve the
recorded repair-start provenance rather than re-checking the old fingerprint as
though no writes had occurred.

### 6.3 Bounded repair tool surface

Keep the existing package-scoped tools and add the following capabilities only
to a repair GENERATE turn (`ctx.rejection_reason` is present and the
repair-start identity check passed):

| Tool | Contract |
| --- | --- |
| `list_files()` | Returns the sorted current package inventory excluding the runner-owned marker: relative path, byte count, line count, and SHA-256. Its output uses the existing file-count and total-size bounds and contains no file body. |
| `delete_file(path)` | Deletes one existing regular file inside the package after lease and workspace authorization checks. It rejects `runner.py`, the pending marker, symlinks, nonexistent paths, directories, absolute paths, traversal, and every path outside the target. It fsyncs the parent directory after deletion. |
| `remove_empty_directory(path)` | Removes one existing empty descendant directory only. It rejects the package root, marker paths, symlinks, non-directories, non-empty directories, traversal, and paths outside the target. It fsyncs the parent directory after removal. |

`write_file()` remains the atomic whole-file replacement operation and
`append_file()` remains a continuation operation. They retain their existing
per-file size limits, lease verification, workspace authorization, symlink
rejection, and atomicity checks. `make_directory()` remains available as today.

These cleanup tools are not exposed for the initial generation turn. They do
not accept recursive deletion, glob patterns, a force flag, another root, a
model-selected package name, or a marker/lock/backup path. A repair remains in
Yolo only within the existing run-owned pending package; it must not gain a
generic workspace writer, shell, subprocess, or project-file deletion tool.

### 6.4 Package inventory and safe cleanup

`list_files()` and repair-start scanning share one canonical inventory function
rather than duplicating traversal logic. It walks only regular files under the
leased target, excludes exactly the pending marker, rejects symlinks and special
files, returns normalized sorted POSIX paths, and enforces the existing maximum
file count, per-file size, and total size. The `PackageManifest` remains the
only completion/validation/finalization authority; a listing is descriptive and
cannot be supplied back as a manifest or completion proof.

A repair may delete stale generated Python helpers only through `delete_file()`.
It may remove their parent directories only after those directories are empty.
The runner refuses to finalize if `runner.py` is missing, and all existing
validator checks still apply to every retained file. The model cannot delete
`runner.py`; it can atomically replace it with `write_file()`.

### 6.5 Repair checkpoint and resume behavior

A checkpoint in repair GENERATE persists the existing authoring lease, pending
marker identity, prior validation evidence, previous package manifest, repair
provenance, and whether repair writes have started. It does not serialize file
contents or the temporary smoke namespace.

On resume before the first repair write, the runner repeats the repair-start
fingerprint check. On resume after repair writes began, it verifies only the
lease, pending marker, safe package topology, and the persisted repair
provenance, then returns to the same repair prompt and tools. It must not delete
partial work or reacquire the target under a new identity. A later
`mark_generation_complete()` re-establishes the authoritative manifest before
VALIDATE.

The existing repair cap remains the only limit on rejections. A failed repair
turn or exhausted repair budget leaves the candidate pending and undiscoverable
for explicit resume or `/workflow reset <run-id>` recovery. Reset retains its
existing transactional behavior of restoring an approved original or discarding
a first-generation candidate; it is not a repair operation.

### 6.6 Smoke import namespace lifetime

Introduce a loader-owned context manager for temporary workflow-package
execution, for example:

```python
with loaded_python_workflow_modules(candidate) as modules:
    ... inspect plugin classes and execute the bounded smoke harness ...
```

It creates a unique temporary package name, registers the package and all local
submodules in `sys.modules`, yields the package/runner modules, and removes only
that invocation's namespace in `finally`. The name must include per-invocation
entropy so concurrent validation or smoke attempts for the same source path
cannot share a mutable `sys.modules` namespace.

The ordinary `load_python_workflow_modules()` API retains its current
inspection-only behavior: it may return module objects after temporary module
cleanup. The smoke harness alone holds the new context for the exact interval
from candidate import through plugin lookup, runner construction, fake phase
execution, checkpoint codec round trip, and injected-error check. It exits the
context before returning the `SmokeReport`.

The context manager must remove all modules under its unique namespace in a
`finally` block whether import, plugin lookup, fake runtime, codec execution,
error injection, cancellation, or a Python base exception fails. It must not
remove unrelated modules, retain generated modules after return, or change
normal discovery's pending-marker exclusion. Candidate code still executes
in-process with normal process permissions; the lifetime fix does not claim
isolation.

### 6.7 Diagnostics

Use explicit bounded diagnostics for these outcomes:

- `repair candidate differs from the validation manifest`;
- `repair package inventory is unsafe`;
- `repair cleanup refused: runner.py is protected`;
- `repair cleanup refused: directory is not empty`; and
- `generated package failed while its temporary smoke namespace was active`.

Reports should distinguish a genuine package import/runtime exception from a
loader-lifetime failure. Do not present “parent package not in sys.modules” as a
generated-workflow authoring defect once the lifetime contract is implemented.

## 7. Functional requirements

### FR-1 — No package-clear repair path

Delete the package-clearing repair API and all repair-time calls. Rejected
candidate bytes remain available until a repair writes a particular file, finalization commits a new
package, or explicit terminal recovery restores/discards the transaction.

### FR-2 — Verified patch start

Before repair tools or provider turns are exposed, compare the live candidate's
bounded manifest fingerprint with the one recorded at validation. Mismatch,
unsafe topology, missing marker, or ownership mismatch fails without mutation.

### FR-3 — Repair-only cleanup tools

Expose `list_files`, `delete_file`, and `remove_empty_directory` only after a
verified rejection. Implement their path checks through the same lease-aware
component used by read/write operations. No operation may recursively remove a
directory or change a marker, lock, backup, or package root.

### FR-4 — Repair provenance

Persist bounded repair-start and repair-completion identities in typed context
and checkpoint codecs. Final validation evidence must bind to the newly scanned
candidate, never the repair-start manifest.

### FR-5 — Accurate repair prompts

Make initial generation and repair prompts distinct. The repair prompt includes
the prior report and manifest, directs inspection before mutation, and says that
correct candidate files remain available. Its reminder supports partial repair
resume without claiming a blank target.

### FR-6 — Scoped namespace lifetime

Provide a context-managed temporary workflow import API. Smoke execution keeps
the exact candidate package namespace registered through all fake-runtime calls,
then reliably removes the unique namespace.

### FR-7 — Relative import regression coverage

Smoke must accept a generated package whose `runner.py` performs a local
relative import inside `run()`, `resume()`, a phase method, or a helper invoked
by the fake provider, provided the package otherwise satisfies the validator
and smoke contracts.

### FR-8 — Existing boundaries remain authoritative

Keep the pending-marker discovery exclusion, workspace policy authorization,
manifest size/inventory limits, validation/smoke requirement, approval gate,
exact-byte trust recording, and committed-package finalization behavior intact.

## 8. Acceptance criteria

1. A rejected candidate retains every file and the pending marker when the next
   GENERATE repair begins; no package-clear operation occurs.
2. Repository search finds no package-clearing repair API definition, export,
   call, test, or LLM documentation entry.
3. The first repair prompt contains the prior report and manifest, and the agent
   can read every retained candidate file before deciding which files to change.
4. A package modified outside the run between validation and first repair turn
   fails closed before any repair provider call or writer tool is exposed.
5. `list_files()` returns a sorted bounded inventory for a pending repair
   candidate and neither includes nor reveals mutable pending-marker contents.
6. `delete_file()` can remove a stale helper but refuses `runner.py`, the marker,
   traversal, symlinks, directories, paths outside the candidate, and files from
   another workflow.
7. `remove_empty_directory()` removes only an empty descendant and refuses the
   package root, a non-empty directory, symlinks, and every protected path.
8. A repaired candidate receives a new manifest and cannot be approved with the
   prior report or evidence ID.
9. An interrupted partial repair resumes the same pending package and repair
   provenance without clearing files or creating a new lease.
10. A failed or repair-budget-exhausted run leaves the candidate pending and
    undiscoverable; explicit reset remains the only discard/restore path.
11. A smoke-tested package with a function-local `from .helper import value`
    succeeds when the helper is valid and reports the helper's genuine exception
    when it raises.
12. The temporary smoke namespace is absent from `sys.modules` after a passing
    smoke, an import error, fake-runtime exception, cancellation, and injected
    error path.
13. Concurrent smoke attempts for the same candidate use different temporary
    namespace names and cannot remove one another's imports.
14. Pending packages remain excluded from normal workflow discovery before,
    during, and after repair until finalization removes the marker.

## 9. Test plan

- Unit tests for rejection-to-GENERATE preserving candidate bytes, marker, lease,
  manifest, validation report, and repair prompt contents.
- Unit tests that inject an out-of-band candidate change before repair and assert
  no provider turn, writer tools, or mutation occurs.
- Generation-tool tests for sorted list output, path normalization, write
  authorization, protected runner/marker deletion refusal, regular-file deletion,
  empty-directory removal, non-empty-directory refusal, symlink rejection, and
  parent-directory fsync behavior.
- State and checkpoint round trips before repair, after repair-start verification,
  during partial repair, after repaired completion, and at repair-budget
  exhaustion; include malformed provenance and impossible state combinations.
- End-to-end fake-provider tests that create a multi-module candidate, reject it,
  list/read retained content, replace one module, delete one stale helper, remove
  its empty directory, complete, validate, approve, and verify publication of
  only the repaired inventory.
- End-to-end interruption/resume tests proving partial repair content survives
  and the resumed agent sees the same report, manifest, and files.
- Loader context-manager tests for success, top-level import failure,
  function-local relative import success, nested helper relative import,
  fake-runtime exception, cancellation, and precise namespace cleanup.
- Concurrent smoke tests proving unique namespace isolation and no surviving
  `_agenthicc_workflow_...` modules for the completed invocation.
- Regression tests preserving unrestricted import roots, pending discovery
  exclusion, replacement backup restoration, manifest fingerprint checks,
  validation approval binding, and exact-byte trust recording.

## 10. Rollout

1. Delete the repair-time reset symbol/path and add regression coverage that a
   rejected candidate persists.
2. Add repair-start manifest comparison and typed, bounded repair provenance
   before exposing additional writer operations.
3. Implement the shared inventory primitive and repair-only list/delete/empty
   directory tools with authorization and filesystem safety tests.
4. Update repair prompts, reminders, checkpoint codec, and user documentation to
   describe patching retained files rather than rebuilding an empty package.
5. Add the context-managed temporary package importer and migrate only the smoke
   harness to its full-lifetime API.
6. Add relative-import, cleanup, interruption/resume, concurrent-smoke, and
   normal-discovery regression coverage.
7. Run the full format, lint, type, unit, integration, E2E, docs, LLM-symbol,
   and build gates. Search the repository for the removed reset symbol and old
   empty-package repair instructions before declaring the migration complete.

## 11. Security and operational considerations

- The pending marker is a visibility/ownership guard, not a code-execution
  sandbox. Imported candidate code still has the validating process's ordinary
  permissions.
- Repair cleanup is deliberately narrower than a project filesystem tool:
  one owned pending package, individual regular files, and empty descendants
  only. There is no recursive removal or wildcard deletion.
- Verify ownership and the base manifest before the first repair turn so an
  external process cannot cause a model to patch unknown bytes under a trusted
  report.
- Keep inventory, provenance, diagnostics, reports, and tool output bounded and
  JSON-serializable. Never place source bodies, secrets, or pending-marker
  payloads into checkpoint provenance.
- `sys.modules` is process-global. The temporary importer must use unique names,
  limit cleanup to its own namespace, and clean in `finally`; it must not claim
  thread/process isolation.
- The implementation must preserve normal loader cleanup outside the smoke
  execution scope so no generated workflow module leaks into future reloads or
  validation attempts.
