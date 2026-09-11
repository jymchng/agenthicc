---
title: "PRD-188: Resume the latest recoverable workflow run"
status: Implemented
version: 1.0.0
created: 2026-09-11
scope: "TUI workflow resume command, durable workflow-run selection, and recovery diagnostics"
related_prds:
  - PRD-156   # resumable plan-mode interrupts and workflow continuation
  - PRD-169   # transaction-safe tool-call conversations
  - PRD-170   # durable workflow recovery
  - PRD-171   # single live owner for resumed sessions
  - PRD-173   # recoverable workflow errors and failure checkpoints
  - PRD-184   # preserve the active workflow phase after transient errors
  - PRD-186   # profile-aware workflow checkpoint topology
tags:
  - workflows
  - resume
  - recovery
  - tui
  - checkpoints
---

# PRD-188 — Resume the latest recoverable workflow run

## 1. Executive summary

In the normal agenthicc session there is one interrupted, resumable workflow
run. Requiring the user to copy a long run ID in that case adds friction and
creates an unnecessary opportunity for selecting the wrong checkpoint.

This PRD makes the following command a complete resume operation:

```text
/workflow resume
```

It is an alias for:

```text
/workflow resume <latest-run-id>
```

The explicit form remains supported and retains its current semantics. When
the ID is omitted, agenthicc must select the newest *eligible durable
workflow checkpoint* for the current session and pass its canonical run ID
through the same guarded resume path used by the explicit command. The
implementation must not create a new run, start at `INIT`, replace a saved
context with the latest transcript summary, or bypass workflow claims,
checkpoint validation, topology validation, or conversation rehydration.

## 2. Evidence-backed current state

### 2.1 Command ownership

`/workflow` is intercepted by `TUISession` rather than dispatched as a normal
stateless command. `_handle_workflow_command()` already parses `resume` and
passes either a supplied ID or `None` to `_handle_workflow_resume()`.
The built-in command metadata advertises `resume [run-id]`, but the user-facing
semantics are not yet defined as “latest when omitted.”

### 2.2 Current no-ID behavior

The resume handler can use an attached in-memory handle. If it has no attached
handle, it consults the recovery records discovered for the current session:

* one candidate is rehydrated;
* more than one candidate produces an instruction to choose an ID; and
* no candidate produces `no_recoverable_workflow`.

The recovery coordinator also has a `select_for_resume()` API, but its current
contract rejects multiple valid runs as ambiguous. This PRD changes only the
no-ID selection policy. Explicit IDs must continue to be resolved and checked
as they are today.

### 2.3 Durable recovery boundary

`WorkflowRecoveryCoordinator` owns inspection and rehydration. A
`WorkflowRecoveryRecord` is eligible only when its checkpoint is valid,
session-bound, context-ready, in a recoverable lifecycle, compatible with the
loaded workflow topology/profile, and not marked diagnostic-only. The
checkpoint store and workflow handle own atomic persistence and the per-run
claim.

The existing record ordering is based primarily on checkpoint creation time.
Creation time represents run birth rather than necessarily the most recent
durable progress. The implementation must define a durable recency key so
“latest” cannot depend on directory enumeration order, an in-memory snapshot,
or filesystem modification time.

## 3. Problem statement

Users commonly see a message such as:

```text
Workflow 'reconstruct_site' can be resumed at architecture.
Use /workflow resume 853cfb6649824f71bedde79a72ecb43a.
```

They should be able to submit `/workflow resume` directly. Today this either
depends on a handle already being attached or fails when more than one
recoverable record is visible, even though the intended user experience is to
continue the latest interrupted run.

The feature must remain safe under process restart, transient provider errors,
repeated pause/resume cycles, stale discovery records, another live owner, and
checkpoint topology changes.

## 4. Goals

The implementation MUST:

1. Make `/workflow resume` equivalent to resuming the latest eligible run ID
   in the current session.
2. Preserve the existing `/workflow resume <run-id>` behavior, including exact
   ID resolution, checkpoint validation, topology/profile checks, claims, and
   diagnostics.
3. Select from authoritative, freshly inspectable durable recovery records,
   not only from a possibly stale TUI projection.
4. Reuse one canonical code path after selection: omitted-ID resume must
   behave exactly like explicit-ID resume from the selected run onward.
5. Continue the selected run from its saved typed context, phase, phase
   iteration, conversation cursor, and provider conversation identity.
6. Keep the operation idempotent with respect to run creation: no new
   workflow run ID may be allocated by this command.
7. Keep single-owner protection intact. Selecting a run must not silently
   steal a live claim.
8. Give actionable feedback when there is no eligible run, when all records
   are invalid, or when the selected run is already claimed.
9. Make selection deterministic when unusual sessions contain multiple
   recoverable runs.
10. Cover the command and recovery boundary with unit, integration, and TUI
    end-to-end tests.

## 5. Non-goals

This PRD does not:

* change the explicit run-ID resume contract;
* change `agenthicc --resume <session-id>` or `--continue` session opening;
* make completed, cancelled, discarded, corrupt, or diagnostic-only records
  resumable;
* remove old checkpoints or alter workflow reset semantics;
* merge multiple workflow runs into one run;
* automatically resolve a live-owner conflict by killing or taking over the
  other process;
* alter workflow phase graphs, custom workflow codecs, or provider retry
  policy; or
* make a transcript summary authoritative over a valid checkpoint.

## 6. User-facing contract

### 6.1 Syntax

```text
/workflow resume [run-id]
```

`run-id` is optional. Whitespace around the command and ID is ignored. More
than one argument remains invalid and must produce usage guidance rather than
being interpreted as a different run ID.

### 6.2 Successful no-ID resume

For `/workflow resume` with one eligible candidate, the TUI MUST:

1. refresh or otherwise read the current durable recovery records;
2. select the latest eligible record;
3. display which workflow and run were selected, using a safely shortened ID
   when appropriate while retaining a way to inspect the full ID;
4. rehydrate and claim that exact run;
5. transition it to `resuming` and persist that transition before execution;
6. invoke the same runner resume method as the explicit-ID path; and
7. continue rendering the existing session transcript and workflow progress.

The success path must not display a new-run message, allocate a new run ID, or
re-run completed phases.

### 6.3 Explicit-ID compatibility

`/workflow resume <run-id>` MUST remain the authoritative way to select a
particular run. It must not be redirected to the latest candidate. Exact IDs
must win over abbreviated unique IDs according to the existing resolver, and
unknown, invalid, terminal, incompatible, or claimed runs must retain their
current structured error behavior.

### 6.4 No candidate

If no eligible durable run exists, the TUI MUST report an actionable,
non-error-looping message, for example:

```text
⚠ no_recoverable_workflow: no saved workflow is available to resume.
```

The command must not start a new workflow or mutate the active workflow
selection. If records exist but are not resumable, the message should identify
the recovery reason and recommend inspection or reset where appropriate.

### 6.5 Live owner conflict

If the selected run is claimed by another live owner, the command MUST retain
the `run_already_claimed` classification and show the existing owner
diagnostic/recovery guidance. It must not fall through to selecting an older
run merely because the newest candidate is claimed. The user can use an
explicit ID after resolving the owner situation.

### 6.6 Active-run guard

If another agent turn is active, `/workflow resume` must use the same active
run guard as explicit resume and leave the active task and checkpoint
unchanged.

## 7. Definition of “latest”

The latest candidate is the eligible record with the greatest durable
activity timestamp. The ordering MUST be total and deterministic:

1. `updated_at` (or an equivalent atomically persisted last-checkpoint time),
   descending;
2. checkpoint revision, descending; and
3. canonical run ID, ascending, as a stable tie-breaker.

If the current checkpoint schema has no durable update timestamp, add one in a
backward-compatible manner. Existing records may use `created_at` as their
recency fallback until their next checkpoint write. Selection must never use
filesystem mtime or the order returned by `list_run_ids()`.

The candidate set MUST be computed after applying all existing recovery
eligibility checks:

* current session/conversation ownership;
* recoverable lifecycle status;
* valid checkpoint content hash and schema;
* context codec availability and successful context validation;
* active workflow/profile topology compatibility;
* workspace and provider-profile identity checks; and
* absence of a terminal or diagnostic-only override.

A record that fails those checks is not “latest” for this command. It remains
available to diagnostics and explicit reset paths where the current product
allows that.

## 8. Proposed architecture and data flow

The implementation should keep selection in the recovery ownership boundary,
not duplicate checkpoint scanning in the command parser:

```text
User enters `/workflow resume`
        │
        ▼
TUISession._handle_workflow_command()
        │  parses no run-id
        ▼
WorkflowRecoveryCoordinator.select_latest_for_resume(...)
        │
        ├── reload durable checkpoint records for this session
        ├── validate eligibility and current topology/profile
        ├── sort by durable recency, revision, run-id
        └── return one canonical WorkflowRecoveryRecord or no candidate
        │
        ▼
existing explicit-ID resume path
        │  run_id = selected.run_id
        ▼
fresh checkpoint reload + revalidation
        │
        ├── claim run (PRD-171)
        ├── rehydrate typed context and session conversation
        ├── persist `resuming`
        ├── call runner.resume(context)
        └── preserve phase/checkpoint/error lifecycle
        │
        ▼
foreground TUI renders the resumed existing run
```

The selection result is a discovery snapshot only. The existing rehydration
path MUST reload the checkpoint and revalidate it immediately before claiming
and execution, so a checkpoint that became terminal or was claimed after
selection cannot be resumed from stale data.

## 9. Detailed implementation requirements

### 9.1 Recovery coordinator API

Add a narrowly named coordinator operation, such as
`select_latest_for_resume()`, or extend `select_for_resume()` with an explicit
selection policy. The API MUST:

* accept the same session, registry, conversation, provider profile, and
  workspace inputs as existing recovery inspection;
* return `WorkflowRecoveryRecord | None` for no candidate;
* return the newest eligible record when one or more candidates exist;
* preserve existing failures for corrupt or non-recoverable matching records;
* not claim or mutate a run; and
* document that its result is stale once returned.

The old “multiple recoverable workflows are present” exception must not be
used for omitted-ID selection. It may remain available to any future caller
that explicitly requests ambiguity rejection.

### 9.2 TUI command integration

`_handle_workflow_resume(None)` MUST call the coordinator’s latest-selection
operation, convert the selected record to its canonical run ID, and continue
through the explicit-ID guarded path. The implementation must avoid a second
independent branch with different claim, phase, or error behavior.

Command help, picker completions, usage text, notifications, and recovery
guidance must consistently show `resume [run-id]` and explain that omission
selects the latest recoverable run.

### 9.3 Checkpoint schema compatibility

If an `updated_at` or equivalent field is added:

* old checkpoints remain readable;
* missing fields receive a safe fallback;
* field validation remains bounded and typed;
* atomic writes and revision monotonicity are preserved; and
* the field is never populated with prompt text, secrets, or transcript
  content.

### 9.4 Session and conversation continuity

No-ID resume MUST preserve the selected checkpoint’s:

* `run_id`;
* workflow name and active profile topology;
* current phase and phase index;
* typed workflow context and phase iteration;
* conversation ID and journal cursor;
* provider profile identity; and
* claim and lifecycle transitions.

It must use the existing session-scoped conversation rather than constructing a
second conversation or replacing the transcript with a summary. Tool-call
transaction repair remains governed by PRD-169.

### 9.5 Diagnostics and observability

The implementation SHOULD publish the same resume lifecycle events as the
explicit command and add a non-secret selection detail, including workflow,
run ID, selection policy (`latest` or `explicit`), and checkpoint revision.
Logs and events MUST NOT include API keys, authorization headers, prompt bodies,
tool arguments, or full transcript content.

## 10. Edge cases and failure policy

| Situation | Required result |
|---|---|
| One paused or interrupted valid run | Select and resume it |
| Multiple valid runs | Select newest by durable recency ordering |
| No valid run | Show `no_recoverable_workflow`; do not start a run |
| Newest record is corrupt/incompatible | Exclude it; select newest eligible record; retain diagnostics |
| All records are invalid | Show the most useful bounded recovery diagnostic; do not mutate records |
| Selected run became terminal after discovery | Fresh revalidation rejects it; do not fall back silently to a stale context |
| Selected run is claimed by another live owner | Show `run_already_claimed`; do not steal or silently select another run |
| Active agent task exists | Use existing active-run guard |
| Explicit ID supplied | Preserve explicit-ID behavior exactly |
| Old checkpoint lacks recency field | Use documented compatibility fallback |
| Multiple records tie on time and revision | Choose deterministic run-ID tie-break |
| Workflow is no longer registered | Preserve existing unavailable/incompatible diagnostic |

Whether a failed newest candidate should cause the command to stop rather than
fall back is policy-sensitive. The default requirement is: exclude records
that fail pre-selection eligibility validation, but do not hide a race detected
after selection. A post-selection claim, topology, or terminal-state failure
must be surfaced for the selected run so the user understands what happened.

## 11. Testing requirements

### 11.1 Unit tests

Add clean-slate tests for:

1. parsing `/workflow resume` as `run_id=None`;
2. parsing one explicit ID and rejecting extra arguments;
3. selecting the only eligible record;
4. selecting the newest of multiple eligible records using durable recency;
5. deterministic revision and run-ID tie-breaks;
6. excluding terminal, corrupt, incompatible, diagnostic-only, and
   context-unready records;
7. backward-compatible ordering for checkpoints without the new recency field;
8. preserving the existing explicit-ID resolver; and
9. preserving structured no-candidate and claim-conflict errors.

### 11.2 Integration tests

Using a temporary checkpoint root and session conversation, verify that:

* omitted-ID selection reloads durable records instead of trusting an old
  discovery snapshot;
* selection returns the existing run ID and does not create a checkpoint for a
  new run;
* a selected run is revalidated before claim and resume;
* a run that becomes terminal or claimed between selection and rehydration is
  rejected safely; and
* profile-aware topology, context codecs, conversation cursor, and checkpoint
  revision are preserved.

### 11.3 TUI end-to-end tests

Exercise the critical user journeys:

1. pause a workflow, submit `/workflow resume`, and assert that the same run
   resumes from the saved phase;
2. restart/open a session with a durable interrupted run, submit the command,
   and assert that it does not return to `INIT`;
3. create two recoverable runs and assert that the newest eligible one is
   selected;
4. submit an explicit older run ID and assert that it still selects that
   exact run;
5. submit the command with no candidates and assert that no workflow starts;
6. exercise a live-owner conflict and assert `run_already_claimed`; and
7. verify the transcript, lifecycle events, and workflow overlay do not show a
   second run or duplicate phase execution.

Tests must use deterministic timestamps/revisions and must not depend on wall
clock timing, directory order, provider availability, or external network
services.

## 12. Security, resilience, and performance

* Keep all existing path, session-identity, checkpoint-size, JSON-schema, and
  claim-owner validation in force.
* Do not accept a run ID from one session merely because it is globally known.
* Do not expose full IDs, prompts, or transcript contents in provider-facing
  requests or unredacted logs.
* Selection should perform one bounded durable scan and reuse the existing
  inspection/revalidation mechanisms; it must not load complete transcripts for
  every historical run.
* A malformed record must not prevent valid records from being discovered.
* A failed selection must not alter the active workflow selector or delete a
  checkpoint.
* The operation must be safe to repeat after an interrupted claim or provider
  error.

## 13. Rollout and compatibility

The feature is a backwards-compatible command enhancement. Existing scripts or
users that provide an explicit run ID retain their behavior. The change should
be enabled by default once tests pass; no configuration flag is required.

Documentation must update:

* `/help` and command picker text;
* the workflow recovery guide under `docs/guides/`;
* `llms-full.txt` if public Python symbols are added; and
* this PRD index.

No migration command is required for old checkpoints. A schema migration is
required only if the selected durable recency field cannot be read with a
backward-compatible default.

## 14. Acceptance criteria

| ID | Acceptance criterion |
|---|---|
| AC-188.1 | `/workflow resume` is accepted without a run ID and selects the latest eligible durable run. |
| AC-188.2 | `/workflow resume <run-id>` retains its existing exact-selection behavior. |
| AC-188.3 | Omitted-ID resume reuses the existing run, checkpoint, typed context, and conversation; it never creates a replacement run. |
| AC-188.4 | Selection is based on durable, deterministic recency and not directory order or filesystem mtime. |
| AC-188.5 | Invalid, terminal, diagnostic-only, and incompatible records are excluded from selection and remain diagnosable. |
| AC-188.6 | A live-owner conflict remains a `run_already_claimed` error and is never resolved by implicit takeover. |
| AC-188.7 | A stale discovery result is revalidated before claim and execution. |
| AC-188.8 | Resumed execution continues from the saved phase/topology and does not restart at `INIT`. |
| AC-188.9 | No-candidate and active-run cases are actionable and do not start or mutate a workflow. |
| AC-188.10 | Unit, integration, and TUI E2E tests cover success, ambiguity, invalid records, races, explicit IDs, and owner conflicts. |
| AC-188.11 | Documentation and command discovery consistently describe `resume [run-id]` and latest-run behavior. |

## 15. Definition of done

The PRD is complete when the implementation:

1. adds the latest eligible-run selection to the recovery ownership boundary;
2. routes omitted-ID resume through the canonical explicit-ID path;
3. preserves all existing checkpoint, topology, conversation, and claim
   invariants;
4. adds deterministic unit, integration, and TUI end-to-end coverage;
5. updates user and developer documentation; and
6. passes the relevant repository checks:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
```

## 16. Assumptions and open decisions

* The normal session has at most one recoverable run; latest selection is a
  deterministic safety net for unusual sessions, not a replacement for a run
  picker.
* “Latest” means latest durable workflow activity, not latest user message or
  latest transcript entry.
* Explicit selection remains available whenever operators need to resume an
  older run deliberately.
* The implementation team may name the recency field differently, provided
  its durable ordering, backward compatibility, and validation semantics meet
  this PRD.
* If product policy later requires user confirmation when multiple runs exist,
  that is a follow-up change; it is not the behavior specified here.

## 17. Implementation and verification evidence

Implemented in:

* `src/agenthicc/workflows/checkpoint.py` — backward-compatible durable
  `updated_at` checkpoint metadata with finite timestamp validation; legacy
  records fall back to `created_at`.
* `src/agenthicc/runners/workflow_recovery.py` — deterministic activity-time,
  revision, and run-ID ordering plus `select_latest_for_resume()`.
* `src/agenthicc/runners/tui_session.py` — omitted-ID selection, fresh durable
  lookup, canonical explicit-ID resume routing, and argument validation.
* `src/agenthicc/commands/builtins.py`, `README.md`, and
  `docs/guides/workflows.md` — command discovery and user guidance.

Coverage was added in:

* `tests/unit/test_workflow_recovery.py`;
* `tests/unit/test_tui_session_edge_coverage.py`;
* `tests/integration/test_workflow_resume_latest_prd188.py`; and
* `tests/e2e/test_workflow_resume_latest_prd188_e2e.py`.

Focused verification completed:

```text
27 passed
ruff check: passed
```

The broader repository unit, integration, E2E, formatting, and type checks
remain the release gate for the complete change.
