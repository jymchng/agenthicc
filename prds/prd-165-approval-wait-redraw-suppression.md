---
title: "PRD-165: Suppress Approval-Wait Redraw Loops"
status: Proposed
version: 1.0.0
created: 2026-08-03
related_prds:
  - PRD-60
  - PRD-78
  - PRD-86
  - PRD-100
  - PRD-120
  - PRD-144
  - PRD-164
tags:
  - tui
  - rich-live
  - approvals
  - plan-review
  - questions
  - rendering
  - performance
---

# PRD-165 — Suppress Approval-Wait Redraw Loops

## 1. Executive summary

When the agent is waiting for a plan-review approval, tool approval, or
`ask_user()` answer, agenthicc can repeatedly append the same complete TUI
surface:

```text
✿ Waiting for plan approval │ 1m 19s
deepseek-v4-flash
<session metadata>
─────────────────────────────────────────────────────────────────  📋 Plan Review
<plan content>
<approval options>
```

The repeated surface is not a second workflow execution, a duplicated plan,
or a duplicated approval request. It is the same Rich `Live` block rendered
again and again. A session tick runs every 50 ms. During a prompt wait, the
animation frame and the displayed active-work clock are intentionally paused,
but the outer `activity_elapsed_s` signal still changes on every tick. The
workspace subscribes to that signal and refreshes the entire Live block.

Rich normally replaces the previous Live block using cursor-control sequences.
Captured output, terminal adapters, and clients that do not interpret those
sequences expose every refresh as appended output. Because the waiting overlay
renders the frozen `display_elapsed_s` value, the successive blocks are
visually identical.

PRD-164 fixed the analogous idle-frame path by preventing static idle ticks
from publishing `frame`. This PRD addresses the distinct approval-wait path:
timer signals must not cause a Live refresh while a user-owned prompt is
waiting, while wall-clock telemetry and legitimate prompt interaction remain
correct.

## 2. Exact problem statement

### 2.1 User-visible symptom

The reported output contains the same `Waiting for plan approval` header,
model, session metadata, plan text, and option list multiple times. The
displayed duration remains `1m 19s` in every copy. The final copy may show the
plan viewport's scroll indicator or lower rows differently because the output
consumer has captured a partial screen update, but the source render is one
stable overlay.

The in-progress workflow remains at one turn and one approval boundary. There
is no evidence of a new LLM request or a new workflow phase transition per
copy.

### 2.2 Current source data flow

```text
TUISession._tick()                                    every 50 ms
        │
        ▼
ConversationStore.tick(paused=True)
        │
        ├─ _advance_activity_clock(now)
        │       └─ activity_elapsed_s.set(new_value)
        │
        ├─ _advance_display_clock(now)
        │       └─ no change while display is paused
        │
        ├─ return before frame.set(...)
        │       └─ animation frame is correctly frozen
        │
        ▼
Workspace subscription to activity_elapsed_s
        │
        ▼
Workspace._redraw()
        │
        ▼
Live.update(_build(), refresh=True)
        │
        ├─ StatusComponent renders the same waiting label and frozen duration
        ├─ PlanApprovalOverlay renders the same fixed-height plan viewport
        └─ composer/footer/options are rendered again
        │
        ▼
Client that does not apply Rich cursor controls
        └─ each replacement becomes another visible copy
```

### 2.3 Relevant implementation facts

| Concern | Current implementation | Finding |
|---|---|---|
| Tick owner | `src/agenthicc/runners/tui_session.py` | Calls `conversation.tick(paused=...)` every 50 ms. |
| Prompt ownership | `AppState.pending_approval` | A non-`None` value pauses the display clock and selects the waiting overlay. |
| Activity clock | `ConversationStore._advance_activity_clock()` | Publishes `activity_elapsed_s` whenever outer activity is active, including prompt waits. |
| Display clock | `ConversationStore._advance_display_clock()` | Correctly excludes the prompt-wait interval from the displayed active-work duration. |
| Frame clock | `ConversationStore.tick()` | Correctly does not publish `frame` after the pause guard. |
| Redraw subscription | `Workspace.start()` | Subscribes `activity_elapsed_s` to the general `_redraw()` path. |
| Redraw scheduling | `Workspace._redraw()` | Coalesces callbacks in one event-loop turn but does not deduplicate renders across ticks. |
| Waiting status | `StatusComponent.render()` | Uses `display_elapsed_s` while a prompt is pending, so activity-signal redraws look identical. |
| Plan overlay | `PlanApprovalOverlay` | Has a fixed-height viewport; it is not the source of the repeated redraw trigger. |
| Persistence | Conversation events and workflow state | The Live refresh is presentation-only and does not append conversation events. |

### 2.4 Reproduction

The defect can be reproduced without an LLM or network call:

1. Create an `AppState` and begin one synthetic conversation turn.
2. Set `pending_approval` to a `plan_review` request.
3. Start a `Workspace` backed by a terminal-capable or recording Rich console.
4. Run five or more ticks with `paused=True`, advancing the monotonic clock.
5. Allow the event loop to process redraw callbacks.
6. Capture or normalize the console output and count the plan-review surface.

The expected internal values during the current reproduction are:

```text
frame                 = unchanged
display_elapsed_s     = unchanged
activity_elapsed_s    = increases once per tick
activity signal       = one notification per tick
Live.update calls     = one or more calls per tick
```

The last three values are the defect. The first two are intentional waiting
behavior.

## 3. Goals

1. Prevent timer-only `Live.update()` calls while a user-owned approval or
   question prompt is pending.
2. Keep a waiting Plan Review, tool approval, or question visually stable when
   no meaningful UI state has changed.
3. Preserve the full wall-clock duration of the outer user activity for
   telemetry, completion events, and workflow diagnostics.
4. Preserve the pause-aware displayed active-work duration used by waiting
   overlays.
5. Resume the activity clock, display clock, animation, and ordinary redraws
   correctly after the user responds.
6. Preserve legitimate redraws caused by selection changes, typed feedback,
   overlay transitions, terminal resize, notifications, mode changes, and
   approval state changes.
7. Apply the contract consistently to generic tool approval, `plan_review`,
   and `questions` prompts.
8. Make the no-redraw-during-wait contract observable through deterministic
   unit, integration, and end-to-end tests.
9. Keep the fix inside the existing `ConversationStore` and `Workspace`
   ownership boundaries.
10. Preserve conversation events, workflow checkpoints, approval journals,
    and session-memory semantics exactly.

## 4. Non-goals

This PRD does not:

- change plan approval, tool approval, or `ask_user()` semantics;
- change which workflows, tools, or capabilities require approval;
- change the workflow phase graph, checkpoint format, or transition rules;
- remove the outer activity clock or stop recording user-wait time in telemetry;
- remove SIGWINCH handling or suppress legitimate geometry redraws;
- replace Rich `Live`, introduce a second renderer, or start/stop Live per turn;
- deduplicate persisted conversation events by changing the event store;
- alter provider requests, LLM memory, prompt caching, or workflow journals;
- promise that an interactive terminal will never emit ANSI control sequences;
- make the headless runner render a TUI surface.

## 5. User-facing and runtime contract

### 5.1 Waiting presentation

When `pending_approval` is non-`None`:

- the status label is stable (`Waiting for approval`, `Waiting for plan
  approval`, or `Waiting for your answer`);
- the flower, Thinking text, and compaction spinner remain frozen;
- the displayed active-work duration remains frozen at the value captured at
  the wait boundary;
- the plan, question, or tool-approval content is rendered once initially;
- the normal session tick does not produce a Live refresh solely because time
  passed.

### 5.2 Legitimate waiting-state redraws

The following actions may redraw the Live block while waiting:

- initial creation or replacement of the pending request;
- changing the selected approval/answer option;
- scrolling a plan review viewport;
- entering, editing, or clearing rejection feedback or answer text;
- moving between the selection and text-entry sub-states;
- terminal resize, when geometry genuinely needs recalculation;
- approval, rejection, answer submission, cancellation, or denial;
- a visible notification, mode, workflow, or overlay change.

Repeated writes of the same pending request must be idempotent and must not
create a timer-driven redraw loop.

### 5.3 Response boundary

When the prompt resolves:

1. the existing approval service clears `pending_approval`;
2. the workspace performs the required transition redraw;
3. the display clock resumes from its paused baseline;
4. the next active tick may publish the current activity/display timing and
   animation frame; and
5. no duplicate redraw is caused merely by the pending-state clear and the
   first active tick arriving together.

If the user interrupts or the agent fails while waiting, the same boundary
must capture the final wall-clock activity duration and return to the normal
idle/error surface without duplicated waiting panels.

## 6. Timing and signal model

### 6.1 Separate clock meanings

The implementation must retain these meanings:

```text
wall_elapsed     = monotonic_now - activity_start_time
display_elapsed  = active-work time shown in the status bar
activity_signal  = reactive value used to redraw a changing active status
frame            = reactive animation counter
```

`wall_elapsed` includes user-owned prompt waits. `display_elapsed` excludes
those waits. `frame` is already activity-aware under PRD-164.

The display renderer must not read `time.monotonic()` directly to decide the
visible waiting duration.

### 6.2 Recommended signal policy

During a paused prompt wait, `ConversationStore.tick()` SHOULD advance or
retain enough internal state to calculate the final wall-clock activity
duration, but MUST NOT publish `activity_elapsed_s` as a visible timer signal
on every tick.

The preferred implementation is:

```text
active tick:
    update display clock
    publish activity_elapsed_s
    publish frame when animation is active

paused tick:
    advance the display-clock baseline without charging wait time
    do not publish activity_elapsed_s
    do not publish frame

prompt resolves:
    establish the active display baseline
    publish one current activity value if needed
    resume normal active ticks

activity ends while paused:
    calculate and persist the final wall-clock duration once
```

The exact private representation may differ, but a prompt wait must not emit a
20 Hz reactive update whose rendered value is intentionally unchanged.

### 6.3 Workspace defense in depth

The workspace MUST also guard the activity-timer redraw path while
`pending_approval` is non-`None`. This protects against compatibility code,
manual signal writes, or a future timing implementation that publishes the
clock while paused. The guard must not suppress redraws from the pending
signal, input signals, overlay state, or resize handler.

The activity signal should have a named callback or redraw reason rather than
being silently removed from the workspace. This keeps ownership and future
diagnostics clear.

### 6.4 Coalescing requirement

Existing same-turn `_redraw()` coalescing remains useful but is insufficient by
itself. The acceptance test must advance time across multiple event-loop turns
and prove that prompt ticks do not call `Live.update()` repeatedly.

## 7. Proposed implementation

### 7.1 ConversationStore changes

Update `ConversationStore.tick()` and its timing helpers so the paused state
is checked before publishing the outer activity timer signal. Preserve the
monotonic baseline needed to calculate the final wall-clock duration.

Required properties:

- `display_elapsed_s` remains unchanged while paused;
- `activity_elapsed_s` has no per-tick subscriber notifications while paused;
- `frame` has no per-tick subscriber notifications while paused;
- `elapsed_s` or the existing completion payload still includes the wait when
  the turn/activity completes;
- repeated `set_display_paused(True)` and repeated pending-request writes are
  idempotent;
- the first active tick after a wait does not double-count the wait interval in
  `display_elapsed_s`.

If the implementation needs an internal unreactive monotonic value, it must
remain private to the conversation timing owner and must not be exposed as a
second public timer source.

### 7.2 Workspace changes

Route `activity_elapsed_s` through a small dedicated callback or guard:

```text
activity_elapsed_s notification
        │
        ├─ pending approval exists → ignore timer-only redraw
        └─ no pending approval     → schedule normal coalesced redraw
```

The existing general signal list must continue to redraw for actual state
changes. The guard must not inspect Rich object identity or compare rendered
strings as its primary correctness mechanism; prompt ownership is already
represented by reactive state.

The existing frame guard from PRD-164 remains separate. A frame notification
and an activity-timer notification have different semantics and must not be
collapsed into one undocumented boolean.

### 7.3 Approval and overlay lifecycle

Do not add a second approval state or a second overlay owner. Continue using:

- `AppState.pending_approval` as the authoritative prompt boundary;
- `ApprovalService` as the response owner;
- `TUISession._wire_approval_overlay()` for request-kind routing;
- `OverlayHost` and `Workspace._redraw()` for rendering.

The initial pending-state transition still causes one redraw so the user sees
the prompt. The response transition still causes one redraw so the normal
state returns. Only timer-only redraws during the stable interval are
suppressed.

### 7.4 Persistence and event isolation

No new `ConversationEvent` may be created for a timer tick. The implementation
must not alter:

- session event-log ordering or replay contents;
- approval journal records;
- workflow checkpoints and phase state;
- conversation memory or provider history;
- user input or pending approval payloads.

The fix is solely a reactive presentation/timing change.

## 8. Functional requirements

### FR-165.1 — Suppress paused activity-timer publications

While a prompt is pending, repeated session ticks MUST NOT publish
`activity_elapsed_s` notifications solely because wall time advanced.

### FR-165.2 — Suppress paused Live refreshes

The workspace MUST NOT call `Live.update()` solely because a paused activity
timer tick occurred. This remains true if a compatibility path writes the
activity signal directly.

### FR-165.3 — Preserve wall-clock telemetry

The final turn/activity completion duration MUST include the time spent
waiting for approval or an answer, using the existing telemetry semantics.

### FR-165.4 — Preserve active-work display timing

The displayed duration used by waiting overlays MUST exclude the prompt wait
and resume without double-counting when the prompt resolves.

### FR-165.5 — Preserve animation and pause behavior

The frame, flower, Thinking text, recovery indicator, and compaction spinner
remain frozen while the prompt owns the terminal and resume during active work.

### FR-165.6 — Preserve legitimate redraws

Input edits, option selection, prompt text, pending-state transitions,
notifications, workflow progress, overlays, resize, mode changes, and response
submission MUST still produce the required current render.

### FR-165.7 — Cover every prompt kind

The contract MUST apply to generic tool approvals, `plan_review`, and
`questions` requests, including rejection/answer text-entry states.

### FR-165.8 — Preserve overlay geometry

The change MUST NOT regress the fixed-height Plan Review viewport, question
overlay layout, or resize-safe Rich cursor restoration.

### FR-165.9 — Preserve event and workflow state

Timer suppression MUST NOT add, remove, duplicate, reorder, or mutate
conversation events, approval records, workflow checkpoints, or session
memory.

### FR-165.10 — Preserve non-TUI behavior

Headless, background, and non-interactive execution MUST retain their current
approval, timing, and output behavior.

## 9. Non-functional requirements

- **Performance:** A stable prompt wait produces no 20 Hz timer-driven Live
  redraw workload.
- **Determinism:** Tests use controlled monotonic clocks and explicit event-loop
  turns; they do not rely on sleeping for wall-clock durations.
- **Terminal portability:** Captured consoles, POSIX terminals, Windows
  terminals, and clients that ignore ANSI replacement controls show one stable
  waiting surface unless a real state or geometry change occurs.
- **Compatibility:** Existing public signals and timing meanings remain
  available; only paused timer publication is changed.
- **Observability:** Optional redraw diagnostics may identify `activity_timer`,
  `frame`, `input`, `overlay`, `resize`, or `state` as reasons, but must not
  log prompts, plan content, tool arguments, credentials, or user answers.
- **Safety:** Approval ownership, capability gates, and fail-closed headless
  behavior are unchanged.
- **Maintainability:** The fix remains within `ConversationStore`, `Workspace`,
  and existing approval lifecycle boundaries; no duplicate renderer is added.

## 10. Acceptance criteria

- **AC-165.1:** With a pending `plan_review`, seven controlled paused ticks
  leave `frame` and `display_elapsed_s` unchanged and produce zero
  `activity_elapsed_s` subscriber notifications.
- **AC-165.2:** With a running `Workspace`, seven paused ticks produce zero
  timer-only `Live.update()` calls after the initial waiting render.
- **AC-165.3:** A captured console or ANSI-insensitive output consumer sees
  one Plan Review surface, not one copy per tick.
- **AC-165.4:** The same no-loop behavior is verified for generic tool approval
  and `questions` prompts.
- **AC-165.5:** Changing the selected option or editing prompt text causes a
  current Live redraw while the prompt remains pending.
- **AC-165.6:** Clearing `pending_approval` causes the normal state to redraw,
  and the next active tick advances the activity/display timing and frame
  exactly according to the existing contract.
- **AC-165.7:** Wall-clock completion duration includes the full approval wait,
  while displayed active-work duration excludes it.
- **AC-165.8:** An interrupted, denied, or failed pending approval returns to
  the correct idle/error state without duplicate waiting panels or duplicate
  conversation events.
- **AC-165.9:** Resize still produces the necessary geometry repaint, and the
  fixed-height Plan Review and Questions overlay tests remain green.
- **AC-165.10:** Conversation-event counts/order, approval journal contents,
  workflow checkpoint state, and resume replay are unchanged by timer
  suppression.
- **AC-165.11:** Existing PRD-164 idle-frame, active-animation, compaction,
  transcript-replay, and captured-output regressions remain green.
- **AC-165.12:** Unit, integration, E2E, Ruff, formatting, type-audit, and
  applicable static checks pass without weakening the repository baseline.

## 11. Test plan

### 11.1 Unit tests

Add or extend tests for:

- paused ticks not notifying `activity_elapsed_s` subscribers;
- paused ticks not notifying `frame` subscribers;
- stable `display_elapsed_s` while waiting;
- final wall-clock duration including the waiting interval;
- no double-counting after repeated pause/resume edges;
- workspace activity-timer redraw suppression while `pending_approval` is set;
- compatibility activity-signal writes being ignored while waiting;
- activity-timer redraws proceeding during ordinary active work;
- Plan Review, Questions, and generic approval status rendering.

### 11.2 Integration tests

Use a real `AppState`, `Workspace`, and recording/non-TTY Rich console to verify:

- initial approval render occurs;
- controlled paused ticks do not invoke `Live.update()`;
- selection and prompt-text signals still invoke coalesced redraws;
- pending-state clearing returns to the active/idle surface;
- resize redraws remain responsive and do not leak stale Live geometry;
- no conversation or workflow persistence writes are generated by ticks.

### 11.3 End-to-end tests

Cover these user journeys:

1. Enter Plan mode, generate a plan, and wait at Plan Review while seven or
   more ticks occur.
2. Reject a plan with feedback after the stable wait and verify the workflow
   receives exactly one response.
3. Approve a plan and verify active animation resumes without a duplicate
   waiting panel.
4. Trigger a generic side-effect approval and verify the same wait contract.
5. Trigger `ask_user()` and verify answer-entry redraws remain responsive.
6. Interrupt or deny a pending prompt and verify clean return to the correct
   status without duplicate events.
7. Replay/resume a session containing an in-progress workflow and verify that
   the pending notification or prompt surface is not multiplied by ticks.

Tests must inspect normalized output and Live update counts, not merely rely
on a human terminal rendering ANSI control sequences correctly.

## 12. Rollout and compatibility

The change is local to the TUI timing and redraw pipeline and requires no
storage migration. Existing sessions, workflow checkpoints, approval journals,
and provider conversations remain readable without conversion.

The implementation should ship without a feature flag after the captured
console and interactive resize tests pass. If a temporary redraw diagnostic is
added, it must be disabled by default, omit sensitive content, and be removed
or documented before release.

If a third-party component directly depends on receiving an activity-timer
signal every 50 ms during an approval wait, that is not a supported rendering
contract. It must use an explicit unpaused timer or subscribe to the raw
monotonic activity owner instead.

## 13. Security and privacy

No new network, filesystem, subprocess, or credential behavior is required.
The implementation MUST NOT log plan text, tool arguments, approval payloads,
user answers, session identifiers, or provider credentials while diagnosing
redraw reasons. Reducing repeated Live writes also reduces accidental exposure
of transient prompt content in captured terminal output.

## 14. Documentation updates required with implementation

When implemented, update:

- `docs/guides/tui.md` with the distinction between idle-frame suppression and
  approval-wait timer suppression;
- `README.md` if the user-visible terminal behavior is documented there;
- `docs/guides/architecture.md` with the timing-signal ownership contract;
- `prds/prd-164-repeated-idle-status-frames.md` with a cross-reference noting
  that approval waits use the separate PRD-165 guard;
- this PRD's status and implementation evidence with exact verification
  results.

## 15. Verification commands

At minimum, run:

```bash
uv run pytest tests/unit/test_unified_tick.py \
  tests/unit/test_waiting_prompt_resize.py \
  tests/unit/test_workspace_redraw.py -q
uv run pytest tests/integration tests/e2e -q
uv run pytest tests/ -q
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
git diff --check
```

## 16. Open decisions

1. Whether to keep `activity_elapsed_s` as a public reactive signal that is
   merely silent while paused, or split its internal wall-clock accumulator
   from the visible signal. The implementation must preserve existing public
   consumers and choose the smallest safe boundary.
2. Whether redraw diagnostics should be enabled only in tests or exposed as a
   short-lived developer option. Any diagnostic must be content-free.
3. Whether the workspace should expose a general redraw-reason enum now or
   keep the first implementation to dedicated activity/frame callbacks.

The recommended default is to keep the existing signals and add the smallest
pause-aware publication and workspace guard needed to satisfy the acceptance
criteria.
