---
title: "TUX-04: Composer ergonomics"
status: Proposed
version: 1.0.0
date: 2026-11-05
scope: "the main composer and every EmbeddedComposer: readline editing, word motion, kill ring, undo, reverse history search, external editor, Shift+Enter, keys reaching the prompt while a picker is open, one-Enter commands, and visible, editable queued messages"
related_prds: [TUX-00, TUX-02, TUX-05, AGI-05, AGX-13, PRD-10, PRD-77]
tags: [tui, composer, keyboard, ergonomics, readline]
---

# TUX-04: Composer ergonomics

## 1. Problem

From TUX-00: F9, F10 and F11.

### 1.1 Missing editing keys

The composer's actions (`tui/actions.py:54–169`) are:

- Enter, Ctrl+J, Backspace, Ctrl+U;
- ←/→, Home/End, ↑/↓ (history);
- Shift+Tab, Tab, Ctrl+G, Ctrl+B, Ctrl+O, Esc, Ctrl+C and Ctrl+D.

The reader already decodes Ctrl+A/E/K/W/Y/R/X/Z, Delete and Alt+<char> (`tui/cbreak_reader.py:33–77`). None of them edits the prompt. A user with shell muscle memory gets nothing from:

- Ctrl+A or Ctrl+E (line start and end);
- Ctrl+W or Alt+Backspace (delete word);
- Ctrl+K (kill to end) and Ctrl+Y (yank);
- Alt+B/F or Ctrl+←/→ (word movement);
- Delete;
- Ctrl+R (search history);
- undo.

Qoder documents all of these, plus Ctrl+X for the external editor. Claude Code and Codex support the readline core.

### 1.2 An open picker swallows editing keys

- `OverlayCapability` consumes every key while an overlay is active (`tui/input/capabilities.py:63–78`).
- The trigger picker handles only Esc, Enter, Tab, ↑/↓, Backspace, `@` and printable characters (`tui/workspace/overlays/trigger_picker.py:165–232`).
- In the study, with `@src/agenthicc/tui/w` typed and the picker open, Ctrl+U did nothing. Typing `/help` then appended to the fragment, and Enter submitted `see @src/agenthicc/tui/w/help` to the model.

### 1.3 A command takes two Enters

- Enter on a highlighted match calls `on_select`, which completes `/help ` into the buffer (`trigger_picker.py:174–189`).
- A second Enter runs it.
- Only Enter with *no* match submits (PRD-77).

### 1.4 Queued messages are a single notification

- Typing during a turn queues the message, and the footer shows `⌛ Queued #1: label…` (`runners/tui_session.py:3359`).
- The queue cannot be seen in full, edited or cancelled.

## 2. Goals

### 2.1 Editing keys

These bind in the `composer` keymap context. They are inherited by every `EmbeddedComposer` (AGI-05) and listed by `/help` and `/shortcuts`.

| Keys | Action |
|---|---|
| Ctrl+A / Ctrl+E | start / end of the line; pressed again at the line edge, start / end of the whole input |
| Alt+B, Ctrl+← / Alt+F, Ctrl+→ | previous / next word |
| Ctrl+W, Alt+Backspace | delete the word before the cursor (into the kill ring) |
| Alt+D | delete the word after the cursor (into the kill ring) |
| Ctrl+K | delete to the end of the line (into the kill ring) |
| Ctrl+U | delete to the start of the line (into the kill ring); clear all when already at the start |
| Ctrl+Y | yank the last kill |
| Delete, Ctrl+D on a non-empty prompt | delete the character under the cursor |
| Ctrl+Z / Ctrl+Shift+Z (Alt+Z where the terminal cannot send it) | undo / redo, by edit group (consecutive inserts are one group) |
| Shift+Enter, Alt+Enter, Ctrl+J | newline |
| Ctrl+R | reverse incremental history search (2.3) |
| Ctrl+X Ctrl+E, and Ctrl+X alone after 300 ms | open `$VISUAL`/`$EDITOR` (2.4) |
| Ctrl+L | redraw the live region |

Notes on the keys:

- **Ctrl+D.** It keeps its EOF meaning on an empty prompt, so AGX-13's binding is unchanged.
- **Shift+Enter.** Terminals send it via the kitty keyboard protocol or CSI-u. The reader enables `CSI > 1 u` where it is supported and restores it on exit, and decodes `CSI 13;2u` as `Key.SHIFT_ENTER`. Elsewhere Ctrl+J and Alt+Enter remain.
- **Conflicts.** The keymap's conflict checker (AGX-13) must accept the table. Ctrl+Z replaces nothing, because job-control suspend is already disabled in raw mode.
- **The buffer.** `InputBuffer` (`tui/input/buffer.py`) gains `word_left`, `word_right`, `delete_word_before`, `delete_word_after`, `kill_to_end`, `kill_to_start` and `yank`, plus an undo stack bounded to 200 groups.
- **Word boundaries.** Word boundaries are Unicode-aware: letters, digits and `_` form words, and path separators stop. That way, Ctrl+W on `src/agenthicc/tui` deletes `tui`, as in a shell.

### 2.2 Keys reach the prompt while a picker is open

- The trigger picker handles only navigation and choice: ↑/↓, Ctrl+P/N, PageUp/PageDown, Tab, Enter and Esc.
- Every other key goes to the composer's editing capability first. The picker then re-derives its fragment from the buffer.
  - The trigger is the text from the trigger character to the cursor. If the cursor leaves the trigger span, or the trigger character is deleted, the picker closes.
  - Ctrl+U, Ctrl+W, Ctrl+A/E, ←/→, paste and undo therefore all work with a picker open, and keep the picker in step.
- This replaces `TriggerPickerOverlay`'s private copy of the buffer handling (the `CHAR`, `AT` and `BACKSPACE` cases) with one source of truth: the buffer.

### 2.3 Ctrl+R history search

```text
 (search) git rebas▌                       3 of 41 · ctrl+r older · enter use · esc cancel
 ❯ git rebase -i main and fix the lockfile conflict
```

- Ctrl+R shows a one-row search field above the prompt. It matches history entries by substring, case-insensitively, newest first.
- Ctrl+R again moves to an older match, and Ctrl+S to a newer one.
- Enter or → puts the match into the prompt for editing, without submitting. Esc restores the draft.
- History is per project, from the existing `tui/input/history.py`, and multi-line entries match on every line.

### 2.4 External editor

- Ctrl+X Ctrl+E writes the prompt to a temporary file (mode 0600) under the session's runtime directory.
- It suspends the live region, restores the terminal through `get_backend()`, runs `$VISUAL`, `$EDITOR` or `vi`, and waits.
- It then loads the file back into the prompt with the cursor at the end, and deletes the file.
- A non-zero exit keeps the previous draft and shows a notice.

### 2.5 One Enter runs a command

- In the `/` picker, Enter on a highlighted command **runs it** when the command takes no required argument (`argument_hint` is empty or optional).
- When the command needs an argument, Enter completes `/model ` and opens its argument picker (TUX-05).
- Tab always only completes.
- In the `@` picker, Enter inserts the path and keeps typing (unchanged). Enter again submits.

### 2.6 A visible, editable queue

- While a turn runs, queued messages show above the composer, one muted row each (at most 3, then `+N more`):

  ```text
   ⌛ 1  also update the changelog
   ⌛ 2  /compact
  ```

- ↑ on an empty prompt during a turn moves focus into the queue. There:
  - Enter edits the message (it moves back into the prompt);
  - Delete removes it;
  - Esc returns.
- The footer notification `⌛ Queued #N` is deleted.
- Ctrl+Enter, where the terminal reports it, or Alt+Enter while busy, sends the message as an **interjection**. It is appended at the next tool boundary, which is what `_next_queued_message` does now. Plain Enter queues it for after the turn. Qoder distinguishes the two the same way.

## 3. Non-goals

- A Vim mode. It is listed for a later PRD; the keymap allows a `composer.vim` context to be added without changes here.
- Mouse text selection in the prompt.
- Changing history persistence.

## 4. Acceptance criteria

1. **The editing table.** For every row in §2.1, a unit test feeds the key sequence to `UnifiedInputSession` with a prepared buffer and checks the resulting text and cursor. Cases include:
   - Ctrl+W on `src/agenthicc/tui` → `src/agenthicc/`;
   - Ctrl+K then Ctrl+Y round-tripping;
   - undo after a paste restoring the pre-paste text.
2. **The F10 reproduction.**
   - Type `see @src/agenthicc/tui/w`, then Ctrl+U: the prompt becomes empty and the picker closes.
   - Type `see @src/ag`, then Ctrl+W: the prompt becomes `see @src/` and the picker shows `src/`'s entries.
3. **One Enter.** `/help` then Enter, with `/help` highlighted, opens help after exactly one Enter. `/model` then Enter opens the model argument picker.
4. **Ctrl+R.**
   - With history `["git status", "git rebase -i main"]`, Ctrl+R then `reb` shows the second entry, and Enter puts it into the prompt unsent.
   - Esc restores an earlier draft exactly.
5. **External editor.** With `EDITOR="sh -c 'printf edited > \"$1\"' --"`, Ctrl+X Ctrl+E replaces the prompt with `edited`. The terminal mode before and after is identical (checked by the backend fake).
6. **The queue.**
   - Two messages typed during a turn render as two queue rows.
   - Deleting the first leaves only the second to be sent.
   - Ctrl+Enter's interjection is delivered at the next tool boundary (an integration test with a scripted transport).
7. **Shift+Enter.** The reader decodes `\x1b[13;2u` as Shift+Enter, and the session restores the kitty keyboard mode on exit. This is tested on the POSIX backend with a fake TTY.
8. **Embedded composers inherit everything.** AGI-05's `EmbeddedComposer` tests pass the same key table.

## 5. Rollout

- The new actions go into `DEFAULT_ACTIONS` and are rebindable with `agenthicc keybindings edit`. User binding files that already bind one of the new keys keep their binding; the conflict checker reports it once.
- `TriggerPickerOverlay`'s private buffer handling is deleted.
- The `⌛ Queued` notification path is deleted.

## 6. Verification

- `uv run pytest tests/unit/test_input_buffer*.py tests/unit/test_unified_session*.py tests/unit/test_trigger_picker*.py tests/unit/test_keymap*.py tests/unit/test_embedded_composer.py -q`
- Manual check in tmux, VS Code's terminal, kitty and Windows Terminal (`tests_windows` covers the decoder).
