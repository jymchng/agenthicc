---
title: "AGI-05: One composer for every answer — the ask_user input works like the main input panel"
status: Proposed
version: 1.0.0
date: 2026-11-04
scope: "the text input inside the ask_user overlay (foreground) and the agents hub's answer form (background): the main composer's paste, newline, cursor and history, @ mention picker and injection, and / command and skill picker, through one reusable embedded composer"
related_prds: [AGI-00, AGI-01, PRD-74, PRD-86, PRD-100, PRD-189, AGX-07, AGX-13]
tags: [tui, composer, ask_user, mentions, slash-commands]
---

# AGI-05: One composer for every answer

## 1. Problem

AGI-00 §3.3.

**The overlay's free-text mode uses a bare buffer.**
- `QuestionsOverlay`'s "Other — type your answer" mode feeds keys to
  `PromptOverlay._handle_prompt_key` (`tui/workspace/overlays/prompt.py`).
  It handles only `Key.CHAR`, Backspace, Left/Right and Home/End.
- The reader delivers `@` as `Key.AT` and pastes as `Key.PASTE`, so both are
  **dropped silently**. Reproduced: typing `see @src` and pasting
  `/main.py and more` leaves `'see src/'`.
- There is no newline, Delete, word movement, history or `/` picker, and
  `@` mentions are never resolved.

**Why the composer cannot simply be used.**
- The composer's pickers are overlays, and `OverlayHost` holds one overlay.
- Showing the picker would unmount `QuestionsOverlay`, and remounting it
  clears the typed answer (`on_mount` → `self._buf.clear()`).

**The pattern already exists in the hub.**
- The hub's session-input composer (`AgentsHub._build_target_input_editor`,
  `hub.py:1694`) already builds a complete embedded composer:
  - a `UnifiedInputSession` with its own `AppState` and `OverlayHost`;
  - a `TriggerManager` with `@`, `/` and skill triggers;
  - the target's cwd and configuration;
  - a submit callback.
- It is private to that one use, so there are three editors: the composer,
  `PromptOverlay` and the hub's `LineEditor` (used for its Answer field).

## 2. Goals

1. **An embedded composer, extracted.**
   - Move the construction in `_build_target_input_editor` into
     `tui/input/embedded.py` as `EmbeddedComposer`. Inputs:
     - the workspace root and configuration;
     - the command and workflow registries;
     - a history list;
     - a capability profile;
     - an `on_submit(text) -> Awaitable[SubmitOutcome]`.
   - It owns a private `AppState`/`InputState`, its own `OverlayHost` for its
     pickers, and a `UnifiedInputSession`.
   - It exposes `handle_key`, `render(width) -> RenderableType` (the prompt
     line, plus an open picker below it) and `text`.
   - The hub's session input uses it; that is a refactor, with no behaviour
     change.
2. **The overlay and the hub form embed it.**
   - `QuestionsOverlay`'s typing mode hosts an `EmbeddedComposer`. Its picker
     renders inside the questions overlay, through the composer's own
     `OverlayHost`, so nothing is unmounted.
   - AGI-01's hub form uses the same component for "Other" and for
     single-question items.
   - `PromptOverlay`'s use in `QuestionsOverlay` and the hub's `LineEditor`
     Answer field are deleted. `PromptOverlay` stays only where it is still
     used, and `rg PromptOverlay` lists each remaining use.
3. **Editing parity.** Inside an answer, the following behave exactly as in
   the main composer:
   - bracketed paste, with condensed large pastes;
   - `Ctrl+J` newline;
   - Delete;
   - word movement and deletion;
   - Home/End;
   - an answer history per session.

   Enter confirms the answer. Esc closes an open picker first, then leaves
   typing mode with the text kept.
4. **`@` mentions.**
   - `@` opens the mention picker, rooted at the asking session's workspace:
     the TUI's project, or the background session's cwd in the hub.
   - On confirm, mentions in the answer are resolved with
     `mentions.injector.build_context_prefix`, under the same workspace
     policy, caps and cache as a message.
   - The tool receives the context prefix followed by the typed text, the
     same shape the model sees for a message. `mention_chips` are recorded
     for the transcript.
   - A mention that cannot be resolved is left as typed, with an
     `unresolved` chip.
5. **`/` commands and skills.**
   - `/` opens the command and skill picker, filtered for a waiting turn by
     each command's `BusyPolicy`:
     - `IMMEDIATE_READ_ONLY` commands (`/help`, `/usage`, `/perf`, …) run at
       once and the answer stays open;
     - `IMMEDIATE_CONTROL` commands act on the waiting turn. For example,
       `/cancel` cancels the running intent, which ends the question as
       cancelled;
     - `QUEUE` and `REJECT` commands (`/loop`, …) are shown disabled, with
       "not available while the agent waits for your answer".
   - A skill inserts its invocation into the answer text, so the agent
     receives it.
   - In the hub, commands are scoped to the target session, as the session
     input composer already does.
6. **Keys reach the answer.** While an answer composer has focus, it
   receives every key before the overlay's own bindings: `[`/`]` and
   PageUp/PageDown question scrolling apply only in selecting mode. The fix
   for `Key.AT` being dropped is therefore structural, not a special case.

## 3. Non-goals

- Changing the main composer's behaviour.
- Mentions inside option labels, or anywhere other than free-text answers.

## 4. Acceptance criteria

1. **Typing and paste.** In the `ask_user` overlay's "Other" mode, typing
   `see @`, choosing `src/main.py` in the picker, pasting ` and more`,
   `Ctrl+J`, then `line two` and Enter returns an answer whose typed part is
   `see @src/main.py and more\nline two`. The answer is preceded by the
   mention's context prefix. The `qo.py` reproduction (AGI-00 §6) becomes
   this test.
2. **The picker does not unmount the overlay.** Opening and closing the `@`
   and `/` pickers inside the overlay keeps the question, the cursor and the
   earlier answers. `QuestionsOverlay.on_unmount` is not called.
3. **`/` behaviour.**
   - `/usage` runs while the question waits, and the answer is still open.
   - `/cancel` cancels the turn, and `ask_user` returns `{"cancelled": true}`.
   - `/loop` (`QUEUE`) is listed as disabled, with the reason.
4. **The hub uses the same component.** The hub's "Other" answer and its
   session input are both `EmbeddedComposer` instances (asserted by type),
   with mentions rooted at the background session's cwd.
5. **One editor.** `LineEditor` is no longer used for answers, and
   `QuestionsOverlay` no longer inherits `PromptOverlay`.
6. **Mention safety.** An `@` path outside the asking session's workspace is
   refused by the same `WorkspaceAccessPolicy` that refuses it in a message.

## 5. Rollout and security

- **Mentions.** Mentions in answers resolve under the asking session's
  `WorkspaceAccessPolicy` and the PRD-167 caps, so an answer can reveal no
  more of the filesystem than a message can.
- **Commands.** Commands from an answer composer run with the waiting
  session's mode and `BusyPolicy`. Nothing runs that would be refused while
  the turn is busy.
- **Answer size.** The answer payload grows by the mention context prefix.
  It is bounded by the same injection caps as a message, and the tool
  result is bounded by TOOLR-04's envelope limits.

## 6. Verification

- `uv run pytest tests/unit/test_questions_overlay*.py
  tests/e2e/test_waiting_modal_resize_e2e.py tests/unit/test_agents_hub*.py
  -q`
- New: `tests/unit/test_embedded_composer.py`, and an overlay test built
  from the `qo.py` reproduction (criteria 1–3, 6).
