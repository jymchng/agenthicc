---
title: "TUX-10: Motion, feedback and terminal integration"
status: Proposed
version: 1.0.0
date: 2026-11-05
scope: "the working indicator (one spinner, an activity verb, elapsed time), turn completion and interruption rows, notifications when the terminal is unfocused, the window title and progress OSC, clickable paths (OSC 8), and a generated /shortcuts reference"
related_prds: [TUX-00, TUX-01, TUX-02, TUX-06, TUX-08, AGX-10, AGX-11, AGX-13, TUIR-04]
tags: [tui, motion, feedback, notifications, terminal, polish]
---

# TUX-10: Motion, feedback and terminal integration

## 1. Problem

These are the smaller things that make a tool feel finished.

### 1.1 The working indicator says little

- While a turn runs, the status line shows a cycling flower (`✿❀❁❃✾❋✽❊`, `tui/workspace/components.py:39`) and the word "Thinking", with one bold letter sweeping across it (`_thinking_markup`, `:205`).
- The flower glyphs are missing from common monospace fonts (TUX-00 F4).
- "Thinking" is shown even while a tool runs. The tool name appears as a separate `⚒ run_bash` segment, using the internal name.
- Compaction has a second, braille spinner (`_COMPACT_SPINNER`).

### 1.2 Turn endings are inconsistent

- A completed turn prints a dim `turn_complete` text, when one is provided.
- An interrupted turn prints whatever the cancellation path emits.
- The user does not get a consistent "done, here is what changed" or "stopped" row.

### 1.3 The terminal does not tell the user when a turn needs them

- AGX-10 already sends an OSC 9 notification for **background** agents that need attention (`tui/attention.py:366`), and sets the window title from the attention router.
- The **foreground** turn sends nothing when it finishes or asks for approval while the terminal is unfocused. Long turns leave the user checking back.
- Qoder exposes its state over OSC (`idle/working/blocked/done/error`) and a dynamic title. Windows Terminal, ConEmu and Ghostty show a progress bar from OSC 9;4.

### 1.4 Paths cannot be clicked

- Paths in tool rows, diffs and notices are plain text.
- Terminals that support OSC 8 hyperlinks (iTerm2, kitty, WezTerm, Windows Terminal, VS Code, GNOME Terminal) could open them.

### 1.5 There is no single list of keys

- The keymap (AGX-13) generates `/help` sections and the hub's help, but there is no `/shortcuts` page and no `?` on an empty prompt, as there is in Codex and Qoder.
- TUX-04 and TUX-09 add many keys, which makes such a page essential.

## 2. Goals

### 2.1 One working indicator

```text
 ⠹ Running tests · 12s · esc to interrupt                       ctx 41% ▰▰▱▱▱
 ⠼ Reading recurrence.py · 3s
 ⠦ Thinking · 48s · ↓ 2.1k tokens
 ◆ Waiting for you · approve a command
```

**The spinner.**

- There is one spinner, the braille frames from TUX-01's glyph set (ASCII: `|/-\`), at 10 fps.
- It is driven by AGX-11's frame scheduler, so it never redraws more than the status line's state segment.
- The flower and the sweeping bold letter are removed.

**The verb.** It describes the current activity, from the tool family's summary (TUX-06):

| Activity | Verb |
|---|---|
| model streaming with no tool | Thinking |
| read/search tools | Reading / Searching + target |
| execute tools | Running + the summary (`npm test`) |
| write tools | Editing + file |
| subagent pool | Working with N agents |
| compaction | Compacting |
| provider retry | Retrying (2/5) in 8s |

**Other segments.**

- **Elapsed time.** It appears after 3 s, in `muted`.
- **Tokens.** `↓ tokens` appears after 10 s, when the provider streams usage.
- **Waiting.** While waiting for the user, the spinner stops and `◆ Waiting for you` uses `warning`. The rest of the line says what for.
- **Reduced motion.** `reduced_motion` shows a static `●` and the same text. The state is never conveyed by motion alone (AGX-13).

### 2.2 Turn-end rows

At the end of every turn, one muted row:

```text
 ── done in 1m 12s · 6 tools · edited 3 files +34 −8 ──────────────────────
 ── interrupted after 22s · 2 tools ran · nothing was changed ─────────────
 ── stopped: rate limited (see above) · /retry ────────────────────────────
```

- The file totals come from the turn's file-change events.
- "nothing was changed" is printed only when no write tool succeeded.
- The row is omitted for turns shorter than 5 s that ran no tools, so a quick answer has no trailer.

### 2.3 Telling the user when they are away

- **Focus.** The TUI enables focus reporting (`CSI ? 1004 h`) through `get_backend()` where the terminal supports it, and disables it on exit.
- **When it notifies.** It notifies when the terminal is **unfocused** (or focus is unknown and the turn ran for more than 30 s) and one of these happens:
  - a turn ends after running for more than 10 s;
  - an approval or question opens;
  - a turn fails.
- **How it notifies.** It reuses `_emit_terminal_notification` (OSC 9), with OSC 777 `notify` for terminals that prefer it, and an optional bell. The setting is `[ui] notify = "auto" | "osc" | "bell" | "off"`.
- **What it says.** The text is the session title and the outcome ("fix flaky test: done in 1m 12s"), sanitised as today.
- **No duplicates.** One AGX-10 routing table decides between this and background attention, so the user never gets two notifications for the same event.

### 2.4 Window title and progress

- **The title** is `<state glyph> <session title> · agenthicc`, for example `⠹ fix flaky test · agenthicc` or `◆ fix flaky test · agenthicc`. It is updated at most once per second, and restored on exit.
  - AGX-10's background attention adds `(2)` when other agents need the user.
  - `[ui] window_title = false` turns it off.
- **Progress.** OSC 9;4 is sent while a turn runs: indeterminate while working, error state on failure, cleared at the end. It is only sent where the backend reports support (Windows Terminal, ConEmu, Ghostty and others, from `TERM_PROGRAM` or `WT_SESSION`), and never written to a non-TTY.

### 2.5 Clickable paths and links

- Workspace paths in tool rows, diffs, notices and the transcript view are written as OSC 8 hyperlinks.
  - The target is a `file://` URL.
  - With `[ui] editor_links = "vscode" | "cursor" | "idea" | "zed"`, it uses the editor's URL scheme with the line number.
  - URLs in assistant text are also linked.
- `[ui] hyperlinks = "auto" | "on" | "off"`. `auto` turns them on for known-capable terminals and off under `screen_reader`, `NO_COLOR` with `TERM=dumb`, and tmux versions before 3.4.

### 2.6 `/shortcuts` and `?`

- `?` on an empty prompt (or `/shortcuts`) opens a TUX-07 pager listing every key in every context, grouped:
  - Composer;
  - Pickers;
  - Dialogs;
  - Transcript view;
  - Agents hub.

  The list is generated from `DEFAULT_ACTIONS` plus the user's bindings, with TUX-08's condition phrases.
- Each row shows where the binding comes from, `default` or `your keybindings`.
- The hint row (TUX-02) ends with `? shortcuts`.

## 3. Non-goals

- Sound beyond the terminal bell.
- Desktop notifications through OS APIs (`notify-send`, toast). The terminal's OSC is the integration point.
- Animated transitions between layouts.

## 4. Acceptance criteria

1. **The verb.** During a scripted turn running `read_file` then `run_bash npm test`, the status state segment reads `Reading <file>` then `Running npm test`. No frame contains `⚒`, a flower glyph or an internal tool name.
2. **Reduced motion.** With `reduced_motion = true`, two consecutive frames 500 ms apart have identical status lines (a frame-capture test).
3. **Turn-end rows.**
   - A turn that edits 3 files ends with `done in … · edited 3 files +N −M`.
   - An interrupted turn with no writes ends with `nothing was changed`.
   - A 2-second answer has no trailer.
4. **Notifications.**
   - With a fake backend that reports focus-out, a 12-second turn emits exactly one OSC 9 at the end.
   - With focus-in it emits none.
   - An approval while unfocused emits one.
5. **Title and progress.** The title changes to the working glyph during a turn and is restored on exit. OSC 9;4 is written only when `WT_SESSION` is set in the fake environment, and cleared at the end.
6. **OSC 8.** A `Read src/app.py` row contains an OSC 8 link to `file://<workspace>/src/app.py` when hyperlinks are on, and none when they are off.
7. **`/shortcuts`.**
   - Every action in `DEFAULT_ACTIONS` appears exactly once per context.
   - A user rebinding appears with its new key and `your keybindings`.
   - No condition identifier is shown (TUX-08's regular expression).

## 5. Rollout

- `_FLOWERS`, `_thinking_markup` and `_COMPACT_SPINNER` are deleted, and every animated element uses the one spinner.
- Focus reporting, OSC 9;4 and OSC 8 go through `get_backend()`. POSIX writes stay in `posix_backend.py`, and Windows console handling stays in `windows_backend.py`.
- Every new escape sequence is suppressed when stdout is not a TTY and under `--headless`.

## 6. Verification

- `uv run pytest tests/unit/test_status_line*.py tests/unit/test_turn_end*.py tests/unit/test_terminal_osc*.py tests/unit/test_shortcuts*.py -q`
- `uv run nox -s tests_windows` for the Windows backend's focus and progress handling.
- AGX-11's frame budget, with the spinner at 10 fps.
