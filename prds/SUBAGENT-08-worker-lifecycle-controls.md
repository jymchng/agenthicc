---
title: "SUBAGENT-08: Worker lifecycle controls"
status: Proposed
version: 0.1.0
date: 2026-10-29
scope: "one per-worker lifecycle for stop, steer, retry and partial-result salvage inside a subagent pool, including the abort-versus-drain decision that a budget crossing triggers — never budget accounting, limit keys or the budget report"
area: A-8
related_prds:
  - PRD-148
  - PRD-232
  - SUBAGENT-03
  - SUBAGENT-06
  - SUBAGENT-07
  - SUBAGENT-09
tags:
  - subagents
  - lifecycle
  - cancellation
  - steering
  - retry
  - recovery
---

# SUBAGENT-08: Worker lifecycle controls

## 1. Executive summary

The pool has every piece of a worker lifecycle and no lifecycle. `stop_worker`
cancels a task (`src/agenthicc/subagents/pool.py:1188`), `dismiss_worker` hides a
row (`:1204`), `send_subagent_message` enqueues an instruction
(`src/agenthicc/subagents/communication.py:909`), and `run_with_transport_retry`
retries a provider call (`pool.py:1028`). Nothing connects them: a stop carries
no reason, no source and no mode; the instruction is consumed without an
acknowledgement; a retry applies to one HTTP call rather than to the worker; and
a worker cancelled mid-flight returns an empty result even though it had written
text (`:762-776` versus `:1011`).

**This PRD makes termination a first-class, recorded act, and gives the
half-finished pieces one owner.** A stop becomes a request with a mode (`abort`
or `drain`), a source and a reason, using PRD-148's interrupt vocabulary rather
than a second one. Partial output survives every stop. A failed worker can be
retried without re-running the pool.

It also owns the response that SUBAGENT-06 does not: under the locked boundary
M-1 (research report §5.4), A-6 decides *when a budget is exceeded*; A-8 decides
*what happens to the workers*.

## 2. Problem statement

All claims verified at commit `086849bc38498bd26b757c473a6be741ae72a47e`
(2026-10-29); `src/` and `tests/` have not moved since the report's snapshot
`f843040` (§3 re-checks it).

**Four pieces, no record.** `stop_worker` (`src/agenthicc/subagents/pool.py:1188`)
adds a worker id to `_stopped` (`:1180`, `:1197`) and cancels its asyncio task.
That set and one activity string (`:1198`) are the entire state a stop produces.
There is no request id, no reason, no source, no mode, no completion record and
no cleanup report. The error text is the literal string `"stopped by user"`
(`:1321`, `:1336`) regardless of who or what asked.

**Cancellation throws away the work and keeps the inventory.** Every provider
turn stores its answer in `result["text"]` (`:1011`). The `CancelledError`
handler returns a result with `text=""`, `error="cancelled"`, and the accumulated
`tool_calls` and `changed_paths` (`:762-776`). So a worker stopped after three
productive turns reports which files it touched and none of what it concluded —
the salvage mechanism is half-built, and the half that is missing is the half a
parent needs.

**A failure loses the same thing.** The generic `except Exception` handler builds
the same shape with `text=""` and `error=str(exc)` (`:775-787`), so a worker that
read four files, wrote a paragraph and then hit a provider error returns the error
and the file list without the paragraph. The gap is not specific to cancellation;
it is the shape of every unsuccessful terminal path in the worker.

**Steering is fire-and-forget.** `send_subagent_message` returns
`status="sent"` (`communication.py:919-924`). The worker injects pending messages
at a provider-turn boundary under an *UNTRUSTED CONTEXT* heading
(`pool.py:1013-1022`), and `poll` both pops the message and marks it `delivered`
(`communication.py:383-415`). The broker already has `acknowledge`
(`:417-441`), and **no code in `src/` calls it** (§3): a message that is polled
and then lost to a cancellation, a transport failure or a finished worker is
gone, and the parent is never told it did not land.

**Retry exists one layer too low.** `RetryConfig` (`src/agenthicc/runners/retry.py:71`)
and `run_with_transport_retry` (`:95`) re-run one provider call with a memory
rollback; the pool wires them per attempt (`pool.py:1028`), passing a config
built by the turn runner (`src/agenthicc/runners/agent_turn.py:1557`, `:1585`).
Nothing retries a *worker*. A failed worker's only remedy is calling
`spawn_subagents` again (`src/agenthicc/subagents/tool.py:188`) and paying for
every sibling a second time.

**A stopped row cannot be dismissed.** `dismiss_worker` requires
`status == "failed"` (`pool.py:1204-1211`), while a stopped worker's status is
`"stopped"` (`:530`). The panel still offers `x`
(`src/agenthicc/runners/tui_session.py:2677-2695`), so the key does nothing on
exactly the row a user is most likely to want gone; the row instead expires after
`FINISHED_ROW_S = 30.0` (`pool.py:216`, `:281-297`).

**There is no pool-level stop, and the pool has no handle after it returns.**
Cancelling the parent turn reaches `except asyncio.CancelledError`
(`pool.py:1363-1369`), which closes the broker with `reason="pool_cancelled"`
and re-raises; per-worker partial results are not collected on that path. The
only route to a worker is `active_pool(pool_id)`
(`:1099-1104`, registered `:1282`, removed `:1286`), so control exists only
while `run()` is on the stack in this process — which is why durability is A-7's
and this PRD's job is the semantics of a stop, not its reach.

**The vocabulary is missing, not just the wiring.** `grep -rniE
"request_stop|retry_worker|partial_result|stop_mode|abort|drain|reason="
src/agenthicc/subagents/ --include=*.py` returns two hits, both broker
`close(reason=…)` calls. There is no word in the package for the decision this
PRD is about.

## 3. Current-source investigation

Run on 2026-10-29 at the commit in §2.

```
$ git rev-parse HEAD
086849bc38498bd26b757c473a6be741ae72a47e
$ git diff --stat f843040..HEAD -- src/ tests/
(no output)
```

Nothing in the implementation has moved since the report's snapshot, so §1 of
the report still describes the code and every citation below was read, not
inherited.

**The pieces.** `grep -rn "def stop_worker\|def dismiss_worker\|def
send_subagent_message\|class AgentMessageBroker\|retry"
src/agenthicc/subagents/*.py` returns the definitions at `pool.py:1188`,
`pool.py:1204`, `communication.py:909`, `communication.py:147`, and the retry
plumbing at `pool.py:62`, `:632`, `:659`, `:1013-1034`, `:1126`, `:1151`,
`:1248`, `tool.py:101`, `:292`.

**The callers, which are thinner than the definitions.**

```
$ grep -rn "stop_worker\|dismiss_worker" src/ tests/ --include=*.py | grep -v "def "
src/agenthicc/runners/tui_session.py:2670:        if pool is None or not pool.stop_worker(worker_id):
src/agenthicc/runners/tui_session.py:2685:            return pool.dismiss_worker(worker_id)
tests/integration/test_subagent_panel_agx08.py:185:    assert pool.stop_worker("task-1")
tests/integration/test_subagent_panel_agx08.py:186:    assert not pool.stop_worker("task-9")
tests/integration/test_subagent_panel_agx08.py:195:    assert not pool.stop_worker("task-0")
```

One production caller, reached only through the panel's `x`
(`tui_session.py:2665-2674`). No model-facing tool stops a worker; no
pool-level entry point exists.

**The cancellation path, read in full.**

```
$ grep -n "CancelledError" src/agenthicc/subagents/pool.py
762:        except asyncio.CancelledError:
1330:                    except asyncio.CancelledError:
1364:        except asyncio.CancelledError:
```

At `:762` the worker returns a result whose `text` is empty and whose
`tool_calls`/`changed_paths` are preserved. At `:1330` `_bounded` re-raises. At
`:1364` the pool closes the broker and re-raises to the parent turn.

The same read covers the ordinary failure path, which the `CancelledError` grep
does not surface: `except Exception` at `:775` builds the identical result shape,
with `text=""` at `:782`. Reading both handlers is what makes §2's claim "a
failure loses the same thing" an observation rather than an inference from one
exception type.

**The acknowledgement that nothing calls.**

```
$ grep -rn "acknowledge(" src/ --include=*.py | grep -v "def acknowledge"
(no output)
```

The broker implements `acknowledge` (`communication.py:417-441`) and
`poll` marks `delivered` (`:409`), but no caller ever acknowledges. The stronger
claim — that a polled instruction can be lost — follows from `poll` popping the
mailbox under the lock (`:392-397`): there is no redelivery path.

**The absence proof for this PRD's own vocabulary.**

```
$ grep -rniE "request_stop|retry_worker|partial_result|stop_mode|abort|drain|reason=" \
    src/agenthicc/subagents/ --include=*.py
src/agenthicc/subagents/pool.py:1366:                await self._communication.close(reason="pool_cancelled")
src/agenthicc/subagents/pool.py:1402:            await self._communication.close(reason="pool_completed")
```

The pattern and the scope are the whole `subagents/` package, so this is a
bounded claim about the commit, not a guess: no stop mode, no worker retry, no
partial-result record and no stop reason exists today.

**Existing tests, measured on 2026-10-29.**

```
$ pytest tests/unit/test_subagent_communication.py tests/unit/test_subagent_resume.py \
         tests/unit/test_subagent_pool.py tests/unit/test_subagent_pool_runtime_coverage.py \
         tests/unit/test_subagent_tui.py tests/integration/test_subagent_panel_agx08.py -q
107 passed
```

Per file: 9, 16, 58, 3, 20 and 5 passed. `test_subagent_panel_agx08.py` already
stops one worker and asserts the other two keep running, the aggregate contains
`stopped by user`, and the panel ends `[done, stopped, done]`
(`tests/integration/test_subagent_panel_agx08.py:185-198`) — so the *scheduling*
half of a stop is tested and the *content* half is not.

**Surprises, recorded rather than smoothed over.** Two of them changed this
PRD's goals. First, the cancelled result already carries tool calls and changed
paths (`pool.py:772-773`), so partial salvage is a repair of an existing field
rather than a new mechanism — that is why goal 5 is small. Second, the broker
already implements acknowledgement (`communication.py:417`), so goal 6 is
"call the thing", not "build a delivery protocol". The absence that is real is
the lifecycle record itself, and the stopped-row dismissal defect
(`pool.py:1204-1211` versus `:530`) which §2 cites and goal 9 fixes.

## 4. Goals

1. **One lifecycle record per worker.** A `WorkerLifecycle` — worker id, pool
   id, attempt number, phase, current stop request, partial result and
   undelivered-message count — is the single place a worker's control state
   lives. `WorkerState.status` keeps its six existing values (`pool.py:227`) for
   the panel, and the lifecycle record is what the controls read and write, so
   the panel's contract with AGX-08 does not change.
2. **A stop is a request with a mode, a source and a reason.** `request_stop`
   takes `mode` (`abort` or `drain`), `source` and `reason`, returns a
   `WorkerStopRequest` with a stable `request_id`, and is idempotent: repeated
   requests collapse onto one record and one terminal transition.
3. **`abort` cancels now.** The worker's task is cancelled, the pool awaits it
   to completion before recording the result, and the terminal phase is
   `cancelled` — not `failed`, which is what a provider error means.
4. **`drain` stops the wait, not the work already paid for.** Pending workers
   never start (`pool.py:1314-1322` already implements the skip), running
   workers finish normally, and their results are kept. A drain of a pool
   therefore reduces `total` to "the workers that ever ran", and the report says
   which workers never started.
5. **Partial output survives every unsuccessful ending.** A cancelled worker
   returns its accumulated text, the tool evidence it already keeps, `partial=True`
   and a `stop_reason`. A stop in the middle of a turn truncates at the last
   completed provider turn; the result says so rather than implying the answer is
   whole. The same fields carry a timeout (`pool.py:746-760`), a failure
   (`:775-787`) and a transport-exhausted attempt, so "stopped or failed" is one
   representation of salvage rather than two.
6. **Steering is acknowledged, not merely sent.** Each injected instruction is
   acknowledged after the turn that consumed it, via the broker's existing
   `acknowledge` (`communication.py:417`); a message still unconsumed when the
   worker terminates is reported as `undelivered_messages`, so a parent learns
   its instruction never landed instead of assuming it did.
7. **Worker retry is a new attempt, not a transport retry.** `retry_worker`
   accepts a worker whose last attempt ended `failed` or `cancelled`, starts
   attempt *n+1* with a fresh worker, keeps the previous attempt's partial output
   and error in the lifecycle record, and never re-runs a succeeded worker.
   Transport retry (`retry.py:95`) stays exactly where it is: inside one attempt.
   Whole-pool retry keeps its existing meaning — the parent calls
   `spawn_subagents` again (`src/agenthicc/subagents/tool.py:188`) and pays for
   every sibling twice — and this PRD adds no second pool-retry API, so the choice
   between the two is a choice the parent can see.
8. **A pool-level stop is a fan-out of the worker-level one.** `request_pool_stop`
   applies one mode and one reason to every worker and reports the per-worker
   outcome. There is one cancellation implementation, called at two scopes —
   the same discipline PRD-148 applies to turns.
9. **The M-1 response belongs here.** When SUBAGENT-06 records that a declared
   budget was crossed, the pool applies the configured response: `drain` by
   default (already-incurred spend is not thrown away), `abort` when
   `subagents.on_budget_exceeded = "abort"`. A-8 reads A-6's crossing record and
   decides; A-6 neither calls a stop nor chooses a mode.
10. **Every transition is visible once.** Each request and terminal phase adds
    its fields to the events the pool already emits (`pool.py:1504-1543`) and to
    the worker transcript (`src/agenthicc/subagents/transcripts.py:63-107`), with
    A-9 free to rename the envelope as long as it carries `request_id`, `mode`,
    `source`, `reason`, `partial` and `undelivered_messages`.
11. **No stop, no change.** A pool nobody stops produces the same results,
    events, transcripts and texts as today, plus fields whose absence is
    representable.

## 5. Non-goals

The A-8 boundaries from the research report §5.3, restated as commitments.

- **The response to a crossed limit is ours; the limit is not.** Budget
  accounting, the `budget:` keys, the settings ceiling, the cost provenance and
  the budget report are SUBAGENT-06's. This PRD consumes one record from A-6 —
  "a limit was crossed, here is which one" — and decides what happens to the
  workers, under the locked boundary M-1 (§5.4): A-6 decides *when a budget is
  exceeded*; A-8 decides *what happens to the workers*. A-8 defines no meter, no
  limit key and no spend report, and A-6 defines no mode and calls no stop.
- **The shape of a completed result.** That is A-3's (SUBAGENT-03). This PRD
  adds two scalar fields to an existing result — `partial` and `stop_reason` —
  because a termination that cannot be represented cannot be preserved, and it
  states that A-3's structured envelope must carry them rather than re-derive
  them. It does not add a schema, a validator or a failure taxonomy.
- **The termination semantics of an in-flight turn outside a subagent pool.**
  PRD-148 owns unified interrupt and graceful cancellation, and this PRD extends
  that vocabulary rather than inventing a parallel one: `requested`,
  `cancelling`, `cancelled`, `source`, `reason`, `request_id` and
  `cleanup_error` are PRD-148's words (`prds/prd-148-unified-interrupt-and-graceful-cancellation.md:190-208`),
  reused at worker scope. When PRD-148 lands, the pool registers as a child
  cancellation scope and its existing `except asyncio.CancelledError` path
  (`pool.py:1363-1369`) becomes a call to goal 8's pool stop with
  `source="parent"`, so there is one mechanism with two scopes.
- **Durable pool identity, collection after a restart, and background pools.**
  SUBAGENT-07 (A-7). Today the only handle is `active_pool` (`pool.py:1099`); A-8
  defines what a stop means for a pool it can reach, and A-7 defines the reach.
- **The event and transcript schema.** SUBAGENT-09 (A-9) owns one schema for
  kernel events, transcripts and the TUI. This PRD states the fields a
  transition must expose and emits them on the existing surfaces; it does not
  define the envelope, redaction policy or retention.
- **Type records, permissions and policy.** SUBAGENT-01 and SUBAGENT-05.
  Stopping a worker can only narrow what it may do; no field here can grant a
  tool, a model or an approval bypass.
- **Automatic retry or automatic resumption of a cancelled worker.** PRD-148
  names this a non-goal for turns ("Automatically retrying or automatically
  resuming a user-cancelled turn"), and the same reasoning applies one scope
  down: a stop is a decision, and repeating work is a new explicit attempt.
- **Rollback of side effects.** A stopped worker may have written files. This
  PRD preserves what it reported and never promises to undo a mutation; that is
  why partial results are labelled untrusted and truncated rather than presented
  as a clean state.
- **Depth, fan-out and per-type model choice.** A-4 and A-2. A retry is one more
  attempt of one worker, not a new spawn policy.

## 6. Acceptance criteria

1. A stop request returns a record with a request id, mode, source reason and
   phase `requested`, and repeating it returns the same record rather than a
   second transition: `tests/unit/test_subagent_pool.py`.
2. An `abort` stop of a running worker ends with phase `cancelled`, the panel row
   status `stopped`, and the sibling workers untouched:
   `tests/integration/test_subagent_panel_agx08.py`.
3. **Partial-result preservation on abort:** a worker stopped after a completed
   provider turn returns that turn's text with `partial=True` and
   `stop_reason="user_requested"`, and the aggregate contains both the text and
   the marker: `tests/unit/test_subagent_pool.py`.
4. A `drain` stop never starts a pending worker, and the report names the workers
   that never ran: `tests/unit/test_subagent_pool.py`.
5. A stop while the worker waits for an approval leaves no answerable prompt for
   that worker and does not disturb another worker's approval:
   `tests/integration/test_subagent_panel_agx08.py`.
6. A steering message that the worker consumed is acknowledged, and one left
   unconsumed at termination is counted in `undelivered_messages`:
   `tests/unit/test_subagent_communication.py`.
7. `retry_worker` on a failed worker starts attempt 2 and leaves every succeeded
   worker's result and attempt number unchanged: `tests/unit/test_subagent_pool.py`.
8. `retry_worker` refuses a live worker and a succeeded one, and leaves the
   lifecycle phase unchanged: `tests/unit/test_subagent_pool.py`.
9. A pool-level stop applies one mode to every worker and reports one outcome per
   worker; a parent-turn cancellation reaches the same code path with
   `source="parent"`: `tests/unit/test_subagent_pool.py`.
10. A budget crossing record with `source="budget"` produces a `drain` by default
    and an `abort` when `subagents.on_budget_exceeded = "abort"`, and the two
    outcomes differ only in whether running workers finish:
    `tests/unit/test_subagent_pool.py`.
11. `dismiss_worker` accepts a stopped row as well as a failed one, and the panel
    action removes it: `tests/integration/test_subagent_panel_agx08.py`.
12. A pool with no stop request produces the same text, events and transcripts as
    the current implementation: `tests/unit/test_subagent_pool_runtime_coverage.py`.
13. A worker that fails after a productive provider turn returns that turn's text
    with `partial=True` and a `stop_reason` naming the failure, and the aggregate
    shows the text alongside the error: `tests/unit/test_subagent_pool.py`.

## 7. Rollout, migration and security

**Compatibility.** `stop_worker` remains as a thin alias for
`request_stop(mode="abort", source="user", reason="user_requested")`, so
`tui_session.py:2670` and the AGX-08 panel keep working and keep their `x`
binding. `dismiss_worker`, `send_subagent_message` and `SubagentPoolState` gain
behaviour, not required arguments; `SubagentResult` gains two defaulted fields
(`partial=False`, `stop_reason=""`) appended after `changed_paths`
(`pool.py:318-329`), so existing positional construction and `replace()` calls
(`:1336`) are unaffected. A pool with no stop request is byte-identical in
result text and event payloads.

**Defaults.** No setting is required to get today's behaviour: a pool that is
never stopped never creates a stop record. Two settings are introduced, both
with the conservative value:

| Setting | Default | Meaning |
|---|---|---|
| `subagents.on_budget_exceeded` | `"drain"` | response to an A-6 crossing: let running workers finish |
| `subagents.stop_grace_s` | `5.0` | how long an `abort` waits for cleanup before recording `cleanup_error` |

Both are A-8's own policy keys, not budget keys: neither names a limit or a
spend quantity.

**Migration.** None for users. For durable records: a pool result written before
this PRD has no `partial` or `stop_reason`, which reads as "not partial", the
same way absence is treated for every other optional field. `PRD-232`'s attempt
records do not exist yet, so attempt numbering starts at 1 for every historical
worker.

**Security.** Three properties, each testable.

- **A stop narrows.** Stopping and retrying cannot grant a tool, raise
  `max_turns`, change a model or alter the approval policy: a retried worker is
  built from the same immutable `SubagentExecutionPolicy` and the same
  `allowed_tools` as its first attempt, and SUBAGENT-05's rules apply unchanged.
- **Partial output is untrusted, and is labelled as such.** It is redacted by the
  pool's existing redactor before it reaches the panel or a transcript
  (`pool.py:1446-1454` is the redactor every row's activity passes through), it is truncated at
  a completed turn rather than at an arbitrary byte, and it reaches the parent as
  a *partial* result — a parent that treats half an answer as whole is a security
  problem masquerading as a quality problem.
- **A failed cleanup is reported, not hidden.** If a cancelled worker does not
  finish within `stop_grace_s`, the lifecycle records `cleanup_error`, the row
  becomes `failed` rather than `stopped`, and the pool reports it; fail-closed
  here means "do not claim a clean stop you did not achieve".

## 8. Verification

Current suite that must stay green:

```console
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/unit/test_subagent_communication.py -q
$ pytest tests/unit/test_subagent_resume.py -q
$ pytest tests/unit/test_subagent_tui.py -q
$ pytest tests/integration/test_subagent_panel_agx08.py -q
```

Measured on 2026-10-29 at the commit in §3: 58 passed (test_subagent_pool),
3 passed (test_subagent_pool_runtime_coverage), 9 passed
(test_subagent_communication), 16 passed (test_subagent_resume), 20 passed
(test_subagent_tui), 5 passed (integration test_subagent_panel_agx08).

New cases belong in `tests/unit/test_subagent_pool.py` (the stop request record,
idempotence, partial preservation, drain, retry attempts, the pool-level fan-out
and the budget-crossing response), in `tests/unit/test_subagent_communication.py`
(acknowledgement and the undelivered count), and in
`tests/integration/test_subagent_panel_agx08.py` (the panel status after an
abort, dismissal of a stopped row, and the approval interaction).

The two tests this PRD most needs are criteria 3 and 8. Criterion 3 fails today
for a structural reason — the `CancelledError` handler assigns `text=""` at
`pool.py:769` even though the accumulated text is in scope — and criterion 8 is
the boundary asserted as behaviour: a retry must not be a disguised second spawn
of the pool.

## 9. Related and superseded PRDs

- **PRD-148 — extends.** PRD-148 owns unified interrupt and graceful
  cancellation and proposes `InterruptRequest`/`InterruptState` with
  `request_id`, `source`, `reason`, `phase`, `cleanup_error`. This PRD reuses
  those names verbatim at worker scope and adds only `worker_id` and `mode`, so
  the series has one cancellation vocabulary and two scopes.
- **PRD-232 — extends.** PRD-232 proposes the durable team graph and states that
  "retry creates a new attempt record; completed tasks are not repeated". This
  PRD gives that sentence its worker-scoped mechanism and does not build a second
  coordination model: no task graph, no dependency scheduling, no durable
  messaging.
- **SUBAGENT-03 — narrows against.** A-3 owns the shape of a completed result.
  This PRD adds the two fields a termination needs and defers the structured
  envelope to A-3, which must carry them instead of re-deriving them.
- **SUBAGENT-06 — supplies the trigger, narrows against.** A-6 detects and
  reports a crossed budget; this PRD decides the response and is the only one of
  the two that defines a mode, a stop or a cancellation path. Both documents
  carry the same M-1 sentence so the split is visible from either side.
- **SUBAGENT-07 — narrows against.** A-7 owns durable pool identity, background
  execution and collection across turns and restarts. This PRD defines what a
  stop means for a pool it can reach; A-7 defines the reach.
- **SUBAGENT-09 — narrows against.** A-9 owns one event and transcript schema.
  This PRD names the fields a transition must expose and emits them on the
  existing surfaces; renaming the envelope is A-9's, dropping the fields is not.

## A. Competitor evidence

The strongest area of the series on mechanism, and the weakest on uniformity:
three harnesses have a working answer and none of them answers the same
question. Codex splits the verb, Claude Code keeps the name, opencode keeps the
session id, and DeepSeek names the drain. The rows below are the ones this PRD
actually leans on, with the depth label copied from the report's §2.1 table.

| Harness | Evidence | Depth | What it does about stopping, steering and retrying a worker | Take or reject |
|---|---|---|---|---|
| Codex CLI | `codex-rs/core/src/agent/control.rs:315` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | Six dedicated tools rather than one — `spawn_agent`, `send_message`, `followup_task`, `wait_agent`, `interrupt_agent`, `list_agents` — with `interrupt_agent` and `list_agents` also exposed on a control API, and identity as a validated `AgentPath` tree position so a path prefix selects agents | Take the split between *continue* (`send_message`), *queue more work* (`followup_task`), *wait* (`wait_agent`) and *stop* (`interrupt_agent`), and the refusal to let one tool carry four verbs (goals 1, 6, 7); reject the tree-path identity as A-4/A-7 scope, not this PRD's |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | Subagents are stopped and resumed by name; a `/tasks` list retains finished rows briefly (30 seconds); a foreground subagent cut off by a rate limit or server error returns its partial output with a note that it was cut off, and a background one is marked failed with the error *and its last output*, so partial work is not lost | Take partial-output preservation as a stated product promise rather than a side effect (goal 5) and the bounded row retention that AGX-08 already implements (`FINISHED_ROW_S`); reject resume-by-name until A-7 supplies durable identity |
| opencode | `packages/core/src/tool/plugin/subagent.ts:41-43` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | The `subagent` tool returns `{ sessionID, status: "completed" \| "running", output }`, and passing `sessionID` sends a follow-up to an already running *or finished* subagent, so steering and resumption are one mechanism | Take the explicit terminal-vs-running status in the return value as precedent for a phase field (goal 1); reject conflating steering with resumption, because a stopped worker in a blocking pool has nothing to resume into until A-7 exists |
| DeepSeek Harness | `.agents/notes/archived/architecture/2026-08-18-experimental-agent-teams-packages.md` @ `f3803c96974ad4a94195fb97e5c637e9c2a3afe3` | code + design notes | The Agent Teams note records that "the generic caller-reserved continuable child identity and selective direct-child drain remain in the stable Subagent service", with the experimental Team service consuming them in one permitted direction | Take *selective direct-child drain* as the name and the semantics of goal 4 — stop the subtree's waiting work, not the world — and *caller-reserved identity* as corroboration that the parent, not the child, names the thing it stops; reject the two-service split as architecture this PRD does not need |
