---
title: "PRD-213: Select the resumed workflow as the active workflow"
status: Implemented
version: 1.0.0
date: 2026-10-01
repository: jymchng/agenthicc
related_prds:
  - PRD-114  # Workflow override command and composite workflow selection
  - PRD-155  # Mode defaults and workflow-bound modes
  - PRD-170  # Reliable workflow resume and durable recovery
  - PRD-188  # Select the latest recoverable workflow
tags:
  - workflows
  - resume
  - tui
  - session-state
---

# PRD-213 — Select the resumed workflow as the active workflow

## 1. Summary

When a user resumes a durable workflow with `/workflow resume [run-id]`,
Agenthicc continues the saved run but does not make that run's workflow the
session's active workflow selection. For example, resuming a saved `goal_flow`
run can leave the TUI's selected-workflow indicator unset or pointing at a
previous workflow. After the resumed run ends, normal turns continue to use
that stale selection or the active mode's default instead of `goal_flow`.

On successful resume acceptance, the workflow named by the selected,
validated checkpoint MUST become the current session-local workflow
selection. The selected name must be reflected in both the runner's canonical
selection state and the reactive TUI projection. This selection remains until
the user chooses another workflow or resets selection to the mode default.

This PRD is about workflow **selection**, not restoring or executing the
workflow run itself. PRD-170 remains the authority for checkpoint validation,
ownership claims, typed-context restoration, and exact phase continuation.

## 2. Current-source investigation

### 2.1 Selection state

The interactive TUI represents a session-local workflow override in two
places (`src/agenthicc/runners/tui_session.py` and
`src/agenthicc/tui/conversation_store.py`):

- `TUISession._workflow_override` is read by `run_turn()` when selecting a
  workflow for a normal user turn. The selected override takes precedence over
  `ctx.app_state.active_mode().default_workflow`.
- `ConversationStore.workflow_override` is the reactive projection observed by
  the workspace. `FooterComponent` renders it as the `⬡ workflow-name`
  indicator.

`TUISession.__init__()` initializes these values from
`SessionContext.initial_workflow`. `_handle_workflow_command()` for
`/workflow <name>` updates both values. `_reset_workflow_to_mode_default()`
clears both values so later turns use the active mode's default.
`FooterComponent.render()` in `src/agenthicc/tui/workspace/components.py`
reads the signal to display the indicator. The code therefore already defines
what “current active workflow” means: the session-local override, mirrored to
the TUI signal.

### 2.2 Resume path

The successful `/workflow resume` path in
`TUISession._handle_workflow_resume()` (`src/agenthicc/runners/tui_session.py`):

1. selects the latest eligible record or resolves the supplied run ID;
2. refreshes recovery records when required;
3. rehydrates the workflow handle and typed context;
4. verifies that the workflow definition and context are available;
5. claims the run if it is not already owned by this TUI;
6. marks the handle `resuming` and persists the checkpoint;
7. publishes `workflow_resume_started` and creates
   `_resume_workflow_task()`.

It does **not** assign `handle.workflow_name` to
`self._workflow_override`, and it does not update
`conv.workflow_override`. `_resume_workflow_task()` correctly invokes
`runner.resume()` on the selected run, but that run handle is separate from the
session's normal-turn selector. The ordinary continuation path also invokes
`_resume_workflow_task()` independently and does not synchronize selection.

The regular turn selector later evaluates:

```text
_workflow_override or active_mode.default_workflow
```

The workspace footer reads the separate reactive value
`ConversationStore.workflow_override`.

As a result, successful resume can execute `goal_flow` while the session
continues to advertise and select another workflow (or no override) for later
turns. The workspace footer can consequently disagree with the workflow that
the user just chose to resume.

### 2.3 Existing recovery invariant

PRD-170 acceptance criterion A-1 requires that a resume attempt with no
recoverable checkpoint not change the active workflow selection. The fix must
not set selection prematurely while looking up an ID, showing a multi-run
choice, validating a checkpoint, or claiming a run. Selection should change
only after the intended run has passed all guards and the resume transition is
durably accepted.

## 3. Problem statement

The current implementation has two distinct state concepts but does not join
them during resume:

```text
workflow run identity: handle.workflow_name = "goal_flow"
session selection:     _workflow_override / workflow_override signal = stale
```

The direct resume task uses the first value, so the saved run itself can
continue correctly. Normal workflow dispatch and the visible footer use the
second value. This produces a split-brain session state: the TUI resumes one
workflow while its active selection still names another workflow or the mode
default.

Existing resume coverage exercises run selection, claims, repeated pause and
resume, and checkpoint lifecycle. It does not assert that a successful resume
changes the selector signal; the repeated-resume test verifies the same handle
and run ID but not `workflow_override`. This leaves the selection defect
undetected despite checkpoint resumption passing.

The defect is especially visible when:

- the saved workflow differs from the current mode's default;
- the user previously selected another workflow in this TUI;
- the process was restarted and the in-memory override initialized from a
  different invocation/default; or
- the resumed workflow completes and a subsequent ordinary message is sent.

## 4. Goals

1. Make the successfully resumed workflow the session's active workflow
   selection.
2. Keep the private dispatch selector and reactive TUI indicator consistent.
3. Ensure the selection is derived from the validated checkpoint/handle, not
   from user-supplied text or a stale selector.
4. Preserve the existing behavior for failed, ambiguous, invalid, or
   unclaimable resume attempts.
5. Keep resumed-run identity, phase, context, ownership, and checkpoint
   semantics unchanged.
6. Make ordinary turns after resume select the resumed workflow until the user
   explicitly selects another workflow or resets to the mode default.

## 5. Non-goals

- Change which checkpoint `/workflow resume` selects or how ambiguity is
  resolved.
- Change checkpoint format or duplicate the selected workflow name into a new
  durable store.
- Change the `WorkflowRunHandle` lifecycle or the runner's `resume()` contract.
- Automatically choose a workflow merely because a recovery record is
  discovered or displayed at startup.
- Change workflow selection for headless runs or detached background sessions;
  those paths already receive an explicit workflow name at invocation.
- Change mode selection, mode defaults, phase-level mode overrides, or the
  Safe-mode transition policy.
- Clear the selected workflow automatically when a resumed run completes.

## 6. Product requirements

### FR-1 — Select the validated workflow when resume is accepted

After `/workflow resume` has identified the intended handle, verified its
workflow definition and checkpoint-supported context, obtained the required
claim, and successfully persisted the `resuming` transition, the session MUST
set its active workflow selection to the canonical `handle.workflow_name`.

The active selection MUST be updated before the resumed task can yield control
to another input/dispatch operation. The workflow name MUST come from the
validated handle/checkpoint, never from the untrusted command argument.

### FR-2 — Keep selection state and UI projection in sync

The update MUST change both:

```text
TUISession._workflow_override
ConversationStore.workflow_override
```

Implementation SHOULD centralize selection and clearing through one
TUISession helper so `/workflow <name>`, successful resume, `/workflow reset`,
and registry reload cannot independently mutate one representation. No second
source of truth should be introduced.

The workspace footer MUST show the resumed workflow's `⬡ <name>` indicator
after resume is accepted, including when the active mode's default is another
workflow or `None`.

### FR-3 — Keep the selected workflow after the run completes

After the resumed run reaches a terminal lifecycle, the session-local active
selection MUST remain the resumed workflow. A subsequent ordinary message
MUST dispatch according to that workflow, subject to the existing active-mode
and workflow policy rules.

The selection changes only when the user:

- runs `/workflow <another-name>`; or
- runs `/workflow reset`, which clears the override and restores mode-default
  selection.

Completion, pause, interruption, or provider failure of the resumed run MUST
NOT silently clear or replace the selection. The existing mode transition on
workflow completion may still occur; it MUST NOT erase this explicit workflow
selection.

### FR-4 — Apply equally to supported resume entry points

All interactive TUI entry points that dispatch the same saved workflow through
`_resume_workflow_task()` MUST establish the active selection consistently.
This includes:

- `/workflow resume <run-id>`;
- `/workflow resume` when it selects a sole/latest eligible run; and
- an ordinary continuation message that resumes an already selected paused
  workflow, as defined by PRD-170.

These are the two current call sites of `_resume_workflow_task()`; the rule is
defined against that shared dispatch boundary so any future interactive resume
entry point cannot bypass selection synchronization.

The update MUST be idempotent for repeated pause/resume cycles of the same
handle. It MUST NOT create a new run or alter the saved run ID.

### FR-5 — Failed selection and resume attempts are side-effect free

The active workflow selection MUST remain unchanged when any pre-dispatch
condition fails, including:

- no recoverable run exists;
- multiple runs require explicit selection;
- the run ID is invalid or belongs to another session;
- the checkpoint/context/plugin is invalid or unavailable;
- another live owner holds the claim; or
- the `resuming` transition/checkpoint persistence fails.

In particular, the implementation MUST preserve PRD-170 A-1: an unsuccessful
resume lookup cannot change the session's workflow selector.

### FR-6 — Derive selection from recovered identity after restart

The selector itself remains session-local and need not add a new persistence
format. When a process is restarted, a successful `/workflow resume` MUST
rehydrate the handle from the durable checkpoint and derive selection from
that handle's canonical `workflow_name`. A stale `initial_workflow`, prior
session override, current mode default, or startup recovery notice MUST NOT
override the successfully selected checkpoint's workflow name.

If the recovered workflow is not loaded or cannot be safely resumed, fail with
the existing typed diagnostic and leave the active selection unchanged.

### FR-7 — Events and diagnostics reflect accepted state

The `workflow_resume_started` event and TUI confirmation SHOULD expose the
same canonical workflow name that is selected. Do not emit a “resuming” success
notice or publish a started event before claim and transition acceptance.
Existing event schemas should remain backwards compatible; any added field is
optional and bounded.

## 7. Proposed state transition and data flow

```text
/workflow resume [run-id]
        │
        ▼
resolve exact eligible checkpoint
        │
        ▼
rehydrate validated handle
        │
        ├── workflow_name = checkpoint/handle.workflow_name
        ▼
validate plugin + typed context
        │
        ▼
claim run + persist RESUMING transition
        │
        ├── failure ──> preserve current selector and show error
        ▼
set session selector + reactive selector signal
        │
        ├── private selector: goal_flow
        └── UI signal:        goal_flow
        ▼
publish resume-started + dispatch runner.resume(context)
        │
        ▼
resume / pause / completion / failure
        │
        └── active selection remains goal_flow
                 │
                 ├── /workflow another-name → select another
                 └── /workflow reset → mode default
```

The selector update belongs in the accepted resume transition boundary, not
inside a workflow runner. This keeps built-in and custom workflows consistent
and keeps the runner focused on its own typed checkpoint state.

## 8. User experience

Example: the active mode defaults to `code_plan`, but the user resumes a
checkpoint for `goal_flow`.

Before the fix:

```text
user: /workflow resume run-abc
Agenthicc: ↻ Resuming workflow 'goal_flow'…
footer: ... ⬡ code_plan       # stale selection, or no workflow indicator
```

After the fix:

```text
user: /workflow resume run-abc
Agenthicc: ↻ Resuming workflow 'goal_flow'…
footer: ... ⬡ goal_flow
```

After the resumed run completes, the next ordinary message continues to use
`goal_flow` until the user selects a different workflow or resets selection.

## 9. Acceptance criteria

### AC-1 — Explicit resume selects the recovered workflow

Set the current override to `code_plan`, provide a valid paused `goal_flow`
checkpoint, and run `/workflow resume <run-id>`. Assert the same run is
resumed, `_workflow_override == "goal_flow"`, and the reactive
`workflow_override()` signal equals `"goal_flow"` before resume dispatch.

### AC-2 — Latest/sole-run resume selects the recovered workflow

With no in-memory handle and exactly one eligible `goal_flow` checkpoint,
`/workflow resume` selects it and projects `goal_flow`. Repeat with multiple
checkpoints and verify the ambiguity path changes neither selector.

### AC-3 — Subsequent ordinary turn uses the selected workflow

After successful resume, finish the resumed run and submit a normal message.
Assert the workflow selection logic chooses `goal_flow`, even if the mode
default is unset or names another workflow.

### AC-4 — Footer displays the selected workflow

Render the real footer/workspace after accepted resume and assert it displays
`⬡ goal_flow`. Verify reset removes the indicator and returns dispatch to the
active mode's default.

### AC-5 — Failed resume does not mutate selection

For no candidate, invalid ID, missing plugin/context, claim conflict, and
checkpoint transition failure, seed a different override and assert both the
private value and UI signal retain that value. For an initially unset
selector, assert both remain unset.

### AC-6 — Same-process repeated resume is stable

Pause and resume the same workflow multiple times. The selector remains the
handle's workflow, the run ID is unchanged, and no duplicate runner dispatch
or checkpoint is created.

### AC-7 — Restart recovery derives selection from checkpoint

Reopen a session with an `initial_workflow` or mode default different from the
checkpoint workflow. After explicit successful resume, the selector and
footer show the workflow stored in the recovered checkpoint.

### AC-8 — Other selection behavior remains unchanged

Verify `/workflow <name>` updates the selector, `/workflow reset` clears it,
mode-default dispatch still works when no override is selected, and
workflow-completion mode transitions do not clear an explicit resumed
selection.

## 10. Test strategy

### Unit

- Central selection helper updates the internal selector and reactive signal
  together, and reset clears both.
- Explicit-ID and latest/sole-run success paths select the handle's workflow.
- The selector update occurs only after successful claim and persisted
  `resuming` transition.
- Every lookup/validation/claim/transition failure leaves both representations
  unchanged.
- Ordinary turn dispatch reads the resumed selection after the resumed run
  completes.

### Integration

- Exercise the real `TUISession` resume flow with a temporary
  `SessionConversation`, `WorkflowCheckpointStore`, and registered test
  workflow; assert event order, selection state, runner resume call, and
  repeated pause/resume behavior.
- Resume a checkpoint with a workflow name different from the active mode
  default and verify the footer projection and next normal dispatch.
- Verify restart rehydration chooses the checkpoint name rather than stale
  `initial_workflow` or mode default.

### End to end

- In a pseudo-terminal, run a workflow to a persisted pause, alter or start
  with a different active selector, issue `/workflow resume`, and verify the
  footer marker changes to the resumed name.
- Complete the run, submit another message, and verify it dispatches through
  the selected workflow.
- Exercise `/workflow reset` and verify the marker disappears and mode-default
  selection resumes.
- Include failure cases for ambiguous runs and live-claim conflicts to prove
  they do not mutate selection.

Tests must be deterministic, isolated, and must not call a real model provider.

## 11. Compatibility and safety

- No checkpoint schema migration is needed; the checkpoint already identifies
  the workflow by name.
- No changes are made to ownership claims, workspace confinement, approval
  policy, workflow artifacts, or phase resume position.
- Existing event consumers continue to work; new projection fields, if any,
  are additive.
- A selector update is not itself authorization to execute a workflow. Normal
  mode, capability, workspace, approval, and loaded-plugin checks still apply.
- On failure before resume acceptance, existing selector state is preserved.

## 12. Implementation plan

1. Confirm `_workflow_override` and `ConversationStore.workflow_override` as
   the canonical selected-workflow state and projection.
2. Add one TUISession method for setting a validated workflow selection and use
   it from `/workflow <name>`, successful resume, and related selection paths;
   retain a corresponding centralized reset operation.
3. Call the selector method only after a resume handle is fully validated,
   claimed, and its `resuming` checkpoint transition succeeds.
4. Apply the same invariant to the ordinary-message continuation path that
   resumes a paused handle.
5. Add unit, integration, footer projection, and deterministic TUI E2E tests for
   the acceptance criteria. The E2E test drives the actual session, resume
   task, and rendered footer in-process without a model provider; this keeps
   checkpoint, selection, and UI behavior deterministic while exercising the
   production boundaries involved in the defect.
6. Update the workflow guide and PRD index after implementation; this PRD
   specifies the change and is not itself implementation evidence.

## 13. Definition of done

Every accepted interactive resume selects its recovered workflow in both
session dispatch state and the UI projection. Subsequent normal turns use that
selection until explicit replacement or reset. Unsuccessful resume attempts
leave selection untouched. The full unit, integration, and E2E coverage for
selection, resume, reset, restart, and failure paths passes.

## 14. Implementation and verification evidence

- `TUISession._set_workflow_override()` is the single mutation path for the
  private workflow selector and `ConversationStore.workflow_override`.
- Both `/workflow resume` and ordinary-message workflow continuation now use
  `_dispatch_workflow_resume()` after validation, claim acquisition, and
  durable `resuming` checkpoint persistence. Failed attempts therefore never
  reach the selector update.
- Unit regression coverage is in
  `tests/unit/test_tui_session_edge_coverage.py`; disk-backed restart and event
  projection coverage is in
  `tests/integration/test_workflow_resume_selection_prd213.py`; the complete
  rendered-footer, resume-completion, subsequent-turn, and reset journey is in
  `tests/e2e/test_workflow_resume_selection_prd213_e2e.py`.
- User documentation is updated in `docs/guides/workflows.md`.
- Verified on 2026-10-01 with `uv run pytest tests/ -q` (4,058 passed, 16
  skipped), `uv run ruff check src/ tests/ scripts/`, focused Ruff formatting
  checks for changed files, `uv run mypy src/agenthicc/runners/tui_session.py
  --follow-imports=silent`, `uv run python
  scripts/type_audit.py --check docs/reference/type-safety-baseline.json`, and
  `uv run mkdocs build --strict`.
- The repository-wide formatter check still reports eight unrelated existing
  files requiring formatting. Repository-wide mypy still reports 250 existing
  errors in unrelated modules; the changed `tui_session.py` passes its focused
  mypy check.
