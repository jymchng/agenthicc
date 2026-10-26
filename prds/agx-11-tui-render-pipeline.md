---
title: "AGX-11: TUI render pipeline — frame scheduling, render caches and synchronized output"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P1
series: AGX
related_prds: [agx-00, agx-05, agx-06, agx-14, prd-60, prd-164, prd-165, prd-166]
tags: [tui, rendering, performance, latency]
---

# AGX-11 — TUI render pipeline: frame scheduling, render caches and synchronized output

## Implementation status (2026-10-23)

Implemented. Rich's `Live` is gone from the workspace; the block is drawn by
`agenthicc.tui.workspace.live_region.LiveRegion`.

- **Frame scheduler (§5.1).** `tui/workspace/frames.FrameScheduler` merges
  redraw requests into at most one frame per `[behaviour] frame_interval_ms`
  (default 16 ms). Input, cursor, paste and overlay changes request an
  *immediate* frame, drawn on the next loop iteration even inside the
  interval. The session tick (spinner and elapsed time) runs at 10 Hz and the
  frame signal still only advances while a turn or compaction runs. Stopping
  the workspace draws a frame still waiting out the interval, as Rich did.
- **Render caches (§5.2).** `StatusComponent` and `FooterComponent` produce
  their rows as markup (`markup_lines(cols)`); the workspace renders each
  distinct `(markup, width)` once into segment lines and reuses it, so a
  spinner frame renders one row and an unchanged footer parses no markup.
  `visible_len`, `fit`, hint building and notification wrapping are cached.
  Overlays still render each frame they are visible.
- **Virtualised composer (§5.3).** The composer folds each logical line at the
  width (continuation rows indented under `❯`) with the fold cached per line,
  and shows at most `[behaviour] composer_max_rows` rows (default 12) around the
  cursor, moving the window only when the cursor leaves it, with
  `↑ N more lines` / `↓ N more lines`. **Visible change:** a long line now
  folds at the cell boundary instead of Rich's word wrap, so `height()` is
  exact rather than an estimate. The agents screen's composer is the same
  implementation through `render()`.
- **Line-diffing writer (§5.4).** `LiveRegion` keeps the lines it wrote and
  rewrites only changed rows (erase, then write, so a full-width row is never
  clipped), appends or clears rows when the height changes, and falls back to a
  full redraw after a resize, a width change or when more than half of the rows
  changed. Output printed while it is live goes through its render hook —
  erase, print, repaint below — and stdout/stderr are redirected to the console
  as Rich did. Frames are written through the console, so Rich still translates
  styles and controls (including for legacy Windows consoles).
- **Synchronized output (§5.5).** The first POSIX raw-mode entry in a process
  sends DECRQM for mode 2026; `read_key` consumes the DECRPM answer instead of
  returning it as a keystroke (it previously would have decoded as `Esc`) and
  records it in `tui.terminal.capabilities`. When supported, every write —
  frames and scroll-back repaints — is bracketed. **Deviation:** Windows is not
  asked, because its console input would deliver the answer as typed
  characters.
- **Diagnostics (§5.6).** `/perf` prints frames drawn, full redraws, render and
  write p50/p95, characters per frame and the synchronized-output state.
  `scripts/benchmark_tui_render.py` measures the budgets below for AGX-14.

Measured with `scripts/benchmark_tui_render.py` (120×40, 300 samples) on the
development host at load average 60–80 on 8 vCPUs. Wall-clock tails there are
scheduling stalls, so per-frame CPU (`thread_time`) is reported alongside:

| Measure | Result | Budget |
|---|---:|---:|
| Keystroke echo, 10,000-character prompt | CPU p50 0.57 ms, p95 1.15 ms (wall p95 7.0 ms) | p95 ≤ 16 ms |
| Keystroke echo, 200-line prompt | CPU p50 0.59 ms, p95 1.31 ms (wall p95 7.1 ms) | p95 ≤ 16 ms |
| Keystroke echo, 4,000 characters | CPU p95 0.64 ms (wall p95 0.74 ms) | p95 ≤ 16 ms |
| Frame after a state change | CPU p95 0.25 ms | p95 ≤ 5 ms |
| Frames while idle | 0 in 20 s | 0 |
| Spinner frame | 54 bytes, CPU p95 0.12 ms | ≤ 200 bytes |
| CPU while animating | 2.7% of a core (a bare asyncio loop waking at 10 Hz costs 1.0% on this host) | ≤ 2% |

The animation figure is inflated by the host: the same loop with nothing to
render costs 1.0%, about forty times what it costs on an idle machine, and the
render work itself is ~0.1 ms per frame at 10 Hz. AGX-14 adds these budgets to
CI on a quiet runner.

Tests: `tests/unit/test_live_region.py` (60-seed property test: diffed frames
leave the same `pyte` screen as full redraws, with scroll-back output
interleaved; an unchanged frame writes nothing; transient stop; stdout while
live; bracketing only when supported; a spinner frame rewrites one row in at
most 200 bytes; typing echoes inside the interval; the footer parses markup
only when a row changes; `/perf`), `tests/unit/test_multiline_composer.py`
(folding, wide characters, the viewport, typing re-folds one line),
`tests/unit/test_terminal_capabilities.py` (DECRPM values, malformed reports,
one query per process) and the rewritten workspace redraw, layout and
approval-wait tests.


## 1. Summary

The TUI rebuilds and rewrites its entire live block whenever any of about 30
signals changes, and 20 times a second while an agent runs, because a 50 ms
animation tick bumps a frame counter. Nothing is cached between frames: the
footer re-parses Rich markup on every frame to fit its hints, and the composer
re-wraps the whole buffer on every keystroke, so a 200-line prompt costs ≈ 83 ms
per key. Introduce a frame scheduler, per-component render caches, a virtualised
composer, a line-diffing writer for the live region and synchronized output, so
redraws cost what changed rather than what is on screen.

## 2. Problem and evidence

### 2.1 Pipeline today

- `Workspace` (`src/agenthicc/tui/workspace/workspace.py`) owns one Rich `Live`
  with `auto_refresh=False`. It subscribes `_redraw` to ~30 signals
  (agent state, tokens, cost, input buffer and cursor, overlay, approvals,
  workflow progress, subagent pool, terminal waits, …). `_redraw` coalesces
  calls within one event-loop tick (`loop.call_soon`) and then calls
  `Live.update(self._build(), refresh=True)`.
- `_build()` constructs the full `Group` — status, composer, footer, overlays —
  and Rich's `LiveRender` moves the cursor to the top of the block and rewrites
  every line.
- While an agent is running, `TUISession._tick` sleeps 50 ms and calls
  `conversation.tick()`, which increments the `frame` signal
  (`src/agenthicc/tui/conversation_store.py:372`); each increment triggers a
  full rebuild and rewrite for the spinner.
- There is no frame-rate cap, no per-component cache and no synchronized-output
  bracketing.

### 2.2 Measured

One `_build()` plus Rich render at 120×40 (AGX-00 §4.3):

| State | p50 | p95 |
|---|---:|---:|
| Idle live block | 2.0–2.7 ms | ≈ 78 ms |
| Composer, 4,000 characters | 5.3 ms | 81 ms |
| Composer, 200 lines | 83 ms | 103 ms |

The idle tail persists with garbage collection disabled. A profile attributes
about 60% of render time to `FooterComponent` → `_build_hints`
(`src/agenthicc/tui/workspace/components.py:562`), which pops hint segments in
a loop and calls `visible_len` → `Text.from_markup` on each candidate, and then
`_fit` parses again — six markup parses per frame, uncached.

At 20 animation frames per second and ≥ 2 ms per frame, an active turn spends
≥ 4% of a core redrawing a spinner; the long tail makes some frames visibly
late.

## 3. How others render

- **Claude Code** rewrote its renderer to diff terminal cells and emit minimal
  escape sequences, keeping inline rendering (not the alternate screen) to
  preserve native search, selection and copy; it used property-based tests
  comparing old and new output, and later added an opt-in fullscreen mode
  (*secondary sources*).
- **Codex** added a frame scheduler that coalesces redraw requests and clamps
  them to a perceptible rate, and caches wrapped transcript lines and
  rasterised rows after finding that re-segmenting unchanged text burned CPU
  while streaming; it keeps a performance-testing playbook for idle and
  streaming CPU (*commit mirror*).
- **OpenCode** (OpenTUI) renders only what changed between frames and targets
  60 FPS.

## 4. Goals and non-goals

**Goals**

1. Redraw cost is proportional to what changed.
2. Keystroke echo stays within one 60 Hz frame for prompts up to 10,000
   characters, including multi-line prompts.
3. An idle TUI produces no frames; an animating one writes only the changed
   cells.
4. Frames are atomic on terminals that support synchronized output.

**Non-goals**

- Switching to the alternate screen by default; inline rendering and native
  scroll-back stay (an opt-in fullscreen mode may follow).
- Replacing Rich for content rendering (Markdown, syntax highlighting).

## 5. Design

### 5.1 Frame scheduler

A single `FrameScheduler` owns redraws for the TUI and the agents hub:

- Signals mark components dirty instead of calling `_redraw`.
- The scheduler renders at most once per frame interval (default 16 ms,
  configurable), merging all dirty marks.
- Input echo uses an immediate path: a key event renders on the next loop
  iteration even inside the interval.
- Animations request frames only while visible and at their own rate (spinner:
  10 Hz instead of 20 Hz).

### 5.2 Component render caches

Each component declares the signals it reads. Its rendered lines are cached by
`(signal versions, width)` and reused when unchanged:

| Component | Cache key | Effect |
|---|---|---|
| Footer hints | `(raw hint string, columns)` | Markup parsed once per change, not per frame |
| Status bar | Its signal versions | Spinner frames re-render one cell group |
| Composer | `(buffer version, cursor, width)` plus per-line wrap cache | See §5.3 |
| Overlays | Overlay state version | Unchanged overlays cost nothing |

`visible_len` and `fit` gain a small LRU cache for repeated markup strings.

### 5.3 Virtualised composer

The composer wraps and renders only the lines in its viewport (a configurable
maximum height, default 12 rows) around the cursor, with a `↑ 188 more lines`
indicator; wrapped lines are cached per logical line and invalidated only for
edited lines.

### 5.4 Line-diffing live writer

Replace Rich `LiveRender`'s rewrite-everything strategy with a writer that keeps
the previous frame's lines and, for the next frame:

1. renders the dirty components to lines (cached ones are reused);
2. moves the cursor to and rewrites only lines that differ, erasing to end of
   line;
3. handles height changes by appending or clearing trailing lines;
4. falls back to a full redraw after a resize or when more than half of the
   lines changed.

Scroll-back printing (`ScrollBufferAppender`) stays as it is: committed history
is printed above the live block, then the writer re-anchors.

### 5.5 Synchronized output

At start-up the terminal is queried with DECRQM for private mode 2026. When
supported, each frame is wrapped in `CSI ? 2026 h` … `CSI ? 2026 l`, so the
terminal presents it atomically (no tearing in tmux or slow terminals).
Unsupported terminals are unaffected.

### 5.6 Diagnostics

`/perf` shows frame counts, render and write time percentiles and bytes per
frame for the session; the same counters feed AGX-14's benchmarks.

## 6. Budgets and acceptance criteria

| Measure (reference host, 120×40) | Budget |
|---|---:|
| Keystroke echo render, prompt ≤ 10,000 characters (incl. 200 lines) | p95 ≤ 16 ms |
| Idle live-block render when something changed | p95 ≤ 5 ms |
| Frames while idle, 60 s | 0 |
| Spinner frame bytes written | ≤ 200 bytes |
| CPU while a turn waits on the provider (animation only) | ≤ 2% of one core |

1. The footer parses markup only when its hint string or width changes
   (asserted by a counter in tests).
2. With a 200-line prompt, typing a character re-wraps only the edited line.
3. A spinner frame rewrites only the status line (asserted by capturing the
   terminal stream).
4. On a terminal answering DECRQM 2026 as set/reset-able, every frame is
   bracketed; on others no 2026 sequences are emitted.
5. Golden-output equivalence: for a corpus of UI states, the diffed output
   reproduces the same final screen as a full redraw (property test, as Claude
   Code did for its renderer).

## 7. Test plan

- Property test with a virtual terminal emulator in the test suite, comparing
  screen contents after diffed and full redraws for random state sequences.
  (The removed `render_frame_ansi` frame contract stays removed; this compares
  the writer's two strategies against each other, not against stored frames.)
- Micro-benchmarks for footer, status and composer renders in AGX-14.
- Stream-capture tests for spinner frames and synchronized-output bracketing.

## 8. Risks

- **Cursor bookkeeping.** A diffing writer must track exactly what is on screen;
  terminal wrapping of over-wide lines is the classic failure. Mitigation:
  lines are rendered to the exact width and never rely on terminal wrapping;
  any uncertainty forces a full redraw.
- **Interaction with scroll-back printing.** Mitigation: the writer is the only
  component that moves the cursor; appender output goes through it.

## Sources

- Claude Code renderer rewrite (secondary): <https://steipete.me/posts/2025/signature-flicker>
- Codex transcript render caches (commit mirror): <https://forge.lthn.ai/core/core-agent-ide/commit/90f37e854992c14d66147787f92b90f604246e42>
- OpenTUI performance: <https://www.mintlify.com/anomalyco/opentui/advanced/performance>
- Synchronized output (mode 2026) specification: <https://gist.github.com/christianparpart/d8a62cc1ab659194337d73e399004036>
