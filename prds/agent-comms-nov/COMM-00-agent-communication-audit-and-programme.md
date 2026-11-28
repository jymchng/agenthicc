---
title: "COMM-00: Agent communication audit and programme"
status: Proposed
version: 1.0.0
date: 2026-11-27
priority: P1
related_prds:
  - SUBAGENT-08
  - SUBAGENT-11
  - SUBAGENT-18
  - SUBAGENT-19
  - prd-141
  - prd-149
  - prd-150
---

# COMM-00 — Agent communication audit and programme

This programme finds where work and messages cannot cross a boundary today and
fixes them in dependency order. The boundaries are the ones an operator meets
when work runs long: from the operator to a running tool, from background work
to the agent, between the agent in the foreground and its subagents, between
subagents, and between agenthicc sessions.

The audit below states what the source does today, with the file that proves
it. A claim marked **not found** means the search named in the row returned no
caller; it is not a claim about code outside this repository.

## Why now

A 39-minute `bun probe.mjs | tail -80` held a turn with no way to let it
continue (COMM-01). Fixing that exposed the same shape elsewhere: a handle is
returned, and nothing tells the agent when it ends; a worker deep in a tool call
cannot be reached; two workers in different pools cannot talk; a session cannot
ask another session anything. Each is a separate hole in one fabric, so they are
written as one series with one set of rules.

## Audit

| # | Channel | What exists | Gap | PRD |
|---|---|---|---|---|
| A | Operator → running work | Esc cancels a turn; `/stop` and `/ps` act on terminals; Ctrl+B detaches a foreground subagent pool (`tui/actions.py`, `SubagentPool.request_background`) | A running shell command had no equivalent: the user could only cancel it and lose the work | [COMM-01](COMM-01-foreground-work-to-background.md) |
| B | Background work → agent | A background terminal is polled with `wait_terminal` / `inspect_terminal`; a finished worker pool is pushed to the parent as a notice (`subagents/delivery.py`: `PendingNotice.kind` is `worker`, `question` or `message`) | Nothing pushed a finished terminal to the agent; a `backgrounded` receipt promised a handle and then silence. Search for `TerminalState`/`terminal_manager` in `runners/agent_turn.py`, `subagents/delivery.py`, `agent_turn_context.py`, `communication.py`: **not found** at the time of the audit. **Closed by COMM-02** | [COMM-02](COMM-02-background-terminal-completion-notices.md) |
| C | Foreground agent ↔ subagent | Durable per-pool broker; parent tools `send_subagent_message`, `answer_subagent`, `collect_subagent_results`; worker tools `send_parent_message`, `ask_parent`, `poll_agent_messages` (`subagents/communication.py`) | The main turn takes queued input at a safe tool boundary (`QueuedMessageSource`, `NoticeSource`); `SubagentWorker.run` deliberately clears that with `bind_turn_notices(None)`. A worker reads mail at the start of each provider run (`pool.py`, `_run_with_retry`) or when its model chooses to call `poll_agent_messages`. A worker inside a long tool call heard nothing until then. **Closed by COMM-03**: mail is now added between tool calls; a running tool call is still never interrupted | [COMM-03](COMM-03-tool-boundary-delivery-to-workers.md) |
| D | Subagent ↔ subagent | `send_peer_message`, `ask_peer`, `answer_peer`; membership is `{"main", …that pool's workers}` (`AgentMessageBroker._members`), checked on every send | One broker per pool. Workers of two pools running at once, and a nested pool's workers and their ancestors, had no route to each other. **Closed by COMM-04**: a worker addresses another as `<pool_id>/<worker_id>` when the pools are related; a cross-pool question is a message and a reply | [COMM-04](COMM-04-cross-pool-and-nested-worker-addressing.md) |
| E | Session ↔ session | Operators: `agenthicc agents input <session>`, `agenthicc session send`, `agenthicc attach`; a durable, owner-fenced `BackgroundInputInbox` (≤ 64 000 characters a message, ≤ 64 pending) | No tool lets an agent address another session. Search for `BackgroundInputInbox`, `.enqueue(` and `session_service` in `tools/`, `subagents/`, `agents/`, `skills/`: **not found** at the time of the audit. **Closed by COMM-05**: `list_sessions` and `message_session` reach the other live background sessions of the project (on by default, `[sessions] agent_messaging`), with an origin chain that refuses loops | [COMM-05](COMM-05-model-facing-inter-session-messaging.md) |

## Rules every PRD in the series follows

1. **Durable and correlated.** A message or notice has a stable ID, survives a
   restart, and is acknowledged only after it has been persisted where it is
   used. The pool broker and the session inbox already do this; nothing new may
   be fire-and-forget.
2. **Delivered at a safe boundary.** Content enters a model's context between
   tool calls or between provider runs, never inside one. A tool call that is
   running is not interrupted to deliver text.
3. **Untrusted by default.** Anything one agent writes for another is framed as
   untrusted context, as worker mail already is (`## AGENT MESSAGES (UNTRUSTED
   CONTEXT)`). A message never changes a policy or a permission.
4. **Scopes only narrow.** A child's capability scope may only restrict its
   parent's. A new channel must not let a message carry authority across a scope
   boundary.
5. **Bounded.** Every mailbox, queue and payload has a limit, and a refused
   message says which limit.
6. **Visible to the operator.** A message between agents appears in the hub
   transcript, as a worker's notice to its parent already does.
7. **Replace, don't layer.** When a PRD supersedes a symbol, the symbol is
   deleted with its callers and tests in the same change.

## Order and dependencies

```text
COMM-01 (implemented) ── COMM-02 ──┐
                                   ├── COMM-05
COMM-03 ── COMM-04 ────────────────┘
```

COMM-02 and COMM-03 are independent and each ships alone. COMM-04 needs the
delivery boundary COMM-03 adds, because a peer in another pool is only useful if
the mail reaches it before its tool call returns. COMM-05 reuses COMM-02's
notice kind for replies and COMM-04's addressing for the identity it carries.

## Non-goals

- A new transport. Pools keep their broker, sessions keep their inbox and the
  hosted controller protocol.
- Real-time or streaming chat between agents. Delivery is at boundaries.
- Messaging across machines or users. The series covers one user's sessions on
  one host.

## Open questions

1. Should a backgrounded command's completion wake an idle parent as a finished
   worker does, or only appear on the next turn? (COMM-02)
2. Is a message to a worker inside a tool call worth interrupting that tool call
   for, ever? The series assumes not. (COMM-03)
3. Should an agent in a project be able to message a session of another
   project, or only sessions of the same project root? (COMM-05)
