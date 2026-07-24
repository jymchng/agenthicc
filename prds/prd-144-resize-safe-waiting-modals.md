---
title: "PRD-144: Resize-Safe Waiting Modals and Pause-Aware Display Timing"
status: Implemented
version: 1.0.0
created: 2026-07-24
related_prds:
  - PRD-73   # Workspace layout
  - PRD-78   # Approval system
  - PRD-86   # Plan approval overlay
  - PRD-88   # Plan approval scrolling
  - PRD-100  # Questions overlay
  - PRD-120  # Unified animation tick
  - PRD-143  # Safe commands during active runs
tags:
  - tui
  - rich-live
  - resize
  - approvals
  - questions
  - plan-review
  - rendering
supersedes: []
---

# PRD-144 — Resize-Safe Waiting Modals and Pause-Aware Display Timing

Study date: 2026-07-24. This PRD defines and implements a robust rendering and
timing contract for the interactive TUI while the LLM is blocked on an
approval, plan review, or user question.

## 1. Executive summary

The current TUI already pauses its animation frame tick while
`AppState.pending_approval` is non-null. That prevents the Thinking flower and
word animation from advancing during a modal wait. However, the status
component still reads `ConversationStore.elapsed_s` during every render, and
that property computes `time.monotonic() - _start_time` at render time.

Terminal resize is a legitimate reason to rebuild the Rich Live block. During
a pending Plan Review, each resize therefore rebuilds a visually identical
modal with a newly rounded seconds/minutes timer. The changing timer makes
otherwise static content look like a new frame and can interact badly with
Rich's cursor-shape restoration, producing repeated Plan Review headers or
stale Live-region geometry.

PRD-144 separates two concepts:

- wall-clock turn duration, retained for telemetry and `turn_complete` events;
- a cached, pause-aware display duration, used by the status bar.

While a user prompt is pending, the display duration and animation frame are
frozen. A resize may still perform exactly one geometry repaint, but it cannot
advance the displayed timer or create duplicate modal content. When the user
responds, the normal active display clock and animation resume through the
existing event-loop ownership.

## 2. Evidence and current ownership

The current source establishes these facts:

| Concern | Current implementation | Finding |
|---|---|---|
| Prompt state | `src/agenthicc/tui/conversation_store.py` → `AppState.pending_approval` | One signal covers tool approvals, `kind="plan_review"`, and `kind="questions"`. |
| Animation | `src/agenthicc/runners/tui_session.py` → `_tick()` and `ConversationStore.tick(paused=...)` | The frame tick is already paused while a prompt is pending. |
| Status rendering | `src/agenthicc/tui/workspace/components.py` → `StatusComponent.render()` | Reads `conv.elapsed_s` during every render. |
| Wall-clock duration | `ConversationStore.elapsed_s` | Computes from `time.monotonic()` on demand; it is not a signal or cached display value. |
| Live redraw | `src/agenthicc/tui/workspace/workspace.py` → `_redraw()` | Coalesces signal redraws and debounces SIGWINCH, then explicitly updates Rich Live. |
| Resize restoration | `Workspace._reset_live_after_resize()` | Restores the prior cursor shape and clears Rich's cached shape before repainting. |
| Plan modal | `src/agenthicc/tui/workspace/overlays/plan_approval.py` | Uses a width-sensitive, padded viewport intended to keep height stable. |
| Question modal | `src/agenthicc/tui/workspace/overlays/questions.py` | Uses a terminal-height-derived fixed options area. |

The diagnosis is therefore evidence-backed: animation pausing does not pause
the wall-clock property read by a resize-triggered render. The duplicate
header is treated as a rendering symptom to prevent and test; the PRD does
not assume that the timer is the only Rich cursor bug.

## 3. Problem statement

When the LLM is waiting for a user response:

1. the model is not producing new output;
2. the approval/question overlay is the only interactive surface;
3. the animation frame is already intentionally static; but
4. terminal geometry changes force a Live rebuild whose status timer changes
   solely because wall time elapsed between renders.

This creates avoidable visual churn and makes it difficult to distinguish a
real state transition from a resize repaint. In the reported Plan Review
case, the terminal can show the same header more than once after height
adjustment.

The fix must preserve resize responsiveness. A smaller terminal can change
the visible plan viewport, wrapped Markdown, question options, footer fit, or
overlay scroll bounds; suppressing all resize repaints would be incorrect.

## 4. Goals and non-goals

### Goals

- Make waiting-modal renders deterministic when no meaningful state or
  geometry has changed.
- Freeze the status bar's displayed active duration while a prompt owns the
  terminal.
- Keep wall-clock turn duration available for persistence, telemetry, and
  `turn_complete` rendering semantics.
- Keep the existing one-Live-block workspace and Rich cursor restoration
  ownership.
- Allow one debounced geometry repaint per settled terminal resize.
- Prevent duplicate Plan Review or Questions headers during resize storms.
- Resume the active display clock and animation exactly once after the user
  responds.
- Cover tool approval, plan review, and question prompts with unit,
  integration, and end-to-end tests.

### Non-goals

- Changing approval, denial, plan-review, or `ask_user()` semantics.
- Changing provider cancellation, retry, timeout, or workflow behaviour.
- Removing SIGWINCH handling or making the Live block static during all
  terminal changes.
- Adding a second renderer, transcript, timer service, event bus, or
  persistence format.
- Changing the meaning of persisted wall-clock `elapsed_s` events without an
  explicit migration decision.
- Changing headless mode, background worker rendering, or CLI output.

## 5. User-facing contract

### 5.1 Waiting state

The status line uses a stable label while `pending_approval` is non-null:

| Request kind | Status label |
|---|---|
| `tool` or unknown approval kind | `Waiting for approval` |
| `plan_review` | `Waiting for plan approval` |
| `questions` | `Waiting for your answer` |

The flower, Thinking text, and compaction spinner do not advance during this
state. The displayed active duration remains at the value captured when the
wait began. The UI may show a static duration beside the label, but it must
not display a live waiting timer that causes periodic redraws.

### 5.2 Resize behavior

Resize remains responsive:

1. SIGWINCH bursts are debounced by the existing workspace mechanism;
2. Rich's previous cursor geometry is restored once for the settled resize;
3. overlay width/height caches are invalidated and rebuilt for the new size;
4. the Live block receives one current renderable; and
5. the status duration and prompt content are otherwise identical when the
   new geometry does not change them.

If a resize genuinely changes wrapping or viewport height, the plan or
question content may change. The modal header still appears exactly once in
the resulting Live render.

### 5.3 Response behavior

When the approval or answer is submitted:

- the pending state clears through the existing `ApprovalService` path;
- the next active tick advances the display clock and animation;
- the status changes back to the normal run state; and
- the next render is not duplicated merely because both the pending signal
  and frame/timing state changed.

## 6. Timing model

### 6.1 Separate wall and display clocks

The implementation must retain two explicit values:

```text
wall_elapsed = monotonic_now - turn_started_monotonic
display_elapsed = active_work_time shown by the status component
```

`wall_elapsed` remains the source for turn completion diagnostics unless a
separate product decision changes it. `display_elapsed` is a reactive,
cached UI value owned by the existing conversation store or its current
session timing owner. `StatusComponent.render()` must not call
`time.monotonic()` to derive a visible timer.

### 6.2 Wait transitions

The pending-approval signal is the authoritative transition source. The
implementation should make the transition idempotent:

```text
pending_approval: None → request
    capture display_elapsed
    mark display clock paused

pending_approval: request → None
    mark display clock active
    resume on the next normal tick
```

Repeated signal writes, approval overlay replacement, or a resize while
paused must not accumulate pause intervals twice. If a new request replaces
an old request in the same event-loop turn, the state machine must not briefly
resume animation between them.

The initial implementation should exclude user-wait time from
`display_elapsed`, because the displayed metric is explicitly active work
time. Wall-clock duration remains available for the existing `turn_complete`
payload. If product review chooses to include wait time later, it must still
be advanced only by the normal timing tick, never by arbitrary rendering.

## 7. Rendering and invalidation design

### 7.1 Cached status snapshot

`StatusComponent` should consume a stable timing/wait snapshot rather than
deriving time during `render()`. The snapshot should include at least:

- display elapsed seconds;
- whether a user prompt is pending;
- prompt kind or stable waiting label; and
- current animation frame.

The snapshot may remain a set of existing reactive fields rather than a new
public class if that preserves the current ownership boundary. The important
invariant is that render is a pure projection of state plus terminal
geometry.

### 7.2 Dirty reasons

If the current callback shape makes it impossible to distinguish a timer tick
from a resize, introduce a small internal invalidation reason in
`workspace.py` rather than adding a second renderer. Valid reasons are:

- state/content change;
- active timing/animation tick;
- overlay interaction; and
- terminal geometry change.

While waiting, timing/animation ticks are suppressed. Geometry invalidation
still runs, but it must not mutate timing state. A settled resize must call
the existing cursor restoration once and update the Live block once.

### 7.3 Stable overlay height

Plan Review and Questions overlays must continue to pad their variable
content to a deterministic height for the current terminal geometry. A
resize invalidates the width/height cache and rebuilds it; it must not append
the overlay to scrollback or start a second Live block.

## 8. Failure handling and invariants

- If the pending request is malformed or has an unknown kind, use the stable
  generic approval label and still pause timing.
- If a resize occurs before the first modal render, the first settled render
  must still contain one complete modal.
- If approval submission and SIGWINCH occur in the same event-loop turn, the
  final render must reflect the latest state and contain no stale modal.
- If the terminal is non-interactive, existing headless behavior is unchanged.
- If Rich internals are unavailable, the existing best-effort resize fallback
  remains safe and must not mutate the timing snapshot.
- A cancelled/error turn must close the pending state and restore normal
  timing/animation cleanup through the existing idempotent paths.
- No timer callback may write directly to the terminal.
- `turn_complete` must be emitted at most once and retain an explicit,
  documented elapsed-duration source.

## 9. Rollout

### Phase 1 — Timing contract

- Add the display-clock state and pending-wait transition invariant.
- Keep wall-clock event payloads unchanged.
- Add unit tests for pause, resume, duplicate transitions, and malformed
  request kinds.

### Phase 2 — Status and Live integration

- Switch the status component to the cached display snapshot.
- Ensure waiting labels are stable and animation ticks are suppressed.
- Add resize and same-size repaint tests for both modal types.

### Phase 3 — Interactive verification

- Exercise a slow fake provider with tool approval, plan review, and
  `ask_user()` in a PTY or deterministic terminal harness.
- Send repeated SIGWINCH events while the modal is open.
- Verify one Live update per settled resize and one modal header in the
  captured terminal output.

### Phase 4 — Regression gate

- Run the unit, integration, and E2E suites plus Ruff, format, mypy,
  type-audit, and documentation checks.
- Record any platform-specific terminal limitation rather than weakening the
  timing or resize invariant.

## 10. Acceptance criteria

- **A1** — Tool approvals, plan reviews, questions, and unknown approval kinds
  all enter one explicit waiting state.
- **A2** — While waiting, the animation frame and status display duration do
  not advance merely because time passes or the terminal is resized.
- **A3** — `StatusComponent.render()` is a pure projection and does not read
  a wall clock to mutate or derive its visible timer.
- **A4** — Wall-clock turn duration remains available for existing completion
  diagnostics, with its source documented and tested.
- **A5** — A resize storm is debounced and causes at most one settled Live
  update after cursor restoration.
- **A6** — Resizing a pending Plan Review produces exactly one Plan Review
  header in the resulting render and no stale duplicate header in scrollback.
- **A7** — Resizing a Questions overlay preserves the current question,
  answer, and selection while recalculating only geometry-dependent layout.
- **A8** — Approval/answer submission resumes timing and animation exactly once
  and does not duplicate the next status or modal render.
- **A9** — Approval service behavior, workflow behavior, persistence, and
  headless output are unchanged.
- **A10** — No second Live lifecycle, terminal writer, timer service, or
  persistence owner is introduced.

## 11. Verification plan

### Implementation evidence

The implementation keeps wall-clock `ConversationStore.elapsed_s` separate from
the reactive `display_elapsed_s` signal. `AppState.pending_approval` synchronously
drives an idempotent pause/resume edge; `StatusComponent` reads only the cached
display value; and `Workspace` subscribes to that value alongside the existing
frame signal. No second Live lifecycle or terminal writer was added.

Verification is provided by:

- `tests/unit/test_unified_tick.py` for fake-clock separation, prompt
  transition idempotence, malformed kinds, and `turn_complete` telemetry;
- `tests/unit/test_waiting_prompt_resize.py` for stable waiting renders;
- `tests/integration/test_waiting_modal_timing.py` for the real
  `ApprovalService` across tool, plan, question, and unknown prompt kinds; and
- `tests/e2e/test_waiting_modal_resize_e2e.py` for actual Rich Live resize
  storms and one current modal repaint per settled burst.

The final validation run completed with `2263 passed, 15 skipped, 4 warnings`.
Ruff, formatting, mypy, the type audit, and `nox -s llms_check` also passed.

### Unit tests

- Fake monotonic time proves wall and display clocks are separate.
- `None → request → None` transitions pause and resume once.
- Repeated pending writes and request replacement do not double-count waits.
- `StatusComponent` renders identical output for repeated waiting renders at
  different wall times when geometry is unchanged.
- Unknown/malformed approval kinds use the generic stable label.
- `ConversationStore.tick()` remains monotonic when active and unchanged when
  paused.

### Integration tests

- A real `ApprovalService` opens each overlay and changes the pending signal.
- A fake active agent remains suspended while frame/timing values stay fixed.
- A settled SIGWINCH redraw updates the workspace once and restores Rich's
  cached shape once.
- Plan text wrapping changes only when terminal width changes.
- Approval response clears the modal and resumes the normal active status.

### End-to-end tests

- A slow fake provider plus plan review is exercised through the interactive
  input/session path.
- Repeated terminal height changes while the plan is pending produce one
  visible Plan Review header per current Live render, with no leaked headers
  in scrollback.
- The same scenario is run for `ask_user()` questions and tool approval.
- Non-interactive/headless tests confirm no new waiting UI or timing contract
  leaks into JSON-lines output.

## 12. Implementation ownership

| Change | Owner |
|---|---|
| Display/wall timing split | `src/agenthicc/tui/conversation_store.py` and current session timing owner |
| Pending wait transition | `AppState.pending_approval` and existing approval service boundary |
| Waiting status projection | `src/agenthicc/tui/workspace/components.py` |
| Animation tick gating | `src/agenthicc/runners/tui_session.py` and `ConversationStore.tick()` |
| Live invalidation/debounce | `src/agenthicc/tui/workspace/workspace.py` |
| Plan/question geometry | Existing `plan_approval.py` and `questions.py` overlays |
| Verification | `tests/unit/`, `tests/integration/`, and `tests/e2e/` current TUI surfaces |
| Documentation | This PRD and the current TUI/architecture guides |

Do not introduce historical `tui/app.py`, `tui/events.py`,
`tui/transcript.py`, `tools/hooks.py`, or `tools/executor.py` ownership
boundaries.

## 13. Related documentation

- [PRD-73 — Workspace Layout](prd-73-workspace-layout.md)
- [PRD-78 — Approval System](prd-78-approval-system.md)
- [PRD-86 — Plan Approval Overlay](prd-86-plan-approval-overlay.md)
- [PRD-100 — Questions Overlay](prd-100-questions-overlay.md)
- [PRD-120 — Unified Tick Frame Counter](prd-120-unified-tick-frame.md)
- [TUI guide](../docs/guides/tui.md)
- [Architecture guide](../docs/guides/architecture.md)
