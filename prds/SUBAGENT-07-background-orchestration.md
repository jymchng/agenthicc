---
title: "SUBAGENT-07: Background and non-blocking orchestration"
status: Proposed
version: 0.1.0
date: 2026-10-29
scope: "a fire-and-return spawn mode for a subagent pool, a durable pool identity and ledger, and collection and resumption of a pool's result across turns and process restarts — never worker steering or cancellation, session lifecycle, or detached process supervision"
area: A-7
related_prds:
  - PRD-124
  - PRD-141
  - PRD-204
  - AGX-08
  - SUBAGENT-06
  - SUBAGENT-08
  - SUBAGENT-09
tags:
  - subagents
  - orchestration
  - background
  - durability
  - sessions
---

# SUBAGENT-07: Background and non-blocking orchestration

## 1. Executive summary

A subagent pool is currently a *stack frame*. `spawn_subagents` starts the pool
and then awaits it (`src/agenthicc/subagents/tool.py:333`), so the parent turn
cannot do anything else until every worker finishes; the one exception is not a
feature but a hostage situation — the pool returns early only when a worker asks
the parent a question (`:305-330`). `PoolContinuationRegistry`
(`src/agenthicc/subagents/communication.py:727-752`) exists solely to hold that
one hostage case, and its handle wraps a live `asyncio.Task` (`:144`), so the
whole mechanism evaporates when the process does.

**This PRD makes a pool an object a session can own, name, come back to, and be
told about.** Spawning gains a fire-and-return mode. A pool records its identity
durably before its first worker starts, so a pool that outlives a turn — or a
process — is still identifiable. Collection becomes a snapshot read rather than a
join, and a pool that finishes while no turn was watching is delivered to the
next turn instead of being lost. A pool whose process died is reported
`orphaned` with whatever its durable worker records hold, because "your pool is
gone" is a fact and "unknown or expired subagent pool" is a bug report.

It rides on the session lifecycle instead of rebuilding it: PRD-141's background
sessions and PRD-204's detached runs stay the owners of *processes*. A-7 owns the
question "may a turn end while my pool is still running, and how do I get the
answer".

## 2. Problem statement

All claims verified at commit `cc7b1b842e60c6853337146b4a0c4ffe4b1befc0`
(2026-10-29); `src/` and `tests/` have not moved since the report's snapshot
`f843040` (§3 re-checks it).

**The parent turn is the pool's lifetime.** `spawn_subagents` validates its
arguments, builds a `SubagentPool`, starts it with `asyncio.create_task`
(`src/agenthicc/subagents/tool.py:304`) and then, unless a question intervenes,
executes `result = await pool_task` (`:333`). `SubagentPool.run()` in turn awaits
the whole worker fan-out (`src/agenthicc/subagents/pool.py:1360`) and returns the
aggregate. There is no parameter, no setting and no code path that returns the
pool to the model while it runs: the tool call is the pool's lifetime.

**The single non-blocking exit is a question, not a mode.** When a worker calls
`ask_parent`, the tool races the pool against `communication.wait_for_pending()`
(`tool.py:305-330`). If the question wins, the pool handle is registered
(`:313`) and the tool returns `status="awaiting_clarification"` with the pending
questions (`:315-325`). So the only way a parent turn can currently survive a
running pool is if a worker is *blocked waiting for that same parent*. This is
why `PoolContinuationRegistry` exists — its docstring says so: "Session-keyed
registry for pools that temporarily await answers"
(`src/agenthicc/subagents/communication.py:728`).

**Identity is a random string with no record, and the handle is a task.** A
pool's id is `uuid.uuid4().hex`, assigned in `__init__`
(`src/agenthicc/subagents/pool.py:1140`). Nothing writes it down before the pool
finishes: the first durable mention of a `pool_id` is a *worker* record
(`src/agenthicc/memory/journal.py:857`), and the pool-level record is written
only on complete success (`tool.py:338`, `src/agenthicc/memory/journal.py:897`). `PoolHandle` holds
`pool_id`, `conversation_id`, `broker` and `task: asyncio.Task[object]`
(`communication.py:138-144`) — an in-process object graph, not a record. The
registry is a plain dict (`:731`) inside a process-global map
(`_SESSION_REGISTRIES`, `:755`), with a weakref-keyed variant for conversation
stores (`:757-759`). A process restart does not lose the pool's *history*; it
loses the pool.

**`get` cannot expire, and `collect` always waits.** `PoolContinuationRegistry.get`
tests `handle.task.done()` and then returns the handle either way
(`communication.py:736-740`), so the "or expired" half of the error message at
`:748` describes a check that does not exist. `collect` awaits `handle.task`
unconditionally (`:745-750`) and pops the handle in a `finally` (`:751-752`).
Three consequences, all of them defects for a background pool: collecting a
*running* pool blocks the parent turn, exactly as spawning does; collecting a
*finished* pool works once and then raises `CommunicationError("unknown or
expired subagent pool")` on the second call; and the model-facing tool
(`:927-944`) inherits both.

**The durable records are a cache, not an inbox.** `_find_cached_result`
(`tool.py:443-495`) searches the journal fold and the reactive store by task
fingerprint (`:425`) and returns the cached `text` string. It is consulted
*before* spawning (`:255-275`) so an identical task set is not paid for twice.
Nothing in this path answers "my pool finished while I was doing something else";
there is no `uncollected` state, no delivery, and no reason the model would ever
look. The resume cache is the right primitive applied to the wrong question.

**A durable pool index already exists and the model cannot see it.** `PoolRecord`
(`src/agenthicc/subagents/transcripts.py:127-137`) carries `pool_id`, timestamps,
worker records and the transcript directory, and `list_pools(root, limit)`
(`:408`) reads them newest-first. The TUI already consumes it: the subagents
overlay renders "EarlierPool" rows from it
(`src/agenthicc/tui/workspace/overlays/subagents.py:80`). So an operator can see
a past pool that the model has no tool to ask about. The reachability asymmetry
is the gap, not the storage.

**Nothing owns the task.** The pool task is created with
`asyncio.create_task` (`tool.py:304`) and held only by a local variable, or — on
the question path — by the registry entry (`communication.py:734`), which is
popped when it is collected (`:752`). This is the exact hazard the project
already documents for itself: "``asyncio`` keeps only weak references to tasks. A
task that nobody holds can be garbage-collected before it finishes"
(`src/agenthicc/supervised_tasks.py:1-8`). The TUI session owns a
`SupervisedTasks` manager (`src/agenthicc/runners/tui_session.py:1517`) with
`start`, `wait` and `aclose` (`src/agenthicc/supervised_tasks.py:49`, `:81`, `:96`), and the
pool bypasses it. Today that is invisible because the parent turn's own frame is
a strong reference for exactly as long as the pool runs. Make spawning
non-blocking and the reference disappears with the frame.

**A non-interactive parent cannot answer, and waits three hundred seconds
anyway.** `ask_parent` defaults to `DEFAULT_MESSAGE_TIMEOUT_S = 300.0`
(`communication.py:56`) and the pool tool waits for a question
(`tool.py:305-330`). In a background session (PRD-141, implemented —
`src/agenthicc/background/supervisor.py:247`,
`src/agenthicc/background/model.py:356`) or a detached run (PRD-204, implemented)
the "main agent" side of that exchange is an agent loop with no human attached.
The question race is therefore not a general-purpose background mode; it is a
human-in-the-loop affordance that happens to be the only way out.

**There is no word for any of this in the package.**

```
$ grep -rniE "background|fire_and|detach" src/agenthicc/subagents/*.py
src/agenthicc/subagents/tool.py:205:  "context (str, optional): Additional background context."
src/agenthicc/subagents/transcripts.py:7:  "on one background thread in order, and a worker's file is kept under"
src/agenthicc/subagents/transcripts.py:150: "Calls return at once; a single background thread performs the writes in"
src/agenthicc/subagents/transcripts.py:193: "# ── the background thread ──"
```

Four hits, all prose about a writer thread and a task argument. No orchestration
mode, no detached pool and no asynchronous spawn exists in the package.

## 3. Current-source investigation

Run on 2026-10-29 at the commit in §2.

```
$ git rev-parse HEAD
cc7b1b842e60c6853337146b4a0c4ffe4b1befc0
$ git diff --stat f843040..HEAD -- src/ tests/
(no output)
```

Nothing in the implementation has moved since the report's snapshot, so §1 of the
report still describes the code and every citation below was read, not inherited.

**The blocking spine, read in order.**

```
$ grep -n "create_task" src/agenthicc/subagents/*.py
src/agenthicc/subagents/pool.py:1326:                    running = asyncio.create_task(worker.run(), name=f"subagent-{worker_id}")
src/agenthicc/subagents/tool.py:304:        pool_task = asyncio.create_task(pool.run())
src/agenthicc/subagents/tool.py:306:            pending_task = asyncio.create_task(pool.communication.wait_for_pending())
```

Three task creations in the package: a worker, the pool, and the question wait.
Only the second has no owner. The pool task is awaited two ways — by the race at
`tool.py:307-330` and by the join at `:333` — and both are inside the tool
callable, so both end when the call returns.

**The handle, the registry and the expiry that is not there.**

```
$ grep -n "class PoolHandle" -A7 src/agenthicc/subagents/communication.py
138:class PoolHandle:
139:    """A resumable pool owned by one session."""
141:    pool_id: str
142:    conversation_id: str
143:    broker: "AgentMessageBroker"
144:    task: asyncio.Task[object]
```

Reading `get` (`:736-740`) is what makes §2's expiry claim an observation: the
`.done()` branch and the fall-through return the same value, so a finished pool
and a vanished one are indistinguishable to every caller. `remove` (`:742-743`)
and `collect` (`:745-752`) are the only mutators of `_handles` (`:731`).

**The durable record writes, and their conditions.**

```
$ grep -n "def subagent_worker_result\|def subagent_pool_result" -A2 src/agenthicc/memory/journal.py
857:    def subagent_worker_result(
897:    def subagent_pool_result(
$ awk 'NR>=338 && NR<=338' src/agenthicc/subagents/tool.py
        if conversation_journal is not None and result.failed == 0:
```

Worker records are unconditional on the success path
(`pool.py:1460-1476`); the pool record requires a fully successful pool
(`tool.py:338`). So a background pool that fails one worker of four leaves four
worker records and no pool record — durable partial truth with no aggregate, and
no record at all for a pool killed mid-flight.

**The durable read paths, and who uses them.**

```
$ grep -rn "list_pools\|active_pool(" src/agenthicc/ --include=*.py | grep -v "def "
src/agenthicc/tui/workspace/overlays/subagents.py:80:        EarlierPool(record, _record_workers(record)) for record in list_pools(root, limit=limit)
src/agenthicc/runners/tui_session.py:2669:        pool = active_pool(pool_id)
src/agenthicc/runners/tui_session.py:2683:        pool = active_pool(pool_id)
```

`list_pools` has exactly one consumer and it is the TUI overlay; `active_pool`
(`pool.py:1102-1104`, populated at `:1282`, popped at `:1286`) has exactly two,
both the panel's stop/dismiss actions. No model-facing tool reads either. The
journalled folds (`src/agenthicc/memory/journal.py:962`, `:971`) are read by `_find_cached_result`
(`tool.py:443`) only.

**The absence proof for this PRD's own vocabulary.**

```
$ grep -rniE "background|fire_and|detach" src/agenthicc/subagents/*.py
(four prose hits, quoted in §2)
$ grep -rn "PoolHandle" src/agenthicc/ --include=*.py
src/agenthicc/subagents/__init__.py:27:    PoolHandle,
src/agenthicc/subagents/tool.py:34:    PoolHandle,
src/agenthicc/subagents/communication.py:138:class PoolHandle:
src/agenthicc/subagents/communication.py:731:        self._handles: dict[str, PoolHandle] = {}
src/agenthicc/subagents/communication.py:733:    def register(self, handle: PoolHandle) -> None:
src/agenthicc/subagents/communication.py:736:    def get(self, pool_id: str) -> PoolHandle | None:
src/agenthicc/subagents/communication.py:885:    def _handle(pool_id: str) -> PoolHandle:
```

Two exports, one definition, one dict, one accessor — and no serializer, no
`to_dict`, no journal method that takes a `PoolHandle`. The types are real and
the persistence is absent, which is a bounded claim about this commit because the
pattern covers the whole package plus every `PoolHandle` reference in `src/`.

**Existing tests, measured on 2026-10-29.**

```
$ pytest tests/unit/test_subagent_communication.py tests/unit/test_subagent_pool.py \
         tests/unit/test_subagent_resume.py tests/integration/test_subagent_tool_execution.py \
         tests/unit/test_subagent_panel_agx08.py -q
116 passed
```

Per file: 9, 58, 16, 11 and 22 passed. `tests/integration/test_subagent_tool_execution.py`
contains the project's only end-to-end exercise of the continuation path — it
spawns, reads `status == "awaiting_clarification"`, answers, and collects
(`:384-398`) — which is the behaviour goal 4 must generalise rather than break.
`tests/unit/test_subagent_resume.py` (16 passed) covers the fingerprint cache
that goal 4's delivery record must not be confused with.

**Surprises, recorded rather than smoothed over.** Three changed this PRD.

1. `list_pools` and `PoolRecord` already give a durable, directory-backed pool
   index (`transcripts.py:127`, `:408`) and the TUI already renders it
   (`src/agenthicc/tui/workspace/overlays/subagents.py:80`). Goal 2 therefore *reuses* that shape for the
   ledger instead of inventing a second pool index, and goal 7 becomes
   "give the model the query the panel already has" rather than "build one".
2. `SupervisedTasks` exists, is used by the TUI session
   (`tui_session.py:1517`), and its own module docstring states the precise
   hazard the pool currently courts (`src/agenthicc/supervised_tasks.py:1-8`). Goal 1 is
   therefore "route an existing task through an existing owner", not "add task
   supervision".
3. The registry's expiry test is dead code (`communication.py:736-740`), which
   means the error string at `:748` has been describing an intention since it was
   written. Goal 5's `orphaned` state is the first honest implementation of it.

## 4. Goals

1. **A pool can be left running.** `spawn_subagents` gains
   `background: bool = False`. With `True` it starts the pool, registers it, and
   returns `{status: "running", pool_id, total, workers}` after the pool's ledger
   record exists — never after the pool finishes. The pool task is started
   through a `SupervisedTasks` owner rather than `asyncio.create_task`
   (`tool.py:304`), so an unobserved task cannot be garbage-collected
   (`src/agenthicc/supervised_tasks.py:1-8`) and shutdown has something to wait on.
2. **Identity exists before work does.** A pool ledger record is written when the
   pool is registered and updated at every terminal state. It carries `pool_id`,
   `conversation_id`, `parent_run_id`, `run_id`, `fingerprint`, `background`,
   `created_at`, `finished_at`, `state`, `total` and one row per worker. The
   record is written *before* the first worker starts, which is the only ordering
   that survives a crash mid-pool. The shape follows `PoolRecord`
   (`transcripts.py:127-137`) so the ledger and the transcript index describe the
   same pool the same way.
3. **Collection is a read, not a join.** `collect_subagent_results` gains
   `wait: bool = False` and `timeout_s`. With `wait=False` it returns the current
   snapshot for a running pool (`status: "running"`, per-worker rows, no blocking)
   and the aggregate for a finished one (`status: "completed"`). `wait=True`
   preserves today's join for callers that want it. No path awaits
   `handle.task` without an explicit request (`communication.py:745-750` is the
   line this goal removes).
4. **A result is delivered once, to whoever is next.** Collecting marks the
   record `collected` and returns the recorded result; a second collect returns
   the same result instead of raising (`communication.py:748`). A pool that
   reaches a terminal state while no turn is waiting becomes
   `finished_uncollected`; the next turn receives it — as a bounded system event
   naming the pool id and the worker counts, with the aggregate available through
   `collect_subagent_results`. This is the difference between a cache
   (`tool.py:443`) and an inbox.
5. **A restart reports the truth.** On session restore the ledger is read and any
   record still `running` with no live pool in this process (`active_pool`,
   `pool.py:1102`) is transitioned to `orphaned`, preserving its worker records
   (`src/agenthicc/memory/journal.py:857`) and transcripts (`transcripts.py:408`) and remaining
   collectable. `orphaned` is a terminal state that says "the process ended
   first" — never `completed`, and never an exception.
6. **A question is only asked where it can be answered.** A pool spawned from a
   non-interactive context — a background session (PRD-141) or a detached run
   (PRD-204) — applies `subagents.on_clarification`: `queue` surfaces the question
   through the background inbox and leaves the worker waiting until the answer or
   the timeout, and `fail` ends that worker immediately with
   `error="unanswerable_in_background"` instead of consuming
   `DEFAULT_MESSAGE_TIMEOUT_S = 300.0` (`communication.py:56`). The default is
   `fail` for non-interactive parents and `queue` for interactive ones, so no
   existing interactive behaviour changes.
7. **Background pools are visible where pools already are.** The pool continues
   to publish `SubagentPoolState` (`pool.py:1281`, `:1411`) and to write transcripts
   (`transcripts.py:173`), so the AGX-08 panel shows a running background pool
   without new rendering work; and the model gains the query the panel already
   has by reading the ledger rather than `list_pools` alone
   (`src/agenthicc/tui/workspace/overlays/subagents.py:80`). A restored session lists its pools — including
   orphaned ones — from the ledger.
8. **Nothing is left `running` for ever.** On session shutdown, background pools
   are drained or cancelled through A-8's pool stop — not through a second
   cancellation path — and each pool records its terminal state before the
   process exits. Finished-and-collected records are reaped by the count and age
   bounds A-4 declares; this PRD defines the reaping *state machine*, not the
   limit values.
9. **`background=False` is byte-identical to today.** Same aggregate text, same
   events, same transcripts, same `pool_id` semantics, same resume-cache
   behaviour. The new ledger record is written on both paths and is additive.

## 5. Non-goals

The A-7 boundaries from the research report §5.3, restated as commitments.

- **Mid-flight steering, cancellation, retry and salvage.** SUBAGENT-08 (A-8).
  A-7 makes a pool reachable after its turn ends; what a stop *means* — modes,
  reasons, partial output, per-worker retry — is A-8's, and goal 8 explicitly
  calls A-8's pool stop rather than defining one. This PRD adds no `stop`,
  `abort`, `drain` or `retry` verb.
- **Budget accounting and the response to a crossed limit.** SUBAGENT-06 owns the
  meter, the limit keys and the budget report; SUBAGENT-08 owns the response,
  under the locked boundary M-1 (research report §5.4): A-6 decides *when* a
  budget is exceeded; A-8 decides *what happens to the workers*. A-7 neither
  meters nor aborts; a background pool is subject to both.
- **The session lifecycle, session state machine and process supervision.**
  PRD-141 (`Implemented`) owns background sessions, and PRD-204 (`Implemented`)
  owns detached runs and durable run identity. This PRD *extends* both: it adds a
  pool-level record that names their session and run ids, and it never starts,
  stops or reattaches a process.
- **Re-executing a worker whose process died.** The honest boundary: a pool runs
  in the process that spawned it, so a crash ends the work. A-7 reports
  `orphaned` and preserves what exists; durable *re-execution* is what a detached
  run (PRD-204) provides, and A-7 does not grow a second supervisor to fake it.
- **The shape of a completed result.** SUBAGENT-03 (A-3). Progress and terminal
  states in this PRD are control-plane states, not a result envelope; the
  aggregate stays plain text (`pool.py:1388`) until A-3 says otherwise.
- **Depth, fan-out and pool-count limits.** SUBAGENT-04 (A-4). A-7 reaps records
  under A-4's declared bounds and declares none of its own.
- **The event and transcript schema.** SUBAGENT-09 (A-9) owns one envelope for
  kernel events, transcripts and the TUI. This PRD adds fields to records it does
  not own and states them so A-9 can absorb them.
- **Type definitions and per-type model routing.** SUBAGENT-01 and SUBAGENT-02.
  A background pool is a scheduling mode, not a new type.
- **A durable multi-agent task graph or cross-session team coordination.**
  PRD-232. This PRD makes one pool collectable across turns; it builds no
  dependency graph and no cross-session messaging.
- **External notifications.** PRD-141 and the notification outbox
  (`src/agenthicc/background/notifications.py:178`) own delivery to a phone or a
  webhook. Goal 4's delivery is in-session, and goal 6 only *routes* a question
  into that outbox.
- **Polling.** The tool result tells the model it will be notified and must not
  poll for progress; nothing in this PRD adds a progress-polling loop to the
  model's side of the conversation.

## 6. Acceptance criteria

1. `spawn_subagents(..., background=True)` returns within one second with
   `status="running"` and a `pool_id`, while its workers are still running, and
   `background=False` still returns the aggregate as today:
   `tests/integration/test_subagent_tool_execution.py`.
2. A ledger record exists with `state="running"` and one row per worker *before*
   the first worker's first turn, and it is readable from the journal alone:
   `tests/unit/test_subagent_resume.py`.
3. `collect_subagent_results(pool_id)` on a running pool returns
   `status="running"` with per-worker rows and does not block the parent turn:
   `tests/unit/test_subagent_communication.py`.
4. `collect_subagent_results(pool_id, wait=True)` on a running pool returns the
   aggregate when the pool finishes, preserving today's join for explicit
   callers: `tests/integration/test_subagent_tool_execution.py`.
5. Collecting a finished pool twice returns the same aggregate both times, and
   the second call does not raise `unknown or expired subagent pool`:
   `tests/unit/test_subagent_communication.py`.
6. A pool that finishes while no turn is waiting is delivered exactly once: the
   next turn receives one system event naming the pool id, one `ok` field, and
   the aggregate is retrievable afterwards: `tests/unit/test_subagent_resume.py`.
7. After a simulated restart — a fresh registry and no live pool — a record
   previously `running` reads `orphaned`, keeps its worker records, and its
   aggregate or partial output is collectable: `tests/unit/test_subagent_resume.py`.
8. A background pool whose worker asks the parent with
   `subagents.on_clarification="fail"` ends that worker with
   `unanswerable_in_background` in under a second, and with `"queue"` the
   question is visible on the parent side and answerable later:
   `tests/unit/test_subagent_communication.py`.
9. A background pool appears on the panel while running, without the panel being
   told about a new event kind: `tests/integration/test_subagent_panel_agx08.py`.
10. A restored session lists its pools, including orphaned ones, from the ledger
    rather than only from transcripts:
    `tests/unit/test_subagent_panel_agx08.py`.
11. Session shutdown leaves no record in `running`: every background pool is
    terminal, and the terminal state was written before the shutdown completed:
    `tests/unit/test_subagent_pool.py`.
12. `background=False` produces the same aggregate text, event sequence and
    transcript files as the current implementation:
    `tests/unit/test_subagent_pool_runtime_coverage.py`.
13. Two background pools from the same session collect independently, in either
    order, with no cross-talk in results or worker ids:
    `tests/integration/test_subagent_tool_execution.py`.

## 7. Rollout, migration and security

**Compatibility.** `background` and `wait` are defaulted parameters on existing
tools, so every current caller — including the workflows that receive
`spawn_subagents` by injection (`src/agenthicc/runners/agent_turn.py:1596`) — is
unaffected. `PoolHandle` keeps its fields and gains no required argument; the
registry keeps `register`, `get`, `remove` and `collect` with their present
signatures, and `collect`'s new behaviour is that it is *non-blocking by
default*, which changes a call that today blocks — that is the point of the
PRD, and `wait=True` is the migration path for anything that wants the old
semantics. `PoolContinuationRegistry.get`'s dead branch
(`communication.py:736-740`) is replaced by a real state check, so a caller that
relied on it raising for an expired pool must now look at the returned state.

**Defaults.** No setting is required to get today's behaviour.

| Setting | Default | Meaning |
|---|---|---|
| `subagents.background.enabled` | `true` | whether the `background` parameter is offered at all; `false` hides it from the schema |
| `subagents.on_clarification` | `"queue"` interactive, `"fail"` non-interactive | what a pool does when a worker asks its parent a question |
| `subagents.collect_default_wait_s` | `0` | how long `collect_subagent_results` waits before returning a snapshot |

The clarification default is derived from the session kind (PRD-141's
interactive/background distinction), not a fourth global mode, so a background
session cannot accidentally inherit an interactive default.

**Migration.** Journals written before this PRD have `subagent_worker_result` and
`subagent_pool_result` records (`src/agenthicc/memory/journal.py:857`, `:897`) and no ledger record.
The ledger folds those on first read: a `subagent_pool_result` becomes a
`completed`, `collected` record, and worker records without a pool record become
a `partial` record with `state="orphaned"` — because that is what they are. No
rewrite of existing journal files, and a legacy record is never reported as
`running`.

**Security.**

- **A background pool does not widen anything.** It is built from the same
  immutable `SubagentExecutionPolicy`, the same `allowed_tools` ceiling and the
  same workspace scope as a foreground pool; the only new input is *when* the
  parent learns the answer. SUBAGENT-05's narrowing rules apply unchanged.
- **Delivery is scoped, not broadcast.** A pool's aggregate is delivered only to
  the session that owns the record — `PoolHandle.conversation_id`
  (`communication.py:142`) is checked by the existing parent-tool guard
  (`:885-889`) — so a background pool cannot leak results into another session
  that happens to know a `pool_id`.
- **The ledger stores bounded, redacted text.** The aggregate and worker texts it
  carries are the same strings the pool already redacts and truncates before
  display (`pool.py:1446-1454` is the redactor every row passes through, and the
  transcript writer applies its own limits at `transcripts.py:209`), so a
  background pool does not create a second, unredacted copy of model output on
  disk.
- **`fail` is the fail-closed clarification default.** A non-interactive parent
  that cannot answer stops the worker quickly and says so, rather than holding a
  slot and a 300-second timer (`communication.py:56`). `queue` is the opt-in, and
  it is bounded by the same pending-question cap the broker already enforces
  (`MAX_PENDING_QUESTIONS`).
- **Orphaning is honest, not fatal.** A crashed pool leaves `orphaned` records
  that are readable and collectable and are never silently converted to
  `completed`; a parent that treats partial work as whole is a security problem
  wearing a quality problem's clothes, so the state is explicit and appears in
  the panel and the tool result.

## 8. Verification

Current suite that must stay green:

```console
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/unit/test_subagent_communication.py -q
$ pytest tests/unit/test_subagent_resume.py -q
$ pytest tests/unit/test_subagent_panel_agx08.py -q
$ pytest tests/integration/test_subagent_tool_execution.py -q
$ pytest tests/integration/test_subagent_panel_agx08.py -q
```

Measured on 2026-10-29 at the commit in §3: 58 passed (test_subagent_pool),
3 passed (test_subagent_pool_runtime_coverage), 9 passed
(test_subagent_communication), 16 passed (test_subagent_resume), 22 passed
(unit test_subagent_panel_agx08), 11 passed
(integration test_subagent_tool_execution), 5 passed
(integration test_subagent_panel_agx08).

New cases belong in `tests/integration/test_subagent_tool_execution.py` (the
fire-and-return spawn, the explicit `wait=True` join and the two-pool isolation),
in `tests/unit/test_subagent_communication.py` (non-blocking collect, the repeat
collect, and both clarification policies), in `tests/unit/test_subagent_resume.py`
(the ledger record before work, exactly-once delivery, and orphan recovery), and
in `tests/unit/test_subagent_panel_agx08.py` / the integration panel test (a
running background pool on the rails, and the restored-session listing).

The two tests this PRD most needs are criteria 1 and 7. Criterion 1 fails today
for a structural reason — `tool.py:333` is an unconditional join, so there is no
value the tool can return while the pool runs — and criterion 7 is the boundary
asserted as behaviour: a fresh process must describe a dead pool as `orphaned`
rather than as absent or complete.

## 9. Related and superseded PRDs

- **PRD-124 — extends.** PRD-124 introduced the concurrent typed pool, the
  plain-text aggregate and the fingerprint resume cache
  (`tool.py:425`). This PRD keeps all three and adds the two things a
  turn-scoped design could not have: a fire-and-return mode and a pool record
  that outlives the call. It does not revisit PRD-124's plain-text result
  decision; that is SUBAGENT-03's to qualify.
- **PRD-141 — extends, never duplicates.** Background sessions are `Implemented`
  and own the session state machine, the supervisor and the notification outbox
  (`src/agenthicc/background/supervisor.py:247`, `src/agenthicc/background/notifications.py:178`). A-7 adds a
  `session_id` correlation on the pool record and defines what a *pool* does
  inside a session it does not own; it starts no process and adds no session
  state.
- **PRD-204 — extends, never duplicates.** Detached runs are `Implemented` and
  own durable run identity and terminal independence. A-7 adds a `run_id`
  correlation on the pool record and states explicitly that re-execution after a
  process exit is PRD-204's detached run, not something A-7 imitates with an
  orphaned record.
- **AGX-08 — extends.** The subagent panel (phase 1, implemented) already
  renders live pool state and reads past pools through `list_pools`
  (`src/agenthicc/tui/workspace/overlays/subagents.py:80`). This PRD requires no new rendering: a background
  pool is visible because pool state publication and transcripts already exist.
  A-9 formalises the event contract; A-7 only guarantees the events keep coming
  when the parent turn has ended.
- **SUBAGENT-06 — coexists, narrows against.** A-6 meters spend and declares
  limits; a background pool is simply another pool under that meter. This PRD
  defines no limit, no budget and no abort, and goal 8 does not respond to a
  crossing.
- **SUBAGENT-08 — depends on, narrows against.** A-8 owns what a stop *is* and
  owns the abort-versus-drain response to an A-6 crossing (M-1, §5.4). A-7 owns
  the reach that makes a background pool stoppable at all, and goal 8 calls A-8's
  pool stop rather than defining one. The research report's §5.7 dependency is
  therefore literal: a background pool that cannot be steered or stopped is a
  leak, so A-7 lands after A-8's verb exists.
- **SUBAGENT-09 — narrows against.** A-9 owns one event and transcript schema. This PRD
  names the fields its records add — `pool_id`, `state`, `background`,
  `collected_at`, `orphaned_at`, `run_id`, `session_id` — so A-9 can absorb them
  instead of discovering them.

## A. Competitor evidence

Strong on the capability (four of six) and uneven on the mechanism: only one of
the four keeps a background pool alive *and* says how the model is told, and only
one of those has a tested lifetime. The rows below are the ones this PRD leans
on, with the depth label copied from the report's §2.1 table.

| Harness | Evidence | Depth | What it does about a non-blocking spawn | Take or reject |
|---|---|---|---|---|
| opencode | `packages/core/src/tool/plugin/subagent.ts:19-28`, `:30-54`, `:41-43` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | `background: true` returns immediately with `{ sessionID, status: "running", output }`, tells the model it "will be notified automatically when it finishes", and adds "DO NOT sleep, poll for progress, ask the subagent for status, or duplicate this subagent's work"; passing `sessionID` continues a running *or finished* subagent. Lifetime is tested against eviction (`script/drive/subagent-eviction.md`, `packages/core/test/subagent-lifetime.test.ts`) with SQLite persistence and parent wake | Take the explicit `status: "running"` return and the anti-polling instruction verbatim in the tool description (goals 1, 3), and the tested-lifetime discipline as the model for criterion 7; reject `sessionID`-as-continuation, because our continuation target is a *pool* and A-8 owns steering a *worker* |
| Codex CLI | `codex-rs/core/tests/suite/multi_agent_tool_descriptions.rs:18-25`, `codex-rs/core/src/config/mod.rs:1404` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | Six tools rather than one, and blocking is an explicit, disableable tool: `wait_agent` exists and `multi_agent_v2.wait_agent_enabled` defaults to `true`, so a model waits only by calling a tool — the harness never blocks the turn for it. `list_agents` enumerates agents by path prefix, and identity is a validated `AgentPath` | Take "blocking is an explicit act by the caller" as the shape of `wait=True` (goal 3), and `list_agents` as precedent for giving the *model* an enumeration query instead of leaving it to the TUI (goal 7); reject a six-tool split, which is A-8's surface, not the scheduling one |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | `background` is a subagent frontmatter field, and background subagents have different semantics rather than different scheduling: a smaller built-in tool set, permission prompts surfaced in the main session, and results delivered as a completion notification in a *later turn* | Take later-turn completion delivery as the product promise behind goal 4 (an inbox, not a cache) and the explicit statement that background mode narrows tools as the precedent for §7's "does not widen anything"; reject per-definition background, because A-7 makes it a per-call mode and SUBAGENT-01 owns the type record |
| Grok CLI | `src/grok/tools.ts:593-595` @ `fb97af83f06dca873281d60168430f06c8de6324` | code | Two spawn tools split by synchronicity: `task` for a foreground delegation, `delegate` for a read-only background agent that "can keep researching while you continue working… You will be notified when it completes" | Take the promise in the description — the model is told it will be notified, so it does not poll (goal 3's description text); reject the two-tool split as the primary design because our `spawn_subagents` already carries a large typed schema, and a second near-identical tool doubles it for one boolean; the split's real benefit (no accidental backgrounding) is bought instead by `background=False` as the default and `subagents.background.enabled` |
| DeepSeek Harness | `.agents/notes/archived/architecture/2026-08-18-experimental-agent-teams-packages.md` @ `f3803c96974ad4a94195fb97e5c637e9c2a3afe3` | code + design notes | "the generic caller-reserved continuable child identity and selective direct-child drain remain in the stable Subagent service" — identity is reserved by the caller and lives in a stable service that outlives the caller's frame | Take *caller-reserved identity* as the reason goal 2 writes the ledger record before the first worker starts, and the stable-service split as the reason the pool record belongs to the session rather than to the tool call; reject the host/agent two-plane architecture as more structure than one pool record needs |
| Qoder CLI | `https://docs.qoder.com/cli/subagent` fetched 2026-10-29 | docs-only | `background` and `isolation` are Markdown-frontmatter fields on a subagent definition, alongside `timeoutMins` | Record as corroboration only: it confirms `background` is a recognised per-definition concept in a second closed harness, and contributes nothing to the mechanism. No requirement in this PRD rests on this row |
