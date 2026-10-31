---
title: "BEAU-3: Tool Activity, Exploratory Groups, and File Changes"
status: Proposed
version: 2.0.0
created: 2026-10-31
scope: "specify render blocks for tool_complete, generic overflow, PRD-161 exploratory groups, and file_modified"
related_prds: [BEAU-1, PRD-101, PRD-161, AGX-11]
tags: [tui, scrollback, tools, diffs, accessibility]
---

# BEAU-3 — Tool Activity, Exploratory Groups, and File Changes

## 1. Problem and objective

Tool rows and file diffs are evidence of work, but large output can bury the
operation, result, or changed path. `tool_complete` currently prints an
operation, argument text, status, up to four numbered output lines, a count,
and a separator. Generic overflow hides later tool bodies. PRD-161 separately
consolidates explicitly marked exploratory successes. `file_modified` delegates
to an existing bounded diff renderer.

This PRD makes action and outcome scannable, gives exact target blocks for each
state, and preserves PRD-161's data/grouping invariants. It adds no diff engine,
details overlay, or tool state.

## 2. Research applied

- **Claude Code** documents expandable tool results in fullscreen. Borrow the
  concise operation plus subordinate preview; do not promise click expansion in
  native scrollback.
- **OpenCode** documents `/details` and adaptive/stacked diff styles. Borrow the
  width-safe stacked form and clear primary/detail hierarchy, not its command,
  theme, or interactive toggle.
- **Codex CLI** encourages inspecting commands/diffs as they appear but does not
  specify their exact row format. Keep action, outcome, and changed-file evidence
  adjacent without attributing styling to Codex.
- **Qoder** describes following commands/interim results and reviewing a diff;
  its CLI hides lower-priority status items when narrow. Borrow inspectability
  and progressive width reduction.
- **Grok Build** documents a separate bottom live-context line, not a tool card.
  Keep status separate from scrollback.
- **DeepSeek Harness** documentation contains no terminal tool/diff block.
  Do not invent its visual style. See the evidence matrix in BEAU-1.

Sources: [Claude Code fullscreen](https://code.claude.com/docs/en/fullscreen),
[OpenCode TUI](https://opencode.ai/docs/tui/),
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli),
[Qoder CLI](https://docs.qoder.com/cli/interface.md),
[Qoder overview](https://docs.qoder.com/qoder/overview), and
[Grok Build](https://docs.x.ai/build/overview).

## 3. Event and state coverage

| Surface | Current facts | Target |
|---|---|---|
| `tool_complete` | Operation, `args_str`, success/state/ready, duration, output preview, omitted-line count; optional exploratory metadata. | Operation headline; explicit status; bounded preview. |
| Generic overflow | Default max 5 rendered calls in a contiguous group; live overflow count flushes at boundaries. | One summary; preserve count and boundary semantics. |
| Exploratory group | Marked contiguous successful events; PRD-161 owns 12-row cap, target bounds, feature flag, failure and replay rules. | Retain `Explored` + tree children; harmonize only shared tokens. |
| `file_modified` update | Before/after lines; `render_file_diff` summarizes and renders bounded unified context. | Existing one-column diff and truthful summary. |
| `file_modified` create | `render_file_create` previews up to 10 lines with an omitted-lines marker. | Keep header, summary, bounded numbered preview. |
| `file_modified` fallback | Missing snapshots yield operation/path, not a fabricated diff. | Preserve truthful fallback. |

## 4. Normative render blocks

Color notes are descriptive. Rendered text must carry the meaning without ANSI.

### 4.1 Completed tool with output

```text
● Read (src/agenthicc/tui/workspace/appender.py)
  └─ ✓ Completed · 0.8s · 37 lines
       1   class ScrollBufferAppender:
       2       """Subscribes to conversation events."""
       3       def mount(self) -> None:
       4           self._unsub = self._state.conversation.on_event(...)
       ⋯ +33 more lines
```

Use the existing operation mapping (`Read`, `Search`, `Run`, `Test`, `Update`,
`Delete`, etc.) and safe argument representation. Keep the operation heading
neutral; reserve green for confirmed success. Put state, duration, and count on
a subordinate row when they fit. Keep output attached, ordered, escaped, and
bounded to the current four preview lines. Empty output has a result status but
no empty preview. An omitted preview must say how much is hidden. Numbered rows
are preview positions, not source-file line numbers. Never expose arguments or
results absent from the event's safe projection.

### 4.2 State distinctions

```text
● Run (uv run agenthicc serve)
  └─ ⟳ Running · service startup

● Run (uv run agenthicc serve)
  └─ ✓ Ready · 1.2s

● Wait (terminal-7)
  └─ … Waiting

● Run (uv run pytest -q)
  └─ ✗ Timed out · 30s · 4 lines
```

| Event fields | Visible status |
|---|---|
| `state == "running"`, `ready == true` | `Ready` |
| `state == "running"` | `Running` |
| `state == "waiting"` | `Waiting` |
| `state == "timed_out"` | `Timed out` |
| `state == "cancelled"` | `Cancelled` |
| `state == "starting_timeout"` | `Readiness timeout` |
| no state and success; or `state == "exited"` and success | `Completed` |
| otherwise / `success == false` | `Failed` |

The status word is mandatory. Output on a failed call does not convert failure
to success. A heading must not imply that an action was approved or safe.

### 4.3 Generic overflow

```text
● Read (a.py)
  └─ ✓ Completed

● Search (needle, path=src)
  └─ ✓ Completed

  …and 7 more tool calls
```

Keep the current configured maximum, counter, and flush boundaries. Emit one
summary at the boundary, then reset; print it before the next turn, error,
assistant response, or shutdown. Clear the live-footer count using the existing
owner. Do not double-count exploratory calls handled by PRD-161.

### 4.4 PRD-161 exploratory block

```text
● Explored
  └ Read command.py, builtins.py, and 5 more files
  └ Search def _emit|event_sinks|run_sinks in _runner.py
  └ Read agent_turn.py
  └ Search async def _emit|def _emit in _runner.py
  └ Read _signals.py
  └ Read _runner.py, agent_turn_context.py
    …and 4 more exploratory calls
```

PRD-161 remains normative for classification, data, event granularity, order,
redaction, caps, failure behavior, feature flag, flush boundaries, replay, and
counting. BEAU-3 applies the same typography and indentation only. Do not fold a
failure or unmarked tool into this success group.

### 4.5 File update diff

```text
● Update(src/agenthicc/tui/rendering.py)
└─ Added 1 line, removed 1 line

  264   # context line
  265   def visible_len(value: str) -> int:
  266 -     return len(value)
  266 +     return cell_len(value)
  267
  ...   12 more diff lines
```

The existing `render_file_diff` is canonical. Preserve its operation/path
header; added/removed summary; context; line numbers; `+`/`-` markers; paired
word highlighting; omission row; and final separation. Its current changed-row
preview is six rows per contiguous block (first three and last three with an
omission row); context is controlled by `context`. Full snapshots remain in the
event and are not printed.

Use a single stacked column, consistent with the width-safe option documented
by OpenCode; do not make a side-by-side diff in append-only scrollback. Preserve
the existing syntax renderer and do not derive a diff from truncated output.

### 4.6 File creation and fallback

```text
● Create(src/new_module.py)
└─ Created 42 lines

  1 + from pathlib import Path
  2 +
  3 + def load_config(path: Path) -> str:
  4 +     return path.read_text()
  5 +
  6 +
  7 + class ConfigError(ValueError):
  8 +     pass
  9 +
 10 + DEFAULT_NAME = "agenthicc"
  ⋯ +32 more lines
```

If snapshots are absent:

```text
  Update: src/unknown.py
```

Keep the current 10-line creation preview, total count, and omission label. Do
not draw an empty/fabricated diff when snapshots are unavailable.

## 5. Width and safe projection

- Measure terminal display cells for operation, path, arguments, status and
  output; do not rely on Python character count.
- At 120 columns show the full safe target, status and bounded preview. At 80,
  preserve status and primary target before secondary arguments. At 48–64,
  shorten the low-priority target tail or preview before status. At 40, wrap
  below the output body or visibly omit; never wrap under a status glyph.
- Keep `+`, `-`, line numbers, and outcome words in ANSI-off output. Hard output
  row limits apply before wrapping, so a wide or long result cannot become
  unbounded.
- Escape paths, arguments, labels, and tool output. Preserve existing redaction;
  never consult raw provider messages or re-fetch results for display.
- Visual compression changes no model-visible result, journal record,
  idempotency, workflow checkpoint, event count, permission decision, or
  snapshot.

## 6. Non-goals

- Changing execution, approvals, capabilities, retries, retention, event
  payloads, or PRD-161 classification/behavior.
- Adding an interactive detail toggle, pager, mouse mode, side-by-side diff, or
  second diff implementation.
- Rendering failed exploration as a successful group.

## 7. Acceptance criteria

1. Success, ready, running, waiting, timeout, cancellation, readiness timeout,
   and failure match §4.2 and remain understandable without color.
2. Tool previews obey existing bounds, include omission counts, and keep full
   results canonical.
3. Generic overflow and PRD-161 groups remain separate, honor their own counts,
   flush at existing boundaries, and replay deterministically.
4. Update/create/fallback output matches §4.5–4.6 and preserves diff content and
   ordering.
5. Labels, targets, statuses, and diff markers are legible at 40/48/64/80/120/
   200 columns; test wide glyphs, long paths, ANSI-off, and screen reader.
6. Test empty/malformed outputs, failure with output, running/waiting states,
   max calls, boundary flush, shutdown flush, create/update/fallback, and replay.
7. No result, count, event, permission decision, or file snapshot changes.

## 8. Verification

Extend `tests/unit/test_appender_tool_collapse.py` and
`tests/unit/test_diff_renderer.py`. Add a combined golden sequence with
exploratory reads, a command, a failed call, file update, and assistant response.
Preserve PRD-161 integration/replay tests. No provider or network access is
required.
