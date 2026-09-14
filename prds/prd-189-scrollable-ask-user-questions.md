---
title: "PRD-189: Scrollable Ask User Questions"
status: Implemented
version: 1.0.0
created: 2026-09-13
scope: "QuestionsOverlay rendering, keyboard navigation, and long-question TUI usability"
related_prds:
  - PRD-86   # prompt-overlay text input and editing contract
  - PRD-144  # resize-safe waiting modals and pause-aware display timing
  - PRD-161  # exploratory TUI presentation and transcript rendering
  - PRD-165  # approval-wait redraw suppression
  - PRD-188  # durable workflow resume and interactive recovery context
tags:
  - tui
  - overlays
  - ask-user
  - questions
  - scrolling
  - accessibility
---

# PRD-189 — Scrollable Ask User Questions

## 1. Executive summary

The `ask_user` tool can submit a question whose text is much longer than the
terminal width. The TUI currently renders that text as one unbounded line in
`QuestionsOverlay`. Rich cannot display the complete line in the available
viewport, and the overlay provides no way to move through the hidden content.
The user therefore cannot reliably read the question before choosing an
answer.

This PRD adds a bounded, scrollable question-text viewport to
`QuestionsOverlay`. It is modelled on the existing scrollable plan content in
`PlanApprovalOverlay`: question text is pre-rendered into terminal-width-aware
lines, the viewport has a stable height, the current offset is clamped, and a
visible indicator describes hidden content. The same question remains fully
available while the user selects an option or types an `Other` answer.

The change is presentation-only. The `ask_user` input schema, question IDs,
option values, answer JSON, approval service, durable conversation, and agent
tool contract remain unchanged.

## 2. Evidence-backed current state

### 2.1 Ask User data flow

The current interactive path is:

```text
agent calls ask_user(questions=[...])
        │
        ▼
ApprovalRequest(kind="questions", tool_input={"questions": [...]})
        │
        ▼
ApprovalService publishes the pending request
        │
        ▼
OverlayHost shows QuestionsOverlay
        │
        ├── SELECTING: choose a predefined option or Other
        └── TYPING: enter the Other answer
```

`QuestionsOverlay` parses each question into the existing `Question` model and
keeps answer/cursor state in `_QState`. The overlay responds through
`ApprovalService.respond()` only after the user confirms all answers or
cancels. No LLM or workflow code needs to know how the question is displayed.

### 2.2 Current rendering defect

In `src/agenthicc/tui/workspace/overlays/questions.py`, the selecting state
currently emits:

```python
lines.append(Text(f"  {q.text}"))
```

The typing state has the same unbounded rendering behavior. A long single-line
question is not wrapped into the available content width, and a multi-line
question has no viewport or offset. The options viewport can scroll, but that
scroll state only applies to options and cannot reveal question text.

The result is especially problematic for generated questions containing a
workflow decision, requirements summary, long path, policy explanation, or a
large list of constraints. The text may be visually clipped while the answer
options remain visible, making the question ambiguous.

### 2.3 Existing approval-overlay pattern

`PlanApprovalOverlay` already solves the same class of problem for long plan
content. It:

1. renders Markdown into a flat list of terminal lines at the current width;
2. stores a scroll offset;
3. renders only a fixed-size viewport;
4. pads short content to preserve the Live block height;
5. displays `↑ · lines X–Y of N · ↓` when content overflows;
6. changes the offset with `[` and `]`; and
7. clamps or rebuilds state after terminal resizing.

`ApprovalOverlay` also demonstrates bounded scrolling for selectable options.
PRD-189 applies these proven UI invariants to question text without changing
the existing option-navigation keys.

## 3. Problem statement

An end user must be able to read the complete question that an agent asks
before providing an answer. The current overlay assumes that question text is
short enough for one terminal row. That assumption is false for realistic
agent-generated questions and creates an accessibility and correctness defect:

* important text is clipped horizontally;
* there is no indication that content is hidden;
* `↑` and `↓` already belong to option navigation, so they cannot be reused
  for question scrolling;
* changing questions must not accidentally change their selected options or
  answer state; and
* a terminal resize must not cause stale lines to bleed through the Rich Live
  region.

## 4. Goals

The implementation MUST:

1. Display long and multi-line question text without horizontal clipping by
   wrapping it to the available terminal width.
2. Let the user reveal every rendered question line while remaining in the
   question overlay.
3. Keep question scrolling independent from option selection, question
   navigation, free-text editing, and answer submission.
4. Preserve a stable overlay height across scrolling, question changes, and
   short-versus-long question content.
5. Preserve the complete question text exactly for display semantics, while
   treating it as literal text rather than Rich markup.
6. Recompute wrapping and viewport bounds when the terminal width or height
   changes.
7. Make the scroll affordance discoverable in the overlay hint and indicator.
8. Keep the existing `ask_user` request and response contracts backwards
   compatible.
9. Reuse the rendering and state-management principles already established by
   `PlanApprovalOverlay`.
10. Add unit, integration, and end-to-end regression coverage for long
    questions in both selecting and typing states.

## 5. Non-goals

PRD-189 does not:

* change the `ask_user` tool schema;
* change question IDs, option values, answer JSON, or `ApprovalService`;
* change which questions or options an agent is allowed to submit;
* change `Enter`, `Esc`, `←`, `→`, `↑`, or `↓` behavior for existing flows;
* make the question text editable in SELECTING mode;
* add a second conversation, transcript, or workflow state store;
* alter `ApprovalOverlay` authorization decisions;
* change plan-review semantics or provider behavior; or
* silently truncate the question as a substitute for scrolling.

## 6. User-facing contract

### 6.1 Selecting state

The selecting view continues to show:

* the Questions header and answered-count;
* the current question number and navigation indicators;
* the current question text;
* the option viewport;
* the existing option cursor and answer markers; and
* the existing Enter/Esc behavior.

The question text becomes a fixed-height viewport. A short question is shown
in full and padded with blank rows. A long question is wrapped and shows a
scroll indicator such as:

```text
  ↑ · lines 6–17 of 43 · ↓
```

The exact glyphs may follow the plan-review overlay, but the indicator MUST
communicate whether content exists above or below the current viewport and the
visible range/total. The question viewport MUST never display an arbitrary
ellipsis in place of undisplayed text; the user must be able to scroll to it.

### 6.2 Scrolling controls

Question scrolling in SELECTING mode MUST use the same controls as the
scrollable plan-review content:

* `[` moves the question viewport up by one rendered line; and
* `]` moves the question viewport down by one rendered line.

The controls are deliberately separate from option navigation: `↑` and `↓`
remain option navigation, while `←` and `→` remain question navigation. The
hint line MUST advertise `[/] scroll` when the question has more rendered
lines than the viewport. The scroll indicator MUST show whether more content
exists above or below the current view.

In TYPING mode, `Page Up` and `Page Down` MUST scroll the question viewport by
one viewport. The ordinary `[` and `]` characters MUST remain insertable into
the free-text answer, so they are not intercepted in that mode. This preserves
the ability to provide answers containing brackets while keeping `[`/`]` the
canonical controls for the option-selection view. The typing hint MUST expose
the page controls when scrolling is available.

Scrolling at either boundary is a no-op. It MUST not submit, cancel, change an
answer, or move the option cursor.

### 6.3 Question navigation

Each question has its own question-scroll offset. Moving from question A to
question B MUST NOT alter either question's answer or option cursor. The
implementation SHOULD preserve each question's last scroll position during
the current overlay lifetime, clamping it if the terminal is resized.

On first display, every question starts at its first rendered line. When the
user returns to a previously viewed question, its prior offset may be restored
so the user can continue reading where they left off.

### 6.4 Free-text (`Other`) mode

When the user selects `Other`, the typing view MUST retain the scrollable
question viewport. The prompt/input row and Enter/Esc behavior remain those of
`PromptOverlay`:

* ordinary characters, including `[` and `]`, edit the answer buffer;
* Page Up/Page Down scroll only the question viewport;
* Enter confirms a non-empty answer; and
* Esc returns to SELECTING without submitting.

The complete typed answer must continue to be submitted in the existing answer
JSON. Rendering a shortened preview of a previously entered answer must not
modify the stored answer.

### 6.5 Small terminals

The layout MUST remain usable when the terminal is too short to show all
question lines and options simultaneously. A layout-budget helper MUST reserve
at least one row for the question viewport, at least one row for the option
viewport, the existing chrome, and the existing hint/footer rows. Remaining
space may be allocated to the question and options according to a documented,
deterministic policy.

The implementation MUST NOT emit a variable-height overlay that causes old
Rich Live rows to remain visible after a redraw. If the terminal is below the
minimum usable height, the overlay may show the minimum one-row viewports and
the existing hints; it must remain navigable and must not crash.

## 7. Functional requirements

### FR-1: Terminal-width-aware line preparation

Implement a question-line preparation path that:

* wraps each question to the actual content width after indentation;
* preserves explicit newlines and blank paragraphs;
* handles Unicode and wide characters using Rich's terminal-aware measurement;
* produces a stable `list[Text]` or equivalent line representation;
* treats question content as literal text and does not interpret model text as
  Rich markup; and
* caches the result by question text and render width.

Markdown rendering is not required for questions. The default behavior should
be plain text wrapping, because the question is user-facing model content and
must not gain formatting or markup side effects merely because it contains
brackets.

### FR-2: Per-question viewport state

Extend the existing per-question state with a bounded question-scroll offset,
or introduce an equivalent presentation-only structure. The offset MUST NOT
be part of the answer payload or durable workflow state.

The state API MUST provide:

* first line;
* last valid offset;
* one-line movement;
* page movement; and
* clamping after cache rebuild or viewport-size changes.

### FR-3: Fixed-height rendering

The selecting and typing renderers MUST calculate their layout budget before
emitting rows. The question viewport MUST render exactly the allocated number
of rows on every redraw, padding with blank `Text` rows when necessary.

The selecting renderer MUST keep the options viewport height-stable as well.
Changing question text, scroll position, selected option, or answered state
must not change the total Live-region height except where the existing terminal
size budget changes.

### FR-4: Scroll indicator and hints

When overflow exists, render a stable indicator row showing the direction(s)
available and the visible line range. When there is no overflow, render a blank
placeholder row if needed to preserve height.

Hints MUST distinguish:

* option controls (`↑↓`);
* question navigation (`←→`);
* question scrolling (`[/]` in SELECTING and PageUp/PageDown in TYPING); and
* submission/cancellation.

### FR-5: Resize handling

The renderer MUST invalidate prepared question lines when the content width
changes. It MUST rebuild at the new width, clamp every question's offset, and
recalculate the available question/option viewport rows from the new terminal
height.

Resize handling MUST be pure presentation state. It must not call the approval
service, alter answers, re-run the agent, or create a new question request.

### FR-6: Input dispatch precedence

Question-scroll keys MUST be handled before generic prompt-buffer dispatch only
when the overlay is in a mode where those keys are reserved for scrolling.
Specifically:

* SELECTING reserves `[` and `]` for question scrolling;
* TYPING reserves only `Page Up` and `Page Down`; and
* TYPING passes `[` and `]` to `PromptOverlay._handle_prompt_key()`.

All existing keys not assigned to scrolling retain their current behavior.

### FR-7: No answer or persistence changes

`QuestionsOverlay._submit()` MUST continue to produce the same mapping of
question ID to answer and call the same `ApprovalService.respond()` method.
Scroll state MUST never be serialized into:

* the tool result;
* conversation journal records;
* workflow checkpoints;
* session transcripts; or
* cassette approval records.

## 8. Proposed implementation design

### 8.1 Shared scrollable-text primitive

Prefer extracting the common algorithm currently embodied by
`PlanApprovalOverlay` into a small overlay-local helper, for example
`ScrollableTextViewport`. It should own line preparation, width-keyed cache,
visible-line slicing, offset movement, indicator metadata, and clamping. If an
extraction would create unnecessary churn, `QuestionsOverlay` may implement
the same contract locally first, but the behavior and invariants MUST match
plan review.

The helper MUST not own input dispatch or approval responses. The owning
overlay remains responsible for selecting the correct key behavior and
rendering its chrome.

Conceptual data flow:

```text
Question.text + terminal width
          │
          ▼
ScrollableTextViewport.prepare()
          │
          ├── wrapped literal Text lines
          ├── total line count
          ├── per-question offset
          └── fixed-size visible slice + indicator
          │
          ▼
QuestionsOverlay.render()
```

### 8.2 Width and indentation

The viewport should prepare content at the width available after the question
indent and any safety margin. Rendering should prepend the display indent only
after line preparation, as `PlanApprovalOverlay` does for plan lines. This
prevents the indent from being counted twice and ensures the last visible
character fits the terminal.

### 8.3 Height allocation

Replace the current implicit “one question row plus options” assumption with a
named layout-budget calculation. The calculation should account for:

* workspace chrome outside the overlay;
* QuestionsOverlay header, navigation, separators, border, and hint rows;
* question viewport rows and its indicator row;
* option viewport rows; and
* the minimum rows required for both content areas.

The returned budget must be deterministic for a given terminal width, height,
question count, and maximum option count. Unit-test the budget independently so
future chrome changes cannot silently make the Live block taller than the
terminal.

### 8.4 TYPING layout

Use the same prepared question lines and viewport state in `_render_typing()`.
The answer prompt remains a single-line editor. The viewport may use a
different fixed row allocation from SELECTING because the option list is not
visible, but it must still be height-stable and must reserve rows for the
prompt, borders, and hints.

### 8.5 Plan approval compatibility

If the shared helper is introduced, migrate `PlanApprovalOverlay` without
changing its visible controls, plan rendering, or approval behavior. Existing
plan-review tests must remain authoritative for bracket scrolling, fixed
height, indicator boundaries, and resize invalidation. A staged extraction is
acceptable if PRD-189's QuestionsOverlay behavior is delivered first and the
shared helper is covered before use by both overlays.

## 9. Acceptance criteria

### AC-1: Long single-line question is readable

Given a question longer than the terminal width, the selecting overlay wraps
it into multiple rows, shows an overflow indicator, and allows the user to
reach the final rendered line. No question content is silently discarded.

### AC-2: Explicit newlines and blank lines survive

Given a question containing multiple paragraphs, explicit newlines, and blank
lines, the rendered viewport preserves their ordering and the user can scroll
through all of them.

### AC-3: Scroll boundaries are safe

`[`/`]` in SELECTING mode and Page Up/Down in TYPING mode stop at the first
and last valid offsets. Repeated boundary presses do not wrap, alter the
option cursor, select an option, or submit a response.

### AC-4: Existing option navigation is unchanged

`↑`, `↓`, `←`, `→`, Enter, and Esc retain their current behavior for one or
multiple questions. Scrolling a question does not mutate `_QState.answer`,
`answered`, or `cursor`.

### AC-5: Per-question offsets are isolated

Scrolling question 1, moving to question 2, and returning to question 1 does
not change either question's answer or cursor. The implementation either
restores the prior offset or explicitly resets it to a valid offset according
to the documented policy.

### AC-6: Other-answer typing remains correct

In TYPING mode, Page Up/Down scrolls the question, while `[` and `]` are
inserted into the answer buffer. Enter and Esc retain their existing submit and
back behavior, and the full answer is returned unchanged.

### AC-7: Fixed-height Live rendering

For a fixed terminal size, rendering the same question at the first, middle,
and last scroll offsets produces the same number of terminal rows. Switching
between short and long questions also produces the same row count for the same
layout budget.

### AC-8: Terminal resize is safe

After width or height changes, the question is rewrapped, the offset is
clamped, the indicator is correct, and no stale rows remain in the Live
region. The overlay remains usable at the smallest supported terminal size.

### AC-9: Literal rendering is safe

Question text containing Rich markup-looking content such as `[red]` or
backticks is displayed as question text and cannot inject styling or alter the
overlay layout through markup parsing.

### AC-10: Answer contract is unchanged

For identical key sequences and question payloads, the `ApprovalService`
response remains byte-for-byte equivalent apart from no new scroll metadata.
Scroll-only input never causes a response.

### AC-11: Layered regression coverage

The implementation has:

* unit tests for wrapping, Unicode/newlines, viewport movement, clamping,
  layout budgeting, literal rendering, and key precedence;
* integration tests for `OverlayHost` redraws and `QuestionsOverlay` service
  interaction; and
* an E2E test that renders a long question, scrolls to its end, selects an
  option, and verifies the same answer payload as before.

## 10. Test plan

### Unit tests

Extend or add tests near `tests/unit/test_tui_overlays_coverage.py` and create
a focused `tests/unit/test_questions_overlay.py` if the existing file becomes
too broad. Cover:

1. one-line, wrapped, multi-paragraph, empty, and Unicode questions;
2. the line cache and invalidation when width changes;
3. first/middle/last offsets and page/line movement;
4. per-question offset isolation;
5. fixed-height output across offsets and question lengths;
6. minimum-height layout allocation;
7. SELECTING `[`/`]` interception;
8. TYPING PageUp/PageDown interception and bracket insertion;
9. option cursor/answer immutability during scroll; and
10. literal Rich-looking text.

### Integration tests

Use a real `OverlayHost`, `AppState`, `ApprovalRequest`, and a deterministic
terminal-size fixture to verify that:

* each handled scroll key causes a redraw but not a service response;
* a final Enter still produces the existing answer JSON;
* resize-triggered renders do not call the service; and
* the overlay remains the active modal until submit/cancel.

### E2E tests

Render the overlay through the same Rich Live-facing path used by the TUI.
Exercise a long generated question that describes a decision in enough text
to exceed the terminal height, scroll to the end, return to the beginning,
choose an option, and verify the response. Repeat with `Other` and an answer
containing `[` and `]` to protect typing-mode precedence.

The E2E test must assert visible text and service payload, not only internal
offset fields.

### Quality gates

Run the relevant focused checks first, then the repository gates:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run pytest tests/unit/test_questions_overlay.py tests/unit/test_tui_overlays_coverage.py -q
uv run pytest tests/integration tests/e2e -q
```

Any existing environment blocker must be reported rather than weakening the
scrolling contract or skipping the new regression tests.

## 11. Compatibility, security, and accessibility

The feature is backwards compatible because it changes only the rendering and
input handling of the modal while preserving the tool request and response
schemas. Headless and cassette modes do not render the overlay and therefore
must remain unchanged.

Question text is untrusted model output. It must be rendered as escaped/plain
Rich `Text`; no markup parsing, hyperlinks, terminal escape sequences, or
filesystem/network actions may be introduced by the viewport. The existing
terminal-width and Rich measurement behavior remains the single source of
layout truth.

The range indicator and keyboard hint are required accessibility affordances:
they make hidden content discoverable without relying on color or cursor
position. Selected options must remain distinguishable from unselected options
when the user is scrolled through question text.

## 12. Rollout and observability

No migration is needed. The change can ship behind no feature flag because
short questions retain the existing visual structure and scroll state is
presentation-only. If a feature flag is desired for staged rollout, it must
default to enabled and must not create two answer or approval code paths.

No new persistent telemetry is required. Local debug logging, if added, must
record viewport dimensions and offsets only; it must not log complete question
text or typed answers because they may contain secrets or private project
requirements.

## 13. Implementation checklist

- [x] Add the scrollable question-line preparation and width cache.
- [x] Add per-question scroll offsets and a deterministic layout-budget helper.
- [x] Render fixed-height question viewports in SELECTING and TYPING states.
- [x] Add clamped SELECTING `[`/`]` scrolling and TYPING PageUp/PageDown
  scrolling without intercepting bracket characters in answers.
- [x] Preserve bracket characters in TYPING answers.
- [x] Add resize invalidation and stale-row regression coverage.
- [x] Keep question text literal and answer/persistence contracts unchanged.
- [x] Add unit, integration, and E2E tests from the test plan.
- [x] Update user-facing TUI documentation if the key hints are documented
  outside the overlay.
- [x] Record implementation evidence and verification commands in this PRD,
  then change status to `Implemented` only after all acceptance criteria pass.

## 14. Definition of done

PRD-189 is complete when all acceptance criteria are demonstrated by current
tests and rendered-output assertions, the full question is readable at
narrow terminal widths, scrolling is bounded and discoverable in both overlay
states, terminal resizing is stable, no answer contract changes are observed,
and the implementation evidence is recorded here and in the relevant TUI guide.

## 15. Implementation evidence

Implemented on 2026-09-13.

The implementation is in:

* `src/agenthicc/tui/workspace/overlays/questions.py` — width-aware literal
  question wrapping, per-question offsets, fixed-height viewports, range
  indicators, resize invalidation/clamping, `[`/`]` selection scrolling, and
  PageUp/PageDown typing scrolling;
* `tests/unit/test_questions_overlay_scroll.py` — wrapping, literal rendering,
  scroll boundaries, stable height, resize behavior, and typing-mode bracket
  preservation;
* `tests/integration/test_questions_overlay_scroll.py` — OverlayHost and
  ApprovalService response flow; and
* `tests/e2e/test_waiting_modal_resize_e2e.py` — full Workspace rendering and
  long-question answer journey.

Verification completed:

```text
10 passed focused PRD-189 unit, integration, and E2E tests
58 passed broader approval, plan-review, overlay, integration, and E2E tests
mypy src/agenthicc/tui/workspace/overlays/questions.py: Success
ruff check and ruff format: passed
```
