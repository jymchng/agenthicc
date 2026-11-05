---
title: "TUX-09: Navigation — transcript view, search and fullscreen mode"
status: Proposed
version: 1.0.0
date: 2026-11-05
scope: "a full-screen transcript view (Ctrl+T) over the session log with turn jumping, search, expandable tool output and copy; /logs in the same viewer; and an optional fullscreen layout that keeps the composer docked while the transcript scrolls"
related_prds: [TUX-00, TUX-02, TUX-04, TUX-06, TUX-07, TUX-08, AGX-07, AGX-11, AGX-13, PRD-256]
tags: [tui, navigation, transcript, search, fullscreen]
---

# TUX-09: Navigation — transcript view, search and fullscreen mode

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
