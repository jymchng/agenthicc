---
title: "PRD-212: Make detached goal mode authoritative before execution"
status: Implemented
version: 1.1.0
date: 2026-09-30
repository: jymchng/agenthicc
related_prds:
  - PRD-155  # Canonical Safe, Plan, and Yolo modes
  - PRD-204  # Goal runs and detached orchestration
  - PRD-205  # Detached goal worker lifecycle
  - PRD-210  # Preserve explicit modes in detached background runs
tags:
  - cli
  - modes
  - background
  - goal-runs
  - startup
---

# PRD-212 — Make detached goal mode authoritative before execution

## 1. Summary

Close the remaining startup-observability and verification gap for explicit
modes on detached goal runs, especially:

```bash
agenthicc --mode YOLO --goal "..." --detach
```

PRD-210 fixed the original dropped-argument defect: current source forwards the
requested mode through the CLI, goal-run manager, serialized background
request, worker request, and `_build_session_context()`. The worker applies it
through `ModeManager` before calling the workflow runner.

However, the durable `BackgroundSession` is created before the worker starts
and does not receive the requested mode. Detached startup returns as soon as
the process is launched, while the worker only writes the effective mode after
session construction. During that asynchronous interval, the session record
has no mode even though the request has one. Existing regression tests verify
request forwarding and use a fake session builder; they do not prove that the
production session builder's selected mode is the mode observed by the first
agent turn.

The system must distinguish **requested**, **pending**, **applied**, and
**failed** mode state; it must never present an uninitialized mode as the
effective mode, and no agent/provider/tool execution may begin until the
requested mode has been validated and applied.

## 2. Findings from current-source investigation

The current code path is:

```text
--mode YOLO
  → CLIContext.mode_name
  → run_goal_cli()
  → GoalRunManager.start_detached(mode_name=...)
  → BackgroundSupervisor.submit(mode_name=...)
  → BackgroundRequest JSON (mode_name included)
  → WorkerRequest (mode_name decoded)
  → _build_session_context(mode_name=...)
  → ModeManager.set_by_name("YOLO") → canonical "Yolo"
  → workflow execution
```

Mode lookup is case-insensitive, so uppercase `YOLO` is not itself the cause.
The explicit-mode branch in `_build_session_context_impl()` runs before the
resume-mode branch, so a persisted Safe value should not override an explicit
Yolo request during session construction. The implementation phase's declared
`mode_override="Yolo"` is a separate workflow policy; other goal-flow phases
inherit the selected session mode unless they declare an override.

The concrete residual gap is at the durable-record boundary:

1. `BackgroundSupervisor.submit()` serializes the explicit mode into the
   private worker request.
2. It creates the `BackgroundSession` without initializing its mode metadata.
3. It launches the child and returns without waiting for session construction.
4. Only after `_build_session_context()` returns does the worker update the
   session record with `session.mode_manager.active_name`.

Consequently, an immediate `jobs status`, `agenthicc agents`, or run projection
can show an empty/default mode while startup is still in progress. That is a
real reporting/initialization race. By itself it does not prove the worker
executed under Safe: current source passes the explicit value into the
production session builder. The production first-turn behavior remains
insufficiently asserted, so a separate runtime mismatch must be treated as an
unverified regression rather than assumed away.

A second, concrete resume-path hazard was found while implementing the durable
state contract. Before building the context, the worker registers missing
session metadata with the normal Safe default. The old resume-mode selector
treated any existing metadata mode as successfully applied. If startup failed
before the explicit request was resolved, a retry could therefore discard the
still-unconsumed YOLO request and resume from that placeholder Safe value. The
resume selector must consult the attempt-scoped application state first; a
pending/failed requested value takes precedence over placeholder metadata.

## 3. User problem

The user explicitly selects YOLO and starts a detached goal. The process is
accepted, but early session inspection does not reliably show that selection;
and if the first agent turn is in fact using Safe, current tests do not
faithfully reproduce the production path to identify where it changes.

This is particularly confusing because the detached command is asynchronous:
the parent prints success before the child has finished constructing the
session. A missing mode in that first persisted projection looks indistinguish-
able from the session having defaulted to Safe.

## 4. Goals

1. Keep the explicit CLI mode durable and visible from the first persisted
   detached-session state.
2. Make effective-mode application a fail-closed startup gate before the
   first provider request, tool call, workflow phase, or subagent spawn.
3. Verify the actual effective mode at the first agent-turn boundary using
   production session construction, not only mocked argument forwarding.
4. Preserve mode state across worker restart, resume, and retry without letting
   stale attempt state overwrite the current attempt.
5. Distinguish a requested mode from a canonical effective mode in APIs,
   diagnostics, and the TUI.
6. Preserve existing mode policy, phase overrides, workspace boundaries,
   approvals, and compatibility with request records written before PRD-210.

## 5. Non-goals

- Change the meanings or defaults of Safe, Plan, or Yolo.
- Make Yolo the default mode.
- Equate Yolo with `--dangerously-skip-permissions`.
- Remove intentional workflow phase mode overrides.
- Delay every detached start until all workflow registries, MCP servers, or
  other nonessential startup services are ready.
- Add a new mode registry or a parallel detached-only mode implementation.

## 6. Product requirements

### FR-1 — Persist requested mode at session creation

When `BackgroundSupervisor.submit()` receives an explicit `mode_name`, the
new durable session record MUST retain that invocation choice before process
launch. The record MUST distinguish it from the effective canonical mode if
validation has not yet succeeded. Do not label an unvalidated raw value as an
effective mode.

Recommended representation:

```text
requested_mode_name: "YOLO"
mode_application_status: "pending"
mode_name: ""                 # effective canonical mode, not known yet
```

For a request without an explicit mode, the mode state MUST clearly mean
“pending normal session resolution,” not “Yolo” and not an asserted effective
Safe mode. Legacy records lacking these fields MUST remain readable.

### FR-2 — Apply and attest mode before agent work

The worker MUST validate the requested mode through the normal `ModeManager`
and persist the canonical effective mode (for example, `Yolo`) before any
provider request, agent turn, workflow phase execution, or worker-agent spawn.

The first turn MUST observe the same active mode as the successfully
initialized session, except where a declared, scoped phase override is
currently active. If mode application fails, the worker MUST persist a
startup failure and MUST NOT start agent work.

### FR-3 — Report startup mode state honestly

Background-session and goal-run projections MUST expose enough bounded state
to distinguish:

- explicit mode requested, application pending;
- mode successfully applied, with canonical effective name;
- mode absent, normal default/persisted resolution pending or complete;
- explicit mode invalid/unavailable, startup failed.

The CLI's accepted response may remain prompt and asynchronous, but it MUST
not report `Safe` or `Yolo` as effective until the worker has attested that
mode. `jobs status`, `agenthicc agents`, and run inspection MUST not collapse
pending into an effective default.

### FR-4 — Preserve attempt ownership

Mode-application state is attempt-scoped. A delayed event from an earlier
worker attempt MUST NOT change the requested/effective mode or application
status of a newer attempt. Resume and retry MUST retain an unconsumed explicit
mode until a successful application is durably recorded. Once applied, the
canonical persisted session mode remains authoritative unless a later
invocation explicitly supplies another mode.

### FR-5 — Canonicalization and invalid modes

Case variants and existing aliases MUST resolve through the standard registry
(`YOLO` → `Yolo`, for example). An unknown explicit mode MUST fail clearly,
retain the requested value for diagnosis, and never fall back silently to
Safe. Older request records with no mode field MUST continue to use the
existing persisted-mode-or-Safe behavior.

### FR-6 — Preserve security semantics

Mode application MUST not infer or enable `dangerously_skip_permissions`.
Yolo retains its existing capability semantics and remains subject to OS,
container, and configured workspace policy. Safe approvals, Plan hard blocks,
and explicit per-phase overrides remain unchanged.

### FR-7 — Observability without secret leakage

Persist a bounded startup/mode-applied event or equivalent typed projection
containing session ID, attempt identity, requested mode (if any), canonical
effective mode (if applied), and outcome. Do not include credentials, full
configuration, prompts, or unrelated transcript contents.

## 7. Proposed state model and flow

Use the existing background-session and worker lifecycle. Do not add another
agent runner or mode subsystem.

```text
CLI mode
   │
   ├── validate/retain request identity
   ▼
BackgroundSession(mode=pending, requested=YOLO)
   │
   ├── durable request JSON(mode=YOLO)
   ▼
Worker claims attempt N
   │
   ├── build normal session → ModeManager resolves YOLO to Yolo
   ├── persist mode_applied(attempt=N, effective=Yolo)
   ├── assert mode state before dispatch
   ▼
first provider/agent turn observes Yolo
```

Failure path:

```text
unknown/unavailable mode
   → persist mode_application_status=failed and bounded error
   → mark startup attempt failed
   → zero provider/tool/workflow execution
```

The session record should retain separate values for invocation intent and
runtime truth. Naming may follow local model conventions, but a single
`mode_name` field MUST NOT ambiguously mean “requested before startup” in one
place and “effective canonical mode” in another.

## 8. Compatibility and migration

- Old background records and request JSON without mode metadata load without
  migration errors.
- Existing `mode_name` values written by PRD-210 are interpreted as effective
  canonical mode when present; missing values are not assumed to be Yolo.
- Existing `--mode` aliases and case-insensitive lookup remain supported.
- No-mode starts preserve current default and resume semantics.
- Existing TUI and CLI consumers receive additive fields; avoid breaking
  existing JSON keys.
- Mode history is bounded in session summaries; the append-only event/audit
  retention follows the existing background-store policy.

## 9. Acceptance criteria

1. Starting `agenthicc --mode YOLO --goal "..." --detach` immediately creates
   a durable session projection that records the request as pending, rather
   than implying the effective mode is Safe or leaving the choice invisible.
2. Once the worker is initialized, its canonical effective mode is `Yolo` in
   the background record and session-mode metadata.
3. A production-built detached session's first `goal_flow` agent turn observes
   Yolo through the actual `AppState`/`ModeManager` used for policy, prompt, and
   tool filtering.
4. The test observes mode at first provider/agent-turn dispatch, not only in
   request JSON, `_build_session_context()` kwargs, or a fake `active_name`.
5. No provider call, tool execution, workflow phase, or subagent starts before
   the explicit mode is applied and attested.
6. A deliberately injected mode-application failure produces a durable startup
   failure and zero agent invocations; it does not execute in Safe.
7. `YOLO`, `Yolo`, and supported aliases resolve to the canonical mode using
   existing mode registry behavior.
8. A no-mode detached start preserves the documented persisted-mode-or-Safe
   behavior and does not falsely show pending Yolo.
9. A retry before mode application retains the requested mode. A late event
   from attempt N cannot overwrite mode state for attempt N+1.
10. A resume without explicit mode uses the successfully persisted canonical
    mode; an explicit resume mode takes precedence.
11. Session list/details, `jobs status`, `agenthicc agents`, and goal-run
    inspection consistently distinguish pending from effective mode.
12. Selecting Yolo does not alter `dangerously_skip_permissions`; existing
    Safe/Plan/Yolo capability and workspace-policy tests remain green.
13. Legacy records/request payloads without the new fields load and resume
    without data loss or schema errors.

## 10. Test plan

### Unit tests

- `BackgroundSession.create()` persists an explicit requested mode as pending
  without marking it effective.
- Mode-state evolution validates allowed statuses and preserves attempt
  identity.
- Projections distinguish requested, pending, applied, and failed states.
- Case and alias canonicalization use the existing `ModeManager` registry.
- Legacy records with only `mode_name`, or with no mode fields, decode
  compatibly.
- Stale attempt mode-applied events are rejected/ignored.
- Invalid explicit mode produces a startup failure and no fallback.

### Integration tests

- Submit a detached goal with `mode_name="YOLO"`; read the durable record and
  request before starting a real worker and verify honest pending state.
- Run a worker with production `_build_session_context()` and a deterministic
  fake provider boundary; assert the first turn observes canonical Yolo in
  `AppState.active_mode()`, workspace policy, prompt suffix, and capability
  filtering.
- Inject failure between session construction and the first turn; verify no
  provider call occurs unless the applied-mode event is durable.
- Exercise resume/retry across attempt boundaries and a delayed stale worker
  update.
- Verify goal-run and background-session projections agree on startup and
  effective-mode state.

### End-to-end tests

- Run the CLI path `--mode YOLO --goal ... --detach` with isolated HOME,
  deterministic configuration, and a fake provider; wait for a durable
  first-turn observation and assert Yolo.
- Assert the immediate CLI response and immediately-readable `agents`/`jobs`
  record show pending rather than falsely showing the default.
- Repeat for no explicit mode, invalid mode, retry before initialization,
  and resume after successful initialization.
- Verify a Safe/Plan/Yolo policy matrix at the first tool request, including
  that Yolo does not toggle the dangerous-permissions flag.

## 11. Operational and performance requirements

- Detached CLI return remains asynchronous and bounded; it need not wait for
  nonessential startup services.
- The mode-applied handshake/event must be durable and idempotent within an
  attempt.
- No polling loop with unbounded wait is introduced. Any startup readiness
  wait must have an explicit timeout and a recoverable status.
- Request and projection payloads remain bounded and do not duplicate
  transcript or configuration data.

## 12. Implementation sequence

1. Add explicit requested/effective/application-state fields to the durable
   background model and backward-compatible codec.
2. Initialize pending mode state atomically with session creation.
3. Have the worker persist canonical mode application before dispatching
   `execute_workflow()` or a direct turn, guarded by attempt identity.
4. Add a fail-closed assertion at the worker-to-agent-turn boundary.
5. Project startup state consistently in CLI JSON, `jobs status`, manager TUI,
   and goal-run inspection.
6. Add production session-builder/first-turn integration and isolated CLI E2E
   coverage.
7. Update background-session/goal-run guides and the PRD implementation record.

## 13. Definition of done

The PRD is complete only when a detached goal's explicit mode is durably
visible from session creation, is canonical and attested before any agent work,
is observed by the first production-path turn, survives retry/resume without
stale-attempt overwrites, and is represented consistently in user-facing
projections. Existing security and workflow phase-mode behavior must remain
unchanged.

## 14. Implementation record

Implemented on 2026-09-30.

### Runtime changes

- `BackgroundSession` now persists `requested_mode_name`, effective
  `mode_name`, `mode_application_status`, `mode_application_attempt`, and a
  bounded/redacted application diagnostic. A newly submitted session records
  the requested value and `pending` status before `_launch()` starts the child.
- `BackgroundStore.record_mode_application()` is the sole worker attestation
  boundary. It requires a non-empty canonical mode, the current running
  attempt, and that attempt's lease. Direct updates cannot mark a mode applied
  without those fences. Claim/retry resets effective state and advances the
  application attempt; delayed writes fail the same attempt/lease checks.
- The worker builds the normal production session context and resolves an
  explicit request through its `ModeManager`. It durably records the effective
  mode before it starts the event processor or dispatches a direct turn or
  workflow. A second guard immediately before dispatch checks the live
  `ModeManager`, durable application state, attempt, and lease. Startup or
  attestation failure is terminal for that attempt and does not fall back to
  Safe or invoke agent work.
- Resume selection now checks new-format durable request/application state
  before session metadata. An unconsumed explicit mode survives a failed
  initialization even if placeholder metadata contains Safe; legacy records
  retain the prior persisted-mode-first compatibility behavior.
- Session and goal-run projections carry the requested/effective/status
  distinction into `jobs status`, the `agents` session table/details, `runs`
  JSON/text, and `agents --run`. Diagnostics are bounded and redacted. Empty
  explicit values remain distinguishable from an omitted mode.
- Legacy background records with only a non-empty `mode_name` decode as
  previously applied; records with no mode continue normal persisted-mode or
  Safe resolution. Existing session metadata and per-phase mode overrides
  remain authoritative at their existing boundaries. Mode selection does not
  change `dangerously_skip_permissions`.

### Verification added

- Unit coverage checks durable pending creation, mode-state normalization,
  guarded attestation, retry reset, stale-attempt rejection, canonical aliases,
  and legacy decoding.
- Integration coverage runs a worker through the real `_build_session_context`
  and observes the actual `AppState`/`ModeManager` at the first-turn dispatch
  boundary. It verifies `YOLO` is active and durably applied before dispatch,
  while the dangerous-permissions flag remains false. Additional cases prove
  that invalid mode resolution and failed durable attestation produce zero
  agent invocations.
- The isolated CLI end-to-end test starts
  `agenthicc --json --mode YOLO --goal ... --detach`, verifies the initial
  projection is either honestly pending or already applied, then checks the
  canonical applied state across `jobs status`, `runs show`, and
  `agents --run`.
- Documentation now describes the status protocol in the background-session
  and goal-run guides; `llms-full.txt` documents the public mode status type
  and session fields.

### Implementation assumptions

- `mode_name` remains the compatibility field for the **effective canonical**
  mode; new request state is additive in `requested_mode_name`.
- The detached CLI stays asynchronous. If the worker wins the race and applies
  the mode before the parent returns, the launch projection may already say
  `applied`; otherwise it must say `pending`. Both are truthful snapshots.
- A session with no explicit request remains `pending` until normal
  session construction resolves its persisted mode or Safe default; it is not
  pre-labeled Safe at record creation.

### Verification results

Verified on 2026-09-30:

- `uv run pytest tests/ -q` — **4,049 passed, 16 skipped**.
- `uv run ruff check src/ tests/ scripts/` and formatting checks for all
  changed source/test files — passed.
- `uv run python scripts/type_audit.py --check
  docs/reference/type-safety-baseline.json` — passed.
- `uv run mkdocs build --strict` — passed.
- `uv run nox -s llms_check` — passed for all 22 documented public symbols.
- `uv run nox -s build` — passed; both sdist and wheel were produced. Both
  artifacts passed `twine check` in an isolated `uvx` environment. The Nox
  `build_check` wrapper could not locate `twine` in its dev environment.
- Repository-wide `uv run mypy src/agenthicc` remains blocked by unrelated
  existing typing errors in other modules; the changed worker module passes a
  focused check with imported modules skipped.
