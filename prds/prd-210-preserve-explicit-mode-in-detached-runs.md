---
title: "PRD-210: Preserve explicit modes in detached background runs"
status: Implemented
version: 1.0.0
date: 2026-09-30
repository: jymchng/agenthicc
related_prds:
  - PRD-141  # Durable background sessions
  - PRD-204  # Goal runner and detached orchestration
  - PRD-205  # Detached goal worker lifecycle
tags:
  - cli
  - modes
  - background
  - goal-runs
  - configuration
---

# PRD-210 — Preserve explicit modes in detached background runs

## 1. Summary

`agenthicc --mode YOLO --goal "..." --detach` accepts the mode flag but starts
the detached session in `Safe`. This PRD defines an end-to-end fix so explicit
CLI mode selection survives the goal-run manager, background request
serialization, worker reconstruction, session initialization, and subsequent
resume/retry operations.

The mode must be applied by the canonical `ModeManager` during normal session
construction. Detached execution must not create a separate mode-selection
implementation or weaken capability/approval policy.

## 2. User-visible problem

The CLI accepts `--mode MODE` and stores it in `CLIContext.mode_name`. Attached
`--goal` execution forwards that value to `_run_tui_session`, which passes it
to `_build_session_context`. Detached execution takes another path:

```text
--mode YOLO --goal ... --detach
  → CLIContext.mode_name = "YOLO"
  → run_goal_cli()
  → GoalRunManager.start_detached()
  → BackgroundSupervisor.submit()
  → persisted background request
  → background worker
  → _build_session_context()
```

The value is dropped between `run_goal_cli()` and `start_detached()`. The
detached request models do not currently carry a mode, and the worker calls
`_build_session_context()` without `mode_name`. Session construction therefore
uses its new-session default, `Safe`. This explains why the command succeeds
but the worker does not start in YOLO.

The attached path is evidence of the intended behavior: it forwards
`ctx.mode_name` directly into session construction. The detached path must
preserve the same explicit-selection semantics rather than silently falling
back to Safe.

| Boundary | Current behavior |
|---|---|
| `cli/parser.py` → `CLIContext` | Correctly parses `--mode` into `mode_name`. |
| `runs/cli.py::run_goal_cli` → `GoalRunManager.start_detached` | Forwards config and permission options, but omits `ctx.mode_name`. |
| `runs/manager.py::start_detached` → `BackgroundSupervisor.submit` | Creates a detached request without a mode field. |
| `background/supervisor.py::BackgroundRequest` | Persists workflow, intent, config overrides, and run metadata, but no mode. |
| `background/worker.py::WorkerRequest` and worker startup | Decodes no mode and invokes `_build_session_context()` without `mode_name`. |
| `runners/tui_session.py::_build_session_context_impl` | Starts in Safe, then applies an explicit `mode_name`; on resume without one, it loads the persisted session mode if present. |

`goal_flow` also has a workflow-specific Yolo override for its implementation
phase. The CLI mode is the session's initial/default mode; existing explicit
phase overrides remain authoritative for the phases where they are declared.

## 3. Goals

1. Preserve an explicitly selected mode across every handoff involved in a
   detached CLI run.
2. Apply the requested mode through the same `ModeManager` and session-mode
   persistence used by attached sessions.
3. Preserve explicit mode selection across worker retries and recovery, even
   if the worker exits before completing its first session initialization.
4. Keep old persisted background requests readable and retain existing
   behavior when a request has no explicit mode.
5. Make the effective mode observable enough to diagnose a mismatch.
6. Keep mode selection separate from security bypass flags and approval
   decisions.

## 4. Non-goals

- Changing what Safe, Plan, or Yolo modes mean.
- Making Yolo the default mode.
- Changing the behavior of `--dangerously-skip-permissions`.
- Adding mode selection to workflow phase overrides or subagent policy.
- Replacing the canonical session-mode persistence mechanism.
- Changing provider, model, or workflow selection semantics.

## 5. Functional requirements

### FR-1 — Carry the explicit mode through detached goal creation

`run_goal_cli()` must pass `CLIContext.mode_name` to
`GoalRunManager.start_detached()`. The manager must pass it into the
`BackgroundSupervisor.submit()` request without translating it into a config
override or dropping its original spelling before canonical validation.

### FR-2 — Persist mode in the worker request

The persisted background request and worker request must each support an
optional `mode_name` field. Serialization and decoding must preserve a string
value exactly enough for the existing mode registry to resolve it. The field
must be optional so requests written by earlier Agenthicc versions remain
valid.

### FR-3 — Apply mode using standard session construction

The worker must pass `request.mode_name` into `_build_session_context()`.
`_build_session_context` remains responsible for validating the requested
mode, resolving aliases/canonical spelling according to the existing mode
registry, and persisting the selected mode for future session resumes.

An unknown explicit mode must produce a clear startup failure naming the
requested mode and available selectable modes. It must not silently fall back
to Safe.

### FR-4 — Preserve mode through retry and recovery

The explicit mode is durable invocation state until session construction
successfully persists the canonical selected mode. If a worker fails before
that point, resuming or retrying the background session must retain the
requested mode. Once the session mode is persisted, future resumes without an
explicit override must use that persisted mode.

An explicit mode supplied on a later resume invocation takes precedence over
the persisted session mode, matching existing `_build_session_context`
behavior.

### FR-5 — Cover other CLI background entry points

Any CLI path that accepts the global `--mode` option and submits a background
worker, including `run --background`, must forward the explicit mode to the
same worker request contract. Existing non-CLI worker dispatch paths may omit
the field and retain their current inherited/default behavior.

### FR-6 — Keep security semantics unchanged

Selecting Yolo is not equivalent to setting
`--dangerously-skip-permissions`. The fix must not infer, enable, or persist a
permission bypass from the mode name. Existing capability checks, workspace
boundaries, approvals, and explicit security flags remain authoritative.

### FR-7 — Report effective mode for diagnosis

Detached-start output or the durable session detail view must make the
effective selected mode inspectable, either by showing the canonical mode or
by directing the user to an existing session-details field that reports it.
Do not expose credentials or unrelated configuration while adding this
diagnostic.

## 6. Data model and compatibility

The preferred design is to treat `mode_name` as optional launch-request
metadata and let the existing session mode file remain the canonical persisted
session choice:

```text
CLI --mode
    ↓
BackgroundRequest.mode_name
    ↓ serialize / decode (missing field → None)
WorkerRequest.mode_name
    ↓
_build_session_context(mode_name=...)
    ↓ validate + canonicalize + persist selected mode
session mode record
```

If the current retry path reconstructs a request rather than reusing the
original request file, it must carry forward the original optional mode until
the session mode record exists. Do not add a second, conflicting source of
truth for an already-initialized session.

Compatibility requirements:

- Old request JSON without `mode_name` decodes as `None`.
- A missing mode keeps the existing resume behavior: use the session's
  persisted mode when available, otherwise use the standard Safe default.
- Existing CLI invocations without `--mode` remain unchanged.
- Explicit valid mode aliases continue to be resolved by the existing mode
   registry and persisted in canonical form.

## 7. Acceptance criteria

1. `agenthicc --mode Yolo --goal "..." --detach` starts its worker with the
   canonical Yolo mode selected, not Safe.
2. A test observes the mode at worker session construction, rather than only
   asserting that `mode_name` appears in an intermediate request object.
3. `agenthicc --mode Yolo --goal "..."` (attached) continues to start in Yolo.
4. `agenthicc --mode Yolo run --background ...` (or the repository's
   equivalent command ordering) preserves the mode when that CLI path is
   supported.
5. A detached session with no explicit mode still uses its persisted mode on
   resume, or Safe when no persisted value exists.
6. A worker that fails before initial session-mode persistence can be retried
   without losing the original explicit mode.
7. An explicit mode on resume overrides the persisted mode and is itself
   persisted canonically.
8. A request written before this change, with no `mode_name`, loads and runs
   without a migration error.
9. An invalid explicit mode produces a clear, durable failure and is never
   silently replaced by Safe.
10. Selecting Yolo does not set `dangerously_skip_permissions`; capability,
    approval, and workspace-policy regression tests remain green.
11. Detached run/session diagnostics show enough mode information to confirm
    which mode was actually selected.
12. The first `goal_flow` phase observes the selected session mode, while
    declared per-phase mode overrides continue to take precedence in their
    respective phases.

## 8. Test plan

### Unit tests

- `CLIContext.mode_name` is forwarded by `run_goal_cli()` to
  `GoalRunManager.start_detached()`.
- The manager forwards the value through `BackgroundSupervisor.submit()`.
- `BackgroundRequest` and `WorkerRequest` round-trip explicit modes.
- Legacy request dictionaries without `mode_name` decode to `None`.
- Worker session construction passes the decoded mode to
  `_build_session_context()`.
- Mode validation errors are preserved as worker failures rather than
  converted to Safe.
- A mode selection does not alter the dangerous-permissions flag.

### Integration tests

- Start a detached goal with a fake worker launcher, read the persisted request,
  and verify the selected mode survives every serialization boundary.
- Run worker startup against a controlled mode registry and verify the
  effective mode and session-mode persistence.
- Simulate a pre-initialization worker failure followed by retry; verify the
  explicit mode is retained.
- Resume after successful initialization without a CLI mode and verify the
  stored canonical mode is selected.

### End-to-end tests

- Exercise the CLI `--mode Yolo --goal ... --detach` path through the
  background worker/session builder using deterministic fake model and process
  boundaries; assert the first workflow phase observes Yolo.
- Exercise the normal attached `--goal` path as a regression control.
- Exercise a no-mode detached start and an invalid-mode detached start.
- Assert the resulting goal run, background record, and session mode remain
  consistent after completion and resume.

## 9. Observability and failure handling

The worker's durable status/error and the session's selected mode must make it
possible to distinguish:

- mode was not requested;
- mode was requested and applied;
- mode was unknown or unavailable in the current registry;
- worker failed before applying the mode.

Do not log secrets or the full configuration. An invalid mode must fail closed
with a useful diagnostic rather than run under a different permission posture
than the user requested.

## 10. Implementation sequence

1. Add the optional request field and backward-compatible codecs.
2. Thread `mode_name` through CLI → goal manager → supervisor.
3. Thread it through worker request decoding → `CLIContext` → session
   construction.
4. Preserve it through retry/recovery until the canonical session mode has
   been persisted.
5. Add observable effective-mode reporting.
6. Add unit, integration, and end-to-end regression coverage for both detached
   and attached paths.

## 11. Definition of done

The PRD is complete when detached goal execution honors the exact explicit
mode selected on the CLI; the selected mode survives process boundaries,
retries, and resumes; legacy requests remain compatible; and security policy
is unchanged. Tests must verify the actual mode observed by worker session
construction, not merely request serialization.

## 12. Implementation record

Implemented in the runtime and CLI as follows:

- Added an optional `mode_name` to the durable background request and worker
  request codecs. Older records without the field continue to decode.
- Threaded the explicit mode through detached goal startup, direct
  `run --background`, goal-run resume, and background job resume/retry.
- Passed the requested mode into `CLIContext` and `_build_session_context`,
  leaving canonical validation and persistence to the existing `ModeManager`.
- On resume/retry, reuse the original request mode only if session metadata
  does not yet contain a canonical mode. Explicit mode on the resume command
  takes precedence; otherwise already-persisted session mode remains
  authoritative.
- Persisted the canonical effective mode on the background session record
  after session construction and exposed it in the session details page and
  `jobs status` output.
- Preserved workflow-level phase overrides and did not infer or enable
  `--dangerously-skip-permissions`.

Regression coverage verifies request round trips, detached CLI propagation,
worker session-builder arguments and effective-mode persistence, recovery
precedence, session details rendering, invalid-mode failure, and compatibility
with the existing attached-goal path.

Verification performed:

- Focused background/goal unit, integration, and E2E checks: **77 passed**;
  the final focused rerun after additional error-path coverage: **23 passed**.
- `ruff check` on the changed source and tests: passed. Formatting checks on
  all changed Python files: passed. The repository-wide format check still
  reports eight untouched files that need formatting.
- Mypy on the eight changed source modules: passed. The repository-wide mypy
  run reports existing diagnostics in untouched `process_lease.py`,
  `name_that_ui.py`, and `cli/commands/session_service.py`.
- Type-safety audit and `mkdocs build --strict`: passed.
- Full `pytest tests/ -q`: **4,032 passed, 16 skipped**. The PTY screenshot
  lifecycle test was made deterministic by waiting for a short idle interval
  before asserting asynchronously captured output.
