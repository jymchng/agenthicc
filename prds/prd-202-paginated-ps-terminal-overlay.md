---
title: "PRD-202: Paginated /ps background-terminal overlay"
status: Implemented
version: 1.0.0
date: 2026-09-29
related_prds:
  - PRD-141  # Background Sessions and Session Manager TUI
  - PRD-143  # Safe Commands During Active LPM Runs
  - PRD-144  # Resize-Safe Waiting Modals and Pause-Aware Display Timing
  - PRD-148  # Unified Interrupt and Graceful Cancellation
  - PRD-149  # Background Terminals and Responsive Wait Control
tags:
  - terminals
  - tui
  - pagination
  - responsive-layout
  - accessibility
---

# PRD-202 — Paginated `/ps` background-terminal overlay

## 1. Summary

`/ps` opens the live list of background terminals owned by the current
session. The current `TerminalListOverlay` renders every terminal record in a
single Rich table, followed by the selected-terminal details and the keyboard
controls. As the number of records grows, the table becomes taller than the
Live region and the controls at the bottom disappear below the visible
screen. Long labels and captured output can make the overflow worse.

PRD-202 makes the interactive `/ps` list paginated and viewport-aware. The
overlay displays only the records that fit beside its bounded detail and
footer regions, shows a clear page/range indicator, and provides keyboard
navigation across all records. Selection is tracked by the stable terminal
ID, not only by a list index, so manager refreshes, process completion,
record removal, and terminal resize do not silently move actions to another
terminal.

The existing `/ps --json` command remains a complete, scriptable projection
of all owned records. Pagination is a presentation concern for the
interactive overlay and must not hide records from the manager, change
ownership checks, or alter `/stop` semantics.

## 2. Problem statement

### 2.1 User-visible failure

With many terminal records, the screen currently resembles:

```text
Background Terminals
Handle          State       Label                 Exit
term-...        running     rest-tests-stress     —
term-...        exited      rest-tests-port       0
term-...        timed_out   rest-tests-port      -2
...             ...         ...                  ...
term-...        exited      test-build             0

State: exited
Command: ...
Output: ...
↑/↓ select  Enter details  s stop  Ctrl+X stop  Esc close
```

When the table contains enough rows, the detail and footer are rendered below
the terminal viewport. The user cannot see the controls and may not know how
to close the overlay or stop the selected process. This is especially easy to
hit after repeated test runs because completed terminal records remain in the
session-scoped manager list.

### 2.2 Current implementation and cause

The current implementation is concentrated in
`src/agenthicc/tui/workspace/overlays/terminals.py`:

* `_records()` returns the complete `TerminalManager.list_records()` result;
* `render()` adds one table row for every record;
* `render()` then adds selected-record details and the footer; and
* the overlay base contract exposes `render()` without a viewport argument.

`OverlayHost` redraws the active overlay, but it does not currently provide
the available Live-region width and height to the overlay. Consequently,
`TerminalListOverlay` has no reliable way to calculate how many rows are
safe. A fixed page size alone would still fail on a short terminal and could
leave unused space on a tall terminal. The existing `Key` enum already
supports `PAGE_UP`, `PAGE_DOWN`, `HOME`, and `END`, and other overlays already
use bounded page projections; `/ps` does not yet apply that pattern.

### 2.3 Impact

The overflow causes more than an aesthetic defect:

* close and stop controls become invisible;
* the user may press keys without knowing which terminal is selected;
* wrapped labels and multiline output make the visible row count
  nondeterministic;
* a manager refresh can change the list while the user is selecting a row;
* a stopped or completed process can cause an index to point at a different
  terminal; and
* tests that render the overlay at realistic terminal dimensions cannot
  assert a bounded layout.

## 3. Goals

### 3.1 Product goals

1. Keep the `/ps` footer and its close/stop instructions visible whenever the
   overlay is rendered in a supported positive-size viewport.
2. Let users reach every owned terminal without rendering the whole list at
   once.
3. Make the current page, visible range, total count, and selection obvious.
4. Preserve correct selection and stop behavior while terminal records change
   asynchronously.
5. Reflow safely when the terminal is resized, including narrow and short
   terminals.
6. Keep `/ps --json` complete and backwards compatible for scripts.
7. Keep all existing session ownership, authorization, process-group, and
   bounded-output policies intact.

### 3.2 Engineering goals

* Keep pagination state in the presentation layer; do not add a second
  terminal registry or alter `TerminalManager` persistence.
* Use the existing `Overlay`, `OverlayHost`, `Key`, and Rich Live architecture.
* Track selected records by stable `terminal_id` and derive indices from a
  fresh snapshot.
* Make page capacity a function of measured viewport height and the space
  reserved for title, table header, details, and controls.
* Ensure every rendered field is bounded and safe for narrow widths.
* Add deterministic unit, integration, and terminal-sized rendering tests.

## 4. Non-goals

This PRD does not authorize the implementation to:

* change how terminals are spawned, waited on, stopped, persisted, or
  recovered;
* expose terminals belonging to another session, project, or owner;
* remove records merely because they are not on the current page;
* change `/stop`, `stop_terminal`, or `wait_terminal` arguments or semantics;
* paginate or truncate the machine-readable `/ps --json` response;
* replace the Rich Live workspace with a second terminal UI framework;
* discover arbitrary host processes by PID scanning;
* increase output-retention limits to compensate for the new display; or
* introduce a new global selection or pagination state store.

## 5. Users and use cases

### 5.1 Inspect a large terminal list

A user runs `/ps` after many builds and test commands. The first page is
bounded, includes a range such as `1–6 of 15`, and leaves the footer visible.
The user can move to later pages without leaving the overlay.

### 5.2 Select and stop a terminal on a later page

The user navigates to a later terminal, sees its handle and bounded details,
and presses `s` or `Ctrl+X`. The stop callback receives exactly the selected
terminal ID, regardless of the page currently shown.

### 5.3 Open `/ps <terminal-id>`

The command opens the same paginated overlay but initially selects the
requested terminal and automatically displays the page containing it. If the
ID is no longer present, the overlay falls back to its normal first/nearest
selection without showing another terminal as though it were the requested
one.

### 5.4 Live completion and removal

While the overlay is open, a running process exits or a record disappears.
The next redraw keeps the selected ID when possible. If it disappeared, the
selection moves to the nearest remaining record, the page count is recomputed,
and the page never points beyond the final page.

### 5.5 Terminal resize

The user resizes the terminal while `/ps` is open. The page capacity is
recomputed from the new viewport, the selected terminal remains selected, and
the footer remains part of the visible bounded layout.

### 5.6 Scriptable inspection

A script runs `/ps --json` or invokes the corresponding command path. It still
receives all owned records in the existing JSON schema and is not affected by
the interactive page size.

## 6. User experience contract

### 6.1 Page header and table

The interactive overlay must communicate both pagination and selection. The
exact typography may follow the current Rich style, but the semantic content
must include:

```text
Background Terminals · page 1/3 · showing 1–6 of 15
```

The table contains only the current page. It retains the existing columns for
selection marker, handle, state, label, and exit status. The marker must be
present for the selected record, including when the selected record is the
only row on a page.

For zero records, the overlay shows an explicit empty state and a footer. It
must not render a misleading page such as `page 1/0`.

### 6.2 Page capacity and bounded layout

Page size is calculated from the current available Live-region height, not
from the total record count. The implementation must reserve space for:

* the title/page indicator;
* the table header and borders;
* a bounded selected-record detail area; and
* the footer/control line.

The selected output preview must be normalized and bounded by both characters
and display lines. It must not allow embedded newlines or an unusually long
label, command, or output fragment to consume the space reserved for the
footer. If the viewport is too short for the normal detail panel, the overlay
uses a compact detail summary or hides the detail body while retaining the
selected handle, page indicator, and controls. If the viewport is narrow,
columns are shortened or cropped without wrapping the layout into an
unbounded number of lines.

The output of `render()` should fit within the height supplied by the Live
workspace for normal supported terminal sizes. For an extremely small
terminal where all content cannot be shown, the degradation order is:

1. reduce output/detail lines;
2. use compact table headings and shortened values;
3. reduce the page to one row; and
4. retain the footer and close control as the highest-priority content.

The implementation must not solve overflow by silently dropping the footer.

### 6.3 Keyboard controls

The overlay keeps the existing controls and adds explicit page navigation:

| Key | Behavior |
|---|---|
| `↑` / `k` | Select the previous record; crossing the first visible row moves to the previous page when one exists |
| `↓` / `j` | Select the next record; crossing the last visible row moves to the next page when one exists |
| `PageUp` | Move backward by one calculated page |
| `PageDown` | Move forward by one calculated page |
| `Home` | Select the first record and show page 1 |
| `End` | Select the last record and show the final page |
| `Enter` | Preserve the existing details/selection action; it must act on the selected terminal ID |
| `s` | Request stop for the selected terminal |
| `Ctrl+X` | Request stop for the selected terminal, preserving current confirmation/policy behavior |
| `Esc` | Close the overlay |

Page navigation must never select a record outside the displayed page after a
redraw. Whether `PageUp`/`PageDown` preserve the selected row offset or choose
the nearest boundary is an implementation detail, but the selected ID and
resulting page must be deterministic and covered by tests.

The footer must list the controls that are actually available in the current
compact mode. It must not promise an action that the handler silently ignores.

### 6.4 Selection identity

The selected terminal ID is the authoritative selection identity. The numeric
index is only a derived position in the current snapshot. On refresh:

* if the selected ID still exists, it remains selected;
* if it was removed, choose the nearest valid record and explain the change
  only through the normal updated view, not a duplicate transcript message;
* if the list is empty, reset selection safely; and
* a requested `selected_id` is honored only when it exists in the owned
  snapshot.

This prevents a stop key from targeting a different terminal after a record
is removed or reordered.

## 7. Technical design

### 7.1 Viewport propagation

The current `Overlay.render()` method has no size parameter. Add a backwards-
compatible viewport mechanism at the overlay/workspace boundary. The
canonical design is an optional base-overlay hook such as
`set_viewport(width: int, height: int)` or an equivalent typed viewport
object, called by `OverlayHost` whenever the Live region is measured or
resized. Existing overlays remain valid through a no-op default.

`TerminalListOverlay` stores only the latest viewport measurement and uses a
safe fallback before its first measurement. The fallback must be deterministic
for tests and must never depend on the total number of records. The workspace
continues to own terminal measurement and Rich Live redraw timing; the overlay
must not call `os.get_terminal_size()` directly or write to the terminal.

If the existing workspace can provide Rich `ConsoleOptions` more naturally
than a hook, the implementation may use that equivalent, provided the overlay
receives a reliable width and height before each render and resize. The public
`Overlay` contract must remain compatible with third-party overlays.

### 7.2 Pagination model

Use one snapshot of `TerminalManager.list_records()` for each refresh/render
operation. Derive:

```text
page_size = max(1, available_height - reserved_height)
page_count = max(1, ceil(record_count / page_size))
selected_index = index(selected_terminal_id) or nearest valid index
page_index = selected_index // page_size
visible_records = records[page_index * page_size : ...]
```

The exact reserved-height calculation may account for compact-mode changes,
but it must be centralized and unit tested. Page capacity must be recomputed
when viewport height changes. Do not copy the fixed `_PAGE_SIZE` pattern from
overlays whose content has no live detail/output panel unless the resulting
layout is proven to fit this overlay.

The manager remains the source of truth. Pagination must not mutate its
records, persistence, subscription, or ownership filtering.

### 7.3 Stable ordering and refreshes

The overlay must use the manager's documented record order or apply one
explicit stable presentation order. It must not alternate ordering between
renders. If sorting is added, the key must include stable terminal identity so
equal state/timestamps cannot cause flicker.

Manager change notifications continue to trigger redraws. Refresh logic must
not perform a stop, wait, or other side effect. A render failure caused by a
malformed record should degrade that field to a safe placeholder rather than
crashing the entire Live workspace.

### 7.4 Width and content safety

Before adding values to the table or detail view:

* collapse or replace embedded newlines in handles, states, labels, commands,
  and exit values;
* use Rich-aware shortening/cropping rather than allowing automatic wrapping;
* keep the output preview bounded by a fixed character limit and a fixed line
  limit; and
* render user/process data as literal text, not interpolated Rich markup.

Existing redaction and output-retention policies remain authoritative. This
PRD adds display bounds; it does not permit storing or printing more data.

### 7.5 Command and JSON compatibility

`_cmd_ps` keeps its current argument behavior:

* `/ps` opens the paginated overlay;
* `/ps <terminal-id>` opens it with that initial selection; and
* `/ps --json` prints the complete list using the existing record schema.

If an argument combination contains `--json`, pagination state must not affect
the JSON output. No page cursor is persisted in the session or sent to the
provider.

### 7.6 Details action

The current footer advertises `Enter details`; the implementation must align
the handler and presentation. Enter must either retain the existing details
view/callback contract or introduce a small bounded details view within the
same overlay. It must not create a second terminal-control path or lose the
selected ID. Esc must return from details to the paginated list before closing
the overlay if a details view is introduced.

## 8. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-202.1 | The interactive `/ps` overlay renders only a bounded current page and never renders every record in one unbounded table. | Must |
| FR-202.2 | The overlay displays page number, total page count, visible range, and total record count when records exist. | Must |
| FR-202.3 | All owned records are reachable through selection and page navigation. | Must |
| FR-202.4 | Up/down navigation crosses page boundaries predictably and never leaves the selected record off-page. | Must |
| FR-202.5 | PageUp/PageDown, Home, and End operate on the complete record set. | Must |
| FR-202.6 | Selection is identified by terminal ID and survives refreshes, completion events, and viewport changes when that ID remains available. | Must |
| FR-202.7 | Stop actions target the selected terminal ID from the current snapshot, including on later pages. | Must |
| FR-202.8 | The requested ID supplied to `/ps <terminal-id>` opens on the correct page when the record is owned and available. | Must |
| FR-202.9 | Record removal clamps selection and page state without producing an invalid page or targeting a replacement terminal accidentally. | Must |
| FR-202.10 | The overlay adapts its page capacity to measured width and height and retains a visible footer/control line. | Must |
| FR-202.11 | Long values, multiline output, narrow widths, and short heights use bounded compact rendering. | Must |
| FR-202.12 | Empty and one-page lists render explicit, valid states with close controls. | Must |
| FR-202.13 | `/ps --json` remains complete, unpaginated, and schema-compatible. | Must |
| FR-202.14 | Existing manager ownership, security, stop, lifecycle, and persistence behavior remains unchanged. | Must |
| FR-202.15 | The Enter/details hint matches the actual implemented key behavior. | Should |

## 9. Non-functional requirements

### 9.1 Responsiveness

Rendering and key handling must be local and bounded by the number of visible
rows plus a small amount of selection bookkeeping. A large historical record
set must not cause a proportional Rich render tree on every frame. Manager
refreshes must remain non-blocking and must not wait for subprocesses.

### 9.2 Determinism

Given the same record snapshot, viewport, and selected ID, the page metadata,
visible rows, and selected marker must be deterministic. Tests must not depend
on wall-clock timing, process scheduling, or an actual user's terminal size.

### 9.3 Accessibility and degraded terminals

The page indicator and footer must remain understandable in plain-text and
non-color output. Selection cannot be communicated by color alone; retain a
text marker or equivalent. Narrow layouts must avoid forcing a user to infer
hidden controls from clipped output.

### 9.4 Security and privacy

The change must not widen terminal visibility. Existing session/project
ownership checks, command redaction, output bounds, and workspace policy
remain in force. Page metadata may contain counts and opaque handles already
permitted by `/ps`; it must not add credentials or raw secrets.

### 9.5 Compatibility

The base overlay contract must remain source-compatible for existing overlays
and plugins. Existing `/ps` command invocation and JSON consumers must remain
valid. No new required configuration is introduced.

## 10. Test plan

Tests must be rewritten or extended against the current checkout after the
reset; historical tests must not be treated as proof of the new behavior.

### 10.1 Unit tests

Add deterministic unit coverage for:

* zero, one, exact-page, multi-page, and partial-final-page record counts;
* page-size calculation at normal, narrow, short, and compact viewport sizes;
* valid page/range labels, including empty and one-page lists;
* selection by initial terminal ID and fallback when the ID is absent;
* selection preservation when records are added, removed, reordered, or
  change state;
* selection/page clamping after the selected record disappears;
* up/down boundary transitions, PageUp/PageDown, Home, and End;
* stop and Ctrl+X callbacks receiving the correct later-page terminal ID;
* bounded output/detail rendering with multiline and very long values;
* literal rendering of values that resemble Rich markup; and
* compact behavior when the available height is too small for the normal
  detail panel.

### 10.2 Integration tests

Add tests that:

* dispatch `/ps` with a manager containing more records than one page and
  verify that a `TerminalListOverlay` is installed;
* open `/ps <terminal-id>` and verify the requested row/page is selected;
* exercise a real `TerminalManager` change notification while the overlay is
  mounted and verify selection remains safe;
* select and stop a record that is not on page one;
* verify `/ps --json` includes every record regardless of interactive page
  size; and
* verify manager ownership filtering is unchanged.

### 10.3 Rendering and end-to-end tests

Use fixed Rich console/Live dimensions or a pseudo-terminal to render at
least:

* the 15-record example from the problem report;
* a normal terminal with enough rows for several pages;
* a short terminal where the detail section must compact;
* a narrow terminal with long labels and command/output text; and
* a resize from short to tall and back while a selection is active.

Assertions must verify that the rendered height is bounded by the supplied
viewport (within the workspace's documented border allowance), the footer is
present, page navigation can reach the final record, and the correct record
is stopped. An end-to-end test must also verify Esc closes the overlay from
the list and, if a details view is retained, returns from details correctly.

No test may spawn arbitrary host processes merely to create table rows; use
deterministic manager/record fixtures for layout tests and the existing
isolated terminal manager fixture for lifecycle behavior.

## 11. Acceptance criteria

| ID | Acceptance criterion |
|---|---|
| AC-202.1 | With 15 records, `/ps` shows a bounded first page, a page/range/total indicator, and the footer in the visible viewport. |
| AC-202.2 | The user can reach and select every record using Up/Down and PageUp/PageDown; the final record appears on the final page. |
| AC-202.3 | `s` and `Ctrl+X` stop exactly the selected record on any page. |
| AC-202.4 | `/ps <owned-id>` opens directly on the page containing that ID and marks it selected. |
| AC-202.5 | Removing the selected record during the overlay lifetime produces a valid nearest selection and page without a crash or accidental stop target. |
| AC-202.6 | Resizing the terminal recomputes visible rows while preserving selection by terminal ID. |
| AC-202.7 | Long/multiline labels, commands, and output do not push the footer below the supported viewport or create unbounded wrapping. |
| AC-202.8 | Empty and one-page lists show valid page metadata, an understandable empty/normal state, and a visible Esc/close control. |
| AC-202.9 | `/ps --json` remains unpaginated and contains all records with its existing schema. |
| AC-202.10 | Records from another session/project remain absent and no new process-discovery path is introduced. |
| AC-202.11 | Existing terminal lifecycle integration tests and stop/wait behavior continue to pass. |
| AC-202.12 | Unit, integration, and rendering/E2E regression tests cover the page math, keyboard controls, resize behavior, content bounds, and the 15-record reproduction. |
| AC-202.13 | Relevant TUI/background-session documentation and the PRD index are updated, and the implementation passes the applicable lint, type, and test gates. |

## 12. Implementation plan

1. Add the minimal backwards-compatible viewport propagation hook to the
   overlay/workspace boundary and test that existing overlays still render.
2. Refactor `TerminalListOverlay` around a snapshot, stable selected ID,
   viewport-derived page model, and bounded page projection.
3. Add compact width/height rendering for the table, selected details, and
   footer. Reconcile the Enter/details behavior with the visible help text.
4. Add page-aware keyboard handling while retaining stop, Ctrl+X, and Esc
   behavior.
5. Add unit, integration, and fixed-dimension rendering tests, including the
   15-record regression case.
6. Update the `/ps` documentation and TUI/background-session guide with page
   navigation and the unchanged `/ps --json` contract.
7. Run the source-surface gates from `AGENTS.md`: Ruff check/format, mypy,
   type audit, unit/integration/E2E suites, and the relevant nox sessions.

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| A fixed reservation still overflows on short terminals | Measure viewport height, centralize page math, add compact modes, and assert rendered bounds at multiple heights. |
| Refreshes move the stop target | Make terminal ID authoritative and test removal/reordering during selection. |
| Rich wraps values unexpectedly | Normalize, shorten, and use non-wrapping columns; test narrow widths with embedded newlines. |
| Adding viewport state breaks third-party overlays | Make the hook optional/no-op and keep `render()`/`handle_key()` signatures compatible. |
| Pagination hides information users expect from `/ps` | Show range and total counts; keep `/ps --json` complete and document the distinction. |
| Page navigation conflicts with existing controls | Use canonical `Key.PAGE_UP`, `Key.PAGE_DOWN`, `HOME`, and `END`; preserve existing `s`, Ctrl+X, and Esc mappings. |
| Detail output consumes the layout budget | Apply independent character and line bounds and compact/hide it before sacrificing the footer. |

## 14. Documentation updates

The implementation must update:

* `docs/guides/background-sessions.md` with `/ps` page indicators,
  navigation, selection behavior, and the distinction between interactive and
  JSON output;
* `docs/guides/tui.md` or `docs/tui-architecture.md` with the viewport-aware
  overlay contract and resize behavior;
* `README.md` if the built-in command table or user-facing `/ps` guidance is
  changed; and
* `prds/README.md` with this PRD entry.

No public Python export is required by this PRD. If implementation introduces
one, update `llms.txt`/`llms-full.txt` and run the documented export gate.

## 15. Definition of done

The work is complete when:

* the 15-record reproduction renders a bounded, navigable `/ps` overlay with
  visible controls;
* all functional and acceptance criteria above are verified by deterministic
  tests;
* `/ps --json`, ownership, lifecycle, stop, wait, and persistence contracts
  remain compatible;
* narrow, short, resized, empty, and long-content layouts are covered;
* the relevant source gates pass without known regressions; and
* the user-facing and architecture documentation is updated alongside the
  implementation.
