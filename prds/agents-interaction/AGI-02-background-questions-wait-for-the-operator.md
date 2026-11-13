---
title: "AGI-02: Background questions wait for the operator, can be answered late, and never linger stale"
status: Implemented with amendments (2026-11-05; AGI wave 3, pending merge)
version: 1.0.0
date: 2026-11-04
scope: "the deadline a background ask_user waits under, what happens to an answer after the deadline, and the inbox items the hub shows as needing the operator"
related_prds: [AGI-00, AGI-01, PRD-192, PRD-227, AGX-10, TUIR-05]
tags: [background-sessions, ask_user, inbox, attention]
---

# AGI-02: Background questions wait for the operator

## Implementation status (2026-11-05)

Delivered in AGI wave 3 (branch `agi-wave-3-questions-wait`).

- **No background deadline by default.**
  `[background] question_timeout_s` defaults to `0`, which means no
  deadline. `BackgroundApprovalService(question_timeout_s=None)` publishes
  the question with no expiry and waits until it is answered, expired by the
  operator, or the session stops. `[tools] question_timeout_s` now governs
  the foreground overlay only.
- **Late answers.** The hub lists an expired question, for a session still
  active on the same attempt, as `◇ answer late` ("timed out · …").
  - Answering it through AGI-01's form enqueues one input message through
    `enqueue_input_async`: "Late answers to your earlier questions (they
    timed out after N s and you continued with an assumption): id: answer;
    … Revisit your assumption if these change it."
  - The item is then closed as `delivered` (`inbox.close_expired`).
  - `x` on it dismisses it (`rejected`).
- **Stale items.** `list_items()` marks a pending item stale on read when
  its session no longer waits on it, so stale items never count in Needs
  you or the badge.
- **The time waited.** The row's age column already shows it ("2h").
- **The model knows.** The `ask_user` description says a background session
  waits for the operator, and that a `timed_out` result may be followed by
  late answers.

Amendments:

- **Stale on read, not reconcile on open.** This covers every reader (the
  hub, the CLI and `/inbox`) without a separate call.
- **The label is `◇ answer late`.** "expired — answer anyway" did not fit the
  status column.
- **Approvals are unchanged.** Approval waits were already unbounded.
- **Amendment (post-merge): the default deadline is 300 s.** The first release
  used `[background] question_timeout_s = 0` (wait forever). It now defaults to
  `300`, matching the foreground overlay, so an unattended question cannot
  park a turn indefinitely. An explicit `0` remains the unbounded wait. Tests
  assert `BackgroundSettings().question_timeout_s == 300.0` and that an
  explicit `0` still opts out.

Tests: `tests/unit/test_background_questions_wait.py` (7). They cover:

- the setting's default;
- a wait past 0.3 s that is then answered;
- a configured deadline expiring;
- a late answer delivered as one message;
- `x` dismissing an expired question;
- a stale pending item giving a zero Needs-you count;
- the "2h" age.

`tests/unit/test_background_interaction_inbox_prd227.py` adds the
stale-on-read case.

## 1. Problem

AGI-00 §3.1.

- **Background questions expire before anyone sees them.**
  `BackgroundApprovalService.request_input` waits under
  `[tools] question_timeout_s`, default 300 s (`worker._session_question_timeout`).
  33 of the operator's 39 questions expired. The agent carried on with its
  own assumption (PRD-192's `timed_out` result), and there was nothing left
  to answer when the operator came to the hub. The 300 s default was chosen
  for the foreground overlay, which is on screen. A background agent is by
  definition unattended.
- **An expired question cannot be answered at all.** The item is `expired`,
  the hub drops it, and nothing tells the agent the operator's answer, even
  though the operator can still send the session a message.
- **Stale items are shown as needing the operator.**
  - All 7 items still marked `pending` in the operator's store belong to an
    older attempt of a session that has since failed, been deleted or
    completed.
  - The hub lists pending items without an attempt check
    (`inbox.list_items` → `needs_you_rows`), so each one is a "needs you" row
    and counts in the badge (TUIR-05).
  - `reconcile()`, which would mark them `stale`, runs only from
    `agents inbox` and `/inbox`.

## 2. Goals

1. **A background deadline of its own.**
   - New setting `[background] question_timeout_s`, default unset: wait until
     the question is answered, expired by the operator (`x` in the hub, or
     `agents inbox expire`), or the session is stopped.
   - A number restores a deadline.
   - `[tools] question_timeout_s` keeps governing foreground overlays only.
   - `[background] wall_timeout_s` and stopping the session still end the
     wait. The new setting sits beside it in `BackgroundSettings`.
2. **A waiting agent is visible.**
   - While a background question waits, the session is `waiting_input`
     (already the case), and AGX-10 notifies once when it starts waiting.
   - The hub row shows how long it has waited ("◆ needs input · 2 h"). With
     a deadline set, it shows how much time is left.
3. **Late answers reach the agent.**
   - An `expired` question item stays listed in Needs you as "expired —
     answer anyway" until the session ends or the operator dismisses it. On
     an expired item, `x` (expire on a pending one) means dismiss.
   - Answering it opens AGI-01's form. It delivers the answers to the
     session as its next input message, in a bounded, fixed format:

     ```text
     Late answers to your earlier questions (they timed out after 300 s and
     you continued with an assumption): hero: Derive it from the README;
     assets: Use the existing docs images. Revisit your assumption if these
     change it.
     ```

   - Delivery goes through `BackgroundInputInbox`, the same path as `i` on a
     running session. Nothing is injected into the finished tool exchange, so
     replay stays safe.
4. **Stale items are never shown as pending.**
   - The hub calls `inbox.reconcile()` off the event loop when it opens and
     after each session update that changes an attempt.
   - `list_items` returns only items whose attempt matches their session's
     current attempt and whose session is active. A mismatch is marked
     `stale` on read.
   - The badge counts only those items.
5. **The agent knows the rules.** The `ask_user` description tells the model
   that a background session waits for the operator, possibly for hours, and
   that a `timed_out` result may be followed later by a late-answer message.

## 3. Non-goals

- Changing foreground `ask_user` timing (PRD-192).
- Approvals' deadlines. Approvals already wait without a deadline in
  background sessions (`request_approval` passes `timeout_s=None`).

## 4. Acceptance criteria

1. **No default deadline.** With default settings, a background `ask_user`
   still waits after `[tools] question_timeout_s` has elapsed, using an
   injected clock. Answering from the hub then returns the answers to the
   tool.
2. **A configured deadline.** With `[background] question_timeout_s = 60`,
   the question expires after 60 s and the tool returns `timed_out`, as
   today.
3. **A late answer.** Answering an expired item from the hub delivers one
   input message, in the format above, to the session. The session is
   `waiting_input` for its next message or picks it up at the next turn
   boundary. The hub then shows the item as `delivered`.
4. **Stale items.** A store with the study's 7 stale pending items shows 0
   Needs-you rows and a 0 badge when the hub opens, and each item's status is
   `stale`.
5. **The time waited is shown.** A session that has waited two hours shows
   "2 h" on its row.

## 5. Rollout and security

- **Migration.** An existing `[tools] question_timeout_s` no longer limits
  background questions. Operators who relied on the 300 s fallback set
  `[background] question_timeout_s`. This is called out in the changelog and
  in `docs/guides/question-timeouts.md` and
  `docs/guides/background-sessions.md`.
- **Late answers.** A late answer is ordinary operator input. It goes through
  the same owner check as `i` on a running session, so no new privilege is
  added.
- **Waiting costs nothing.** A waiting worker spends no provider tokens, but
  it does hold a worker slot. The hub's capacity display counts waiting
  sessions as it does today.

## 6. Verification

- `uv run pytest tests/unit/test_ask_user_timeout.py
  tests/e2e/test_ask_user_timeout_e2e.py
  tests/unit/test_background_interaction_inbox_prd227.py -q`
- New: an inbox test for stale-on-read, using the study's 7-item fixture, and
  a hub test for the late-answer row and its delivery.
