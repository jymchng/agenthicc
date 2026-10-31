---
title: "PRD-256: Compact and Honest TUI Status and Usage Strip"
status: Implemented
version: 1.1.0
created: 2026-10-31
scope: "improve the readability and width behavior of status-bar, idle-header, and usage-quality presentation without changing canonical usage values or accounting"
related_prds:
  - PRD-68
  - PRD-157
  - AGX-11
  - PRD-166
  - PRD-255
tags:
  - tui
  - status
  - usage
  - ux
  - responsive-layout
---

# PRD-256 — Compact and Honest TUI Status and Usage Strip

## 1. Summary

The status area currently puts raw comma-grouped input/output token totals beside
animated state, elapsed time and subagent progress. The model and session
metadata occupy adjacent rows; usage quality can be represented by a lone `?`,
and an unavailable cost is rendered as `cost unknown`. This remains accurate in
many cases, but large values dominate the line and the quality marker is easy to
miss or misunderstand. Multiple independent segments also make short-width
truncation hard to scan.

PRD-256 defines a compact, consistent visual projection for the parent status
area and idle session header. It makes state and activity primary, summarizes
large usage values, labels estimated cost and partial/unavailable usage
explicitly, and gives every rendered state a deterministic width policy.
`/usage` remains the place to see exact values and detailed accounting status.

PRD-157 remains the sole owner of usage records, totals, inclusion policy,
reconciliation and data-quality semantics. This PRD changes only how those
already-computed values are shown.

## 2. Current-state evidence

Reviewed on 2026-10-31:

- `src/agenthicc/tui/workspace/components.py` builds the active status line from
  state/animation, elapsed time, comma-formatted input/output counts and, when
  present, `done/total subagents`. It then renders a model row and session
  metadata. Width handling removes trailing fields, but it does not shorten a
  large token value before removing an entire semantic field.
- The unavailable/partial usage state may appear as a standalone `?` after the
  counts. That symbol is compact but does not say whether the uncertainty is
  about token counts, cost, or both.
- `src/agenthicc/tui/workspace/appender.py::print_idle_header` prints the
  session ID, turn count, cost and exact input/output counts as one scrollback
  line. Long model identifiers and large usage totals make the status area and
  idle header visually dense.
- `PRD-157` specifies canonical accounting and requires unknown values not to
  masquerade as zero. It also calls for deterministic narrow-terminal behavior;
  PRD-256 supplies a concrete presentation policy without changing that
  contract.
- `AGX-11` owns frame scheduling, render caching and terminal writes. Its
  rendering and performance guarantees remain unchanged.

## 3. Goals

1. Make the current state and elapsed time the first things a user reads.
2. Keep exact usage accounting intact while displaying large token counts in a
   compact, recognizable form.
3. Replace an unexplained quality marker with an explicit, accessible indication
   of partial or unavailable usage and estimated or unavailable cost.
4. Keep the active status, idle header and `/usage` consistent about the same
   session without repeating low-value metadata.
5. Define width priorities so state, activity, model identity and pool progress
   never run together, wrap unexpectedly or become ambiguous.
6. Preserve screen-reader, color-disabled and append-safe rendering behavior.

## 4. Non-goals

- Changing provider-reported counts, pricing, usage aggregation, estimates,
  inclusion policy, cost quality or the `/usage` record format.
- Hiding large usage or rounding it in storage, exports, tests of accounting, or
  the `/usage` command. Compaction is a display-only projection.
- Replacing the two-line status component, Rich, `LiveRegion`, or the existing
  footer and workspace render pipeline.
- Adding hover tooltips, network lookups, provider invoice data or new metrics.
- Changing the subagent panel's detailed progress layout; PRD-255 owns that
  surface. The parent status line may show a short, consistently delimited pool
  count when width allows.

## 5. Status information hierarchy

The status projection uses these priorities:

1. **State:** Idle, Thinking, Running, Recovering, Waiting, Compacting, Error
   or Complete, with its existing semantic styling and non-color equivalent.
2. **Elapsed time** while a turn or wait is active.
3. **Active activity:** current tool or a concise workflow/terminal wait label.
4. **Subagent progress:** a short `N/M workers` segment only when a pool is
   active and there is room. The panel summary in PRD-255 remains the full pool
   status.
5. **Usage:** compact input/output counts plus an explicit quality state when
   it is not complete.
6. **Identity and accounting metadata:** the model may shorten to fit; the full
   session ID and current directory each occupy their own unlabeled row. Turns,
   cost and usage quality remain on secondary metadata rows.

Every adjacent segment has a visible separator and sufficient spacing. A
subagent counter must never be concatenated with the model name or another field.
The status component is allowed to omit a lower-priority field at narrow widths;
it must not truncate the state into a different word or erase the only visible
indication of a failure, approval wait or data-quality limitation.

## 6. Compact numeric formatting

### 6.1 Token values

Only the TUI status/header projection uses compact notation. Use these stable
rounding rules:

| Exact value | Compact display | Example |
|---:|---|---|
| 0–999 | exact integer | `↑ 824` |
| 1,000–999,999 | one decimal with `k` | `↑ 18.1k`, `↓ 260.3k` |
| 1,000,000 and above | one decimal with `M`/`B` as needed | `↑ 230.0M`, `↓ 1.5M` |

Rounding is half-up to one decimal place. Trailing `.0` may be removed at
1,000,000 or above (`230M`) to save space; it is retained for `k` values so the
existing worker-panel convention remains predictable. If rounding reaches
`1,000.0k`, promote the value to `1.0M` rather than displaying a four-digit
mantissa.

The exact integer remains unchanged in the canonical usage ledger, `/usage`,
session inspection and export. No compact value is fed back into accounting.

### 6.2 Usage and cost quality

Token usage quality and cost provenance are separate contracts:

- **Complete usage:** no marker.
- **Partial usage:** show `partial` when there is room; compact form `?` is
  allowed only when the screen-reader output and help text expand it to
  `usage partial`.
- **Unavailable usage:** render `usage unavailable`, never a numeric zero that
  could be read as a real zero.
- **Estimated cost:** show `estimated` when there is room; compact form `~` is
  allowed only when screen-reader text expands it to `estimated cost`.
- **Authoritative cost:** show its amount without an estimate marker.
- **Unavailable cost:** render `cost unknown`.

Cost stays separate from token quantities. An estimated cost uses the existing
PRD-157 status (`~$0.123` or a compact equivalent) and an unavailable cost is
written as `cost unknown`. The status strip must not imply that an estimate is a
provider charge.

### 6.3 Model and session identity

The model row preserves provider and model identity when width permits. On
narrow terminals, shorten only at path/component boundaries, preserving the
provider and a recognizable trailing model component. The full session ID and
current working directory are shown on separate rows without labels; wrap by
terminal cell width when needed rather than truncating either value. The current
directory comes from the session's working directory at startup.

## 7. Width policy and examples

The implementation must use terminal display-cell width. State/activity rows must
never wrap, while the unlabeled session-ID and current-directory rows preserve all
characters by wrapping when required. The following examples show the target
hierarchy; exact borders and glyph choice remain with the existing renderer.

**Wide terminal (120 columns):**

```text
❁ Thinking · 1m 31s · ↑ 230M ↓ 1.5M · 0/4 workers
openai · deepseek-v4.1-flash
7ff08d4a8c5b4d6c9f0e123456789abc
/workspace/agenthicc
2 turns · cost unknown · usage partial
```

**Medium terminal (80 columns):**

```text
❁ Thinking · 1m 31s · ↑ 230M ↓ 1.5M · 0/4 workers
openai · deepseek-v4.1-flash
7ff08d4a8c5b4d6c9f0e123456789abc
/workspace/agenthicc
2 turns · cost unknown · usage partial
```

**Narrow terminal (48 columns):**

```text
❁ Thinking · 1m 31s · ↑ 230M ↓ 1.5M · ?
openai · …flash
7ff08d4a8c5b4d6c9f0e123456789abc
/workspace/agenthicc
```

Session identity rows are never omitted or abbreviated. If an ID or directory
exceeds the terminal width, it wraps while preserving every character. Lower
priority turn/cost metadata may be omitted at narrow widths; `/usage` retains the
full accounting details. If a quality state is partial or estimated, the status
row must retain its compact marker unless doing so would remove the primary state
or activity. In that case, put the explicit word on the metadata row and preserve
it in screen-reader output. A compact `?` is explicitly the partial-usage marker
defined in §6.2; it is accompanied by the screen-reader phrase `usage partial`.

The idle header is a one-time scrollback summary, not a second live meter. It
uses the same compact token formatter and quality labels as the status component
and avoids duplicating the same usage quantities on adjacent rows.

## 8. Accessibility and terminal behavior

- Screen-reader output uses exact integer counts with direction labels, e.g.
  `230,044,280 input tokens, 1,506,692 output tokens, usage partial`.
- Color-disabled output still distinguishes status and data quality through
  words and stable symbols.
- Append-safe/captured rendering emits no extra status snapshots for animation
  frames; PRD-166 continues to govern which redraws are emitted.
- All output is fitted by display cells at 40, 48, 64, 80, 120 and 200 columns,
  including wide Unicode glyphs, long model IDs, active tools and subagent
  progress.
- Long tool names, model identifiers, and metadata are truncated at a readable
  boundary with an ellipsis. The state word and its meaning are never ellipsized.

## 9. Acceptance criteria

1. Exact accounting values remain unchanged before and after formatting; tests
   prove that compact strings are never written to the ledger or session export.
2. Unit tests cover token formatting at 0, 999, 1,000, 9,999, 10,000,
   999,999, 1,000,000, 999,999,999 and unit-carry boundaries.
3. Complete, estimated, partial and unavailable states have distinct, explicit
   output; unavailable counts/costs do not render as known zero.
4. Status rows for idle, thinking, tool-running, approval-wait, recovery,
   compaction, error, completion and active subagent states render without
   overflow at every width in §8.
5. The parent pool count, model identifier and neighboring fields always have
   visible delimiters and never concatenate when the line is full.
6. At narrow widths, fields disappear in the priority order in §5; the primary
   state and a failure/wait/quality indication are retained or exposed in the
   same visible secondary row.
7. The idle header and live status use the same compact formatter, while
   `/usage`, session inspection and export continue to show exact values.
8. Screen-reader and color-disabled tests spell out direction, units, usage
   quality and unavailable cost without relying on arrows, color or `?` alone.
9. Captured-console tests confirm that active animation does not create repeated
   compact status rows; existing AGX-11 and PRD-166 render-policy behavior is
   unchanged.
10. No provider request, tool execution, session event, usage record, ledger
    aggregate or cost value changes as a consequence of rendering.
11. The full session ID and current directory appear as separate unlabeled rows;
    both preserve all characters by wrapping at constrained widths. Cross-session
    attention gets its own footer row immediately before the composer hints.

## 10. Implementation and verification

1. Add one pure display formatter for compact token values and one typed
   presentation of usage quality; consume existing `ConversationStore`/usage
   projection fields only.
2. Apply the formatter and the width-priority policy to
   `StatusComponent` in `src/agenthicc/tui/workspace/components.py` and the idle
   summary in `src/agenthicc/tui/workspace/appender.py`.
3. Keep subagent status text short and explicitly delimited; use PRD-255 for the
   live panel's full pool counts and worker rows.
4. Add pure formatting tests, golden screen-width cases and accessibility tests
   under the current TUI status/component test suites.
5. Update the TUI and usage guides to explain compact values, exact-value access
   through `/usage`, and quality markers. Update PRD-157 only with a cross-link;
   do not rewrite its historical accounting decisions.

Verification must include the focused status/component tests, TUI render tests,
accessibility output tests, Ruff, formatting, type checks and the repository's
documentation gate. No live provider or external billing call is required.

## 11. Related PRDs and ownership

- **PRD-157** is canonical for usage records, exact totals, quality states and
  `/usage`. PRD-256 defines only the screen formatter and layout.
- **PRD-68** is the historical TUI feature checklist. Where its examples require
  raw comma-separated counts or conflict with compact rendering, PRD-256 is the
  current active-status presentation target; the values themselves stay exact.
- **AGX-11** owns render scheduling, caching, line diffing and synchronized
  output. PRD-256 adds no render loop or timer.
- **PRD-166** owns append-safe status rendering. Compact output must obey its
  redraw policy and must not create transcript snapshots per animation frame.
- **PRD-255** owns subagent panel and pool-start/completion presentation. This
  PRD owns only the short parent status-line projection of pool progress.
