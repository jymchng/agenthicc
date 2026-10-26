---
title: "AGX-13: Unified keymap, generated help, mouse and accessibility"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P2
series: AGX
related_prds: [agx-00, agx-04, agx-05, agx-06, agx-07, agx-08, agx-10, agx-11, prd-60, prd-144]
tags: [tui, keyboard, accessibility, mouse, ux]
---

# AGX-13 — Unified keymap, generated help, mouse and accessibility

## Implementation status (2026-10-24)

Implemented. The registry, the bindings file, the decoder and the
accessibility modes drive the composer and the agents hub (AGX-05/06/07).

- **Registry (§5.1).** `agenthicc.tui.keymap` defines `Action` (name,
  context, keys, label, hint priority, `when`/`unless` conditions,
  `rebindable`) and the immutable `Keymap`, which rejects unknown contexts,
  duplicate names, unnormalised keys and conflicts at build time: two actions
  may share a key in a context only when their conditions are mutually
  exclusive. The PRD's `enabled=` predicate is expressed as named conditions
  (`running`, `paste`, `paste_at_cursor`) so conflicts can be checked
  statically. `tui/actions.py` holds the default actions. The composer
  resolves every key through `keymap.resolve("composer", key, conditions)` and
  its capabilities act on the resolved action; the footer hints are
  `keymap.hints(...)` for the conditions that apply, so the phantom
  `R Retry` and `Ctrl+L Clear` hints, which had no binding, are gone.
- **User bindings (§5.2).** `~/.agenthicc/keybindings.toml`
  (`AGENTHICC_KEYBINDINGS` overrides the path) rebinds, adds and removes
  (`"none"`) keys, and may set `leader` and `leader_timeout_ms`. The TUI
  watches it with AGX-04's `FileWatcher`: a valid edit applies at once, an
  invalid one keeps the previous bindings and says why. `/keybindings` and
  `agenthicc keybindings` show the effective bindings with their source;
  `agenthicc keybindings edit` creates the commented template and opens it in
  `$EDITOR`. `/` and `@` open their pickers and cannot be rebound.
- **Decoder (§5.3).** `cbreak_reader` decodes Ctrl+A…Z, Alt, modified
  cursor keys, Delete/Insert, F1–F12, focus reports and SGR mouse reports
  through pure functions (`decode_control`, `decode_csi`, `decode_ss3`,
  `decode_alt`); unknown sequences are `Key.UNKNOWN`, never an Esc press.
  The Windows backend decodes the same keys from console key records (AltGr
  text stays text), and its focus and mouse records.
- **Mouse (§5.4).** `[ui] mouse` is `auto`, `on` or `off`. The inline TUI
  reports the mouse only for `on`; the wheel then moves the composer cursor.
  Leaving raw mode always turns reporting off.
- **Screen reader and reduced motion (§5.5).** `[ui] screen_reader` draws no
  spinner, flower or border, redraws only on content changes and announces
  each agent, approval, notification and mode change once as a sentence in
  scroll-back. `[ui] reduced_motion` keeps the layout with a static `●`.

- **Agents contexts.** `agents` (operations on an agent), `agents.list`,
  `agents.peek` and `agents.detail` hold the hub's actions; `PARENTS` makes
  `agents.list` and `agents.detail` inherit `agents` and `agents.peek` inherit
  `agents.list`, a context's own key shadowing an inherited one. The
  bindings file takes one table per context (`[agents.list]`). The hub's
  footer, `?` help and the composer's `agents.open_hub` (Ctrl+G, and the
  `left_on_empty` pseudo-key for ← on an empty prompt) come from the registry;
  a click on a footer hint runs its action (`Hint.action`).
- **Hub mouse and screen reader.** With the mouse on, a click selects a row
  (a second opens it), the wheel scrolls and a click on a tab switches to it;
  screen-reader mode draws rows as sentences.

Acceptance: (1) `test_every_hub_action_is_in_help_and_resolves_in_its_context`
and the composer's hint tests; (2) `test_rebinding_approve_applies_without_restart`;
(3) the decoder tests for POSIX bytes and Windows records; (4)
`test_a_click_selects_a_row_and_the_wheel_scrolls` and the `off` tests;
(5) the screen-reader announcement tests. The `transcript` and `subagents`
contexts receive their actions with AGX-08.

## 1. Summary

Each agenthicc screen hard-codes its own keys, help text and footer hints, and
they drift: the agents help omits pin, approve/reject, worktree actions and
Space. The terminal decoder knows 21 keys — no Alt combinations, function keys,
modified arrows or most Ctrl letters — so the shortcuts other agents use cannot
even be expressed, and nothing can be rebound. Introduce one keymap registry
that drives dispatch, help and hints for every screen; user-configurable
bindings with hot reload; a complete key decoder; opt-in mouse support; and
screen-reader and reduced-motion modes.

## 2. Problem and evidence

- **Decoder.** `Key` in `src/agenthicc/tui/cbreak_reader.py` has `UP DOWN LEFT
  RIGHT HOME END ENTER CTRL_ENTER CTRL_V PASTE TAB ESC BACKSPACE CTRL_C CTRL_D
  CTRL_U CTRL_X SHIFT_TAB PAGE_UP PAGE_DOWN AT CHAR`. Ctrl+G, Ctrl+B, Ctrl+O,
  Ctrl+R, Ctrl+T, Alt+arrows, Shift+arrows, Delete and function keys are not
  decoded.
- **Hard-coded keys.** `BackgroundManager.handle_key` is a 450-line chain of
  `if value == …` / `ch.lower() == …` tests; the footer and `?` help are
  separate string literals. The help screen lists `c`, `a`, Ctrl+X, `/`, `v`,
  `C`/`A`, `i` but not `p` (pin), `y`/`n` (approve/reject), `x` (worktree
  actions) or Space, all of which `handle_key` accepts.
- **No configuration.** Neither `agenthicc.toml` nor any user file binds keys.
- **No mouse, focus events, screen-reader or reduced-motion modes.** Only
  bracketed paste (`CSI ? 2004 h`) is enabled.

## 3. How others handle keys and accessibility

- **Claude Code**: `/keybindings` creates or opens `~/.claude/keybindings.json`,
  a list of blocks per context (for example `Chat`, `Agents`) mapping keystrokes
  to actions; `null` unbinds; changes apply without restarting. Agent view's
  Ctrl+S and Ctrl+T map to `agents:switchView` and `agents:togglePin`. Its docs
  refer to a screen reader mode, and agent view honours a reduced-motion
  setting.
- **OpenCode**: `tui.json` maps action names to keys — `agent_cycle` (Tab),
  `agent_list` (`<leader>a`), `session_list` (`<leader>l`),
  `session_child_cycle` (Right), `session_parent` (Up); the leader is Ctrl+X
  with a 2,000 ms timeout; `"none"` disables an action; actions accept several
  bindings.
- **Grok Build**: a documented keyboard-shortcut reference and a "rich,
  mouse-interactive, fullscreen" TUI.

## 4. Goals and non-goals

**Goals**

1. One registry of named actions per context drives key dispatch, footer hints
   and the help overlay, so they cannot disagree.
2. Users rebind, add and remove bindings in one file, applied without restart.
3. The decoder recognises the keys the AGX PRDs need on POSIX and Windows.
4. Mouse support is available where it does not break native text selection.
5. Screen-reader and reduced-motion modes make every screen usable without
   animation or spatial cues.

**Non-goals**

- Vim-style modal editing in the composer.
- Per-project key bindings (bindings are personal).

## 5. Design

### 5.1 Keymap registry

`src/agenthicc/tui/keymap.py` defines actions with a context, default
bindings, a short label for hints, a hint priority and a predicate for when the
action is valid:

```python
Action("agents.approve", context="agents.list", keys=("y",), label="approve",
       hint_priority=90, enabled=lambda s: s.selected_needs_approval)
Action("agents.open_hub", context="composer", keys=("left_on_empty", "ctrl+g"),
       label="agents", hint_priority=40)
```

Contexts: `composer`, `agents.list`, `agents.peek`, `agents.detail`,
`transcript`, `subagents`, `overlay`, `global`. Conflicts within a context are
errors at start-up and in tests. Screens dispatch through
`keymap.resolve(context, key, state)` instead of hand-written chains; the
`?` overlay and footer hints are generated from the same entries (AGX-06 §5.6).

### 5.2 User bindings

`~/.agenthicc/keybindings.toml`:

```toml
[agents.list]
"agents.approve" = ["y", "ctrl+y"]
"agents.peek" = "space"

[composer]
"agents.open_hub" = ["ctrl+g"]     # drop the ← gesture
"agent.cycle" = "none"             # keep Tab free
```

- `/keybindings` opens the file in `$EDITOR` (creating it with comments
  listing every action) and shows the effective bindings with their source.
- The file is watched (AGX-04's change primitive); valid edits apply
  immediately, invalid ones are reported in `/keybindings` and ignored.
- An optional leader key (`leader = "ctrl+x"`, timeout 2,000 ms) allows
  two-step bindings such as `"<leader>a"`, as OpenCode does.

### 5.3 Decoder

Extend `Key` (canonical in `cbreak_reader.py`, per the repository's terminal
rules) and both backends:

| Input | POSIX sequences | Windows |
|---|---|---|
| Ctrl+A … Ctrl+Z | control bytes, except those claimed by the terminal (Ctrl+S/Ctrl+Q when flow control is on) | key records with the Ctrl state |
| Alt+key | `ESC` prefix | Alt state |
| Shift/Alt/Ctrl + arrows, Home, End | `CSI 1 ; m X` modifiers | modifier state |
| Delete, Insert, F1–F12 | `CSI 3~`, `CSI 2~`, `SS3 P…`/`CSI n~` | virtual-key codes |
| Focus in / out (AGX-10) | `CSI I` / `CSI O` after `CSI ? 1004 h` | focus events |
| Mouse (opt-in) | SGR `CSI < b ; x ; y M/m` after `CSI ? 1000 h` + `CSI ? 1006 h` | mouse events |

Decoding stays a pure function of bytes so Windows cases run on Linux CI.

### 5.4 Mouse

`ui.mouse = "auto" | "on" | "off"`. `auto` enables mouse reporting only in the
full-screen hub (`agenthicc agents` from the shell, which uses the alternate
screen and restores the shell on exit) and leaves it off in the inline TUI,
where native selection and scroll-back matter more. When on: click selects a
row or tab, double-click opens, the wheel scrolls the list or transcript, and a
click on a footer hint runs its action. Holding Shift while selecting keeps
native text selection in terminals that support the convention.

### 5.5 Screen-reader and reduced-motion modes

`ui.screen_reader = true`:

- no spinners, colour flashes or box drawing; frames change only when content
  changes;
- state changes are also printed as plain sentences in scroll-back
  (`fix-flaky now needs approval: bash pytest -k schedule`);
- list rows render as sentences in reading order (status, title, project, age);
- focus moves predictably and is announced (`Agents, Needs you tab, 2 items`).

`ui.reduced_motion = true` keeps the normal layout but removes animation and
flashes (spinners become static `●`).

Both respect `NO_COLOR`; status meaning never depends on colour alone (AGX-06
§5.1).

## 6. Acceptance criteria

1. For every screen, a test enumerates the registry for its contexts and
   checks that each action is dispatchable, appears in `?` help, and appears in
   the footer whenever it is valid and fits.
2. Rebinding `agents.approve` to `ctrl+y` in the user file takes effect without
   restart and removes `y` from help and hints.
3. Decoder tests cover every row of §5.3 for POSIX byte sequences and Windows
   key records.
4. With `ui.mouse = "on"`, clicking a row selects it and the wheel scrolls; with
   `off`, no mouse-reporting sequence is written.
5. With `ui.screen_reader = true`, a recorded session contains no spinner frames
   and each state change appears once as a sentence.

## 7. Test plan

- Registry consistency tests per screen; conflict detection tests.
- Byte-sequence decoder tables for POSIX and Windows backends.
- Golden renders of generated help for each context.
- Screen-reader mode stream tests.

## 8. Risks

- **Terminal-claimed keys.** Ctrl+S/Ctrl+Q may be eaten by flow control and some
  terminals intercept Alt or function keys. Defaults avoid such keys; the
  `/keybindings` view flags bindings that the current terminal cannot deliver
  when detectable.
- **Mouse reporting breaks selection** in some terminals. Hence `auto` keeps it
  off in the inline TUI.

## Sources

- Claude Code keybindings: <https://code.claude.com/docs/en/keybindings>
- Claude Code agent view (Agents context, reduced motion): <https://code.claude.com/docs/en/agent-view>
- OpenCode keybinds: <https://opencode.ai/docs/keybinds/>
- Grok Build keyboard shortcuts: <https://docs.x.ai/build/keyboard-shortcuts>
- Grok Build overview (mouse-interactive TUI): <https://docs.x.ai/build/overview>
- xterm control sequences (mouse, focus, modifiers): <https://invisible-island.net/xterm/ctlseqs/ctlseqs.html>
