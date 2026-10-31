---
title: "PRD-257: Compact TUI Activity Completion Recap"
status: Implemented
version: 1.1.0
created: 2026-10-31
scope: "make per-turn and whole-activity completion durations concise, visually grouped, and semantically clear without changing the underlying clocks or events"
related_prds:
  - PRD-166
  - PRD-256
tags:
  - tui
  - ux
  - telemetry
  - accessibility
---

# PRD-257 — Compact TUI Activity Completion Recap

## 1. Summary

After an agent turn, the scroll buffer can print both a per-turn duration and a
whole-activity duration:

```text
✾ Worked for 53 seconds

✾ Total wall clock time since last IDLE: 54 seconds
```

These are different and useful measurements, but their verbose labels, full-word
units, and blank-line separation make the recap feel repetitive rather than
intentional. “Since last IDLE” describes an internal state boundary instead of
the user's activity, and the current usage guide shows a duration format that
differs from the runtime output.

PRD-257 turns the existing lines into a compact, visually grouped completion
recap. It keeps the per-turn elapsed time distinct from the elapsed time for the
entire user activity, uses short consistent duration units, and removes the
implementation-specific wording. It is a presentation-only change: timer
ownership, elapsed values, events, and accounting remain unchanged.

## 2. Evidence and current behavior

Source and documentation reviewed on 2026-10-31:

- `src/agenthicc/tui/workspace/appender.py::_fmt_worked` formats seconds and
  minutes using full words (for example, `53 seconds` and `1 min 5 seconds`).
- `_render_turn_complete` emits the dim `✾ Worked for …` row for turns lasting
  at least one second, then adds a blank line.
- `_render_activity_complete` emits `✾ Total wall clock time since last IDLE: …`
  after the turn-completion event and adds another blank line.
- `ConversationStore.close_turn` emits the per-turn event. For an outer TUI
  activity, `end_activity` emits the final activity event when that activity
  returns to IDLE; a workflow may contain more than one turn within that scope.
- The per-turn elapsed value and the outer activity elapsed value are separate
  clocks. The latter spans the complete user activity, including internal turns
  and workflow phases. It must not be presented as model-only or active-work-only
  time.
- `tests/unit/test_worked_for.py` asserts the current verbose wording and blank
  lines. `docs/usage/04-tui.md` uses `1m 5s`, while the runtime currently emits
  `1 min 5 seconds`.
- `docs/guides/tui.md` documents the outer activity boundary and its relationship
  to turn completion. That timing contract should remain true after the visual
  change.

## 3. Goals

1. Make the completion recap read as one intentional unit instead of two
   unrelated verbose messages.
2. Let users distinguish an individual turn's elapsed time from the total elapsed
   time of the enclosing activity at a glance.
3. Use compact, consistent duration formatting that works in terminal scrollback
   and documentation examples.
4. Keep wording neutral so a duration is not mistaken for a success signal when
   the turn ended with an error or cancellation.
5. Preserve append-only transcript behavior, readable plain-text output, and the
   existing timer/event contracts.

## 4. Non-goals

- Changing when an activity or turn starts or ends, how elapsed time is measured,
  or whether approval/question waits are part of the outer wall-clock interval.
- Adding events, changing event payloads, changing workflow lifecycle semantics,
  or making a turn's duration equal the enclosing activity's duration.
- Adding token, cost, model, tool, or subagent metrics to the completion recap;
  PRD-256 owns status/usage presentation and PRD-255 owns subagent activity.
- Adding an animated timer, a new overlay, a configurable style, or a provider
  request.
- Replacing the existing TUI, Rich, or scroll-buffer rendering pipeline.

## 5. Design direction and inspiration

Use the named products as design references, not templates to copy:

- **Codex:** favor a concise, outcome-adjacent recap over verbose runtime
  diagnostics.
- **Claude Code:** retain the lightweight completion cue and familiar,
  conversational brevity, while making the timing labels clearer.
- **OpenCode:** treat terminal width, alignment, color-disabled output, and
  text-first readability as first-class constraints.
- **Grok:** prefer direct, natural language over framework-internal phrasing such
  as “since last IDLE.”

Do not reproduce product branding, proprietary copy, or logos. Keep the current
completion glyph unless implementation testing establishes a terminal or
accessibility reason to change it.

## 6. Proposed presentation

Render a compact pair of rows with no empty row between them. Keep the existing
turn cue on the first row; indent the enclosing-activity total so the two values
read as a group:

```text
✾ Turn · 53s
  Total · 54s
```

For an activity containing multiple completed turns, retain one short row for
each turn and one final total row:

```text
✾ Turn · 18s
✾ Turn · 29s
  Total · 54s
```

The total is the outer user-activity duration, not the sum of the visible turn
durations and not a claim about model execution time. Explain this once in the
TUI usage documentation; the scrollback label itself should stay short.

### 6.1 Duration format

Use one pure presentation formatter for both event renderers:

| Elapsed seconds | Display |
|---:|---|
| 0 | `0s` |
| 1 | `1s` |
| 53 | `53s` |
| 60 | `1m` |
| 65 | `1m 5s` |
| 3,600 | `1h` |
| 3,724 | `1h 2m 4s` |

Use whole seconds, flooring fractional values as the current formatter does.
Omit zero-valued trailing units (`1m`, not `1m 0s`; `1h 2m`, not `1h 2m 0s`).
Keep the existing visibility threshold: per-turn rows are omitted below one
second. An outer activity shorter than one second must not produce a noisy
`Total · 0s` recap row.

### 6.2 Grouping and output behavior

- Do not put a blank line between a turn-duration row and its activity-total row.
- Ensure one blank row separates the complete recap from the next scrollback
  event or the persistent live status block. Use the workspace's existing
  separator at the live boundary rather than stacking a second trailing blank.
- If a turn fails or is cancelled, the existing error/cancellation output remains
  authoritative. The timing recap uses neutral `Turn`/`Total` labels and must not
  imply success.
- If an activity ends without a visible per-turn row, do not render an orphaned
  total row for a sub-second activity.
- Keep the visible labels in plain text. The glyph, indentation, color, or dim
  styling may add hierarchy but must not be the only way to understand which
  duration is per-turn and which is total.

## 7. Acceptance criteria

1. Per-turn and whole-activity elapsed values are still read from their existing
   events and rendered separately; no duration is synthesized by adding turns.
2. A single-turn activity renders the target two-row recap in §6, with compact
   labels and no blank line between the rows.
3. A multi-turn activity renders each eligible turn duration followed by exactly
   one total activity duration; the total remains distinct from any individual
   turn duration.
4. The duration formatter passes boundary tests for 0, 1, 59, 60, 61, 3,599,
   3,600, and 3,724 seconds, and for fractional seconds. It emits no redundant
   zero units.
5. Sub-second turn/activity durations do not create a misleading `0s` completion
   recap; turns below the existing one-second visibility threshold remain
   omitted.
6. Failed and cancelled turns retain their existing error/cancellation output
   and still use neutral timing labels where a duration is shown.
7. Captured-console tests verify the exact row ordering, indentation, one final
   blank separator, and absence of the old phrase `since last IDLE`.
8. Color-disabled and screen-reader-oriented output communicates “turn” and
   “total” through words; glyphs and color are supplementary.
9. The recap remains short and does not wrap at 40, 48, 64, 80, 120, or 200
   columns.
10. The TUI usage documentation matches the runtime's compact formatter and
    accurately explains that total duration spans the enclosing user activity.
11. No timer values, event payloads, workflow behavior, token/cost accounting,
    renderer scheduling, or provider interactions change.

## 8. Implementation and verification

1. Add or reuse a pure compact elapsed-time formatter in
   `src/agenthicc/tui/workspace/formatters.py`; remove or delegate the local
   `_fmt_worked` implementation so turn and activity rows cannot drift.
2. Update the `turn_complete` and `activity_complete` renderers in
   `src/agenthicc/tui/workspace/appender.py` to use the target labels, shared
   formatter, and grouped blank-line policy. Keep existing event kinds and
   payloads unchanged.
3. Extend `tests/unit/test_worked_for.py` for formatter boundaries, exact
   rendering, multi-turn ordering, sub-second behavior, errors, and blank-line
   grouping.
4. Update `docs/usage/04-tui.md`, `docs/guides/tui.md`, and any directly affected
   examples so they agree with the runtime and explain the two time scopes.
5. Run focused TUI renderer tests, the unit suite, Ruff, formatting, type checks,
   and the strict documentation build. No live provider or external billing call
   is required.

## 9. Ownership and related work

- **PRD-166** continues to own append-safe status rendering and redraw policy.
  The recap is emitted only at completion boundaries; it must not add snapshots
  for animation frames.
- **PRD-256** continues to own live status and usage display. This PRD changes
  only the completion summary written to scrollback.
- `ConversationStore` remains the source of the per-turn and outer-activity
  duration boundaries. This PRD changes their presentation, not their authority.
