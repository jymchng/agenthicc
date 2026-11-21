---
title: "TUX-10: Motion, feedback and terminal integration"
status: Implemented (branch tux-tux10)
version: 1.0.0
date: 2026-11-05
scope: "the working indicator (one spinner, an activity verb, elapsed time), turn completion and interruption rows, notifications when the terminal is unfocused, the window title and progress OSC, clickable paths (OSC 8), and a generated /shortcuts reference"
related_prds: [TUX-00, TUX-01, TUX-02, TUX-06, TUX-08, AGX-10, AGX-11, AGX-13, TUIR-04]
tags: [tui, motion, feedback, notifications, terminal, polish]
---

# TUX-10: Motion, feedback and terminal integration

## Implementation status

Implemented on branch `tux-tux10`. Every acceptance criterion in §4 has a test;
the table names it. The amendments to the design follow.

| AC | Test |
|---|---|
| 1 The verb | `tests/e2e/test_motion_feedback_e2e.py::test_the_status_line_names_what_the_agent_is_doing_never_a_tool` (a scripted `MockTransport` turn: `read_file` then `run_bash npm test` read `Reading src/app.py` then `Running npm test`; no `⚒` and no internal tool name in any frame); `tests/unit/test_status_line_motion.py` (the verb for each tool family, a plugin tool, subagents, retry, waiting, the 3 s and 10 s tail, a long target) |
| 2 Reduced motion | `test_status_line_motion.py::test_reduced_motion_frames_500_ms_apart_are_identical` (five frame ticks apart, with the same text), `…_without_reduced_motion_the_same_frames_differ`, `…_keeps_the_waiting_state_still_and_its_words` |
| 3 Turn-end rows | `tests/unit/test_turn_end_rows.py`: `…_edits_three_files_ends_with_done_and_the_file_totals`, `…_interrupted_turn_with_no_writes_says_nothing_was_changed`, `…_a_two_second_answer_has_no_trailer`, plus the failed turn, the blank line, the stateless replay; `tests/e2e/test_motion_feedback_e2e.py::test_cancelling_a_running_turn_ends_it_as_interrupted` (the real cancellation path) |
| 4 Notifications | `tests/unit/test_terminal_osc.py`: `…_twelve_second_turn_while_unfocused_emits_exactly_one_osc_9`, `…_a_focused_terminal_gets_none`, `…_an_approval_while_unfocused_emits_one`, `…_a_focus_report_is_how_the_terminal_says_it_is_away` (through the real key path), the unknown-focus 30 s rule, `[ui] notify`, and the routing table |
| 5 Title and progress | `test_terminal_osc.py::test_the_title_changes_to_the_working_glyph_and_is_restored_on_exit`, `…_progress_is_written_only_where_wt_session_is_set` (the real backend, a fake TTY, `WT_SESSION` set and unset), `…_progress_is_indeterminate_while_working_and_cleared_at_the_end`, `…_compaction_progress_is_determinate_only_when_it_is_measured`, the once-a-second throttle, `window_title = false`, a pipe gets nothing (POSIX and Windows backends), `tests/e2e/test_motion_feedback_e2e.py::test_headless_writes_no_escape_sequence_…` |
| 6 OSC 8 | `tests/unit/test_terminal_hyperlinks.py::test_a_read_row_links_to_the_file_when_hyperlinks_are_on` and `…_the_row_has_no_link_when_hyperlinks_are_off`; diff headers, notices, assistant URLs, the transcript view's row and title, the editor schemes, and the terminal matrix (tmux before 3.4, `TERM=dumb`, `screen_reader`) |
| 7 `/shortcuts` | `tests/unit/test_shortcuts.py::test_every_action_in_the_default_actions_appears_exactly_once`, `…_a_rebinding_appears_with_its_new_key_and_says_so`, `…_no_row_shows_a_condition_identifier`; wrapping at 50/80/140 columns, the `?` key (empty prompt, mid-prompt, during a turn, in an answer composer, rebound), the hint row and status line ending with `? shortcuts` |

### Amendments to the design

- **Operator decisions that override §2.1 and §2.2.**
  - The one spinner is agenthicc's flower (`Glyphs.spinner`), not braille, and
    `Thinking` keeps its bold sweeping letter; the other verbs render plainly.
    AC 1's "no flower glyph" therefore reads "no `⚒` and no internal tool name".
  - The turn-end rows keep the operator's `✾ Worked for 24s | 10:20 AM` and
    `✾ Done in … across N turns | …` (no `── now ──`, one blank line after).
    §2.2's information is a muted second line under the row
    (`└ 6 tools · edited 3 files +34 −8`), not a `── … ──` rule, and the
    interrupted and failed rows read `✾ Interrupted after 22s` and
    `✾ Stopped after 8s`. The `/retry` the PRD names does not exist
    (TUX-08), so the failed row has no command.
  - A request that ends interrupted or failed prints no `Done in …` total.
  - Selections have no background (`accent_bg` is deleted), and `/shortcuts`
    has none: it is a pager with no selection.
- **Elapsed time after 3 s.** The status line no longer shows `0s` from the
  first frame (TUX-02 amendment 7); the PRD's thresholds are 3 s for the time
  and 10 s for `↓ N tokens`. The running-tool rows keep their own `(23s)`.
- **The turn summary is data on the event.** §2.2 says the totals come from the
  turn's file-change events; the store computes them when the turn closes
  (`tui/turn_summary.py`) and writes `tools`, `wrote`, `files`, `added`,
  `removed`, `failed` and `interrupted` on `turn_complete`, so a replay and the
  transcript view (which renders one event at a time) print the same rows. A
  log written before this change has no such fields and prints the row only
  for a turn of 5 s or more.
- **`_emit_terminal_notification` is gone.** §2.3 says to reuse it; it wrote to
  the Rich console's stream and had no mode. Notifications now go through
  `tui/notifications.py::NotificationSink` and `TerminalBackend.write_control`
  (OSC 9, OSC 777 or the bell, by `[ui] notify`; the text is stripped of
  control characters as before). The attention router takes
  `send_terminal_notification(kind, summary)` and no longer sets the title.
- **The AGX-10 routing table** (§2.3 "No duplicates") is `notifications.PRIORITY`:
  an approval or a background session that needs you outranks a failure, which
  outranks a finished request. A notification within 2 s of one that outranks
  or equals it, and the same text within 10 s, is dropped. `/dnd` is not
  consulted for the foreground: DND is for background attention.
- **Focus.** Focus reporting was already enabled through the backend by AGX-10;
  this change adds `ConversationStore.terminal_focus_known`, because
  `terminal_focused` starts true and an unreporting terminal could not be told
  from a focused one. Until the terminal reports, only a request of more than
  30 s notifies.
- **Title.** Written through `write_control` (`OSC 2`) at most once a second
  from the session's 10 Hz tick. The user's own title is saved and restored with
  the xterm title stack (`CSI 22;2 t` / `CSI 23;2 t`), which terminals that do
  not implement it ignore. The state glyph is the flower (`●` idle, `◆`
  waiting).
- **Progress.** `OSC 9;4` is sent where `WT_SESSION`, `ConEmuPID` or Ghostty's
  `TERM_PROGRAM` says it is drawn, and never under a multiplexer. During
  compaction it follows `compaction_progress` only when that is not `None`
  and is otherwise indeterminate; nothing is estimated. The compaction row and
  `compaction_*` store API are unchanged.
- **Links.** The check is a capability of the backend (`supports_hyperlinks`),
  a known-terminal list plus `VTE_VERSION`, with `FORCE_HYPERLINK` as the
  override. A tool row or diff path is linked only if it exists, so a document
  name (`read_agenthicc_doc`) or a deleted file is not a dead link. With
  hyperlinks off, assistant Markdown `[text](url)` is printed `text (url)`.
  Dialog diffs (TUX-07) are not linked: the dialog crops its box.
- **`/shortcuts`.** The page is a `Dialog` pager. Long descriptions wrap under
  their key (TUX-09's wrapping rule), and each row shows `default` or
  `your keybindings`. `?` is `composer.shortcuts` (`when: empty`, `yields`, so a
  bindings file that already used `?` keeps it); where a session cannot show
  the page (an in-dialog answer composer) the key is typed. The hint row
  appends `? shortcuts` while the prompt is empty, the other hints giving way
  first; the status line's right-hand hint (TUX-02) switches from `/help` to
  it, and falls back to `/help` if the key is unbound.
- **Merge note.** Merged with `tui-sleek-prds` at 9e16be7 (the compaction row
  and TUX-09). The shared `tests/transcript_support.py` fake backend gained the
  three new backend methods, and its `scheduler_session` turns carry the
  `tools` field the store now writes. The transcript view's title path
  (`render.path_label`) takes the OSC 8 helper, and `TranscriptView` takes
  `links=`.

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
