---
title: "SUBAGENT-11: Model-facing background spawn, collection and worker control"
status: Proposed
version: 0.2.0
date: 2026-11-03
scope: "the parent model's half of background orchestration and worker lifecycle: requesting a background pool, collecting it without blocking, receiving its completion without worker text changing role, answering a backgrounded worker's question, and stopping, retrying and listing workers through the same paths the panel uses — never a new cancellation mechanism, meter, event schema or process supervisor"
area: A-11
related_prds:
  - SUBAGENT-07
  - SUBAGENT-08
  - SUBAGENT-10
  - SUBAGENT-04
  - SUBAGENT-06
  - SUBAGENT-09
  - PRD-169
  - PRD-148
  - PRD-141
  - PRD-227
  - PRD-232
tags:
  - subagents
  - background
  - lifecycle
  - tool-surface
  - security
---

# SUBAGENT-11: Model-facing background spawn, collection and worker control

## Implementation status (2026-11-03)

**Wave 4 of SUBAGENT-10's plan delivers goals 2, 3, 4, 5 and 9** (branch
`subagent-wave-4-collection-and-notices`, pending merge):

- Goal 2: a background request is refused with `background_unavailable`, and
  the pool stopped, when there is no journal or no supervised task owner.
  Pinned by `tests/integration/test_subagent_tool_execution.py::test_backgrounding_without_a_supervisor_is_refused_and_stops_the_pool`.
- Goals 3–4: `collect_subagent_results(pool_id, wait=False, timeout_s=None)` —
  non-blocking snapshot, bounded wait (`subagents.collect_wait_max_s`),
  idempotent results, journal read after a restart, other conversations
  refused. `PoolContinuationRegistry.collect` (single-shot, blocking) is
  removed. Pinned by `tests/unit/test_subagent_resume.py::TestCollectSubagentResults`.
- Goal 5: `agenthicc.subagents.delivery.background_pool_notice` and
  `deliver_background_pool_notices` replace wave 2's framed delivery; no worker
  text enters the user role. Pinned by `TestBackgroundPoolNotice`,
  `TestBackgroundPoolDelivery` and the TUI delivery tests in
  `tests/unit/test_tui_session_coverage.py`.
- Goal 9: background sessions deliver notices at their turn boundary
  (`background/worker.py` `_deliver_background_pool_notices`), pinned by
  `tests/unit/test_background_worker_coverage.py::test_background_session_delivers_finished_pool_notices_like_the_tui`.

**Wave 5 delivers goals 1, 6, 7, 8 and 10** (branch
`subagent-wave-5-model-pool-control`, pending merge):

- Goal 1: `spawn_subagents(background=True)` waits only for the pool's
  journaled start, then returns `status: "running"` with worker rows and the
  do-not-poll instruction. The switch is `subagents.background_enabled`
  (a flat key, not `subagents.background.enabled`). Pinned by
  `tests/integration/test_subagent_tool_execution.py` (`test_a_model_requested_background_*`, `test_on_clarification_fail_*`).
- Goal 6: `subagents.on_clarification` (`queue` | `fail`). Under `queue` the
  parent gets one id-only notice per question
  (`deliver_background_question_notices`); under `fail` the broker refuses
  parent questions with `unanswerable_in_background`. Unset, it follows the
  session (wave 7): `queue` for an interactive session, `fail` otherwise,
  carried by `bind_background_task_owner(owner, interactive=...)`. Only
  `TUISession` binds an owner today, so the PRD-227 inbox route is not needed
  yet.
- Goal 7: `list_subagent_pools` (journal fold plus live handles, with
  `notice_pending`), `stop_subagents` (`source="parent_model"`, abort for one
  worker, drain for a pool) and `retry_subagent`, which relays
  `SubagentPool.retry_refusal`'s reason. The registry keeps every live pool and
  the 32 most recent finished ones, so retryability no longer depends on the
  panel. Pinned by `tests/unit/test_subagent_lifecycle.py` and
  `tests/unit/test_subagent_communication.py`.
- Goal 8: every handle records its owner lineage (`pool_id:task_id` per
  level); a nested worker's tools resolve only handles whose lineage contains
  its own, and never fall back to the journal.
- Goal 10: defaults keep foreground behaviour unchanged.

**Wave 7 closes the remaining criteria** (branch
`subagent-wave-7-control-remainder`, pending merge):

- Criterion 3: both paths' journal records, scroll kinds and aggregate text are
  pinned (`tests/unit/test_subagent_pool_runtime_coverage.py::test_a_backgrounded_pool_produces_the_same_aggregate_events_and_records`).
- Criterion 11: `SubagentSettings.on_clarification` is unset by default and
  `on_clarification_for(interactive=...)` resolves it (`tests/unit/test_config.py`,
  plus the non-interactive case in `tests/integration/test_subagent_tool_execution.py`).
- Criterion 13: a pool drain through `stop_subagents` uses `request_pool_stop`
  and reports `not_started` (`tests/unit/test_subagent_lifecycle.py`).
- Criterion 14: a retry is refused when the pool's budget is used up or when it
  would exceed `subagents.max_active_pools` — a retry reactivated its pool
  without the admission check before, a defect this wave fixes
  (`SubagentPool._admission_refusal`, `_exhausted_budget`).
- Criterion 17: a real nested worker retries its own pool and is refused for
  its sibling's and its parent's (`tests/integration/test_subagent_tool_execution.py::test_a_nested_worker_retries_its_own_pool_and_is_refused_elsewhere`).
- Criterion 18: headless workflow runs deliver notices at their turn boundary
  through `deliver_session_background_notices`, shared with background workers
  (`tests/unit/test_headless_coverage.py::test_a_headless_session_announces_a_pool_interrupted_by_a_crash`).
  Wave 4 had delivered only the background-worker half.
- Criterion 19 is pinned by
  `tests/integration/test_subagent_tool_execution.py::test_session_shutdown_cancels_a_backgrounded_pool`
  (placement differs from the named file).

## 1. Executive summary

SUBAGENT-07 and -08 shipped their *mechanisms* — a durable pool lifecycle,
restart recovery, exactly-once delivery, idempotent stop requests with modes,
partial-result salvage and per-worker retry — and wired every one of them to a
human: Ctrl+B backgrounds a pool, the panel's keys stop and retry a worker. The
parent model can do none of it. It cannot ask for a background pool, cannot read
a running pool without blocking, cannot collect a finished pool twice, cannot
see a question a backgrounded worker asks, and cannot stop or retry a worker it
spawned.

This PRD gives the parent model those five abilities by adding arguments and
three small tools over the existing code paths. It also fixes the channel a
background result arrives on: today worker-authored text is appended to the
parent's context as a **user** message. Here a finished background pool produces
a short, runtime-authored notice — pool id, counts, terminal reason, never
worker text — and the result itself reaches the parent only as the output of a
tool the parent calls. Untrusted text therefore stays in the tool role on every
path, and no synthetic tool exchange is injected into history.

## 2. Problem statement

Evaluated at `0a63bfba1dca68e61a06fe3b9acb4f536637396b` (`main`, the merge of
PR #6), clean tree. SUBAGENT-10 §3 R8–R14 records the same evidence in
remediation form; this section states it as product gaps.

1. **The model cannot request a background pool.** Backgrounding is triggered
   only by `SubagentPool.request_background()`
   (`src/agenthicc/subagents/pool.py:2028-2033`), reached from
   `BackgroundSubagentsCommand` — "Detach the active subagent pool from its
   foreground tool call (Ctrl+B)" (`src/agenthicc/tui/runtime/commands.py:51-52`).
   `spawn_subagents` has no `background` argument
   (`src/agenthicc/subagents/tool.py:220-245`). SUBAGENT-07 goal 1 is met for
   the user and missing for the model.
2. **Collection blocks and is single-shot.** `PoolContinuationRegistry.collect`
   awaits the pool task and pops the handle in `finally`
   (`src/agenthicc/subagents/communication.py:805-812`), and the tool takes only
   `pool_id` (`:987-1002`). A running pool cannot be read without waiting for
   it, and a second collect raises `unknown or expired subagent pool`
   (SUBAGENT-07 goals 3–4).
3. **Background completion changes the author of worker text.** The foreground
   path returns the aggregate as the `spawn_subagents` tool result; the
   background path appends the journaled aggregate to memory with
   `memory.add_user_once(message, …)`
   (`src/agenthicc/runners/tui_session.py:4344-4351`). The journaled text is
   unbounded (`src/agenthicc/memory/journal.py:1059`). Worker output is
   untrusted — workers read web pages, files and other tools' output — and the
   user role is the most trusted position in the parent's context.
4. **A backgrounded worker's question is invisible.** The only reader of
   `pending_questions` is the foreground spawn call (`tool.py:616`, `:676`),
   whose watcher is cancelled once the pool is backgrounded (`tool.py:719-726`).
   A worker that calls `ask_parent` then waits the full
   `DEFAULT_MESSAGE_TIMEOUT_S = 300.0` (`communication.py:56`) with no surface
   able to show it. No `on_clarification` policy exists (SUBAGENT-07 goal 6).
5. **Worker controls exist only for the human.** `request_stop`,
   `request_pool_stop` and `retry_worker` (`pool.py:2043`, `:2114`, `:2237`) are
   called from the panel (`tui_session.py:3011-3063`); the parent model's
   subagent tools are `answer_subagent`, `send_subagent_message` and
   `collect_subagent_results` (`communication.py:1004`). SUBAGENT-08 §3 already
   recorded "No model-facing tool stops a worker"; its goals did not require
   one, so this is new scope rather than a defect.
6. **Delivery is TUI-only.** *(Corrected in SUBAGENT-10 wave 4.)* Restart
   recovery runs in the shared session builder (`tui_session.py:1535`, inside
   `_build_session_context_impl`, which headless workflows and background
   workers also use), so it is not TUI-only. Delivery of finished background
   pools is: only `TUISession` called it (`tui_session.py:4311-4357`), and
   background workers run turns through `_run_agent_turn` directly
   (`background/worker.py:1097-1119`), so a background session resumed on a
   journal with a finished background pool never told the model.
7. **Background tasks are supervised only when an owner exists.**
   `spawn_subagents` uses `task_owner.start(...)` when
   `current_background_task_owner()` returns one and falls back to
   `asyncio.create_task` otherwise (`tool.py:603-609`), which is the
   garbage-collection and shutdown hazard SUBAGENT-07 goal 1 names
   (`src/agenthicc/supervised_tasks.py:1-8`).
8. **Retryability is a property of what the panel shows.** A finished pool is
   reachable for retry only through the process-global `_RETRYABLE_POOLS` map
   (`src/agenthicc/subagents/pool.py:1819`), and `retryable_pool` returns it only
   while it is the pool the conversation store currently displays
   (`:1831-1840`); starting any new pool clears every retryable pool of the same
   conversation (`:2408-2411`). Today, therefore, only the most recent pool of a
   TUI session is retryable, and only until the next spawn. A model-facing retry
   cannot be built on that rule.

## 3. Current-source investigation

```console
$ git rev-parse HEAD
0a63bfba1dca68e61a06fe3b9acb4f536637396b

$ grep -n "background" src/agenthicc/subagents/tool.py | grep -c "background: bool"
0
$ grep -n "def request_background" -A5 src/agenthicc/subagents/pool.py
2028:    def request_background(self) -> bool:
2029-        """Detach the active pool from its parent tool call (Ctrl+B)."""

$ sed -n '805,812p' src/agenthicc/subagents/communication.py      # blocking, single-shot collect
$ grep -n "async def collect_subagent_results" src/agenthicc/subagents/communication.py
987:    async def collect_subagent_results(pool_id: str) -> dict[str, object]:

$ grep -n "add_user_once" src/agenthicc/runners/tui_session.py
4348:            memory.add_user_once(message, message_id=message_id)

$ grep -rn "pending_questions\|wait_for_pending" src/agenthicc --include=*.py \
      | grep -v "subagents/communication.py"
src/agenthicc/subagents/tool.py:616:            pending_task = asyncio.create_task(communication.wait_for_pending())
src/agenthicc/subagents/tool.py:676:                questions = communication.pending_questions(recipient="main")
src/agenthicc/subagents/tool.py:689:                    "pending_questions": questions,
$ grep -rn "on_clarification" src/ --include=*.py
(no output)

$ grep -rn "retry_worker\|request_pool_stop\|request_stop(" src/agenthicc --include=*.py \
      | grep -v "subagents/pool.py"
src/agenthicc/runners/tui_session.py:3063:        result = await pool.retry_worker(worker_id)
$ grep -n "return \[answer_subagent" src/agenthicc/subagents/communication.py
1004:    return [answer_subagent, send_subagent_message, collect_subagent_results]

$ grep -c "recover_interrupted_subagent_pools\|completed_background_subagent_pools" \
      src/agenthicc/runners/headless.py
0
$ sed -n '603,609p' src/agenthicc/subagents/tool.py              # supervised only with an owner
```

**What already exists and is reused, not rebuilt.** The durable lifecycle
(`journal.py:1002-1111`, folded at `:1174-1213`) already writes the pool record
before the first worker (`pool.py:2392-2400`), records `backgrounded`,
`finished`, `interrupted` and `cancelled`, and dedupes delivery by
`subagent_pool_notification_delivered`. Stop, drain, abort, salvage and retry are
SUBAGENT-08's (`pool.py:292-316`, `:2043-2273`). Parent tools are
conversation-scoped through `_handle` (`communication.py:944-948`). Every
requirement below is a new *entry point* to one of these, not a parallel
implementation.

**Diff against the research report.** The report (§1.4, §1.7) predates
SUBAGENT-07/-08's implementation; it describes a blocking pool with unconnected
controls. The source has moved past it, and this PRD follows the source.

**Surprise.** SUBAGENT-07's goal 5 named the restart state `orphaned`; the
implementation chose `interrupted` (`journal.py:1240`). This PRD keeps the
shipped name and treats `orphaned` as superseded wording, because renaming a
durable record kind would need a migration and buys nothing.

## 4. Goals

1. **A model-requested background pool.** `spawn_subagents` gains
   `background: bool = False`. With `True`, and only when a durable conversation
   journal exists and `subagents.background.enabled` is true, the call registers
   the pool, writes `subagent_pool_started` and `subagent_pool_backgrounded`, and
   returns `{status: "running", pool_id, total, workers}` without waiting for any
   worker. The receipt tells the model it will be notified and must not poll,
   sleep or duplicate the work. Without a journal it returns today's
   `background_unavailable` refusal before any worker starts. The Ctrl+B path is
   unchanged and produces the same records.
2. **Every background pool is supervised.** A background pool — model- or
   user-requested — is started through a `SupervisedTasks` owner. When no owner
   is available the background request is refused (`background_unavailable`),
   never run on a bare `asyncio.create_task`; a foreground pool keeps today's
   behaviour.
3. **Collection is a read; waiting is explicit and bounded.**
   `collect_subagent_results(pool_id, wait=False, timeout_s=None)`:
   - `wait=False` on a running pool returns `status: "running"`, per-worker rows
     (id, label, type, status, attempt, duration) and pending question ids, and
     never awaits the pool;
   - on a terminal pool it returns the terminal aggregate with
     `status` ∈ {`completed`, `stopped`, `interrupted`, `cancelled`}, its counts,
     its reason and its text;
   - `wait=True` waits up to `timeout_s` (default and maximum
     `subagents.collect_wait_max_s`) and then returns the running snapshot with
     `timed_out: true` rather than raising.
4. **Collection is idempotent and survives a restart.** Collecting a terminal
   pool any number of times returns the same aggregate. When no live handle
   exists — the pool finished in an earlier turn or the process restarted — the
   result is read from the journal's terminal record for the same conversation.
   A pool id from another conversation is refused exactly as today.
5. **Completion is a notice; the result is pulled.** At the next logical-turn
   boundary after a background pool becomes terminal, the parent receives one
   bounded notice composed entirely by the runtime: pool id, terminal status,
   counts, the stop reason when there is one (SUBAGENT-10 WP-5), and the
   instruction to call `collect_subagent_results(pool_id)`. The notice contains
   **no worker-authored text**. It is delivered once (the existing
   `subagent_pool_notification_delivered` dedupe), and the worker text reaches
   the parent only as the output of the collect call. This replaces the
   user-role injection at `tui_session.py:4344-4351` and supersedes SUBAGENT-10
   WP-6's interim framing.
6. **A backgrounded worker's question is routed by policy.**
   `subagents.on_clarification` ∈ {`queue`, `fail`}, default `queue` for an
   interactive session and `fail` for a headless, background (PRD-141) or
   detached (PRD-204) session.
   - `queue`: the question stays open until answered or timed out; its id
     appears in the turn-boundary notice and in the collect snapshot, and its
     text appears only in the collect snapshot (tool role). `answer_subagent`
     answers it as today. When the parent session is itself a background
     session, the question is additionally routed to the PRD-227 inbox.
   - `fail`: the worker's `ask_parent` returns immediately with
     `error="unanswerable_in_background"`, so the worker proceeds or fails on its
     own terms instead of waiting 300 seconds.
   A foreground pool keeps today's `awaiting_clarification` behaviour.
7. **The parent can list, stop and retry what it spawned.** Three
   `@tool_control` tools, conversation-scoped through the same `_handle` check:
   - `list_subagent_pools()` returns this conversation's pools from the journal
     fold plus live state: id, status, background flag, counts, depth, and
     whether a notice is pending;
   - `stop_subagents(pool_id, worker_id=None, mode=None, reason)` calls
     `request_stop` for one worker or `request_pool_stop` for all, with
     `source="parent_model"`, and returns the per-worker lifecycle outcome.
     An omitted `mode` means `abort` for one worker — a drain of a single
     running worker would only let it finish — and `drain` for a whole pool,
     matching SUBAGENT-08's default for budget responses;
   - `retry_subagent(pool_id, worker_id)` calls `retry_worker` and is refused,
     with the reason, for a live or succeeded worker, while the pool is still
     running, after the pool's wall-clock ceiling, or when SUBAGENT-04's
     admission limits or SUBAGENT-06's ceilings would be exceeded by the new
     attempt.
   No new stop, retry or salvage logic exists: the panel and the model reach the
   same functions, and the lifecycle records `source` to tell them apart.
   One change to `retry_worker` is required: it returns `None` for every refusal
   (`src/agenthicc/subagents/pool.py:2237-2262`), so it must return a refusal
   reason the tool can relay. Its existing rules stay, and two of them are
   stated to the model because they are not obvious: a worker cannot be retried
   while its pool is still active, and a pool whose `max_total_time_s` has
   elapsed since *pool start* refuses every retry — so a background pool's
   failed worker is retryable only within the pool's wall-clock ceiling.
   Retryability is also decoupled from the panel (problem 8): a finished pool
   stays retryable through a **conversation-scoped** registry until it is
   reaped under SUBAGENT-04's pool bounds or its wall-clock ceiling passes,
   independently of which pool the panel shows and of later spawns. The panel
   keeps its present behaviour by reading the same registry.
8. **A child controls only its own descendants.** Inside a nested pool
   (SUBAGENT-04), these tools resolve only pools whose ancestry includes the
   calling worker's pool; a worker cannot list, stop or retry a sibling's or its
   parent's workers.
9. **Delivery parity.** Every runner that resumes a durable session already
   recovers interrupted pools through the shared session builder; every runner
   that runs agent turns on one also delivers background notices at its turn
   boundary through the same function `TUISession` uses.
10. **Defaults change nothing a user does today.** `background=False`, Ctrl+B,
    foreground clarification and the panel's keys behave as now. The one
    deliberate behaviour change is goal 5: a background completion no longer
    places worker text in the user role.

## 5. Non-goals

- **A second cancellation, retry or salvage mechanism.** SUBAGENT-08 owns their
  semantics; goal 7 adds entry points only. The M-1 boundary stands: SUBAGENT-06
  decides *when a budget is exceeded*; SUBAGENT-08 decides *what happens to the
  workers*.
- **Budget accounting, limit keys or the budget report.** SUBAGENT-06 and
  SUBAGENT-10 WP-7. Retry admission *reads* the ceilings; it defines none.
- **Re-executing a worker whose process died.** A pool runs in the process that
  spawned it. `interrupted` is reported and preserved; durable re-execution is
  PRD-204's detached-run model, not a second supervisor here.
- **The event and transcript envelope.** SUBAGENT-09. New tool results and
  notices carry pool and worker ids so SUBAGENT-09 can absorb them; their
  serialisation is not defined here.
- **Cross-session coordination or a task graph.** PRD-232. Every tool here is
  scoped to one conversation.
- **Steering semantics.** `send_subagent_message` and acknowledgement are
  SUBAGENT-08 goal 6, delivered; this PRD does not change them.
- **External notification delivery.** PRD-141 and the notification outbox own
  phone and webhook delivery; goal 6 only routes a question into the PRD-227
  inbox when the parent is itself a background session.
- **Polling support.** The receipt and the notice tell the model not to poll,
  and `wait=True` is bounded; no progress-polling loop is added.
- **Per-definition background mode.** Background is a per-call choice. The type
  record (SUBAGENT-01) gains no `background:` key.

## 6. Acceptance criteria

1. `spawn_subagents(..., background=True)` returns within one second with
   `status="running"` and a `pool_id` while its workers are still running, and
   the journal holds `subagent_pool_started` and `subagent_pool_backgrounded`
   for it before the first worker's first provider call:
   `tests/integration/test_subagent_tool_execution.py`.
2. `background=True` without a durable journal, or without a supervised task
   owner, returns `status="background_unavailable"` and starts no worker:
   `tests/integration/test_subagent_tool_execution.py`.
3. `background=False` and the Ctrl+B path produce the same aggregate text, event
   kinds and journal records as at `0a63bfb`:
   `tests/unit/test_subagent_pool_runtime_coverage.py`.
4. `collect_subagent_results(pool_id)` on a running pool returns
   `status="running"` with one row per worker and does not await the pool task:
   `tests/unit/test_subagent_communication.py`.
5. `collect_subagent_results(pool_id, wait=True, timeout_s=0.1)` on a pool that
   outlives the timeout returns the running snapshot with `timed_out=true` and
   does not raise; on a pool that finishes inside the timeout it returns the
   aggregate: `tests/unit/test_subagent_communication.py`.
6. Collecting a terminal pool three times returns an identical aggregate each
   time and never raises `unknown or expired subagent pool`:
   `tests/unit/test_subagent_communication.py`.
7. After a simulated restart — fresh registry, no live pool — collecting an
   `interrupted` pool from the same conversation returns its preserved worker
   records and counts, and the same pool id from another conversation is
   refused: `tests/unit/test_subagent_resume.py`.
8. A background pool that finishes while no turn is running produces exactly
   one notice at the next turn boundary; the notice contains the pool id,
   status, counts and the collect instruction, and contains no substring of any
   worker's output: `tests/unit/test_subagent_resume.py`.
9. No code path appends worker-authored text to the parent's memory as a user
   message: after a background completion the parent memory's user-role entries
   contain only the runtime notice, and the worker text appears only in the
   collect tool result: `tests/unit/test_subagent_resume.py`.
10. With `on_clarification="fail"`, a backgrounded worker's `ask_parent` returns
    `unanswerable_in_background` in under one second; with `"queue"`, the
    question id appears in the next notice and its text in the collect snapshot,
    and `answer_subagent` resumes the worker:
    `tests/unit/test_subagent_communication.py`.
11. A headless session defaults `on_clarification` to `fail` and an interactive
    TUI session to `queue`: `tests/unit/test_config.py`.
12. `stop_subagents(pool_id, worker_id)` — mode omitted, so `abort` — ends that
    worker with
    phase `cancelled`, `partial=true` when it had completed a provider turn,
    `source="parent_model"`, and leaves siblings running; a repeated call returns
    the same `request_id`: `tests/unit/test_subagent_lifecycle.py`.
13. `stop_subagents(pool_id, mode="drain")` starts no pending worker and reports
    the never-started workers, through the same `request_pool_stop` the budget
    path uses: `tests/unit/test_subagent_lifecycle.py`.
14. `retry_subagent` on a failed worker of a finished pool starts attempt 2 and
    leaves succeeded workers untouched; on a live worker, a succeeded worker, a
    still-running pool, a pool past its `max_total_time_s`, or when an admission
    or budget ceiling would be exceeded it is refused with a reason naming which
    rule applied, and changes no lifecycle record:
    `tests/unit/test_subagent_lifecycle.py`.
15. A failed worker of pool A remains retryable through `retry_subagent` after
    pool B has been spawned and finished in the same conversation, and pool A
    is not retryable from another conversation:
    `tests/unit/test_subagent_lifecycle.py`.
16. `list_subagent_pools()` returns live and journaled pools of this
    conversation, including `interrupted` ones, and none from another
    conversation: `tests/unit/test_subagent_resume.py`.
17. A worker inside a nested pool can stop or retry workers of a pool it spawned
    and is refused for its sibling's or parent's pool:
    `tests/integration/test_subagent_tool_execution.py`.
18. A headless session started on a journal with a `running` pool marks it
    `interrupted` before its first turn and delivers its notice at that turn's
    boundary: `tests/unit/test_headless_coverage.py`.
19. Session shutdown with a background pool running leaves its journal record
    terminal (`cancelled`) and its supervised task awaited:
    `tests/unit/test_subagent_pool.py`.

## 7. Rollout, migration and security

**Prerequisites.** SUBAGENT-10 WP-1 (redaction before every sink), WP-5 (the
`stopped` terminal record with counts, text and reason) and WP-6 (the
delivery-boundary formatter in `agenthicc.subagents.delivery`), all delivered in
SUBAGENT-10 waves 1–2. Goal 5's notice depends on WP-5's reason. WP-6 keeps the
journal's aggregate complete, so goal 4's journal read returns the full text in
the collect tool result — the tool role, as the foreground path does — and only
the turn-boundary notice is runtime-authored.

**Defaults.**

| Setting | Default | Meaning |
|---|---|---|
| `subagents.background.enabled` | `true` | Allows `background=True`; `false` refuses it with `background_unavailable` |
| `subagents.on_clarification` | `queue` interactive, `fail` otherwise | Goal 6 |
| `subagents.collect_wait_max_s` | `60.0` | Upper bound for `wait=True` |

A malformed value fails configuration load naming the key, as other
`[subagents]` keys do (`src/agenthicc/config.py:1236-1364`).

**Ordering.** Each step ships alone:

1. Goals 2 and 9 (supervision, headless parity) — no tool-surface change.
2. Goals 3 and 4 (non-blocking, idempotent collection) — additive arguments.
3. Goal 5 (notice-then-pull), replacing the user-role delivery in one commit with
   criterion 9.
4. Goal 1 (model-requested background), which is safe only after step 3.
5. Goals 6, 7 and 8 (clarification policy and controls).

**Migration.** A journal written before step 3 may hold a completed,
undelivered background pool. On first read after the upgrade it is delivered as
a notice like any other; its text is collectable through goal 4's journal read.
No record kind is renamed.

**Security.**

- *Worker text never changes role.* After step 3 the only parent-context entry a
  background pool produces outside a tool result is runtime-authored. This is
  the substance of the PRD and criterion 9 pins it.
- *Controls only narrow.* `stop_subagents` can only end work. `retry_subagent`
  repeats an attempt under the same type, policy and permissions (SUBAGENT-05),
  subject to the same admission and budget ceilings, so it cannot obtain a tool,
  model or approval the first attempt lacked.
- *Scope is conversation and ancestry.* Every tool resolves pools through the
  conversation-scoped `_handle` check and, inside a nested pool, through
  ancestry (goal 8). A pool id is not a capability.
- *No new prompt-injection route through notices.* The notice is composed from
  integers, enum values and ids generated by the runtime; the stop reason is one
  of the governor's or a lifecycle's fixed strings, or a model-supplied reason
  truncated and marked as such.
- *Bounded waiting.* `wait=True` is capped, so a model cannot hold its turn open
  indefinitely on a background pool.
- *Fail-closed background.* Missing journal or missing supervisor refuses the
  background request rather than running an unsupervised, unrecoverable pool.

The evidence for model-facing control rests on two open-source harnesses with
code-depth evidence (Codex, opencode, Appendix A); the role-of-delivery
requirement rests on agenthicc's own foreground/background asymmetry, not on a
competitor.

## 8. Verification

```console
$ uv run pytest tests/integration/test_subagent_tool_execution.py -q
$ uv run pytest tests/unit/test_subagent_communication.py -q
$ uv run pytest tests/unit/test_subagent_resume.py -q
$ uv run pytest tests/unit/test_subagent_lifecycle.py -q
$ uv run pytest tests/unit/test_subagent_pool.py -q
$ uv run pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ uv run pytest tests/unit/test_headless_coverage.py -q
$ uv run pytest tests/unit/test_config.py -q
$ uv run pytest tests/unit/test_conversation_journal.py -q
$ uv run pytest tests/integration/test_subagent_panel_agx08.py -q
```

Existing tests to extend rather than duplicate:
`tests/unit/test_subagent_resume.py` (journal-backed delivery and restart),
`tests/unit/test_subagent_lifecycle.py` (stop and retry semantics, now reached
through the new tools), and `tests/integration/test_subagent_panel_agx08.py`
(which must keep passing to show the panel path is unchanged). Documentation:
`docs/guides/subagents.md` "Background pools" and "Resume and cache behaviour",
and `llms-full.txt` for the new tool names and settings.

## 9. Related and superseded PRDs

- **SUBAGENT-07 — extends.** Delivers its goals 1, 3, 4 and 6 for the model and
  supersedes its goal-5 state name `orphaned` with the shipped `interrupted`.
- **SUBAGENT-08 — extends.** Adds model-facing entry points to its stop and retry
  mechanisms; changes none of their semantics.
- **SUBAGENT-10 — inherits.** Depends on WP-1, WP-5 and WP-6, and supersedes
  WP-6's interim framing with notice-then-pull.
- **SUBAGENT-04 — narrows against.** Goal 8 applies its ancestry to control;
  retry admission reads its limits.
- **SUBAGENT-06 — narrows against.** Retry admission reads its ceilings; no meter
  or limit key is added.
- **SUBAGENT-09 — unrelated** in mechanism; its envelope will absorb the new
  notice and tool results.
- **PRD-169 — inherits.** Notice-then-pull exists so that no synthetic tool call
  or tool result is inserted into history; every tool exchange remains one the
  model actually made.
- **PRD-148 — inherits.** `source="parent_model"` joins its stop vocabulary at
  worker scope, as SUBAGENT-08 already does.
- **PRD-141 — extends.** A background session's subagent questions default to
  `fail` unless routed to its inbox.
- **PRD-227 — extends.** Goal 6 routes a queued question into its inbox when the
  parent is itself a background session.
- **PRD-232 — unrelated.** No cross-session coordination is added.

## A. Competitor evidence

Rows and depth labels copied from SUBAGENT-07 and SUBAGENT-08 appendix A, which
copied them from the research report §2.1.

| Harness | Evidence | Depth | What it does | Take or reject |
|---|---|---|---|---|
| opencode | `packages/core/src/tool/plugin/subagent.ts:19-28`, `:41-43` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | `background: true` returns immediately with `status: "running"`, tells the model it "will be notified automatically when it finishes", and adds "DO NOT sleep, poll for progress, ask the subagent for status, or duplicate this subagent's work"; lifetime is tested against eviction | Take the immediate `running` receipt and the anti-polling text (goal 1) and the tested-lifetime discipline (criteria 7, 18); reject `sessionID` continuation, because our continuation target is a pool |
| Codex CLI | `codex-rs/core/src/config/mod.rs:1404`, `:1428`; `codex-rs/core/src/agent/control.rs:315` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | Blocking is an explicit, disableable tool (`wait_agent`, `multi_agent_v2.wait_agent_enabled`); `interrupt_agent` and `list_agents` are first-class operations on a control API | Take "waiting is an explicit act by the caller" (goal 3's `wait=True`) and a model-facing list and interrupt (goal 7); reject the six-tool split in full, because send and ask already exist and three tools cover the remainder |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | Background subagents deliver results as a completion notification in a later turn; subagents are stopped and resumed by name | Take later-turn notification as the product promise behind goal 5; docs-only, so the role-of-delivery requirement does not rest on it |
| Grok CLI | `src/grok/tools.ts:593-595` @ `fb97af83f06dca873281d60168430f06c8de6324` | code | A separate `delegate` tool runs a read-only background agent and tells the model it will be notified when it completes | Take the "you will be notified" promise in the receipt; reject a second spawn tool, since one boolean on `spawn_subagents` suffices |
