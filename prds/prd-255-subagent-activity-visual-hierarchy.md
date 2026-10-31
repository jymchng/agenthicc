---
title: "PRD-255: Subagent Activity Visual Hierarchy and Responsive Density"
status: Implemented
version: 1.1.0
created: 2026-10-31
scope: "make a subagent fan-out easy to scan from spawn through completion in the live panel and scrollback, without changing execution, event, result, or usage semantics"
related_prds:
  - AGX-08
  - PRD-124
  - PRD-157
  - SUBAGENT-09
tags:
  - tui
  - subagents
  - ux
  - accessibility
  - responsive-layout
---

# PRD-255 — Subagent Activity Visual Hierarchy and Responsive Density

## 1. Summary

When a parent turn starts a subagent pool, the TUI currently places a spawn
announcement and a line for every worker in scrollback, adds pool progress to the
parent status line, and renders a separate live panel under the composer. The
panel exposes useful details—status, worker label, current activity, elapsed time
and tokens—but they compete for one fixed-width row. Long tool names and targets
can crowd the identity and metrics; the label can repeat the agent type; queued
workers are not called out in the summary; and a large fan-out can spend many
scrollback lines on its initial task list.

This PRD gives that existing information a clear visual hierarchy and a
responsive density policy. The default view answers, in order: **what pool is
running, which workers need attention, what each worker is doing, and where to
open full details**. The transcript and existing overlays remain the places for
full task text, tool targets and results.

The scope is presentation of a subagent pool from `subagent_pool_started` to
`subagent_pool_done`. AGX-08 continues to own the panel's lifecycle and
navigation. SUBAGENT-09 owns event/transcript data contracts. PRD-157 owns usage
accounting. This PRD changes none of those semantics.

## 2. Evidence and current behavior

The current source was reviewed on 2026-10-31.

- `src/agenthicc/tui/workspace/subagent_panel.py` renders a summary followed by
  up to six worker rows. Each row places a fixed-width worker label, activity,
  elapsed time and token count on one line. A terminal shorter than 24 rows gets
  only the summary. Screen-reader mode renders a sentence.
- The summary reports running, done and failed counts, and shows the configured
  concurrency limit when workers are running. It does not distinguish pending
  workers waiting for a concurrency slot from workers waiting for approval.
- `src/agenthicc/tui/workspace/appender.py` prints `Spawning N subagents` and
  then prints every worker label and up to 80 characters of its task. There is
  no default collapse for a large initial list. Completion events are separate
  lines.
- `src/agenthicc/tui/workspace/components.py` adds `done/total subagents` to
  the parent status line, independently of the live panel summary.
- `WorkerState` already provides `label`, `agent_type`, `status`, `activity`,
  elapsed-time fields and token counts. The UI can improve their presentation
  without adding execution state or changing the event contract.
- Existing tests cover a 100-column, four-worker panel, six-row overflow,
  screen-reader wording and the spawn/completion renderers. They do not establish
  a width matrix, the priority of fields under constrained width, or a bounded
  scrollback announcement for large pools.

The concrete user-facing symptom is a dense fan-out display where labels,
raw tool identifiers and paths, elapsed time, large token totals, and the parent
status line all compete for attention. The details exist; their priority and
progressive disclosure do not.

## 3. Goals

1. Make pool state and exceptional states legible before secondary metrics.
2. Give each worker one non-redundant identity and one short, understandable
   activity label.
3. Preserve useful live detail at wide widths and degrade predictably at narrow
   widths without wrapping, clipping status, or silently dropping failures.
4. Bound the scrollback footprint of a pool's initial worker list while keeping
   every full task description reachable through existing navigation.
5. Use one consistent pool summary across the live panel, parent status line and
   scrollback completion message.
6. Keep the experience usable without color and in screen-reader mode.

## 4. Non-goals

- Changing worker scheduling, capability policy, approvals, cancellation,
  backgrounding, retries, aggregation or result delivery.
- Adding an estimated completion percentage when workers do not provide one.
- Defining or changing token accounting, cost calculation, usage inclusion or
  data-quality semantics; PRD-157 remains authoritative.
- Replacing the existing panel, `/subagents`, `Ctrl+O` focus, transcript overlay,
  or transcript retention behavior from AGX-08.
- Changing event payloads, transcript schemas, redaction, replay or truncation;
  those belong to SUBAGENT-09.
- Turning the panel into a general workflow/task dashboard.

## 5. Presentation contract

### 5.1 Information order

The default live view must prioritize information in this order:

1. **Attention state:** failed, stopped, or waiting for approval.
2. **Pool progress:** running, queued/pending, done and failed counts.
3. **Worker identity:** one readable label, e.g. `Reviewer 1`, not both
   `reviewer #1` and a second `reviewer` role label.
4. **Current activity:** a short action and useful subject, e.g.
   `Read PRDs/SUBAGENT-01…`, not a raw internal tool name plus an unbounded path.
5. **Elapsed time and token usage:** shown when they fit; never allowed to crowd
   out status, identity or activity.

The UI must not reorder workers on every update. Default order remains spawn
order; a waiting approval or failure may be visually emphasized but not moved in
a way that makes a row jump beneath the user's selection.

### 5.2 One summary for the pool

The pool summary reports only non-zero categories, plus completed progress when
it is useful. Pending workers waiting for capacity are shown as `queued`; workers
waiting for approval are shown separately as `needs input` (or an equally clear
plain-language phrase). Use one consistent mapping from worker state to summary:
`pending` → queued, `running` → running, approval wait → needs input, `done` →
successful completion, `failed` → failed, and `stopped` → stopped. The configured
concurrency limit is available in the details view and is shown in the summary
only when it explains queued work.

Examples:

```text
Subagents · 4 running · Ctrl+O details
Subagents · 3 running · 2 queued · 1/6 done · Ctrl+O details
Subagents · 2 running · 1 needs input · 2 failed · 3/8 done · Ctrl+O details
```

Zero-valued categories are omitted. `done/total` counts successful completions
against all workers in the pool; failures and stopped workers remain separately
visible when non-zero. `done`, `failed`, `stopped`, `queued` and approval counts
must be derived from the same pool snapshot, not maintained as a second TUI
counter. The parent status line may use a shorter progress form, but must not
contradict this summary.

### 5.3 Responsive worker rows

The subagent panel occupies its own display area below the composer's bordered
input panel. Pool status rows are not rendered as part of the input panel, so the
prompt remains visually distinct while the pool stays directly above the
footer.

Rows adapt to terminal width. The precise cell renderer remains an implementation
decision, but the following content priorities and examples are normative:

**Wide (120 columns or more):** identity, current activity, elapsed time and
compact token count fit on one row.

```text
● Reviewer 1   Reading PRDs/SUBAGENT-01…                           1:14  181k
◆ Reviewer 2   Needs approval: inspect test policy                 1:14  260k
✓ Reviewer 3   Found 4 design gaps                                  0:52   39k
```

**Medium (64–119 columns):** preserve status, identity and a useful activity
prefix. Omit token count before omitting elapsed time; shorten the activity at a
path/component boundary rather than cutting through a word.

```text
● Reviewer 1   Reading PRDs/SUBAGENT-01…             1:14
◆ Reviewer 2   Needs approval: inspect test policy    1:14
```

**Narrow (40–63 columns):** preserve status, identity and the action. Move
elapsed time and tokens to the selected-worker details instead of squeezing them
into an unreadable row.

```text
● Reviewer 1  Reading PRDs/…
◆ Reviewer 2  Needs approval
```

Below 40 columns, show the summary and the currently selected/attention row only;
`Ctrl+O`/`/subagents` must still reach every worker. The existing behavior that
shows the summary alone on very short terminals remains in force. No row may
wrap or exceed the measured terminal width.

Tool activity should use a small human-readable label map for known tools. An
unknown tool keeps a safe, shortened form of its registered name; it must not
render blank or be misidentified. Paths retain the useful trailing component(s)
and use an ellipsis when shortened. The complete redacted activity remains
available in the transcript overlay.

### 5.4 Pool start and completion in scrollback

A pool start is one visual group. Its heading is a level-1 scrollback row,
aligned with tool headings. Show at most three worker previews as indented child
rows. When the pool contains more than three workers, replace the remaining
previews with `+N more · Ctrl+O details`. Do not emit an unbounded series of task
lines merely because the pool is large. Every worker's full task remains
available through the existing subagent list/transcript navigation.

```text
◈ Subagents · 4 workers started
  └ Reviewer 1  Review subagent PRDs for overlap
  └ Reviewer 2  Check current UI rendering paths
  └ Reviewer 3  Compare usage/status designs
  +1 more · Ctrl+O details
✓ [3/4] Reviewer 3  1.2s  Compared usage/status designs
◈ Subagents Failed · 3/4 done · 1 unsuccessful (failed or stopped)

● Spawn Subagents(max_concurrent=1)
└─ Completed · 25.5s · 20 lines
```

Keep one blank row after the pool completion line before following scrollback
content. Worker completion rows and the pool completion summary are also level-1
scrollback rows, never indented beneath a tool or another worker. Use `complete`
only when all workers succeeded and no budget-exceeded reason is present; otherwise
label the summary `Failed`, including pools with unfinished workers. Pool completion
uses the aggregate fields supplied by `subagent_pool_done` and one concise line.
The current event does not carry a pool duration, so the renderer must not invent
or imply one.

When any worker fails, the completion line must retain the event's combined
unsuccessful count instead of showing a success-only total. The active pool
summary/panel remains responsible for distinguishing failed, stopped and
approval-waiting workers while a pool snapshot is available; the aggregate
completion event cannot reconstruct those individual states. A retried or
separate `spawn_subagents` call remains a separate pool unless the runtime
provides an explicit relationship; the renderer must not infer that two pools
are retries of one another.

### 5.5 Detail and accessibility

`Ctrl+O`, `/subagents`, selection and transcript navigation continue to expose
the complete task, current and prior activity, exact elapsed time, exact token
count, approval state, error and result summary. The live row is a preview, not a
lossy replacement for the transcript.

Status cannot be conveyed by color alone. Screen-reader output uses one short
sentence per row and announces queued, waiting-for-approval, failed and stopped
states explicitly. Abbreviated token values are spoken as exact values by
screen-reader output, or are omitted from the sentence with the exact value
available in the detail view.

## 6. State and privacy boundaries

- Read only the immutable `SubagentPoolState`/`WorkerState` snapshot already
  published by the pool. Do not add a renderer-owned source of progress truth.
- Reuse the redacted activity and transcript data. Do not expose raw prompts,
  tool arguments, secrets or unredacted paths to gain a shorter label.
- Keep exact usage numbers and their quality state in the canonical usage
  projection from PRD-157. This PRD may compact their visual representation but
  must not recompute or reinterpret them.
- Rendering, resizing, focusing and collapsing a pool must not affect provider
  messages, pool execution, conversation events or transcript contents.

## 7. Acceptance criteria

1. A representative four-worker pool renders with one identity per worker,
   understandable activity, and no accidental field collision at 40, 63, 64,
   80, 119, 120 and 200 columns.
2. A worker with `pending` status contributes to the queued count; a worker
   waiting on approval contributes to the attention count and is not mislabeled
   as queued or running.
3. Summary counts reconcile to the worker snapshot for mixtures of pending,
   running, waiting, done, failed and stopped states; zero categories are omitted.
4. At widths below 64 columns, token counts are removed before status, identity
   or activity; at widths below 40, the selected/attention row remains reachable
   in the focused overlay.
5. A fan-out with more than three workers emits no more than three initial task
   previews plus one `+N more` line; the details view can still open every
   worker and its full task.
6. Completion output reports successful progress and the event's combined
   non-success count as `unsuccessful (failed or stopped)`; the active summary
   continues to distinguish failed and stopped workers from the pool snapshot.
7. Screen-reader and color-disabled tests communicate every status in words and
   do not depend on glyphs or color to distinguish failure and approval.
8. Tests render snapshots emitted by a real pool for pending, active, approval,
   failure and completion cases; presentation tests also cover malformed or
   unknown activity labels safely.
9. A render-only interaction does not change worker state, event sequence,
   transcript bytes, aggregate result, or usage ledger values.
10. Existing AGX-08 worker focus, transcript navigation, stop, approval and
    finished-row retention tests continue to pass.
11. The bordered input panel ends after the composer; live subagent rows are
    rendered below that border and outside the input area.

## 8. Implementation and verification

Implement in the existing ownership boundaries:

1. Define one presentation formatter for pool counts and row labels, derived
   solely from the pool snapshot.
2. Apply width-aware row priorities in
   `src/agenthicc/tui/workspace/subagent_panel.py` and preserve the existing
   focused-panel navigation contract.
3. Bound the scrollback start group and align start/completion wording in
   `src/agenthicc/tui/workspace/appender.py`.
4. Keep the parent status-line progress short and consistent in
   `src/agenthicc/tui/workspace/components.py`.
5. Add golden and property-style width tests beside
   `tests/unit/test_subagent_panel_agx08.py` and
   `tests/unit/test_subagent_tui.py`, with a real-pool integration case in
   `tests/integration/test_subagent_panel_agx08.py`.
6. Update `docs/guides/subagents.md` and this PRD with the shipped behavior and
   verification evidence.

The screen should be reviewed at 40, 64, 80, 120 and 200 columns; on a terminal
shorter than 24 rows; with ANSI colors disabled; and in screen-reader mode. Test
fixtures must control time and must not call a live provider.

## 9. Related PRDs and ownership

- **AGX-08** owns the subagent panel, worker focus, transcripts, stop action and
  approvals. PRD-255 refines the visible hierarchy and density without replacing
  those interactions.
- **PRD-124** records the original concurrent-subagent design and its TUI
  aspirations. Where its fixed footer grid or unbounded spawn list differs from
  this responsive contract, PRD-255 is the current presentation target.
- **SUBAGENT-09** owns event and transcript schemas, redaction, truncation and
  lifecycle observability. PRD-255 consumes those records; it creates no new
  event vocabulary.
- **PRD-157** owns usage accounting and data quality. PRD-255 does not define
  token semantics or cost.
- **PRD-256** owns the parent status/usage strip's compact number and metadata
  formatting. PRD-255 only requires a consistent, short pool-progress summary.
