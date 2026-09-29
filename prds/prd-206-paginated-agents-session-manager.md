---
title: "PRD-206: Paginated, low-latency `agents` session manager"
status: Implemented
version: 1.0.0
date: 2026-09-29
repository: jymchng/agenthicc
related_prds:
  - PRD-141  # Background sessions and Session Manager TUI
  - PRD-150  # Client-neutral session service and event projection
  - PRD-158  # Resumed TUI transcript
  - PRD-202  # Paginated /ps terminal overlay
  - PRD-204  # Goal runner and detached orchestration
tags:
  - agents
  - sessions
  - tui
  - pagination
  - performance
  - attach
---

# PRD-206 — Paginated, low-latency `agents` session manager

## 1. Summary

Improve `agenthicc agents` so the interactive session manager is a bounded,
viewport-aware TUI. Every visible screen must fit in the terminal, including
the selected-session details and keyboard controls. The session list must be
paginated, selection must remain attached to a stable session ID while the
store changes, and the activity panel must show only the newest text activity.

The manager must also become responsive for repositories with many sessions or
large journals. Rendering must not repeatedly fold the complete background
event log, reread a large conversation file, run process recovery, or rebuild
Rich tables when no observable state changed.

The canonical attach command is:

```text
agenthicc attach <session-id>
```

The argument is an exact durable background session ID. A workspace may contain
many sessions; attach must never guess from the current directory, choose the
most recent session, or interpret a positional session ID as a goal-run ID.
Goal-run discovery remains available through `agenthicc agents --run <run-id>`
and the run projection, after which the selected session ID is attached.

This PRD extends the existing `BackgroundManager`, `BackgroundStore`,
`BackgroundSupervisor`, session journal, CLI registry, and Rich Live surfaces.
It must not introduce a second session store, worker runtime, transcript
format, or attachment/ownership protocol.

## 2. Evidence and current gaps

The current implementation is concentrated in:

* `src/agenthicc/tui/workspace/background_manager.py`;
* `src/agenthicc/cli/commands/background.py` and `runs.py`;
* `src/agenthicc/background/store.py` and `supervisor.py`; and
* `src/agenthicc/tui/runtime/session_log.py`.

The current manager has a fixed-height heuristic (`console.height - 14`) and
some page navigation, but it does not enforce the height of the complete
`Group(table, detail, footer)` render. The selected details panel can grow by
rendering multiple activity lines, so the footer falls below the Live region.
The selection is an integer index, so a refresh or a completed session can
silently move an action to another session.

The current refresh/render path also has avoidable hot-path work:

1. `BackgroundStore.list()` folds the append-only `events.jsonl` history on
   every refresh.
2. `BackgroundManager.refresh()` can call `recover_stale()` from the UI path,
   including process checks, while the screen is being redrawn.
3. `_activity_lines()` rereads and parses the tail of `conversation.jsonl`
   during rendering, even when the selected session has not changed.
4. `render()` asks for the session list again through `selected_session` after
   already obtaining it.
5. Rich receives a complete new table/group at a fixed four refreshes per
   second even when no state or layout changed.
6. The activity projection includes lifecycle/tool rows such as repeated
   `tool_complete` entries rather than displaying the newest meaningful text.

The current `agents` command is the alias for this background-session manager,
which is correct and must remain true. The current positional `attach`
implementation is goal-run-oriented and accepts a run identifier, while the
manager's Enter action already returns a background `session_id`. These are
different identities and cannot be safely conflated when a workspace contains
many sessions.

## 3. Goals

### Product goals

1. Keep the session table, selected details, status indicators, and controls
   visible in every supported positive-size terminal viewport.
2. Let users navigate all sessions without rendering all rows simultaneously.
3. Show the current page, visible range, total count, and selected session
   clearly.
4. Show only the most recent meaningful text activity for the selected session,
   bounded to the available detail area.
5. Make `Enter` and `agenthicc attach <session-id>` attach the exact selected
   session through the existing ownership-safe handoff.
6. Keep all session actions targeted by stable session identity while workers
   start, finish, fail, or disappear in the background.

### Engineering goals

1. Make the UI hot path proportional to the visible page and bounded activity
   tail, not to total session count or total journal size.
2. Separate durable-store polling, process recovery, activity projection, and
   Rich rendering so each has an explicit cadence and cache key.
3. Preserve redaction, workspace/security policy, leases, cancellation, and
   transcript durability.
4. Keep non-interactive and JSON output complete and script-compatible; only
   the interactive presentation is paginated.
5. Add deterministic terminal-size, mutation, large-store, and attach tests.

## 4. Non-goals

This PRD does not:

* change worker execution, workflow semantics, or background-session state
  transitions;
* create a new persistence database or duplicate session registry;
* load the complete transcript into the manager just to display activity;
* hide sessions from JSON/list APIs because they are on another page;
* discover or attach arbitrary host processes by scanning PIDs;
* weaken session leases, workspace access, approval, network, or redaction
  policies;
* make `agents` a registry of configured agent definitions; or
* delete session journals, artifacts, or workflow checkpoints as part of
  pagination or attach.

## 5. Identity and attach contract

### 5.1 Stable identity

`session_id` is the only identity used by the interactive manager and the
canonical attach command. A selected row stores its session ID, not only its
array index. After every refresh, the manager must:

1. keep the same selected session if it still matches the filter;
2. select the nearest surviving row if it was deleted or filtered out;
3. preserve marked-session IDs independently of page boundaries; and
4. reject an action when the selected session disappeared rather than applying
   it to a newly shifted row.

The session's `cwd` is metadata and a filter, never an identity namespace. Two
sessions in one workspace must remain independently selectable and attachable.

### 5.2 `agents` Enter behavior

Pressing Enter on a selected row returns its exact `session_id` to the command
handler. The handler invokes the existing `BackgroundSupervisor` foreground
handoff and then starts the normal TUI with `resume_id=session_id`.

For a live session, the handoff must stop the owned worker and release its
lease before constructing the foreground TUI. If the worker cannot be safely
stopped, attach fails visibly and must not start a duplicate runtime. For a
terminal session, attach opens the existing journal/checkpoint according to
current resume semantics without creating a new session.

### 5.3 CLI attach behavior

Implement the canonical command:

```text
agenthicc attach <session-id>
```

Requirements:

* The positional argument is an exact `BackgroundSession.session_id`.
* An unknown ID returns a clear non-zero error without selecting a session from
  the current workspace.
* A goal run ID such as `run_...` is not accepted as the positional session ID;
  the error points users to `agenthicc agents --run <run-id>` or the explicit
  run projection.
* The command must use the same attach/handoff implementation as Enter in the
  manager, not a second code path.
* `--json` returns the selected session identity and resulting handoff status
  without transcript contents or secrets.
* No attach operation may submit the original intent again or create a second
  session.

If backwards compatibility requires retaining a goal-run attach shortcut, it
must use an explicit non-positional form such as `agenthicc attach --run
<run-id>`. The positional form remains session-only and is documented as the
unambiguous interface.

## 6. Viewport and pagination contract

### 6.1 Layout budget

The manager must compute a `ViewportBudget` from the current terminal width and
height on every resize-sensitive render. The budget reserves space for:

* title/page/status header;
* table column header and visible session rows;
* a bounded selected-session summary;
* a bounded recent-text line; and
* the controls/footer and any transient notice.

The footer is a hard requirement: it may not be pushed below the viewport by
session data, long paths, wrapped text, error messages, or activity output.

All dynamic fields must be width-bounded and use no-wrap/ellipsis behavior
where appropriate. A short terminal may reduce the table to one row and the
details to a compact one-line summary, but it must still show the controls.
The implementation must not assume a fixed 25-row terminal.

### 6.2 Session pagination

Interactive mode displays only the session rows in the current page. The
header must include a stable label such as:

```text
Background Sessions · page 2/7 · showing 7–9 of 21
```

Required navigation:

| Key | Behavior |
|---|---|
| Up/Down or `k`/`j` | Move selection by one session |
| PageUp/PageDown | Move by one visible page |
| Home/End | Select first/last session |
| Enter | Attach the selected session ID |
| `r` | Request an immediate refresh without changing identity |
| `q`/Esc | Leave the manager without stopping sessions |
| `?` | Show bounded controls/help |

The page is derived from the selected session ID and current budget. A terminal
resize or refresh may change the number of rows per page, but must keep the
same selected session visible whenever it still exists.

The non-interactive manager fallback may print a complete bounded diagnostic
listing. `agents --json`, session list APIs, and machine-readable commands must
remain complete and unpaginated.

### 6.3 Details and activity

The selected details section must be bounded independently of the table. It
must show at least the session ID, state, workflow/phase, and a compact error
or wait indicator when present.

The activity section must:

* display only the newest meaningful text event, not a list of repeated
  `tool_complete`, tool-start, heartbeat, token, or internal status events;
* use canonical persisted conversation events, with an explicit allowlist for
  user/assistant text (`text` and `user_message` as applicable to the existing
  event contract);
* select the latest valid text event by journal order/timestamp;
* redact secrets with the existing session-export redactor;
* truncate by both characters and available terminal width; and
* show a neutral placeholder when no text activity exists.

The manager may retain an optional detail/inspection view for a full bounded
activity history, but the default `agents` screen must show only the latest
text activity. It must never parse or render an unbounded transcript in the
main screen.

## 7. Performance and responsiveness

### 7.1 Render purity

`render()` must be side-effect-free and must not invoke process recovery,
durable writes, or an uncached full store fold. It consumes a manager snapshot
and a viewport budget.

### 7.2 Incremental session projection

Extend `BackgroundStore` or add an internal projection cache so repeated list
reads do not fold the entire JSONL history for every frame. The cache must:

* invalidate on local writes;
* detect external changes using a safe file fingerprint (size/mtime or an
  equivalent monotonic sequence);
* preserve the current durable event log as the source of truth;
* remain correct after process restart; and
* avoid retaining unbounded event payloads in memory.

The manager should poll the bounded session projection at a slower cadence
(for example, once per second), while rendering can run at the terminal's
normal refresh cadence without repeating the I/O work.

### 7.3 Isolated recovery cadence

`recover_stale()` must not run from every render. It should run on an explicit
maintenance cadence or through a background refresh task, with a configurable
minimum interval and only the relevant active sessions. Recovery failures must
be shown as bounded diagnostics and must not freeze keyboard input.

### 7.4 Cached activity projection

Cache the latest-text projection per session using the conversation file's
size/mtime (or a journal sequence if available). Read only the tail required
to find the latest allowlisted text event. A session with no journal change
must not cause another file read or JSON decode during each Live repaint.

### 7.5 Render invalidation

The Live surface should be updated only when one of these changes:

* viewport width/height;
* filtered session snapshot or selected session;
* selected session's latest-text fingerprint;
* page/selection/filter/help state; or
* an action/notice changes the visible result.

An idle manager must not consume a full CPU core or repeatedly repaint an
identical screen. Keyboard input must remain responsive while refresh or
recovery work is in progress.

### 7.6 Performance budgets

On a local store with 1,000 sessions and 100 MB of historical journals:

* opening `agents` must display its first complete frame within 250 ms after
  the initial projection is available;
* a normal repaint with no state change must perform zero journal reads and
  zero complete event-log folds;
* a refresh after one changed session must read/project only the changed
  durable state plus the selected session's bounded text tail; and
* PageUp/PageDown and selection changes must complete within 50 ms excluding
  terminal I/O.

These are local deterministic benchmark targets, not network/provider timing
guarantees. Tests must assert operation counts and bounded reads rather than
rely only on wall-clock timing.

## 8. State and data flow

The intended flow is:

```text
BackgroundStore events.jsonl / session journals
                 │
                 ▼
       cached bounded projections
                 │
       ┌─────────┴─────────┐
       ▼                   ▼
 session page snapshot   latest-text cache
       │                   │
       └─────────┬─────────┘
                 ▼
       ViewportBudget + stable selected_session_id
                 │
                 ▼
             Rich render
                 │
                 ▼
 key action → session-id-targeted supervisor/attach operation
```

The TUI is a projection, not the source of truth. Session leases, journals,
workflow checkpoints, and the supervisor remain authoritative. A refresh may
replace the projection, but it must not mutate session state except through an
explicit user action.

## 9. Compatibility and security

* Keep `agenthicc agents` as the background-session manager alias.
* Keep existing `jobs` commands and JSON projections complete.
* Preserve exact session ownership, lease, workspace, approval, terminal,
  network, and dangerous-mode checks.
* Reuse the existing TUI backend and Rich Live lifecycle.
* Reuse the existing redaction rules for errors, titles, activity, and paths.
* Never display intent text, input values, API keys, headers, or raw secrets in
  a manager projection unless the existing redacted projection explicitly
  permits them.
* An attach failure must leave the worker/session in a safe, recoverable state.
* Pagination must never delete, archive, cancel, or hide records from the
  durable store; it only changes the interactive viewport.

## 10. Implementation phases

### Phase 0 — Architecture and baseline

Map the current manager, Rich Live, terminal backend, store fold, journal
format, CLI registry, session lease, and attach paths. Capture render height
and I/O baselines with representative session/journal fixtures.

### Phase 1 — Identity-safe projection

Introduce an immutable manager snapshot and stable `selected_session_id`.
Implement page/range calculation, resize reflow, bounded columns, and all-page
keyboard navigation without changing worker semantics.

### Phase 2 — Guaranteed viewport layout

Add `ViewportBudget` and bounded table/details/footer rendering. Test short,
narrow, normal, and tall terminal sizes, including long paths, errors, and
empty states.

### Phase 3 — Latest-text activity projection

Implement the allowlisted latest-text journal projection, redaction, tail
reading, file-fingerprint cache, and no-activity placeholder. Remove repeated
tool/lifecycle rows from the default activity pane.

### Phase 4 — Incremental refresh and render invalidation

Add durable projection caching/invalidation, isolate stale-worker recovery from
rendering, coalesce refresh notifications, and skip identical Rich updates.

### Phase 5 — Session-only attach

Make `agenthicc attach <session-id>` the exact session attach path. Route Enter
through the same handler. Add explicit goal-run guidance/compatibility without
overloading the positional argument.

### Phase 6 — Verification and rollout

Run unit, integration, E2E, benchmark, type, lint, and documentation gates.
Enable the new manager by default after the old and new projections agree on
session identity, ordering, status, and action results.

## 11. Testing requirements

### Unit tests

Cover:

* viewport budgets at short, narrow, normal, and tall dimensions;
* page count, range labels, empty pages, PageUp/PageDown, Home/End, and
  resize reflow;
* selection retention by session ID across insertion, removal, sorting, and
  filtering;
* actions refusing stale/deleted selections;
* latest-text allowlisting, newest-event selection, malformed records,
  redaction, truncation, and no-activity placeholders;
* activity and projection cache hits/misses on unchanged and changed files;
* no store fold, journal read, recovery call, or Rich update on an unchanged
  repaint;
* bounded rendering with long paths, errors, titles, and activity;
* exact session-ID attach command parsing and unknown/goal-run ID errors; and
* Enter returning the selected session ID.

### Integration tests

Using temporary background stores and session journals:

1. Render 1,000 sessions and verify only one page is materialized in the
   interactive table while JSON/list output remains complete.
2. Mutate the store between refreshes and verify selection stays on the same
   session ID.
3. Append many tool events and one text event, then verify the manager shows
   only that newest text event.
4. Verify unchanged repaint cycles do not reread the journal or fold the full
   event log.
5. Verify a changed selected journal refreshes only its bounded tail.
6. Resize while on a later page and verify the selected session and footer
   remain visible.
7. Press Enter and verify the exact selected session is handed off, resumed,
   and never duplicated.
8. Attach two sessions in the same workspace sequentially and verify each
   opens its own transcript.
9. Verify active-worker attach failure leaves the lease and worker state safe.

### End-to-end tests

Run a real CLI/TUI subprocess with a temporary home, repository, and fake local
worker/provider:

* `agenthicc agents` renders a bounded page whose footer/control line is
  visible at the bottom of short and normal terminal sizes;
* PageDown reaches every session and Enter attaches the selected session ID;
* `agenthicc attach <session-id>` opens the requested transcript when several
  sessions share one workspace;
* a goal/run identifier passed positionally is rejected with actionable
  guidance;
* large journals do not delay keyboard input beyond the performance budget;
* the default activity pane shows the newest text and not repeated tool rows;
  and
* quitting the manager leaves all workers and durable session records intact.

Tests must be deterministic, local, secret-free, and independent of a real
LLM provider or network.

## 12. Acceptance criteria

### AC-1 — Complete visible viewport

At every supported terminal size, the interactive `agents` screen keeps the
session table, selected summary, and controls/footer within the visible Live
region. Long data cannot push the controls off-screen.

### AC-2 — Complete navigation

Given more sessions than fit on one page, users can reach every session with
PageUp/PageDown, Up/Down, Home, and End. The page/range indicator is correct.

### AC-3 — Stable selection

When sessions are inserted, completed, removed, or resorted, the selected
session ID remains selected if it still exists; otherwise the nearest valid
selection is chosen. No action targets a different session due solely to a
refresh.

### AC-4 — Latest text only

The default activity section displays only the newest valid allowlisted text
event, redacted and width-bounded. Repeated tool/lifecycle/token events are
not shown as the activity list.

### AC-5 — Low-latency idle rendering

An unchanged manager repaint performs no durable event-log fold, no journal
read, no stale-process scan, and no Rich update. A changed session updates the
smallest necessary projection.

### AC-6 — Session-specific attach

`agenthicc attach <session-id>` and Enter from `agents` attach exactly the
specified session. With multiple sessions under one workspace, neither path
guesses or opens another session, creates a duplicate owner, or resubmits the
original intent.

### AC-7 — Complete machine interfaces

`agents --json`, `jobs list --json`, and other non-interactive projections
continue to return all matching sessions and are not limited to the current
interactive page.

### AC-8 — Safe lifecycle behavior

Pagination, refresh, resize, and quit never cancel, archive, delete, or alter a
session. Attach and action errors preserve lease, journal, checkpoint, and
worker safety.

### AC-9 — Backward-compatible policy boundaries

Existing redaction, approval, workspace, network, ownership, cancellation,
and background-worker policies remain enforced, and attached sessions use the
canonical existing TUI/session construction path.

## 13. Documentation requirements

Update in the same implementation:

* `README.md` for `agents`, pagination, latest-text activity, and exact attach;
* `docs/guides/background-sessions.md` for the viewport, performance, and
  session-ID attach contract;
* `docs/reference/cli.md` for `agenthicc attach <session-id>` and JSON/list
  pagination semantics;
* `docs/guides/architecture.md` for projection caching versus durable state;
* `llms.txt` and `llms-full.txt` when public command/API symbols change;
* tests documenting deterministic viewport and performance invariants; and
* `prds/README.md` with this PRD and implementation evidence when complete.

## 14. Definition of done

The feature is complete when a user can run `agenthicc agents` in a workspace
containing many sessions, see a complete bounded TUI with controls, paginate
to every session, see only the latest meaningful text activity, and attach the
selected session with Enter. The same session can be opened explicitly with
`agenthicc attach <session-id>`, even when other sessions share the workspace.
The manager remains responsive on large stores, all durable and machine-readable
interfaces remain complete, and no worker/session ownership or security
invariant is weakened.

## 15. Implementation evidence

The implementation is delivered in the existing ownership boundaries:

* `background/store.py` caches the rebuildable event projection and detects
  external JSONL changes without replacing the durable source of truth.
* `tui/workspace/background_manager.py` provides `ViewportBudget`, stable
  session-ID selection, visible `▶` marking, bounded latest-text activity,
  render caching, and a separate stale-worker maintenance cadence.
* `cli/commands/background.py` and `cli/commands/runs.py` share one exact
  session attach/handoff path; `agents --json` remains complete and
  `attach --run` is explicit.
* Unit, integration, and E2E coverage lives in the three
  `test_background_manager_prd206.py` surfaces and exercises viewport bounds,
  projection invalidation, activity caching, large stores, exact attachment,
  and goal-run ID rejection.

Documentation is synchronized in the background-session guide, CLI reference,
architecture guide, README, and the LLM-facing references; the existing
`CLAUDE.md`/`AGENTS.md` ownership boundaries remain applicable.
