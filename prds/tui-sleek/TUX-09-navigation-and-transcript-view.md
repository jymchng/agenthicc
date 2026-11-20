---
title: "TUX-09: Navigation — transcript view, search and fullscreen mode"
status: Implemented (branch tux-tux09)
version: 1.0.0
date: 2026-11-05
scope: "a full-screen transcript view (Ctrl+T) over the session log with turn jumping, search, expandable tool output and copy; /logs in the same viewer; and an optional fullscreen layout that keeps the composer docked while the transcript scrolls"
related_prds: [TUX-00, TUX-02, TUX-04, TUX-06, TUX-07, TUX-08, AGX-07, AGX-11, AGX-13, PRD-256]
tags: [tui, navigation, transcript, search, fullscreen]
---

# TUX-09: Navigation — transcript view, search and fullscreen mode

## Implementation status

Implemented on branch `tux-tux09`. Every acceptance criterion in §4 has a test; the table names it. The amendments follow.

| AC | Test |
|---|---|
| 1 Expand | `tests/e2e/test_transcript_view_e2e.py::test_ctrl_t_shows_the_failed_commands_full_output_and_q_returns_with_the_draft` (a scripted `MockTransport` turn whose real `run_bash` fails; a real `UnifiedInputSession` with a draft, a fake terminal read back through `pyte`: Ctrl+T shows every stored line, Enter collapses and expands, `q` returns with the draft intact and typing resumes) and `test_the_failed_command_is_expanded_with_enter_from_the_history_view` (`/history`, `{`, Enter); `tests/unit/test_transcript_view.py::test_enter_expands_the_failed_command_to_its_full_stored_output_and_again_collapses`, `test_expanded_output_is_cut_at_64_kib_and_says_so` |
| 2 Turn jumps and search | `tests/unit/test_transcript_view.py::test_a_bracket_moves_exactly_one_user_turn` and `test_user_turns_are_numbered_from_one_in_the_gutter` (50 turns), `test_search_cycles_every_hit_in_order_and_the_count_matches_a_direct_scan` (`/scheduler`, `n` visits all 76 hits in order and wraps; the count equals a scan of the fixture's own text), `test_search_is_case_insensitive_unless_the_query_has_capitals`, `test_search_covers_user_text_assistant_text_tool_summaries_and_loaded_output`, `test_hits_are_highlighted_in_the_accent_style` |
| 3 Lag | `tests/performance/test_transcript_search_lag.py::test_searching_a_100_mb_log_never_stalls_the_loop` (the TUIR-04 sampler while a 100 MB, 217,500-item log is indexed and searched twice, one search with 72,500 hits; worst stall 0.12–0.19 s against the 0.25 s budget; indexing about 1.5 s, one search 0.3–1 s), `test_the_needle_is_found_in_every_repeat_of_a_log_of_100_mb` |
| 4 Copy | `tests/e2e/test_transcript_view_e2e.py::test_y_writes_an_osc52_sequence_holding_the_items_plain_text` (the fake terminal's bytes hold exactly one `ESC ] 52 ; c ; <base64> BEL`; it decodes to the item's plain text with no `ESC [`), `test_a_terminal_without_osc52_gets_the_system_clipboard_command`; `tests/unit/test_transcript_clipboard.py` |
| 5 `/history` | `tests/e2e/test_transcript_view_e2e.py::test_the_old_history_overlay_and_log_pager_no_longer_exist` (`TranscriptHistoryOverlay` and `LogTailOverlay` appear nowhere in `src/`), `tests/unit/test_transcript_history_pages.py::test_history_command_opens_the_transcript_view_for_the_active_session`, `tests/unit/test_transcript_session.py::test_the_new_commands_are_registered_and_reach_the_session` |
| 6 Fullscreen | `tests/unit/test_transcript_fullscreen.py`: `test_slash_fullscreen_switches_layout_without_losing_the_draft`, `test_exiting_prints_the_compact_transcript_to_the_normal_screen`, `test_exiting_prints_only_what_the_normal_screen_has_not_seen`, `test_page_up_stops_following_and_end_follows_again`, `test_end_keeps_its_composer_meaning_when_the_pane_follows`, `test_the_wheel_and_shift_arrows_scroll_the_pane`, `test_a_screen_reader_stays_inline`, `test_a_10000_event_log_scrolls_inside_the_frame_budget` (frame p95 about 3 ms against 16 ms; a page of events never rendered before costs 40–90 ms once, against TUIR-04's 0.25 s) |
| 7 `/rewind` | `tests/unit/test_transcript_rewind.py::test_choosing_turn_3_makes_a_session_with_turns_1_and_2_and_the_prompt_as_the_draft` (byte comparison of the original's log and journal before and after), `test_the_forks_memory_holds_only_the_earlier_turns`; `tests/e2e/test_transcript_view_e2e.py::test_rewind_offers_the_dialog_and_choosing_edit_forks_at_that_turn`, `test_rewind_copy_the_prompt_stays_in_the_view`; `tests/unit/test_transcript_session.py::test_rewind_forks_in_a_worker_and_ends_the_session_with_a_fork_switch`, `test_a_fork_switches_to_the_new_session_without_returning_to_the_original`, `test_a_draft_left_by_rewind_lands_in_the_new_sessions_composer` |

Where the code lives:

- **`tui/transcript/`** (new, imported lazily; the first frame loads none of it): `view.py` (selection, anchor scrolling, keys, chrome), `log.py` (the one-pass offset index and the worker-thread search), `sources.py` (`SessionLogSource`, `TextFileSource`, `DocumentSource`), `render.py` (events as rows through the scrollback's renderers), `host.py` (the alternate-screen loop), `controller.py` (what opens it), `clipboard.py` (OSC 52, system clipboard, pager), `fullscreen.py` (the pane), `fork.py` (`/rewind`).
- **Deleted:** `TranscriptHistoryOverlay`, `LogTailOverlay`, `diagnostics.log_tail`, `Dialog`'s own details body (`showing_details`), and their tests. `ScrollBufferAppender` gained `hold`/`release`, `print_events` and a fixed detail level; `Workspace` gained `enter_fullscreen`/`leave_fullscreen`/`scroll_pane`.

### Amendments to the design

1. **The context is `transcript_view`, not `transcript`.** `transcript` is already the worker-transcript context of AGX-08 (`TRANSCRIPT_ACTIONS`: `up`/`down`, `f` follow, `x` stop, left/right between workers), and shares keys such as `f` and `x`. The view's keys are `TRANSCRIPT_VIEW_ACTIONS` in the new `transcript_view` context. They are in `DEFAULT_ACTIONS`, so TUX-10's `/shortcuts` lists them without a change. New conditions (`turns`, `expandable`, `rewind`, `hits`, `fullscreen`, `scrolled`) have phrases.
2. **"Full output" is the stored output.** A `tool_complete` event stores the first 8 and last 32 lines of a call's output (TUX-06 §2.1), so Enter shows those with `… N lines omitted` where the gap is, then the call's arguments. The 64 KiB cap bounds what one expanded item displays; "output truncated at 64 KiB" appears only past it.
3. **Ctrl+T opens expanded; `/history` does not.** §2.1 says the `ctrl+t to expand` footer opens the view with the tool "selected and expanded", and AC 1 says "Enter on the command shows its full stored output". Both hold: Ctrl+T opens with the last tool call already expanded, and Enter toggles it; from `/history` (nothing expanded) Enter expands. The e2e test covers both. The `[expanded]` tag is on the first output row, because the header row is full width.
4. **Rows are the scrollback's rows.** Instead of a second set of renderers, each event is printed by a private `ScrollBufferAppender` into a recording console (`compact` for the collapsed item, `verbose` and the arguments for the expanded one) and read back as styled rows. The scrollback's `ctrl+t to expand` footer reads `enter to expand` in the view. **The view wraps; it does not crop** (operator report, 2026-11-20): the appender has a `wrap` mode, used only by the view, the pane and nothing else, in which a tool row too long for the width wraps under a hanging indent (the duration and exit code stay on its first line), an output line wraps instead of ending in `…`, and the arguments block shows every value in full (multi-line values keep their lines, 64 KiB in all). The scrollback itself is unchanged. A user message the scrollback condenses (more than 12 lines: first four and `[+N more lines]`) is expandable with Enter and then shown whole. A `compaction_status` item exists only for its result statuses (`✓ Context refreshed · 849 tokens · 3s`, `✗ Context refresh failed`), drawn by the appender's own renderer; `started` and `token_count_unavailable` are not items. Events that print nothing in the scrollback are not items.
5. **Search covers what the log stores.** "The loaded output" is the stored head and tail. The query is looked for as raw bytes in each 1 MiB chunk first, and only lines that contain it are decoded, so a miss costs a few C-level passes. A worker pauses 0.5 ms between chunks to hand the interpreter to the loop.
6. **`/rewind` forks with a new function; there was no TUI fork path.** `session_service`'s `fork` command forks a service session, which the TUI does not use. `tui/transcript/fork.py: fork_session` copies the log up to the chosen prompt and the conversation journal up to the `turn_started` record that opened that turn (matched by text, in order, because the journal's message may carry injected mention context), registers the new session with the original's directory, model, mode and agent, and leaves the prompt in a `draft.txt` that the new session puts in its composer once. When the journal has no matching turn, the fork keeps the log and says that the model does not remember the earlier turns. The session ends with the new `SessionSwitch("fork", …)`, which continues in the fork and does not reopen the original. The dialog (TUX-07's `Dialog`) is drawn inside the view, so Esc returns to the view. Rewinding while a turn runs is refused.
7. **A dialog's Ctrl+T opens the view** (TUX-07 said "TUX-09 moves this to its transcript view"). `DialogSettings.open_details` is the seam; the dialog's own details body is deleted. Without it (tests, plugins) the dialog does not offer the key. The approval overlay stays up under the view.
8. **`Ctrl+T` is no longer a usable `leader`.** It is now bound in the composer, and a leader may not shadow a bound key there. The bindings template and the guide suggest `alt+x`.
9. **Fullscreen pane source.** The pane reads the session log (so it needs no event list of its own) and shows what is printed to the console while the layout is on, in order: the welcome header is the first such print. Prints are captured by a render hook that lets frames and mode switches pass; at most the last 500 are kept. Items that are never persisted (screen-reader announcements, replay separators, transient notices) are not in the pane. On exit the normal screen gets what it has not seen: the events and captured prints since the layout began, in `compact` form (a resumed session's earlier history is not reprinted). `End` is `composer.pane_follow` only while the pane is scrolled, so its composer meaning (end of line) is unchanged otherwise; the layout also turns mouse reporting on when `[ui] mouse` is `auto`.
10. **Copy and pager.** OSC 52 cannot be probed, so a terminal that cannot take it is recognised by `TERM` (`linux`, `dumb`), which gets `pbcopy`, `wl-copy`, `xclip`, `xsel` or `clip.exe`; the payload is cut at 74,994 bytes and the footer says so. `e` leaves the alternate screen for `$PAGER`, else `$EDITOR` (`-R` for the vi family) on a private read-only temporary file, else `less`.
11. **The view follows a growing log** while the last item is selected, and redraws when the index or a search finishes without a key press (a bug the e2e test found: the check compared state within one tick).
12. **Seams for TUX-10.** `render.path_label` is the one place the view prints a path (the `/logs` title); paths inside rows come from the scrollback renderers, where OSC 8 links belong. `/shortcuts` and OSC 8 are not implemented here.
13. **Verification.** `tests/unit/test_diagnostics.py` lists `tui/transcript/clipboard.py` among the terminal-owning spawns (the pager). `tests/unit/test_tui_session_coverage.py`'s input double gained `deferred_exit_hint` (the fullscreen layout cannot show the `--resume` hint, so the session prints it after it leaves). Manual checks in tmux, kitty, iTerm2 and Windows Terminal (§6) were not done.


## 1. Problem

The scrollback is append-only (AGX-11). That is the right default: native scrolling, selection and copy work, and output stays in the terminal after exit. It leaves three gaps.

### 1.1 No way to see a detail after it scrolls past

- TUX-06 makes tool rows compact on purpose, so the user needs a way to reach the full output of a call, an approval's arguments (TUX-07) or a long diff.
- Today the only route is `/history`'s `TranscriptHistoryOverlay` (`tui/workspace/overlays/transcript_history.py`). It is a bounded page browser over the session log: it has no search, cannot jump between turns, and shows each event as JSON-ish detail.

### 1.2 No way to find something in a long session

- Terminal search finds text but not structure: "the third failed command", "the turn where I asked about the scheduler".
- Qoder offers turn line numbers and Shift+↑/↓ scrolling. Claude Code has a transcript mode on Ctrl+O and a "rewind" to earlier prompts.

### 1.3 No fullscreen layout

- OpenCode (`screens/oc-idle.png`) is fullscreen: the transcript scrolls in a pane and the prompt stays docked.
- Some users prefer this, especially in small terminals, where the inline live region and long tool output fight for rows.
- agenthicc already runs the agents hub in the alternate screen (`agents_hub/hub.py:4303`), so the machinery exists.

## 2. Goals

### 2.1 The transcript view (Ctrl+T)

The view is a full-screen alternate-screen surface, the same host the hub uses. It reads the session log through the existing bounded page loader (`load_older_transcript_page`).

```text
 Transcript · fix flaky scheduler test · 14 turns                         q close
 ──────────────────────────────────────────────────────────────────────────────
  12 ❯ why does test_due_cursor fail on DST days?
     ● Read schedules/recurrence.py:80-140
     ✗ $ uv run pytest tests/unit/test_recurrence.py -q              exit 1 · 3.2s
 ▌   │ E   AssertionError: 2026-03-29 02:30 not in …                   [expanded]
     │ … 120 lines …
     ● Edit schedules/recurrence.py  +6 −2
  13 ❯ run the whole schedules suite
 ──────────────────────────────────────────────────────────────────────────────
  ↑↓ move · enter expand · [ ] turns · / search · n/N next · y copy · e editor
```

**What it shows.**

- Every event uses the TUX-06 renderers, at `verbose` detail for the selected item and `compact` for the rest.
- User turns are numbered from 1, a stable index that `/rewind` (2.4) uses.

**Keys** (keymap context `transcript`, listed by `/shortcuts`):

- **Moving.**
  - `↑↓`, `j/k`, PageUp/PageDown, Home/End move between items.
  - `[` and `]` jump to the previous and next user turn.
  - `{` and `}` jump to the previous and next failure, approval or error.
- **Expanding.** Enter expands or collapses the selected tool's full output, diff or arguments. They are loaded lazily from the log, bounded by the overlay's existing byte caps, and show "output truncated at 64 KiB" when cut.
- **Searching.**
  - `/` searches. It is a substring search, case-insensitive unless the query has capitals, over user text, assistant text, tool summaries and the loaded output.
  - `n` and `N` move between hits, and hits are highlighted in `accent`.
  - Search runs in a worker thread over the log, so the TUIR-04 lag budget holds on a 100 MB log.
- **Copying and editing.**
  - `y` copies the selected item's plain text with OSC 52, or through the system clipboard command when OSC 52 is not available.
  - `e` opens the selected item's full text in `$PAGER`, or `$EDITOR` read-only.
- **Leaving.** `q`, Esc or Ctrl+T returns to the composer exactly as it was left.

**Opening points.**

- A tool row's "ctrl+t to expand" footer (TUX-06) opens the view with that tool selected and expanded.
- An approval's Ctrl+T (TUX-07) opens the request's full arguments or diff, and Esc returns to the dialog.
- `/history` opens this view. `TranscriptHistoryOverlay` is deleted, and `/history repair` is unchanged.
- `/logs` (TUX-08) opens the session log file in the same viewer, with search and no turn structure.

### 2.2 Scroll position in the live region

- When the user has scrolled the terminal up, agenthicc cannot know it, because native scrollback belongs to the terminal.
- TUX-09 therefore does not try to manage the inline scrollback. The transcript view is the managed alternative.

### 2.3 Fullscreen layout (opt-in)

- `[ui] layout = "inline" | "fullscreen"`, default `inline`. `/fullscreen` toggles it for the session.
- In `fullscreen` the session runs in the alternate screen:
  - The transcript is a scrollable pane, rendered by the transcript view's engine but following the tail.
  - The composer frame and status line (TUX-02) are docked at the bottom.
  - The welcome header (TUX-03) is the pane's first item.
- **Scrolling.**
  - PageUp/PageDown, Shift+↑/↓ and the mouse wheel (when `[ui] mouse` allows, AGX-13) scroll the pane.
  - Scrolling up stops following the tail and shows `↓ N new · end to follow`.
  - End resumes following.
- **On exit**, the conversation is printed to the normal screen in `compact` form, so the terminal's scrollback still holds the session afterwards, as OpenCode does.
- **The render pipeline.** The pane renders only visible rows from a bounded window of rendered items (AGX-11 frame cache). A 10,000-event session scrolls inside the frame budget.
- **Accessibility.** `screen_reader = true` forces `inline`. A screen reader follows the scrollback better than a repainted pane.

### 2.4 `/rewind`

- `/rewind` opens the transcript view in a mode that selects user turns only.
- Enter on turn N offers, through TUX-07's dialog:
  - "Edit and resend from here", which forks the session at turn N through the existing session fork path, with the turn's text loaded into the composer;
  - "Copy the prompt".
- It never deletes the original session.

## 3. Non-goals

- Managing the terminal's own scrollback in inline mode.
- Restoring files when rewinding. That is a checkpoint feature for its own PRD. `/rewind` here forks the conversation only, and the dialog says so.
- Split panes (transcript and hub side by side).

## 4. Acceptance criteria

1. **Expand.** In a session with a failed command, Ctrl+T then Enter on the command shows its full stored output, and `q` returns to the composer with the draft intact. This is an e2e test with the screenshot tool fake.
2. **Turn jumps and search.** On a 50-turn fixture:
   - `]` moves exactly one user turn;
   - `/scheduler` then `n` cycles through every hit in order;
   - the hit count matches a direct scan.
3. **Lag.** Searching a 100 MB synthetic log keeps the TUIR-04 loop-lag gate green, because the work happens in a worker thread.
4. **Copy.** `y` emits an OSC 52 sequence holding the base64 of the item's plain text, with no SGR codes. This is checked with a fake terminal.
5. **`/history`.** It opens the new view, and `TranscriptHistoryOverlay` no longer exists (`rg TranscriptHistoryOverlay src/` is empty).
6. **Fullscreen.**
   - `/fullscreen` switches layout without losing the draft.
   - Exiting prints the compact transcript to the normal screen.
   - PageUp stops following, and End resumes.
   - The frame budget holds on a 10,000-event fixture.
7. **`/rewind`.** Choosing turn 3 creates a new session with turns 1–2 and the composer holding turn 3's text, and the original session's log is unchanged (a byte comparison).

## 5. Rollout

- The transcript view is built on the existing page loader and the TUX-06 renderers. It adds no new persistence.
- Fullscreen is opt-in, and inline remains the default and the tested baseline.
- New keys go into `DEFAULT_ACTIONS`. Ctrl+T is free in the composer context today; Ctrl+O stays `subagents.focus`.

## 6. Verification

- `uv run pytest tests/unit/test_transcript_view*.py tests/e2e/test_transcript_view*.py tests/e2e/test_fullscreen*.py tests/performance -q`
- Manual check in tmux, kitty, iTerm2 and Windows Terminal, covering alternate screen entry and exit, OSC 52, and mouse wheel.
