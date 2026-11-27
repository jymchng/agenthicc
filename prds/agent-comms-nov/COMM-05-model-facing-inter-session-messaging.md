---
title: "COMM-05: Let an agent message another agenthicc session"
status: Partially implemented — the messenger and the receiving side; no agent tool yet
version: 1.1.0
date: 2026-11-27
priority: P2
related_prds:
  - COMM-00
  - COMM-02
  - COMM-04
  - prd-141
  - prd-150
  - SUBAGENT-19
---

# COMM-05 — Let an agent message another agenthicc session

## Problem

A user can run several agenthicc sessions on one project: a long background
build, a review session, an interactive one. The *user* can talk to any of them
(`agenthicc agents input <session>`, `agenthicc session send`, `agenthicc
attach`). The *agents* cannot talk to each other. The interactive agent that
learns the build session's assumption is wrong cannot tell it; the user has to
carry the message by hand.

## Evidence

- Operator paths exist: `agenthicc agents` has `input`, `cancel`, `stop-worker`,
  `approve`, `reject`, `retry` and more; `agenthicc session` has `send`,
  `control`, `events`; `agenthicc attach` joins one.
- `BackgroundInputInbox` (`background/input_inbox.py`) is the durable channel for
  operator input to a live background session: FIFO, owner-fenced, deduplicated,
  at most `MAX_INPUT_CHARS = 64_000` characters a message and
  `MAX_PENDING_MESSAGES = 64` pending, with receipts that move `accepted` →
  `claimed` → `delivered` → `completed`, or end `failed` / `rejected`.
- It carries no sender. `BackgroundInput` has `message_id`, `session_id`,
  `owner_attempt`, `text`, and the lifecycle fields.
- Searching `tools/`, `subagents/`, `agents/` and `skills/` for
  `BackgroundInputInbox`, `.enqueue(` and `session_service` finds nothing: **no
  agent-facing path exists**. The two callers of the inbox's `enqueue` are in
  `background/supervisor.py`.
- **Not yet established, and the design step must read it first:** which session
  kinds accept inbox input. The background store has an `attached` status for a
  terminal session, but whether it drains the inbox is not confirmed here.

## Goals

1. An agent can list the sessions it may message and send one of them text.
2. The text reaches the target through the same durable, ordered path an operator
   uses, so a message never jumps ahead of or races operator input.
3. The target reads it as untrusted context from a named sender at a safe
   boundary, and the operator of the target sees where it came from.
4. A reply is possible and correlated, without either agent waiting on the other.
5. The user stays in control: sending is approved like any other action that
   leaves the session, and a loop is impossible.

## Non-goals

- Reaching other users, other hosts or other project roots.
- Broadcast to every session, or a shared scratch memory between sessions.
- Streaming output between sessions. A session's results are read through its
  own transcript and `agenthicc session events`.
- Giving a message authority. It cannot approve, answer an approval, or change
  the target's mode or capabilities.

## Design

1. **Two tools.** `list_sessions` (read-class) returns the sessions of this
   project root the sender may address: id, label, status, kind, and whether it
   accepts input now. `message_session` (execute-class) takes `session_id`, `text`
   and an optional `reply_to` message id.
2. **Same inbox, with a sender.** `BackgroundInput` gains `sender_session_id`
   (empty for an operator) and `reply_to`. The inbox's ordering, ownership
   fencing, dedupe and limits are unchanged; the new fields only label the entry.
3. **Delivery framing.** The target's next turn receives
   `## SESSION MESSAGE (UNTRUSTED CONTEXT)` with the sender's id and label, using
   the notice mechanism COMM-02 extends with a `session` kind.
4. **Approval.** In Safe and Plan modes the tool asks, showing the target and the
   text. In Yolo it runs, and is logged to both sessions' journals. A capability
   narrowing the sender (a child scope) can withhold the tool entirely.
5. **Loop and flood control.** A message records its origin chain. A turn that
   was started by a session message may send only to sessions not already in the
   chain, and at most N messages per target per hour; refusals name the rule.
6. **Reach.** Only sessions of the same project root and the same user are
   listed. A session in `completed`, `failed`, `cancelled`, `archived` or
   `deleted` status is not addressable; a send to one whose owner died during the
   call is rejected through the inbox's own `rejected` receipt.
7. **Replies.** `reply_to` links a message to the one it answers, so a sender can
   tell, from `list_sessions` or its own journal, that the reply arrived; neither
   side blocks.

## Failure handling

- A full target inbox (64 pending) refuses with that limit, and the sender's tool
  result says so.
- A target that never claims the message leaves it at `accepted`; the sender's
  journal shows the receipt state, so the sender can stop waiting.
- A restart of either session keeps the durable entry, and the owner-fence stops
  a stale owner from consuming it.

## Acceptance criteria

1. `list_sessions` omits sessions of other projects and sessions that are not
   active, and says which accept input.
2. A message sent with `message_session` is delivered to the target once, after
   any operator input already queued ahead of it.
3. The target's model sees it framed as untrusted context with the sender's id.
4. A message from session A to B that causes B to message A is refused at the
   second hop with `loop`, not delivered.
5. In Safe mode the send needs approval and shows the target and the text.
6. The 65th pending message is refused with the inbox limit.
7. A message never alters the target's mode, approvals or capabilities.
8. The target's hub transcript shows the message and its sender.

## Verification

Inbox unit tests for the new fields and for ordering against operator input; a
tool-contract test for approval and capability withholding; an integration test
with two real session runtimes, a scripted provider on each, and a message that
crosses; and a cycle test.

## What shipped (1.1.0), and what did not

**Shipped: the part that can be verified without changing every session's tool surface.**

| Piece | Where |
|---|---|
| `SessionMessenger`: `addressable()` and `send()` over a real `BackgroundStore` and `BackgroundInputInbox`, with named refusals and a per-target limit | `background/session_messages.py` |
| The fixed header, `frame_session_message`, `is_session_message`, `session_message_sender` | `background/session_messages.py` |
| The worker records a framed message as `source: "session_message"` with a summary, not as the operator's input (`input_event_payload`) | `background/session_messages.py`, `background/worker.py` |
| `session_message` joins `RUNTIME_NOTICE_SOURCES`, so it stays out of the prompt history and the session title | `subagents/delivery.py` |
| The agents hub shows it as a `sessions` row, `from <session> (<label>) · <how it begins>` | `tui/workspace/agents_hub/detail.py`, `render.py` |

**Open question, answered.** Only a *background worker* drains the inbox
(`background/worker.py`; `claim_next` has no other caller), and the inbox accepts
input only for a session in `ACTIVE_STATUSES` with a live worker. An interactive
terminal session (`attached`) is not in that set, so **a message reaches background
and hosted sessions only**. That is why `addressable()` lists live sessions with a
worker pid and nothing else.

Decisions where the shipped code differs from the proposal above:

1. **Provenance is in the text, not in the inbox schema.** `BackgroundInput` has no
   sender field and none was added. A fixed header names the sender
   (`## SESSION MESSAGE (UNTRUSTED CONTEXT)` then `From session <id> (<label>).`), and
   the receiving side parses it. The header is also what keeps a message from being
   a command: a worker dispatches input that begins with `/` or `$` as a slash command
   or skill, and a session message always begins with `#`. `reply_to` is a line in the
   header, not a durable correlation field.
2. **It uses the strict live path, never the recovering one.** The operator's path
   (`enqueue_input_or_recover`) starts a new background attempt for a completed,
   failed or orphaned session. The messenger calls `BackgroundInputInbox.enqueue`,
   which accepts only the live owner, so an agent cannot revive and pay for finished
   work by writing to it.
3. **A per-target limit, not an origin chain.** At most 8 messages from one session to
   one target in an hour (`rate_limited`) stops two sessions answering each other
   forever. An origin chain needs the messenger to know which message started the
   current turn, which it does not.
4. **Refusals name the rule:** `unknown_session`, `self_message`, `other_project`,
   `not_live`, `rate_limited`, `inbox_full`, `owner_changed`, `empty_message`,
   `message_too_long`.

**Not done, and why.**

- **The tools `list_sessions` and `message_session` are not registered, so no agent can
  call the messenger yet.** The builtin tool registry has no config-gated seam: its
  groups are unconditional (`BUILTIN_GROUPS`), and optional tools arrive only as
  trusted project plugins. Adding the two tools to the default surface would change
  every session's provider prompt and tool counts, interact with mode ceilings,
  approval policy and subagent tool parity, and is a security decision (an agent
  writing into another session's context). It needs a decision, not a drive-by.
  The shape I would propose: a `[sessions] agent_messaging` setting, off by default,
  that adds a `sessions` tool group to `build_registry` when enabled, with
  `message_session` classed as `execute` so Safe and Plan ask first, and
  `list_sessions` as `read`. I did not build it, because I could not verify its effect
  on recorded cassettes and the tool-surface tests from here.
- **No approval prompt**, for the same reason: it follows from registering the tool.
- **No test runs two live session runtimes.** The messenger is tested against the real
  store and inbox, and the worker's recording through `input_event_payload`, but a
  worker was not run to consume a message. The hosted-runtime path (SUBAGENT-19) was
  not exercised.

## Risks

- A sender that can write to another session's context is a prompt-injection
  path between sessions. The untrusted framing, the live-only rule, the project-root
  boundary and the limit are the defence; none of them is optional, and the tool
  must not be registered without an approval step.
- Because only background and hosted sessions drain the inbox, an agent in a terminal
  session can send but not receive. The tool's `list_sessions` result already lists
  only sessions that can receive.
