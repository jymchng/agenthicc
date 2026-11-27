---
title: "COMM-04: Address workers across pools and nesting levels"
status: Proposed
version: 1.0.0
date: 2026-11-27
priority: P2
related_prds:
  - COMM-00
  - COMM-03
  - SUBAGENT-04
  - SUBAGENT-11
  - SUBAGENT-17
---

# COMM-04 — Address workers across pools and nesting levels

## Problem

Workers coordinate only inside the pool that started them. Two cases fail today:

- **Parallel pools.** Several background pools can run at once (SUBAGENT-07,
  and a phase's pools in SUBAGENT-17). A worker in a "tests" pool that finds the
  API it tests has changed cannot tell a worker of the "implementation" pool; it
  can only tell its own parent, which must relay it.
- **Nesting.** A worker that spawns its own pool (`subagents.max_depth` above 1)
  is the parent of that pool's workers. A grandchild that needs a fact only its
  grandparent holds must go through its parent, which has to be awake and
  choose to relay.

The relay costs a provider turn at each hop, and the relaying agent may drop or
paraphrase the content.

## Evidence

- Each pool owns one `AgentMessageBroker`. Its membership is
  `self._members: set[str] = {"main"}` plus the workers registered for that pool
  (`register_worker`), and `_validate_sender` / `_validate_recipient` reject any
  other id (`subagents/communication.py`).
- The tool descriptions say so: `ask_peer` is "only for same-pool coordination"
  (`subagents/pool.py`, the worker prompt).
- The relationships a route needs already exist. A nested pool is created with
  `parent_pool_id=self._pool_id` (`subagents/pool.py`), and
  `PoolContinuationRegistry` is a "conversation-scoped registry of the pools a
  parent can reach" with `get(pool_id)` and `handles()`
  (`subagents/communication.py`). Today only the parent's tools read it.
- **Not yet established, and the design step must read it first:** how `"main"`
  resolves in a nested pool whose parent is itself a worker, and where that
  worker receives mail while it is blocked on its own pool.

## Goals

1. A worker can send to, and ask, a worker in another pool of the same
   conversation when the two pools are related by the rule below.
2. The address is stable and unambiguous: `<pool_id>/<worker_id>`.
3. Cross-pool messages are as durable, bounded and correlated as in-pool ones,
   and the operator sees them.
4. No message creates a route to a scope the sender could not reach by other
   means.

## Non-goals

- Reaching a worker in another conversation or session. That is COMM-05.
- Letting a worker address the user. Questions to the user stay with the parent
  and `ask_user`.
- Broadcast to every worker in a session.

## Design

1. **Who may talk to whom.** Two workers may exchange messages when their pools
   are the same, siblings (same `parent_pool_id`, including the top level), or
   one is an ancestor of the other. Unrelated pools (different phases, different
   parents) may not. The rule is a function of `parent_pool_id` alone, so it
   needs no new state.
2. **The recipient's broker decides.** A cross-pool send is delivered into the
   recipient's pool broker, which validates the sender's address against the
   rule, applies its own mailbox and size limits, and records the envelope. The
   sender's broker records an outbound copy with the same message id, so both
   ends can show it and neither can lose it.
3. **Directory.** `PoolContinuationRegistry` resolves `pool_id` to a handle for
   the same conversation. A new read-only tool lists the workers a worker may
   address, so a model does not guess ids.
4. **Delivery.** The recipient reads the mail at the boundary COMM-03 adds. A
   message to a worker that has not started yet is held for it; to one that has
   finished it is refused with the worker's final state.
5. **Questions.** `ask_peer` across pools keeps the question/answer correlation
   and the existing timeout, so a worker never waits forever for a sibling that
   is gone.
6. **Authority never travels.** A message is untrusted context. It cannot widen a
   capability scope, grant an approval or change a policy; child scopes may only
   restrict their parent, and the message path does not touch scopes.
7. **Loop control.** A message carries a hop count; a message sent in reply to
   one with `requires_response` may not itself require a response past a fixed
   depth, and a per-pair rate limit applies.

## Failure handling

- A pool that is cancelled refuses new mail with `pool_closed` and fails its open
  questions, as the broker's `close` does.
- A restart rehydrates both ends from the journal (`rehydrate`), so a message in
  flight between two pools is neither lost nor shown twice.
- A refused message tells the sender which rule refused it (`unrelated_pool`,
  `recipient_finished`, `mailbox_full`).

## Acceptance criteria

1. A worker in one of two parallel sibling pools sends to a worker in the other;
   it arrives once with the sender's full address.
2. A grandchild asks its grandparent a question and receives the answer without
   the intermediate worker taking a turn.
3. A worker in an unrelated pool is refused with `unrelated_pool`.
4. A reply loop between two workers stops at the depth limit.
5. After a restart the message is still delivered exactly once.
6. The recipient's hub transcript shows the message with the sender's address.
7. No test message changes a worker's allowed tools.

## Verification

Broker unit tests for the relatedness rule and for refusals; an integration test
with two real pools and a scripted provider; the existing pool-scoped tests stay
unchanged, since a same-pool address keeps working unqualified.

## Risks

- A cheap route between workers makes it easy to build chatter. The hop limit,
  the per-pair rate limit and COMM-03's per-request cap bound it.
- The nested-pool unknown above could change the design of point 2. It is
  recorded so the design step starts there.
