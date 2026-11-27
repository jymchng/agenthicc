---
title: "COMM-03: Deliver parent and peer mail to a worker at a tool boundary"
status: Implemented
version: 1.1.0
date: 2026-11-27
priority: P1
related_prds:
  - COMM-00
  - SUBAGENT-08
  - SUBAGENT-11
  - SUBAGENT-18
---

# COMM-03 — Deliver parent and peer mail to a worker at a tool boundary

## Problem

The agent the user talks to can be reached while it works: input the user types
during a turn is claimed "at a safe tool boundary" and reaches the model before
its next provider request (`QueuedMessageSource.claim`, `NoticeSource.has_pending`
in `runners/agent_turn_context.py` and `subagents/delivery.py`). A subagent
worker cannot. A parent that sends `send_subagent_message("stop; the API changed,
use v2")` reaches a worker that is in the middle of a ten-minute test run only
when that run returns *and* the worker's model next calls `poll_agent_messages`,
or at the start of the worker's next provider run. Until then the worker keeps
spending time and tokens on a plan the parent has already replaced.

## Evidence

- `SubagentWorker.run` wraps the worker in `bind_turn_notices(None)`
  (`subagents/pool.py`), which removes the notice source and the input
  subscriber the main turn uses for exactly this.
- Mail is read in `_run_with_retry`: `self._communication.poll(self._task.task_id,
  limit=16)` before each provider run, appended to the prompt under `## AGENT
  MESSAGES (UNTRUSTED CONTEXT)`, and acknowledged after that run succeeds. A
  worker's provider runs happen at its start, after a clarification answer, a
  textual-tool-call correction, or a finalisation turn.
- `poll_agent_messages` is the worker's own tool (`subagents/communication.py`):
  delivery depends on the model choosing to call it.
- The broker already knows how many messages wait: `undelivered_count`.

## Goals

1. A worker reads pending parent and peer mail between two of its tool calls,
   without waiting for its model to ask.
2. Delivery is at-least-once with acknowledgement after the provider request that
   carried the mail succeeds, so a failed request does not lose it.
3. An operator can see that a message reached a worker, and when.
4. No tool call that is running is interrupted to deliver text.

## Non-goals

- Interrupting or killing a running tool call to deliver mail. A parent that wants
  work stopped uses `stop_subagents`, which already aborts.
- Delivering to a worker that has finished. Its mail goes to its pool's record.
- Changing what a parent can send, or the broker's limits.

## Design

1. **Reuse the main turn's boundary, not a second one.** The main turn's
   machinery claims one item at a tool boundary. A worker should bind a source of
   the same shape over its own mailbox, instead of binding `None`. The broker is
   the source: `claim()` polls the worker's mailbox, `has_pending()` is
   `undelivered_count(worker) > 0`.
2. **Where it lands.** Mail is added to the worker's next provider request as the
   existing untrusted-context block, built by one function shared with
   `_run_with_retry`, so the framing is defined once.
3. **Acknowledge on success.** The ids are acknowledged after the request that
   carried them completes, which is today's rule for the run-boundary path.
4. **Remove the duplicate path.** If tool-boundary delivery covers the
   run-boundary one, `_run_with_retry` stops polling and uses the same claim; the
   two must not both deliver the same message (a worker would read it twice).
5. **Observability.** The existing `message_acknowledged` transcript event is
   joined by `message_delivered`, shown in the worker's hub transcript.

## Failure handling

- A delivery request that fails leaves the mail pending; the next boundary
  delivers it.
- A full mailbox keeps refusing senders with the existing limit error.
- A worker with no tool calls (a pure reasoning worker) is delivered at its
  provider-run boundaries as today.

## Acceptance criteria

1. A worker whose tool call takes longer than the parent's message sees the
   message in the request that follows that tool call, with no
   `poll_agent_messages` call from the model.
2. The message is delivered once. A second poll or a later run does not repeat it.
3. If the request that carried it fails, it is delivered again.
4. The hub transcript shows `message_delivered` with the message id.
5. A message sent while the worker is between provider runs is delivered at the
   next run, as today.
6. Removing the worker's notice binding again fails a test.

## Verification

A unit test over a fake source and the broker; an integration test with a scripted
provider that holds a tool call open on a flag file, sends the message meanwhile,
and asserts the next request's content and the acknowledgement order; the existing
`poll_agent_messages` tests keep passing unchanged.

## What shipped (1.1.0)

| Piece | Where |
|---|---|
| `AgentMessageBroker.peek`: a non-destructive read of a member's pending mail | `subagents/communication.py` |
| `MailBoundaryRunner`: a worker's runner that adds mail between tool calls; `mail_block`, the one framing of worker mail | `subagents/mail.py` |
| `commit_tool_results`: the tool-batch commit the main turn used inline, now shared | `runners/tool_boundary.py`, `runners/agent_turn.py` |
| Worker wiring: the per-attempt `injected` set, the claim, acknowledgement of boundary mail | `subagents/pool.py` |
| The `message_delivered` lifecycle kind and transcript line | `subagents/events.py`, `subagents/transcripts.py`, `docs/guides/subagents.md` |
| **What the operator reads.** A delivered message is recorded with its sender and the start of its text, for mail read at the start of a run and for mail added between tool calls, and the worker transcript view shows `mail from <sender>: <how it begins>`, then `mail acknowledged · <id>`. Before, a delivery line would have been a bare message id | `subagents/pool.py`, `subagents/mail.py` (`mail_excerpt`), `tui/workspace/overlays/subagents.py` |

Decisions where the shipped code differs from the proposal above:

1. **The mechanism is a runner, not a bound source.** The main turn does not poll
   a bound `NoticeSource` in its loop. It subclasses lauren-ai's runner and
   overrides `_execute_tools`: commit the batch's results, add the message,
   return no results. A worker has no journal and no journaled memory, so the
   source binding has nothing to attach to. `MailBoundaryRunner` is the same
   override, and the commit it needs moved into one helper that the main turn now
   calls too.
2. **`peek`, not `poll`.** `poll` pops a message from the mailbox. Mail polled in
   the middle of a run and then lost to a transport retry's memory rollback would
   never be delivered. `peek` leaves the mail queued; the run's acknowledgement is
   what retires it.
3. **A retry delivers the mail again.** The worker keeps the ids it added in the
   current provider attempt and resets them when an attempt starts, so after a
   rollback the same unacknowledged mail is added again. Tested with a real
   `TransientTransportError`.
4. **Acknowledgement is at the end of the run**, not right after the request that
   carried the mail. It is simpler, and it keeps the at-least-once guarantee: a
   run that fails acknowledges nothing.
5. **The start-of-run `poll` stays.** It carries the mail a worker has before its
   first request, and it serves a worker that calls no tools. `peek` skips what
   `poll` took, and a message the model fetched itself with `poll_agent_messages`
   is likewise never added twice, so the proposal's "remove the duplicate path"
   was not needed.
6. **The framing is shared and was fixed.** The worker mail block was written with
   a doubled backslash (`"\\n\\n## AGENT MESSAGES…"`), so the model read literal
   backslash-n characters instead of line breaks. `mail_block` writes real line
   breaks, in one place.
7. **The per-request cap has no "count of the rest".** At most 16 messages (the
   existing `poll` limit) are added at a boundary; the rest follow at the next.

The limit that remains: a tool call is never interrupted. A worker inside a single
ten-minute command still reads the message only when that command returns. The
parent's way to end work sooner is `stop_subagents`.

Tests: `tests/unit/test_worker_mail_boundary.py` and
`tests/integration/test_worker_mail_boundary.py` (a real session, a scripted
worker lane and a real tool). Mutations checked: no mail source, no
already-added filter, no per-attempt reset and no acknowledgement of boundary
mail each fail a test.

## Risks

- A parent that sends many messages can crowd a worker's context. The existing
  `max_mailbox_messages` and `max_message_bytes` limits apply, and at most 16
  messages are added at a boundary.
- Mail delivered between tool calls can change what the worker does mid-plan.
  That is the point, and why it stays untrusted context and never a system
  instruction or permission change.
