# PRD-200: Beautified goal-list mutation notices

```yaml
title: "PRD-200: Beautified goal-list mutation notices"
status: Implemented
version: 1.0.0
date: 2026-09-27
scope: "TUI presentation of goal_flow append_goal and insert_goal results"
related_prds:
  - PRD-185
  - PRD-194
  - PRD-199
tags:
  - goal-flow
  - tui
  - presentation
  - accessibility
  - transcript
```

## 1. Summary

The `goal_flow` workflow can append or insert goals while an agent is working.
The operation is durable and correctly checkpointed, but the TUI currently
renders the resulting event as:

```text
  ⎿ Inserted goal at 7 (13 total); continuing the current goal
```

This is mechanically accurate but visually awkward. It exposes an internal
zero-based index, uses parenthesized implementation metadata, combines two
independent status statements with a semicolon, and reads like a debug trace
rather than a polished product notification. The same problem affects
appended goals, and future renderers could accidentally display the same
mutation twice through the tool result and the derived conversation event.

This PRD defines a presentation-only redesign that makes goal-plan changes
easy to scan without changing goal scheduling, workflow state, checkpointing,
conversation memory, or provider-facing tool contracts.

## 2. Problem statement

### 2.1 User problem

When an agent discovers additional work, the user needs to understand three
things immediately:

1. the plan changed;
2. what kind of change occurred and where the new work sits; and
3. whether the currently active goal was interrupted.

The current notice makes those facts harder to parse because it presents
internal data rather than user-facing language:

```text
Inserted goal at 7 (13 total); continuing the current goal
```

The number `7` is the zero-based mutation index accepted by `insert_goal`,
while users naturally interpret positions as one-based. The parenthesized
`13 total` is compact but visually noisy, and the semicolon makes the
continuation rule look like part of the mutation description.

### 2.2 Technical problem

The durable event and the renderer currently have different responsibilities,
but the presentation boundary is not explicit enough:

* `GoalFlowRunner` persists a `goal_list_mutated` event after the checkpoint
  succeeds.
* The event contains bounded workflow metadata, including `operation`,
  `index`, `goal_count`, revision, and active-goal metadata.
* `ScrollBufferAppender` turns that event into one hard-coded line.
* The provider-facing tool result contains its own machine-oriented `message`.

The implementation must not solve a presentation problem by changing the
workflow state machine or by duplicating goal text and identifiers in the
transcript. It must also ensure that replaying a journal or resuming a session
does not produce a second visible notification for the same committed
mutation.

### 2.3 Evidence in the current tree

The current implementation is located at:

* `src/agenthicc/workflows/goal_flow/runner.py`, where the runner saves the
  checkpoint before appending `goal_list_mutated`.
* `src/agenthicc/tui/conversation_store.py`, where `goal_list_mutated` is a
  registered conversation event kind.
* `src/agenthicc/tui/workspace/appender.py`, where the current renderer emits
  the ugly line.
* `tests/unit/test_appender_tool_collapse.py`, where the existing assertion
  intentionally expects the old wording and zero-based display.

The event is already the correct source of truth for the committed operation.
The change should therefore remain at the TUI projection boundary unless
implementation evidence demonstrates that an event correlation field is
required for exact-once rendering.

## 3. Goals

### 3.1 Product goals

* Replace the debug-like one-line notice with a polished, scannable plan
  update card.
* Use human-friendly one-based positions while retaining zero-based indices
  in machine-facing APIs and durable state.
* Make insertion and append semantics understandable without exposing internal
  identifiers or full goal text.
* Clearly communicate that the active goal continues and was not implicitly
  completed or handed off.
* Keep the notice visually consistent with existing TUI tree markers, colors,
  spacing, and blank-line conventions.
* Make replay and resume produce the same presentation as the original event,
  without duplicate success notices.
* Keep the output safe for narrow terminals, non-color terminals, transcript
  capture, and accessibility-oriented text reading.

### 3.2 Engineering goals

* Preserve the existing `goal_list_mutated` event schema and durable journal
  semantics wherever possible.
* Keep presentation formatting in the appender or a small presentation helper,
  not in the workflow domain model.
* Preserve bounded output and do not add unbounded goal content to events.
* Add deterministic unit, integration, and end-to-end regression coverage.
* Make malformed or legacy event payloads render safely rather than raising
  from the scroll appender.

## 4. Non-goals

This PRD does not authorize the implementation to:

* change how `append_goal` or `insert_goal` validate, order, or schedule goals;
* change the active goal, phase, verification state, or continuation behavior;
* alter checkpoint ordering or make a mutation successful without a durable
  checkpoint;
* change the provider-facing tool names, arguments, schemas, or return values;
* expose the opaque `goal_id`, complete goal text, prompt contents, or evidence
  in the normal TUI notice;
* introduce a second goal list, conversation store, or workflow state model;
* change the meaning of the zero-based `insert_goal(index=...)` argument;
* make the renderer responsible for checkpointing or mutating workflow state;
* replace the event journal with UI-only state;
* redesign unrelated tool cards, phase-control notices, or exploration groups.

## 5. Users and use cases

### 5.1 Agent adds follow-up work

An agent calls `append_goal` during implementation. The user should see a
short plan-update notice, understand that the new work was queued at the end,
and see that the active goal continues.

### 5.2 Agent inserts prerequisite work

An agent calls `insert_goal(index=7, ...)` while the plan contains 13 goals.
The user should see position 8 of 13, not the internal index 7, and should not
mistake the insertion for a phase transition or restart.

### 5.3 Multiple mutations in one turn

If several consecutive goal mutations are committed during one turn, the TUI
should remain readable. It may coalesce adjacent notices into one bounded
summary, but every committed mutation must remain present in the durable event
stream and available to resume/replay logic.

### 5.4 Resume or journal replay

After restart, `--continue`, `--resume`, or TUI transcript replay, a committed
mutation must render with the same user-facing wording and position. Replaying
the event must not create a second event or duplicate the visible success
notice.

### 5.5 Mutation failure

If validation or checkpointing fails, the TUI must show the existing bounded
error presentation. It must not show a successful “Plan updated” notice for
an operation that was rolled back.

## 6. Proposed user experience

### 6.1 Canonical single-mutation rendering

The canonical presentation is a compact, two-level notice:

```text
  ⎿ Plan updated
    Inserted a goal · position 8 of 13
    ↳ Current goal continues
```

For an append operation:

```text
  ⎿ Plan updated
    Added a goal to the end · position 13 of 13
    ↳ Current goal continues
```

The exact glyph may use the repository's established fallback convention, but
the following semantics are mandatory:

* a visible heading identifies the event as a plan update;
* the operation is stated in plain language;
* the displayed position is one-based and bounded by the displayed total;
* continuation is a separate subordinate line, not a semicolon clause;
* the notice does not claim that the newly added goal is complete;
* the notice does not imply that the active goal changed;
* the output ends with the normal blank line used by other appender notices.

The final copy may be refined during implementation for consistency with the
project's established vocabulary, but it must not regress to raw phrases such
as `at 7 (13 total)` or a semicolon-packed status sentence.

### 6.2 Operation wording

The presentation layer must distinguish the two supported operations:

| Event operation | Required meaning | Example wording |
|---|---|---|
| `insert` | A goal was placed at a specific position | `Inserted a goal · position 8 of 13` |
| `append` | A goal was placed at the end | `Added a goal to the end · position 13 of 13` |
| missing/unknown | A committed goal-list addition occurred, but operation detail is unavailable | `Added a goal · position 8 of 13` when valid position data exists |

The renderer must not derive an operation from untrusted goal text. Unknown
operations use a safe generic label.

### 6.3 Position rules

* Durable `index` remains zero-based.
* Displayed position is `index + 1` when `index` is a valid integer.
* Displayed total is `goal_count` when it is a valid non-negative integer.
* A position is shown as `position N of M` only when the values are coherent.
* If only one value is valid, use a bounded fallback such as `Plan size: M`
  or omit the position rather than displaying contradictory metadata.
* Boolean values, floats, negative indices, impossible positions, and enormous
  values must not be interpolated directly into the notice.
* The renderer must never show a zero-based raw index as though it were a user
  position.

### 6.4 Continuation wording

The continuation line should communicate the existing workflow contract:

```text
↳ Current goal continues
```

It must not say “workflow complete”, “moving to the new goal”, “restarting”,
or any equivalent phrase. A mutation is not a handoff.

### 6.5 Multiple adjacent mutations

The implementation should coalesce only adjacent, successful
`goal_list_mutated` presentation events that belong to the same rendered
turn/group. A valid compact form is:

```text
  ⎿ Plan updated
    Added 3 goals · plan now has 13 goals
    ↳ Current goal continues
```

If coalescing would obscure operation-specific positions or make the output
less clear, rendering separate canonical notices is acceptable. The following
rules apply in either design:

* no durable event may be removed or rewritten merely for display;
* the group must remain bounded;
* an unrelated tool, assistant response, turn boundary, error, or user input
  flushes the pending presentation group;
* the same event must not be rendered once as a grouped notice and again as a
  standalone notice.

The initial implementation may ship without coalescing if the exact-once and
single-notice design is demonstrably clearer. If so, this decision and its
trade-off must be recorded in the implementation notes.

### 6.6 Visual and accessibility rules

* Use the existing `⎿` notice/tree marker or its established plain-text
  fallback; do not introduce a new visual language for one event.
* Color must reinforce hierarchy, never carry meaning by itself.
* Plain terminal output must remain understandable when Rich color is disabled.
* Goal text, IDs, provider messages, and prompt content must not leak into the
  compact notice.
* Output must remain readable at the appender's narrowest supported width.
* Rich markup must escape all event-derived strings before interpolation.
* The screen reader order must be heading, operation/position, continuation.
* The notice must use normal appender spacing and must not leave a live footer
  or tool group in an inconsistent state.

## 7. Functional requirements

### FR-1: Presentation-only transformation

The renderer shall transform the existing event payload into polished display
copy without changing workflow context, checkpoint data, event ordering, or
provider conversation memory.

### FR-2: Human-friendly position display

The renderer shall convert valid zero-based indices into one-based positions
for users and shall validate the relationship between position and total
before displaying them.

### FR-3: Distinct append and insert copy

The renderer shall distinguish insertion from append-to-end in normal valid
payloads and shall use a safe generic fallback for unknown operations.

### FR-4: Explicit continuation state

The renderer shall show that the current goal continues as a subordinate line,
without implying a handoff, completion, or phase transition.

### FR-5: Bounded and redacted output

The renderer shall omit opaque goal IDs, full goal bodies, and unbounded
payloads. Any optional display detail must have an explicit character limit,
be markup-escaped, and be safe for transcript persistence.

### FR-6: Exactly-once visible projection

Each committed mutation shall produce at most one visible success notice in a
given transcript projection. The machine-oriented tool result may remain
available to the model, but generic tool rendering and the derived event
renderer must not produce duplicate user-facing success messages.

### FR-7: Failure correctness

The renderer shall display a success notice only for a `goal_list_mutated`
event emitted after a successful checkpoint. Validation and checkpoint errors
shall not be followed by a success notice.

### FR-8: Replay determinism

Rendering a given valid event payload multiple times shall produce equivalent
user-facing output. Resume and journal replay shall not mutate the event or
create a new mutation.

### FR-9: Malformed payload resilience

Malformed or legacy payloads shall result in a safe, bounded notice or a
graceful omission. They shall not raise an exception from the scroll appender
or corrupt neighboring tool/assistant groups.

### FR-10: Group-boundary correctness

The notice shall flush exploration and ordinary tool groups according to
existing appender rules, preserve the required blank-line separation, and
flush any pending mutation presentation before unrelated content.

## 8. Non-functional requirements

### NFR-1: Backwards compatibility

Existing journal events and checkpoint files must remain readable. Existing
event fields remain authoritative; adding an optional bounded presentation
field is permitted only if it is backwards compatible and not required for
correct rendering.

### NFR-2: Security and privacy

Do not render secrets, tool arguments, opaque IDs, full goal content, provider
responses, or arbitrary prompt text in this notice. Event-derived strings must
be escaped before passing to Rich markup.

### NFR-3: Performance

Rendering must be O(1) for a single event and must not read the workflow
checkpoint, filesystem, or provider memory. Grouping, if implemented, must be
bounded by a small fixed event count or character budget.

### NFR-4: Terminal portability

The output must be correct in color and non-color terminals, captured console
tests, narrow terminal widths, and transcript replay.

### NFR-5: Maintainability

Formatting rules should be isolated in a named helper or renderer with clear
tests. Domain state and presentation state must not become a second source of
truth.

### NFR-6: Observability

The durable event and debug logs may retain machine identifiers needed for
correlation, but normal scroll output must remain concise. A diagnostic mode
must not be enabled implicitly by normal rendering.

## 9. Data flow

The intended data flow is:

```text
agent calls append_goal/insert_goal
        │
        ▼
GoalFlowRunner validates and mutates typed GoalContext
        │
        ├─ checkpoint fails ──► restore snapshot ──► bounded error only
        │
        └─ checkpoint succeeds
                │
                ▼
        append durable goal_list_mutated event
                │
                ├─ journal/session replay ──► same event payload
                │
                ▼
        ScrollBufferAppender presentation projection
                │
                ├─ validate/bound/redact fields
                ├─ convert index to human position
                ├─ format plan update card
                └─ flush once at the event/group boundary
                │
                ▼
        user sees one polished notice
```

The event remains the canonical committed fact. The TUI projection is
recomputable and must never be used to decide whether a mutation happened.

## 10. Implementation guidance

### 10.1 Renderer design

Refactor `_render_goal_list_mutated` into a small, testable presentation
boundary. A suitable internal shape is a pure formatter that accepts a
validated projection such as:

```python
GoalMutationPresentation(
    operation="insert",
    position=8,
    total=13,
    continuation="Current goal continues",
)
```

The exact type name is an implementation choice. It must not replace the
workflow's `GoalContext` or durable receipt model.

The helper should:

1. validate primitive payload values;
2. convert the internal index to a human position;
3. choose bounded operation copy;
4. emit Rich markup only after escaping dynamic values; and
5. return a deterministic set of display lines or a safe no-op.

### 10.2 Event schema policy

Prefer deriving the notice from the existing fields. If exact-once
correlation cannot be implemented without additional metadata, add only a
backwards-compatible, bounded event field such as a mutation event ID or
tool-call correlation ID. Do not add full goal text solely to improve the
notice.

If a bounded goal preview is later considered valuable, it requires a separate
privacy review and must be explicitly opted into by the presentation contract;
it is not part of this PRD's minimum scope.

### 10.3 Tool result versus event rendering

The tool result's `message` is primarily model-facing workflow guidance. The
TUI must choose one canonical user-facing success projection. The preferred
approach is:

* retain the structured tool result for the agent;
* render the `goal_list_mutated` event as the polished human notice; and
* suppress or collapse any generic raw tool-result message that would repeat
  the same success fact.

The implementation must verify the actual event ordering in the current
runner rather than assuming that the tool card and event are always adjacent.

### 10.4 No domain regression

The following existing guarantees must remain unchanged:

* checkpoint before success event/result;
* rollback when checkpoint persistence fails;
* active goal and phase remain unchanged after append/insert;
* durable goal IDs remain opaque and stable;
* list revision and mutation receipts continue to be recorded;
* resume selects the same workflow run and conversation;
* the agent continues the current goal after a successful mutation.

## 11. Testing strategy

### 11.1 Unit tests

Add or rewrite appender/presentation tests covering:

* insert at internal index `7`, count `13`: display position `8 of 13`;
* append at the end: clear end-of-plan wording;
* unknown operation: safe generic wording;
* omitted index and count: bounded fallback without fabricated values;
* negative, boolean, float, oversized, or incoherent numeric fields;
* opaque `goal_id` is never rendered;
* goal text or arbitrary extra payload values are never rendered;
* Rich markup characters are escaped;
* exact expected line order: heading, operation/position, continuation;
* no semicolon-packed legacy wording and no raw `at 7 (13 total)` wording;
* final blank line and neighboring group flush behavior;
* non-color console output;
* narrow terminal width and long bounded values;
* repeated rendering produces equivalent output;
* adjacent mutation grouping, if implemented;
* no duplicate output when both a tool completion and mutation event are
  present.

Existing tests that assert `Inserted goal at 0 (4 total)` must be replaced with
the new presentation contract rather than weakened to substring checks.

### 11.2 Integration tests

Exercise the actual goal-flow mutation path with a fake checkpoint store and
conversation store:

* successful `insert_goal` checkpoints first, then emits one mutation event;
* successful `append_goal` has the same ordering;
* checkpoint failure restores the prior context and emits no success event;
* the appender consumes the emitted event and produces one success notice;
* tool-result and event projection do not duplicate the success notice;
* event replay renders the same notice without mutating workflow state;
* unrelated tool and assistant events flush any pending mutation group.

### 11.3 End-to-end tests

Add a TUI/workflow journey for:

1. start a `goal_flow` run with at least 13 goals;
2. insert a goal at zero-based index 7;
3. verify the visible output says position 8 of 13 and current goal
   continuation;
4. restart or resume the run;
5. verify the event remains durable, the workflow cursor is unchanged, and
   the notice is not duplicated or rewritten as a restart;
6. append another goal and verify end-of-plan wording;
7. force checkpoint failure and verify only the bounded failure is shown.

E2E assertions should inspect normalized plain output in addition to styled
  output so they remain portable across terminal color settings.

### 11.4 Negative-control assertions

The test suite must explicitly prove that the implementation does not:

* show the old zero-based index as a user position;
* print `goal_id`, full goal text, or raw tool arguments;
* claim that the new goal is complete;
* change the active goal or phase;
* create a second workflow event during rendering;
* produce a success notice after a failed checkpoint;
* render the same committed mutation twice;
* raise on malformed event payloads.

## 12. Acceptance criteria

The implementation is accepted only when all of the following are true:

1. The exact ugly form `Inserted goal at 7 (13 total); continuing the current
   goal` is no longer emitted by the TUI.
2. A valid insert displays a human-readable one-based position and total.
3. A valid append communicates that the goal was added to the end.
4. Continuation is visually separated from the mutation description.
5. The notice is readable with and without terminal color.
6. Dynamic event fields are bounded and Rich-escaped.
7. Opaque goal IDs and full goal bodies never appear in normal output.
8. A successful mutation still checkpoints before returning success and does
   not alter active-goal or phase semantics.
9. A failed checkpoint produces no success notice and leaves prior state
   intact.
10. A mutation is presented at most once per transcript projection.
11. Replaying or resuming a committed event produces deterministic equivalent
    output and no new mutation.
12. Existing exploration/tool-group blank-line and flush behavior remains
    correct.
13. Unit, integration, and E2E regression tests cover the positive and
    negative controls in this PRD.
14. Relevant lint, type-check, and test gates pass without weakening existing
    security, privacy, or durability checks.

## 13. Documentation requirements

The implementation change should update, in the same delivery:

* `docs/guides/workflows.md` with the user-facing dynamic-goal notice and the
  distinction between zero-based API indices and one-based display positions;
* the relevant TUI or architecture documentation with the event-to-projection
  data flow and exactly-once presentation rule;
* `docs/reference/storage.md` only if the event schema or retention policy is
  changed;
* `llms-full.txt` only if a public Python symbol is added or changed;
* the PRD index if repository convention requires indexing new PRDs.

This PRD itself does not change those artifacts; it records the required
implementation scope.

## 14. Rollout and compatibility

The change is safe to roll out behind the existing renderer because old
`goal_list_mutated` events contain sufficient metadata for the new display.
No migration is required if the implementation derives its presentation from
the current payload. If a new optional correlation field is introduced:

* old events must continue to render;
* missing fields must use the safe fallback;
* the field must be bounded and non-sensitive;
* journal readers must not require the new field;
* replay tests must cover both old and new event shapes.

## 15. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Users lose useful context when the notice is shortened | Keep operation, position, total, and continuation visible; preserve full machine metadata in the durable event. |
| One-based conversion introduces an off-by-one error | Use explicit conversion tests for index 0, middle, and end positions. |
| Tool result and event both appear | Add integration coverage for the actual event sequence and enforce one canonical human projection. |
| Malformed historical events break resume rendering | Use typed/bounded defensive formatting and no-op/fallback behavior. |
| Rich markup injection | Escape every dynamic value and test bracket/control characters. |
| Group coalescing hides individual changes | Keep grouping optional, bounded, and never remove durable events; default to separate notices if clarity suffers. |
| Presentation work accidentally changes workflow semantics | Keep all mutations and checkpoints in `GoalFlowRunner`; restrict the renderer to a pure projection. |

## 16. Definition of done

* The new renderer and any presentation helper are implemented within the
  existing TUI ownership boundary.
* The legacy ugly line is absent from source-generated output and regression
  snapshots.
* All functional, non-functional, accessibility, privacy, replay, and
  exactly-once requirements above are covered by deterministic tests.
* The goal-flow state machine, checkpoint ordering, and resume behavior are
  unchanged except for the intended presentation improvement.
* Required documentation and repository quality gates pass.
