---
title: "AGX-06: Agents list redesign — status-first rows, attention grouping, search"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P0
series: AGX
related_prds: [agx-00, agx-03, agx-05, agx-07, agx-11, agx-13, prd-157, prd-206, prd-209, prd-219]
tags: [agents, tui, ux, accessibility, search]
---

# AGX-06 — Agents list redesign: status-first rows, attention grouping, search


## Implementation status (2026-10-24)

Implemented with the agents hub (AGX-05).

- **Vocabulary (§5.1).** `agenthicc.background.vocabulary.status_look` is the
  one definition of glyph, label, group and style, used by the hub,
  `agenthicc agents list` and the background-sessions guide.
- **Rows (§5.2).** `agents_hub/layout.py` keeps a column at or above its
  minimum or drops it, by priority (activity from 90 columns, project 100,
  agent 110, cost 120 when any row has a cost); the selection is `▸` in a
  reserved gutter, with `✓` for marked and `•` for unseen rows. The Activity
  column is the report line the worker records — the first sentence of the
  agent's latest message, redacted (`report_sentence`) — or the error of a
  failed agent. Mode-application state is in the detail view. The opt-in
  small-model summary is not offered, and the pull-request number waits for
  PRD-231, which records none yet.
- **Groups and order (§5.3).** Pinned, Needs you, Ready for review (finished
  with a worktree branch), Running, Queued, Recently finished (30 minutes);
  empty groups explain themselves; `s` sorts (attention, recent, created,
  title), `g` groups by project, Shift+↑/↓ reorders pins; choices persist in
  `~/.agenthicc/state/agents-hub.json`.
- **Search (§5.4).** Words in any order plus `status:`, `project:`,
  `agent:`, `label:`, `run:`; matches are highlighted; Esc restores the
  selection. History searches the index (schema v4 adds the filter columns).
- **Scrolling and live updates (§5.5).** The list fills the height and
  scrolls; selection is keyed by agent ID across group moves; `•` marks
  unseen changes and survives reopening.
- **Footer and help (§5.6).** Generated from the key map (AGX-13); one line.

Acceptance: golden renders at 60, 80, 120 and 200 columns with complete
labels and a one-line footer; `needs approval` and `needs input` are distinct
labels at every width; a 50-row terminal shows more than ten rows; selection
stability across updates; a hypothesis property test for the layout
(`test_a_shown_column_is_never_narrower_than_its_minimum`). Rendering a
120×30 frame of a 10,000-session registry takes about 2 ms to build and 4 ms
to print at best on the development host; the p95 budgets are AGX-14's to
enforce in CI.

## 1. Summary

The agents list is a Rich table that squeezes seven columns into the terminal
width. At 120 columns statuses truncate to `waiting_ap…`; at 80 columns
`waiting_approval` and `waiting_input` both become `wai…`; the workspace column
disappears entirely; a column shows an internal mode state on every row; and
the list stops at 10 rows per page on any terminal. Redesign the list — used by
every tab of the AGX-05 hub — around what a user scans for: **what needs me,
what is running, what finished**, with readable status labels, columns that
degrade by priority instead of collapsing, live search and scrolling.

## 2. Problem and evidence

Rendered from the current `BackgroundManager.render()` with six seeded sessions
(AGX-00 §4.5). At 120 columns:

```text
Background Sessions · page 1/1 · showing 1–6 of 6
  State         Title                     Workflow        Mode                   Activity
──────────────────────────────────────────────────────────────────────────────────────────────
  running       ▶ Document the CLI ref…   goal_flow       pending: default/pe…   Implementing goal 3 of 7: commands ta…
  waiting_ap…   Fix flaky scheduler te…   code_plan       pending: default/pe…   Approve bash: pytest tests/unit -k sc…
  completed     Refactor auth token re…   code_plan       pending: default/pe…   Done: 4 files changed, tests pass
  failed        Reconstruct marketing …   reconstru…      pending: default/pe…   provider error: rate limit exceeded (…
  waiting_in…   Investigate memory gro…   direct          pending: default/pe…   Which environment should I profile?
  queued        Upgrade dependencies      code_plan       pending: default/pe…
```

At 80 columns:

```text
  Sta…   Title              Wor…    Mode           Activity
  run…   ▶ Document the …   goa…    pending: de…   Implementing goal 3 of 7: c…
  wai…   Fix flaky sched…   cod…    pending: de…   Approve bash: pytest tests/…
  wai…   Investigate mem…   dir…    pending: de…   Which environment should I …
```

| # | Problem | Cause |
|---|---|---|
| 1 | Raw, truncated, ambiguous statuses | `session.status.value` in a no-wrap ellipsis column |
| 2 | Workspace (`WS`) column never shown | Rich shrinks no-wrap columns to zero when the table overflows |
| 3 | `pending: default/persisted` on every row | `_mode_label` exposes mode-application state as a column |
| 4 | Titles cut to ≈ 22 characters at 120 columns | No column priorities or minimum widths |
| 5 | At most 10 rows per page | `MAX_SESSION_ROWS_PER_PAGE = 10` regardless of height |
| 6 | Selection drawn twice (`▸` marker and `▶` in the title) | Marker column collapses on narrow terminals |
| 7 | Absolute timestamps only; no age, duration, progress, model or cost | Not projected |
| 8 | Footer wraps to two lines at 80 columns; hints ignore context (`y`/`n` for a waiting approval is not shown) | Static hint string |
| 9 | No grouping or sort choice; search is a substring match on ID, title, path and workflow | `_matches_session` |
| 10 | Details panel says "no text activity" while the row shows activity | Panel and row read different sources |

## 3. How others present lists

- **Claude Code agent view** (`claude agents`), the closest analogue: rows
  grouped **Pinned**, **Ready for review**, **Needs input**, **Working**,
  **Completed** (Ctrl+S groups by directory instead); each row has a state icon,
  the session name, a one-line summary written by a small model, its age and a
  pull-request label. Empty lists still show every section header with a short
  description; the Completed group takes the remaining height and the header
  compacts on short terminals. Ctrl+F finds by name or first prompt (`n:`
  filter), Alt+↑/↓ jumps between group headers, Shift+↑/↓ reorders.
- **Claude Code** `/tasks`: running work sorted above finished work; finished
  rows stay 30 s; each row shows the model and effort; Enter opens the
  transcript; `x` stops. The subagent panel nests children as a tree with a
  `(+N)` descendant count.
- **Codex** (app Subagents view): separate *Active* and *Done · N* lists.
- **Grok Build**: the session picker "matches every typed word in any order"
  (*third-party cheat sheet*); `grok sessions search` searches titles and
  prompts.
- **OpenCode**: `opencode session list` prints a table or JSON, limited with
  `-n`.

## 4. Goals and non-goals

**Goals**

1. A row's state is readable at any width ≥ 60 columns and never ambiguous.
2. Work that needs the user is always first and visually distinct.
3. Columns degrade by priority; none is squeezed below its minimum.
4. The list fills the terminal and scrolls; selection is stable across updates.
5. Search is incremental, multi-word and searches title, intent, labels,
   activity, ID and project.

**Non-goals**

- The detail view (AGX-07) and keymap (AGX-13) are specified separately; this
  PRD uses their contracts.

## 5. Design

### 5.1 Status vocabulary

One mapping, used by every surface, tests and docs:

| Internal status | Glyph | Label | Group |
|---|---|---|---|
| `waiting_approval` | `◆` | needs approval | Needs you |
| `waiting_input` | `◆` | needs input | Needs you |
| `running` | `●` | running | Running |
| `starting` | `◐` | starting | Running |
| `retrying` | `↻` | retrying | Running |
| `cancelling` | `⊘` | stopping | Running |
| `queued` | `◌` | queued #N | Queued |
| `orphaned` | `⚠` | worker lost | Needs you |
| `failed` | `✗` | failed | Recently finished |
| `completed` | `✓` | done | Ready for review when its worktree has changes or a pull request; otherwise Recently finished |
| `cancelled` | `⊘` | cancelled | Recently finished |
| `archived` | `▫` | archived | History only |

Glyph, label and colour are redundant: the label alone is unambiguous in
no-colour and screen-reader modes (AGX-13).

### 5.2 Row anatomy and responsive columns

```text
 Agents   Running 4 · Needs you 2 · History 214 · Library 9                         ? help
 Needs you
 ◆ needs approval  Fix flaky scheduler test        app-1   code_plan   bash: pytest -k sched…   10m
 ◆ needs input     Investigate memory growth        app-0   direct      Which environment should… 40m
 Running
▸● running         Document the CLI reference       app-0   goal_flow   goal 3/7 · commands table   2m  $0.42
 ◌ queued #1       Upgrade dependencies             app-1   code_plan                               50m
 Ready for review
 ✓ done            Refactor auth token refresh      app-0   code_plan   4 files changed · tests pass 20m  #812
 Recently finished
 ✗ failed          Reconstruct marketing site       app-0   reconstruct rate limit exceeded (429)    30m
 ↑↓ select  Enter open  y approve  n reject  / search  s sort  ? help                         q quit
```

Columns, in priority order (dropped right-to-left as width shrinks):

| Column | Min width | Shown at |
|---|---:|---|
| Status (glyph + label) | 16 | always |
| Title (flex) | 20 | always |
| Age (relative, e.g. `2m`) | 4 | always |
| Activity or progress (flex) | 16 | ≥ 90 columns |
| Project (last path component) | 8 | ≥ 100 columns |
| Agent or workflow | 10 | ≥ 110 columns |
| Cost (PRD-157 usage) | 6 | ≥ 120 columns, when known |

The Activity column shows the agent's **report line**: the first sentence of
its latest assistant message, or the summary the worker records at the end of
an attempt — not a raw tool invocation. Agent view generates its row summaries
with a small model; agenthicc can offer that as an opt-in using the cheapest
configured profile, but the default costs nothing extra. A pull-request number
is shown at the right edge when the agent's branch has one (PRD-231).

The layout is computed by the hub's list primitive, not by Rich's table
algorithm, so a column is either shown at ≥ its minimum or not shown. The
selected row is marked once, by `▸` in a reserved gutter. Mode-application
state moves to the detail view.

### 5.3 Groups and order

- Running tab groups, top to bottom: **Pinned**, **Needs you**, **Ready for
  review**, **Running**, **Queued**, **Recently finished** (finished in the
  last 30 minutes; older rows move to History). *Ready for review* holds
  finished agents whose worktree has changes or an open pull request (PRD-231),
  so review work is not buried among plain completions.
- When the tab has no agents at all, every group header is shown with a
  one-line description ("Agents waiting for your answer appear here"), as
  agent view does; otherwise empty groups are hidden. The last group takes the
  remaining height.
- Within a group, most recent activity first; Shift+↑/↓ reorders pinned rows.
- `g` toggles grouping between status and project; Alt+↑/↓ jumps between group
  headers (both configurable, AGX-13).
- `s` cycles sort keys (attention, recent activity, created, title); the choice
  is remembered per user.
- Goal runs render as trees: the run row, its main agent and workers indented,
  with a `(+N)` count when collapsed (Claude Code's convention).

### 5.4 Search and filters

- `/` opens a search line that filters as you type (AGX-03 full-text index);
  every word must match, in any order, across title, intent, labels, activity,
  ID and project. Matches are highlighted.
- Tokens narrow further: `status:failed`, `project:app-0`, `agent:reviewer`,
  `label:release`, `run:RUN_ID`.
- Esc clears the search; the previous selection is restored.

### 5.5 Scrolling and live updates

- The list uses the full height available to it and scrolls; PgUp/PgDn move
  by a screen; Home/End jump. The 10-row page cap is removed.
- Updates re-render rows in place; the selection stays on the same agent ID
  even when its row moves between groups.
- A row whose activity changed since the user last looked shows `•`; opening it
  clears the mark (persisted per user, so it survives reopening the hub).

### 5.6 Footer

The footer shows the keys valid for the current selection and tab, generated
from the keymap (AGX-13): `y approve  n reject` only on a waiting approval,
`r resume` only on a resumable agent (PRD-219). It never wraps: hints drop by
priority to fit, and `? help` is always last-but-one before `q quit`.

### 5.7 Empty and error states

| State | Message |
|---|---|
| No agents ever | "No agents yet. Start one with `agenthicc --goal "…" --detach`, or `/background` in a session." |
| Search with no match | "No agents match `query`. Esc clears the search." |
| Index rebuilding (AGX-03) | Progress line plus the partial, most-recent rows |

## 6. Budgets and acceptance criteria

| Measure | Budget |
|---|---:|
| List frame render at 120×40, 10k sessions | p95 ≤ 8 ms |
| Search update per keystroke, 10k sessions | p95 ≤ 50 ms |

1. Golden renders at 60, 80, 120 and 200 columns: every visible status label is
   complete; no column is narrower than its minimum; the footer is one line.
2. `waiting_approval` and `waiting_input` rows are distinguishable at every
   width.
3. With a 50-row terminal the list shows more than 10 rows.
4. Updating a row's activity does not move the selection to another agent.
5. The status vocabulary is defined once and used by the hub, `agents list`
   output and docs.

## 7. Test plan

- Golden render tests with fixed clocks at four widths and in no-colour mode.
- Property test: for random widths and session sets, the computed layout never
  assigns a visible column less than its minimum.
- Selection-stability test across group moves and live updates.
- Search tests: multi-word, token filters, highlight positions.

## 8. Risks

- **Custom layout instead of Rich `Table`.** Mitigation: the list primitive
  renders `Text` lines and is shared by every hub tab, so it is written and
  tested once.

## Sources

- Claude Code agent view: <https://code.claude.com/docs/en/agent-view>
- Claude Code subagents (`/tasks`, panel tree): <https://code.claude.com/docs/en/sub-agents>
- Codex subagents (Active / Done lists): <https://learn.chatgpt.com/docs/agent-configuration/subagents>
- Grok Build sessions (`grok sessions search`): <https://docs.x.ai/build/features/sessions>
- Grok Build cheat sheet (*third-party*): <https://www.scriptbyai.com/grok-build-cheat-sheet/>
- OpenCode CLI (`session list`): <https://opencode.ai/docs/cli/>
