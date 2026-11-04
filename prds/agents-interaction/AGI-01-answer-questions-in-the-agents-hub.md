---
title: "AGI-01: Answer an agent's questions in the agents hub — one question at a time, by choosing or typing"
status: Implemented with amendments (2026-11-04; AGI wave 2, pending merge)
version: 1.0.0
date: 2026-11-04
scope: "the agents hub's Needs you and detail views: present a background ask_user request as the same per-question form the foreground overlay uses, and deliver the answers without the operator writing JSON"
related_prds: [AGI-00, AGI-02, AGI-05, PRD-100, PRD-227, AGX-05, AGX-07]
tags: [agents-hub, ask_user, background-sessions, ux]
---

# AGI-01: Answer an agent's questions in the agents hub

## Implementation status (2026-11-04)

Delivered in AGI wave 2 (branch `agi-wave-2-answer-composer`).

- **One form model.** `tui/workspace/question_form.py` holds `QuestionForm`
  and `Question`. `QuestionsOverlay` keeps its rendering and scrolling
  (`_QView`), but its focus, options and answers are the form's.
- **The hub's answer form.**
  - `agents_hub/answer.py` holds `AnswerView`. `i` on a question or input
    item opens it.
  - It shows the question with a scroll indicator, the options and Other,
    the ●/○ progress and "question n of m".
  - Keys: ↑↓, ←→, Enter, Ctrl+S, `[`/`]` and Esc.
  - It delivers `{"answers": form.answers()}` (or `{"answer": text}`)
    through `inbox_respond_async` at the version shown.
  - A failed send keeps the form, its answers and the reason. Esc keeps
    progress per item for the next `i`.
- **Typed answers.** "Other" and input items use AGI-05's composer, built
  off the event loop from the session's directory and recorded launch
  configuration. Keys typed while it loads are replayed.
- **The detail view.** Its Needs you card numbers each question and lists
  each option on its own row.
- **The CLI.** The multi-question error names the expected keys ("answer
  with a JSON object with keys hero, assets").

Amendments:

- **The footer still reads `i reply`.** The keymap action's label is shared
  with session input. `i` opens the answer form on a question item.
- **`/` in the hub inserts text.** The hub has no session to run a command
  in, so a command picked in a hub answer is inserted into the answer text.
  `@` mentions resolve under a Safe-mode `WorkspaceAccessPolicy` scoped to
  the agent's directory.
- **No deadline display yet.** The form shows no time remaining; deadlines
  are AGI-02.

Tests: `tests/unit/test_agents_hub_answer_form.py` (7). They cover:

- the study's two-question case, answered end to end through a real
  `BackgroundApprovalService` wait;
- Other with paste;
- Esc progress;
- a stale send;
- the shared model;
- the CLI error;
- an eight-question item at 80×24.

## 1. Problem

AGI-00 §3.1.

- A background `ask_user` request reaches the hub as a `question` inbox
  item. Its target carries every question's `id`, `text` and `options`.
- The hub's only way to answer is `i`, which opens a one-line `Answer` field
  (`hub.py:_act_input`). That field:
  - shows neither the questions nor their options;
  - accepts plain text only when there is a single question;
  - for two or more questions, rejects anything that is not a JSON object
    keyed by question IDs ("multiple questions require a JSON object keyed by
    question ID", from `inbox.response_from_text`). The hub never shows those
    IDs.
- 25 of the operator's 39 questions asked two or more questions (AGI-00 §3.1).
- The detail view lists the questions and options, without their IDs, and
  there is no way to pick an option.
- Attaching kills the waiting worker (`attach_foreground`), so it is not a
  way to answer either.

The foreground TUI already solves this with `QuestionsOverlay` (PRD-100):

- one question at a time, ←/→ between questions;
- ↑/↓ over the options, plus "Other — type your answer";
- Enter confirms;
- the last confirmation submits every answer as `{id: answer}`.

The hub has no equivalent.

## 2. Goals

1. **One form model for both surfaces.**
   - Extract the form logic from `QuestionsOverlay` into a
     presentation-independent `QuestionForm`: the questions, a per-question
     cursor, the confirmed answers, and "Other" free text.
   - It has the operations `move`, `next`, `previous`, `choose`, `type`,
     `confirm` and `answers() -> dict[str, str]`, and owns completeness
     ("every question answered").
   - `QuestionsOverlay` renders a `QuestionForm`. A new hub view renders the
     same form.
   - Both submit `form.answers()`: the foreground through
     `ApprovalService.respond`, the hub through `inbox_respond_async` as
     `{"answers": form.answers()}`, the shape the inbox already validates.
2. **An answer view in the hub.** On a `question` item, `i` (Needs you or
   detail) opens the form in place of the one-line field. It shows:
   - the agent's title and workspace;
   - "Question n of m";
   - the full question text, scrollable when long, as in the overlay's
     viewport;
   - the options, plus "Other — type your answer";
   - a ✓ beside each question already answered;
   - the time left when the item has a deadline (AGI-02).

   Keys:

   | Key | Action |
   |---|---|
   | ↑/↓ | move between options |
   | ←/→ | move between questions |
   | Enter | choose and advance; on the last unanswered question, send |
   | Esc | close the form without sending; the item stays pending |
   | Ctrl+S | send once every question is answered |
3. **Free text uses the composer.** "Other" opens AGI-05's composer inline,
   bound to the session's workspace. Paste, newline, `@` mentions and the
   allowed `/` commands work as in the main TUI.
4. **Single question, single field.** A one-question item with no options
   opens straight into the composer, with the question shown above it. It is
   the same view, with nothing to choose.
5. **The detail view shows what is being asked.** The detail view's
   "Needs you" section numbers the questions and lists each option on its own
   line, instead of comma-joining them. The footer hint for a question item
   reads `i answer` instead of `i reply`.
6. **Nothing typed is lost.**
   - Esc keeps the partial answers on the item's form (in memory, per item
     ID, for the hub's lifetime), so reopening `i` resumes where it was left.
   - A send that fails (the item went `stale` or `expired`) keeps the form
     open with the reason, as the Answer field does today.
7. **The hub never asks for JSON.**
   - The hub's `answer` prompt kind no longer handles `question` items. It
     remains only for `input` items, which take free text.
   - `agenthicc agents inbox answer ITEM RESPONSE` keeps accepting a JSON
     object for multi-question items, for scripts. Its error names the
     expected IDs: "answer with a JSON object with keys hero, assets".

## 3. Non-goals

- Changing the `ask_user` tool's result contract (`{id: answer}`).
- Foreground sessions seen from the hub. They answer in their own TUI.
- Approvals. The approve/reject keys and plan feedback are unchanged.

## 4. Acceptance criteria

1. **The study's case.** A store with the operator's two-question shape
   (`hero`, `assets`) is answered from the hub with `i`, ↓, Enter, ↓, Enter.
   `inbox.accepted_answer(item_id).response` equals
   `{"answers": {"hero": "<option 2>", "assets": "<option 2>"}}`, and the
   worker's `ask_user` returns `{"hero": …, "assets": …}`. This is an e2e test
   through a real `BackgroundApprovalService` wait.
2. **Other.** Choosing "Other" on question 2 and typing a multi-line answer
   with a paste delivers that text verbatim.
3. **One form.** `QuestionsOverlay` and the hub form produce identical
   `answers()` for the same key sequence. This is a shared test over
   `QuestionForm`.
4. **Esc keeps progress.** Answering question 1, Esc, then `i` reopens on
   question 2 with question 1 still answered.
5. **A stale item.** Sending to an item that went stale shows "Answer
   failed: …" and keeps the form open with its answers.
6. **Widths.** The form renders within 80×24 and 120×40 with an 8-question
   item, and the question viewport scrolls. These are the AGX-13 width-matrix
   cases.
7. **No JSON in the hub.** No hub path calls `response_from_text` for a
   `question` item. The CLI's multi-question error lists the question IDs.

## 5. Rollout and security

- The answer is still resolved through `inbox.respond` at the version the
  operator saw (PRD-227). A stale or replayed answer is rejected exactly as
  today; idempotency keys are unchanged.
- Answers are private: they reach the session and the inbox only. The hub
  keeps partial answers in memory, never on disk.
- No migration. Items already in the inbox render in the new form.

## 6. Verification

- `uv run pytest tests/unit/test_background_interaction_inbox_prd227.py
  tests/e2e/test_background_inbox_prd227_e2e.py -q`
- New: `tests/unit/test_question_form.py` (the shared model) and
  `tests/e2e/test_agents_hub_answer_form_e2e.py` (criteria 1, 2, 4, 5).
