---
title: "TUX-02: Screen layout and one status line"
status: Implemented
version: 1.0.0
date: 2026-11-05
scope: "the live region below the transcript: the composer frame, a single prioritised status line, one hint row, the session title, a context meter, a user statusline command, and the hub/composer hint conflict"
related_prds: [TUX-00, TUX-01, TUX-03, TUX-10, AGX-10, AGX-11, AGX-13, PRD-75, PRD-81]
tags: [tui, layout, status-line, ergonomics]
---

# TUX-02: Screen layout and one status line

## Implementation status

Implemented on branch `tux-tux02`.

- **Code.**
  - `StatusComponent` and `FooterComponent` are deleted. `StatusLine` and `ComposerFrame` replace them in `tui/workspace/components.py`.
  - New: `tui/statusline.py` (user command, git branch, `/status` report) and `tests/unit/test_status_line.py` (§4 criteria 1–8).
- **Live region.** Idle it is exactly four rows: three for the framed composer and one status line. There is no blank separator and no bottom margin row (the scroll buffer already ends each block with a blank row). A running turn adds one hint row.

### Amendments to the design above

- **Workflow segment (operator request, 2026-11-07).** The running workflow and its phase move from segment 5 to priority 2 (`◆ auto → Finalize goals 3/5`). When the line is narrow the segment shortens to the phase, then to the workflow, and is not dropped first. It is also shown under a user status-line command, and while paused.

- **Composer frame (operator request, 2026-11-06).**
  - Rules are now the default, matching Claude Code's input panel: a full-width rule ending in `title · Safe ─`, the prompt with no side borders, and a rule below.
  - The rounded box is still available with `[ui] composer_frame = "box"`.
- **State segment (operator request).** It keeps agenthicc's original turning flower. While thinking, one bold letter sweeps back and forth across `Thinking`. This replaces the braille spinner.

1. **Right-hand hint.**
   - It shows `? shortcuts` only once the keymap has a `composer.shortcuts` action. That binding is TUX-10's (`?` on an empty prompt).
   - Until then it shows `/help`, so the status line never advertises a key that does nothing.
2. **Attention wording.** The segment reads `◆ N sessions need you ←` (short forms `◆ N sess. ←`, `◆ N ←`), not "agents": TUIR-05 requires the badge to name the unit it counts.
3. **Session title.**
   - AGX-05 generates titles only for background sessions (`title or intent[:80]`). A foreground session has no generated title.
   - The frame title is therefore the session's first prompt, cut by the same rule (`session_title_from`, first line, 80 characters), or "new session". Slash commands are ignored.
   - A resumed transcript restores the title.
4. **Segment 5 contents.**
   - It holds the subagent workers, the workflow phase, the `/workflow` selection (`workflow <name>`), running background sessions (`● N sessions running`), DND, startup work still loading, and the background-terminal count during a terminal wait.
   - The primary agent (AGX-09) moved into the frame badge: `reviewer · Safe`.
5. **Hint row.**
   - It shows only *contextual* keymap actions (those with a `when` condition: running, paste, subagents), plus `Esc interrupt · /ps view · /stop stop all` during a background-terminal wait.
   - The always-available keys are in `/help`.
   - Notices (notification, `/loop` status, attention toast) take the hint row's slot, wrapped whole so run IDs stay usable. TUX-08 can later move short notices to the right of the status line.
6. **`← agents` hint deleted.** The `agents_hint` signal and the router's 10-second hint deadline are deleted, with their test.
7. **Waiting state.** It reads `◆ Waiting for you` with no elapsed time, so a prompt that owns the terminal stays frozen. While work runs, elapsed time is shown from `0s`, so the row does not shift on the first tick.
8. **Overlays.**
   - `Overlay.owns_bottom_rows` (true for `AgentsHubOverlay`) hides the frame and the composer's hint row. The status line and any notice stay above the hub's rows.
   - Every other overlay is drawn between the frame's two rules, as before.
9. **AGX-11 bytes.**
   - `LiveRegion` now patches a changed row in place. Segments shared at the start and end of the row are not rewritten, so a spinner frame writes one cell, under 80 bytes.
   - A pyte fuzz test proves the screen matches a full redraw.
   - Frame caches are keyed by markup, width and theme. Segments 2–7 are cached on the signal values they read.
10. **Context meter.**
    - Divides `UsageLedger.context_tokens()` by `ExecutionSettings.effective_context_window()`. The tokens are the latest main-agent call's input plus cache reads and writes; subagent, compaction and run-residual records are skipped. The window is set once at session start.
    - A workflow phase that uses another model still uses the session's window.
11. **`/status`.** It extends the existing read-only `/status` command rather than adding a second one. `CommandContext.app_state` carries the TUI state to it.
12. **User status-line sandbox.**
    - The command runs with the agent's `[tools.exec]` child environment (secrets scrubbed, non-interactive) and its resource limits, in its own process group, which is killed at 500 ms.
    - There is no separate network sandbox: Bubblewrap applies only to worktree environments.
    - Trust: `load_config` drops a project file's `[ui.statusline] command` unless `agenthicc trust add <file>` trusted that file's current content.
13. **New glyph.** `Glyphs.approx` (`≈`, ASCII `~`) marks partial usage, so the ASCII glyph set stays ASCII.
14. **Screenshots.** The screenshots in §6 were not produced. The width snapshots are text tests (`test_width_snapshot[40|50|80|120]`).


## 1. Problem

From TUX-00: F5, F6 and F8. See `screens/agt-idle.png` and `agt-hub.png`.

**The idle live region takes 11 rows** (from the blank separator to the hint row, `screens/agt-idle.png`). `StatusComponent.markup_lines` (`tui/workspace/components.py:259`) draws:

1. `✿ Idle` (followed after a turn by `| ↑ 0 ↓ 0 ?`);
2. the model, `anthropic | claude-opus-4-8`;
3. the session UUID and the working directory, on one or two rows (`_identity_markup_rows`);
4. `0 turns │ cost unknown │ usage unavailable`. This shows in warning colour before any request is made, and it is the same text on every provider that reports no cost.

`FooterComponent.markup_lines` (`components.py:725`) adds:

- a mode row;
- an attention row;
- a hint row, which lists both `Ctrl+G agents` and `← agents` (`:785`).

The welcome card already shows the model and directory.

**What the user acts on.** In practice the user needs:

- the state (idle, working, waiting for them);
- the permission mode;
- the model;
- how full the context is;
- whether background agents need them.

The session ID is useful only when resuming, which `agenthicc sessions` already lists. A cost of "unknown" is not information.

**Comparisons.** The other tools spend 1–3 rows:

- Claude Code (`screens/cc-idle.png`) uses one status line: mode and background agents on the left, effort on the right.
- OpenCode (`oc-idle.png`) puts agent · model · effort inside the prompt card, and cwd:branch and version in a bottom bar.
- Qoder shows model · workspace · ±lines, drops segments by priority when narrow, and can replace the line with a script.

**Conflicting hints.** When the hub opens inline, the composer's hint row stays visible under the hub's own hints (`agt-hub.png`), so the screen shows two sets of keys.

## 2. Goals

### 2.1 The frame

```text
 ╭─ fix flaky scheduler test ──────────────────────────────────────── Safe ─╮
 │ ❯ ask, or type / for commands, @ for files                               │
 ╰──────────────────────────────────────────────────────────────────────────╯
  ● Idle · opus-4.8 · ctx 12% ▰▱▱▱▱ · 2 agents need you ←      ? shortcuts
```

The live region is:

- **The composer**, framed by a rounded box in the `subtle` token, or by two rules when `[ui] composer_frame = "rules"`.
  - The top border carries the **session title** on the left. It is AGX-05's generated title, or "new session" until there is one.
  - The **mode badge** sits on the right, in its `mode_*` token.
  - The empty composer shows a muted placeholder: "ask, or type / for commands, @ for files". TUX-03 rotates in suggestions.
- **One status line**, with segments in a fixed priority order. When the line is too narrow, segments drop from the lowest priority; they are never truncated mid-word:

  | Priority | Segment | Shown when |
  |---|---|---|
  | 1 | State: `● Idle` / spinner + verb + elapsed (TUX-10) / `◆ Waiting for you` | always |
  | 2 | Background attention: `N need you ←` (AGX-10) | N > 0 |
  | 3 | Context meter: `ctx 42%` plus a 5-cell bar, `success` < 60 % ≤ `warning` < 85 % ≤ `danger` | after the first response |
  | 4 | Model, short form (`opus-4.8`, provider dropped when it is the default) | always |
  | 5 | Running workers `3/5 workers`, workflow phase `2/4 implement` | when active |
  | 6 | Tokens `↑12k ↓3k` and cost `$0.42` | only when the provider reported them |
  | 7 | Git branch and dirty mark `main*` | in a repository |
  | right | `? shortcuts`, or a transient notice (TUX-08) | always, unless narrow |

- **One hint row**, at most. Idle shows nothing beyond `? shortcuts`.
  - While a turn runs, it shows `esc interrupt · ctrl+b background`.
  - With a condensed paste, it shows the paste keys.
  - The duplicate `← agents` hint goes: AGX-10's attention segment already says `←`.

### 2.2 Removed or moved

- The session UUID and full working directory move out of the status area. They appear in the welcome header (TUX-03) and in `/status`, a new read-only command that prints:
  - the session ID, cwd and branch;
  - the model and provider;
  - the mode;
  - turns, tokens, cost (or "not reported by <provider>") and context use;
  - the paths of the log and memory.
- **"cost unknown" and "usage unavailable" are never shown on the status line.** They are absence, not state, and `/status` explains it. Partial usage (`usage_status == "partial"`) appears as `≈` before the token count.
- The separate workflow row (PRD-81) becomes status segment 5, and the mode row becomes the frame badge.

### 2.3 A user status line

- **Configuration.** `[ui.statusline] command = "…"` runs a user command in the background at most once per second, and only after state changes. Its stdout replaces segments 3–7.
- **Input.** On stdin the command receives JSON:
  - `session_id`, `title`, `cwd`, `git_branch`;
  - `model`, `provider`, `mode`, `state`;
  - `context_used_pct`, `tokens_in`, `tokens_out`, `cost_usd`;
  - `agents_need_you`, `workers_running`.

  This is the same shape as Qoder's statusline input. Fields that are not known are `null`.
- **Limits.** The command runs:
  - in the workspace;
  - under the configured network/sandbox policy, as an `execute` call that the user has configured (not the agent);
  - with a 500 ms timeout.

  Its output is limited to one line, stripped of control characters except SGR, and fitted to the available width.
- **Failure.** A failing or slow command shows the built-in segments, plus a one-time notice naming the command and the error.

### 2.4 The hub

- While the inline hub is open, the composer frame and its hint row are hidden. The hub owns the bottom rows, and its hint row is the only one.
- Closing the hub restores the composer exactly as it was: draft, cursor and paste state.

### 2.5 Width behaviour

- At ≥ 100 columns everything fits.
- At 60–99 columns the lowest segments drop.
- Below 60 columns the frame becomes the two-rule form, the title is cut with `…`, and the status line shows state, attention and context only.
- Below 40 columns only state is shown.
- Each breakpoint has a snapshot test.

## 3. Non-goals

- The transcript layout (TUX-06) and the full-screen mode (TUX-09).
- The content of the welcome header (TUX-03).
- Changing what AGX-10 counts as needing attention.

## 4. Acceptance criteria

1. **Row budget.**
   - At 120×40, idle after start-up, the live region is ≤ 4 rows: three composer rows and one status line.
   - During a turn it is ≤ 5 rows: one more for the hint row.
   - The tests are `StatusComponent.height` and `FooterComponent.height` plus the frame, at fixed widths.
2. **No placeholder metadata.** No status or footer frame contains `cost unknown`, `usage unavailable`, a UUID-shaped string, or `?` as a token count. This is asserted across idle, running, error and terminal-wait states.
3. **The context meter.** It appears after the first response with usage, and is computed from the provider's reported input tokens divided by the model's context window from `config.PROVIDER_DEFAULT_MODELS` metadata. It changes colour at 60 % and 85 %. A fake usage event test covers each band.
4. **Width.** At 50 columns the status line contains the state and context segments only, with no wrapping or truncated segment. There are snapshot tests at 40, 50, 80 and 120 columns.
5. **A user status line.** A configured command echoing `"⎇ main · 42%"` replaces segments 3–7 within one second. A command that sleeps two seconds is killed at 500 ms, and the built-in line returns with one notice.
6. **The hub.** With the inline hub open, the captured frame contains exactly one hint row.
7. **`/status`.** It prints the session ID and the explanation of absent cost. It is in the `/` picker and in `/help`.
8. **Screen reader.** With `screen_reader = true`:
   - the status line is announced as a sentence only on change ("Idle. Opus 4.8. Context 12 percent.");
   - there is no frame;
   - the AGX-13 announcement tests pass unchanged.

## 5. Rollout and compatibility

- `StatusComponent` and `FooterComponent` are replaced by a `StatusLine` and a `ComposerFrame` in `tui/workspace/`. The old rows and their tests are deleted, with no option to bring the 5-row block back.
- `LiveRegion` height invariants (I-10, AGX-11) are rewritten for the new components, and the frame cache keys include the theme (TUX-01).
- The user status-line command is opt-in, and its trust follows the same rule as a hook command. It is configured only in user or trusted project configuration.

## 6. Verification

- `uv run pytest tests/unit/test_status_line*.py tests/unit/test_footer*.py tests/unit/test_live_region*.py tests/e2e/test_tui_*.py -q`
- AGX-11's frame-time and AGX-14's loop-lag gates still pass. The status line is recomputed only on signal change, never per spinner frame except for the state segment.
- Screenshots at 120, 80 and 50 columns, added to `screens/`.
