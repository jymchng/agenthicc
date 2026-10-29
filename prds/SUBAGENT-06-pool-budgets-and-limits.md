---
title: "SUBAGENT-06: Pool budgets and limits"
status: Proposed
version: 0.1.0
date: 2026-10-29
scope: "token, cost and wall-clock budget accounting for one subagent pool: measuring spend from the existing usage ledger, declaring a limit, and reporting both to the parent, the transcript and the TUI — never the act of stopping a pool"
area: A-6
related_prds:
  - PRD-157
  - PRD-233
  - SUBAGENT-02
  - SUBAGENT-08
  - SUBAGENT-09
tags:
  - subagents
  - budgets
  - usage
  - cost
  - limits
---

# SUBAGENT-06: Pool budgets and limits

## 1. Executive summary

The pool already meters every worker, and reports none of it.

A worker's provider usage is written to the session ledger under
`category="subagent"` (`src/agenthicc/subagents/pool.py:932-943`), and its live
token count is accumulated into `WorkerState.tokens` (`:234`) by
`_WorkerProgress.usage` (`:502-504`). Every surface that leaves the pool then
drops the number: `SubagentResult` carries a duration and a tool list but no
usage (`:318-329`), `AggregatedResult` carries three counts and a string
(`:352-360`), the completion event publishes four fields (`:1545-1561`), and the
tool's return dict publishes none (`src/agenthicc/subagents/tool.py:363-371`).

So a parent can spend an unbounded amount on a single `spawn_subagents` call and
receive a report that cannot tell it so. The only bound anywhere is concurrency:
`max_concurrent`, default 4 (`pool.py:1120`), enforced by one semaphore
(`:1309`). The word *budget* does not appear in the package at all (§3).

**This PRD gives budgets a name, a number and a report. It does not make them
stop anything.** The response to a crossed limit — pre-emptive abort versus
graceful drain, and what partial output survives — belongs to SUBAGENT-08 under
the locked boundary M-1 (research report §5.4): A-6 decides *when a budget is
exceeded*; A-8 decides *what happens to the workers*. Two PRDs in one series
must not each define a cancellation path, so this one stops at detection.

Three additions, each reusing what is already built rather than parallel to it:

- **Accounting** aggregates the ledger's `subagent`-category records per pool,
  respecting the ledger's existing provisional/completed distinction
  (`src/agenthicc/runners/usage_ledger.py:163-202`) so nothing is counted twice.
- **Declaration** is a `budget:` argument on the spawn call — tokens, cost and
  wall clock — with a settings-level ceiling the call may lower but never raise,
  mirroring the clamp `spawn_worker_agents` already applies
  (`src/agenthicc/worktrees/agent_tool.py:55`, `:100`). No definition key is
  added, so the type record and AGX-09's key set are untouched.
- **Reporting** puts usage-versus-limit on the three surfaces that exist: the
  tool result the parent reads, the pool completion event, and the pool state
  the panel renders — with the TUI's existing token and cost projection
  (`src/agenthicc/tui/conversation_store.py:205`) rather than a second widget.

## 2. Problem statement

All claims verified at commit `f843040644db374260ebb272577f4f3d8f976953`
(2026-10-29); the source has not moved since that snapshot (§3 re-checks it).
Commands and output are re-quoted in §3.

**The vocabulary does not exist.** `grep -rniE
"budget|max_cost|cost_limit|token_limit|abort_threshold"
src/agenthicc/subagents/` returns nothing. The package that implements pools and
workers has no concept of a limit of any kind — not a name to declare one, not a
place to compare against one.

**Concurrency is bounded; spend is not.** `SubagentPool.__init__` takes
`max_concurrent: int = 4` (`src/agenthicc/subagents/pool.py:1120`) and `run()`
creates one `asyncio.Semaphore(self._max_concurrent)` (`:1309`). Per worker there
is a turn cap (`max_turns`, `src/agenthicc/subagents/types.py:89`) and a timeout
(`max_turn_time_s`, `:91`). Multiply: four workers × one hour × unlimited tokens
per turn is a legal call today, and nothing in the pool observes the product.

**The meter exists and is wired.** Each worker gets its own
`UsageRunTracker` with `run_id=f"{self._parent_run_id}:subagent:{self._task.task_id}"`,
`category="subagent"` and `agent_name=self.label`
(`src/agenthicc/subagents/pool.py:932-943`), so worker spend is already
attributable to a labelled run inside the durable ledger that PRD-157 owns
(`src/agenthicc/runners/usage_ledger.py:1-12`; `UsageCategory.SUBAGENT` at
`:94`). Live, `_WorkerProgress.usage` sums tokens into the worker's published
state (`pool.py:502-504`, field at `:234`). The pool also publishes a `limit`
field on its state (`:257`) — the concurrency limit, which is the only limit it
has.

**The three exit surfaces drop it.** `SubagentResult`
(`src/agenthicc/subagents/pool.py:318-329`) has `duration_ms` and `tool_calls`
but no token or cost field; `AggregatedResult` (`:352-360`) has `pool_id`,
`total`, `succeeded`, `failed` and `text`; `_emit_pool_completed`
(`:1545-1561`) publishes exactly those four; the tool returns them plus an error
string (`src/agenthicc/subagents/tool.py:363-371`). A parent that wanted to know
what a pool cost has to reconstruct it from the session totals, which mix every
pool and the foreground turn together (`UsageSnapshot`,
`src/agenthicc/runners/usage_ledger.py:269-288`).

**The ledger key cannot identify the pool.** The run id above is built from
`parent_run_id` and `task_id`. `task_id` is `f"task-{i}"`, the task's index in
the call (`src/agenthicc/subagents/tool.py:245`), and `parent_run_id` is the
turn's intent id, `uuid4().hex` once per turn
(`src/agenthicc/runners/agent_turn.py:1144`, passed at `:1582`). Two
`spawn_subagents` calls in the same turn therefore both write
`…:subagent:task-0`, and the records are indistinguishable. Per-pool accounting
is not just missing a total; the existing key cannot produce one.

**The declaration idiom is already in the tree.** `spawn_worker_agents` derives
`effective_limit = max(1, int(max_parallel_tasks))`
(`src/agenthicc/worktrees/agent_tool.py:55`), defaults the model-facing argument
to it (`:78`) and clamps with `limit = min(max_concurrent, effective_limit)`
(`:100`) — the call may lower the configured ceiling, never raise it.
`spawn_subagents` has the opposite shape: the model's `max_concurrent` is passed
through and only floored at 1 (`src/agenthicc/subagents/tool.py:286`), with the
default announced in the docstring (`:206`). A budget argument must follow the
worktree clamp, not this one, or the model can raise its own ceiling.

**Consequence for users.** An expensive pool is indistinguishable from a cheap
one until the session footer moves, and by then the spend is past. There is no
way to say "four workers, but stop me at 200k tokens", and no record afterwards
of which pool spent what — which is also the reason a post-mortem of a runaway
pool cannot name the call that caused it.

## 3. Current-source investigation

Run on 2026-10-29. `git rev-parse HEAD` at the time of writing:

```console
$ git rev-parse HEAD
acc7b2167537ca65c1396957367d908b12391308

$ git diff --stat ffbfa0da681fa76a52a8e49e5f9b5776cbb8f780 HEAD -- src/
(no output: src/ is byte-identical to the research baseline)
```

The absence, and the only bound that exists:

```console
$ grep -rniE "budget|max_cost|cost_limit|token_limit|abort_threshold" src/agenthicc/subagents/
(no output; grep exit status 1)

$ grep -n "max_concurrent" src/agenthicc/subagents/pool.py | head -3
1108:    """Runs a set of SubagentTasks concurrently bounded by *max_concurrent*.
1120:        max_concurrent: int = 4,
1145:        self._max_concurrent = max_concurrent

$ grep -n "Semaphore(self._max_concurrent)" src/agenthicc/subagents/pool.py
1309:        semaphore = asyncio.Semaphore(self._max_concurrent)
```

The meter that is already wired into the ledger:

```console
$ sed -n '932,943p' src/agenthicc/subagents/pool.py
        usage_tracker = None
        if self._usage_ledger is not None:
            from agenthicc.runners.usage_ledger import UsageRunTracker

            usage_tracker = UsageRunTracker(
                self._usage_ledger,
                run_id=f"{self._parent_run_id}:subagent:{self._task.task_id}",
                provider="unknown",
                model=self._parent_model,
                category="subagent",
                agent_name=self.label,
            )
```

The surfaces that drop it:

```console
$ sed -n '318,329p' src/agenthicc/subagents/pool.py
class SubagentResult:
    """Outcome of one subagent worker execution."""

    task_id: str
    agent_type: str
    label: str  # "explorer #1", "tester #2", …
    ok: bool
    text: str  # AgentResponse.content or a tool-evidence fallback (plain text)
    error: str = ""
    duration_ms: float = 0.0
    tool_calls: tuple[str, ...] = ()  # Provider tools actually dispatched
    changed_paths: tuple[str, ...] = ()  # Paths supplied to mutation tools

$ sed -n '1550,1560p' src/agenthicc/subagents/pool.py
        await self._processor.emit(
            Event.create(
                "SubagentPoolCompleted",
                {
                    "pool_id": self.pool_id,
                    "total": agg.total,
                    "succeeded": agg.succeeded,
                    "failed": agg.failed,
                    "text": agg.text,
                },
            )
```

Why the ledger key cannot separate pools:

```console
$ grep -n "task_id=f" src/agenthicc/subagents/tool.py
245:                    task_id=f"task-{i}",

$ sed -n '1144,1144p' src/agenthicc/runners/agent_turn.py
        self._intent_id = self._ctx.resume_turn_id or uuid.uuid4().hex
```

The clamp idiom this PRD copies, from the sibling worker tool:

```console
$ sed -n '55,56p;100,100p' src/agenthicc/worktrees/agent_tool.py
    effective_limit = max(1, int(max_parallel_tasks))
    manifest_store = store or ManifestStore()
        limit = min(max_concurrent, effective_limit)
```

What the ledger offers for aggregation — and what it does not:

```console
$ sed -n '163,183p' src/agenthicc/runners/usage_ledger.py
class UsageRecord:
    """One immutable provider-call usage record."""

    record_id: str
    session_id: str
    conversation_id: str
    run_id: str
    agent_id: str
    agent_name: str
    call_index: int
    provider: str
    model: str
    category: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    cost_status: str
    source: str
    lifecycle: str

$ grep -c "pool" src/agenthicc/runners/usage_ledger.py
0
```

Absence claims, each with the pattern and the scope that establish it:

- **No budget declaration, default or comparison.** Pattern
  `budget|max_cost|cost_limit|token_limit|abort_threshold`, scope
  `src/agenthicc/subagents/`: no match (console above). The spawn signature
  offers exactly `tasks`, `max_concurrent` and `timeout_s`
  (`src/agenthicc/subagents/tool.py:188-192`).
- **No usage on any result surface.** Pattern
  `tokens|cost|usage`, scope the three dataclasses and the completion event
  (`pool.py:318-329`, `:352-360`, `:1545-1561`) and the tool return
  (`tool.py:363-371`): no match. The only token field anywhere near a result is
  `WorkerState.tokens` (`pool.py:234`), which is published state, not a result.
- **No pool concept in the ledger.** Pattern `pool`, scope
  `src/agenthicc/runners/usage_ledger.py`: zero lines (console above). The
  ledger aggregates by session (`UsageSnapshot`, `:269-288`) and enumerates raw
  records (`records()`, `:600-605`); it has no pool or run aggregate.
- **No configured ceiling for `spawn_subagents`.** Pattern
  `max_parallel_tasks|effective_limit`, scope `src/agenthicc/subagents/`: no
  match; both appear only in `src/agenthicc/worktrees/agent_tool.py`
  (`:55`, `:78`, `:100`).

Diff against the research report's `f843040` snapshot: **nothing has moved** for
G-6. The zero-match grep, the semaphore at `pool.py:1309`, the `max_concurrent`
default at `:1120` and the tracker construction at `:932-943` are the same lines
the report cites.

Three findings that shaped the design.

**The meter is the asset, not the gap.** The area papered over a working
per-worker, per-category ledger integration. A PRD that proposed its own token
counter would be the second meter — the exact duplication M-1 forbids on the
cancellation side. Everything here is an aggregation of records that already
exist, with one enabling change: the run id must carry the pool id, because
`task-{i}` is an index (`tool.py:245`) and two calls in one turn collide.

**Unknown cost is not zero cost.** The ledger distinguishes
`CostStatus.AUTHORITATIVE`, `ESTIMATED` and `UNAVAILABLE`
(`src/agenthicc/runners/usage_ledger.py:54-59`) and records `cost_usd=None` when
a provider reports nothing (`:426-427`). A budget report that prints `$0.00` for
an unpriced model is worse than one that prints "cost unknown": it invites a
decision on false data. Cost reporting here carries the ledger's status through
verbatim, and a cost budget on an unpriced model is a declaration error, not a
silent no-op.

**Wall clock already exists per worker, not per pool.** `SubagentResult` has
`duration_ms` (`pool.py:327`), `WorkerState` has `started_at`/`finished_at`
(`:232-233`), and `SubagentPoolState` publishes `finished_at` (`:258`)
from a pool whose start is not recorded as a field. A pool-level wall clock is
therefore a two-field addition, not a new timing system.

## 4. Goals

1. **Per-pool accounting from the ledger.** One pool's usage is the set of
   ledger records whose run id carries that pool id, summed over input, output,
   cache-read and cache-write tokens, with cost summed only over records whose
   `cost_status` is known, and unknown cost propagated as *some unknown*, never
   as zero.
2. **Durable pool identity in the ledger.** The worker run id gains the pool id
   (`…:{pool_id}:subagent:{task_id}`), so records can be attributed to a pool at
   all; the worker's `agent_name` label is unchanged, and existing records keep
   resolving (the run id is a string, not a join key).
3. **No double counting.** Accounting reads completed records and excludes
   provisional ones by the ledger's own lifecycle flag
   (`usage_ledger.py:196-202`), so a pool total is monotone while workers run and
   stable after they finish, and it never disagrees with the session total it is
   a subset of.
4. **A declared limit.** `spawn_subagents` accepts an optional `budget:` mapping
   with `tokens`, `cost_usd` and `wall_clock_s`, any subset present; absent means
   today's behaviour exactly. Values are validated (positive, finite, integer
   tokens) and a malformed budget is refused before any worker starts.
5. **A ceiling the call cannot raise.** A settings-level default
   (`subagents.max_budget`) caps what a call may declare, clamped the way
   `spawn_worker_agents` clamps concurrency
   (`src/agenthicc/worktrees/agent_tool.py:55`, `:100`): a larger request is lowered to the
   ceiling and the result says so, rather than silently honoured or silently
   refused.
6. **Per-worker attestation.** Each `SubagentResult` carries its own tokens,
   cost and cost status, and `AggregatedResult` carries the pool totals, so the
   parent can see which worker in a pool was the expensive one.
7. **A report on the surfaces that exist.** Usage-versus-limit appears in the
   tool result's text digest, in the `SubagentPoolCompleted` event payload, and
   on `SubagentPoolState` for the panel — each a superset of today's fields, so
   an existing consumer keeps working.
8. **Reuse the TUI's projection.** The panel shows pool spend through the token
   and cost signals PRD-157 already maintains
   (`src/agenthicc/tui/conversation_store.py:205`, `:565-582`); this PRD adds no
   second cost widget, no second pricing table and no second formatter.
9. **Detection, not response.** When a declared limit is crossed, the pool
   records the crossing, marks the report, and emits it. It does not stop,
   drain, cancel or trim anything; `§5` states that boundary in the same words as
   SUBAGENT-08, and goal 9's partial ordering is A-8's concern.
10. **No change for undeclared budgets.** A call without `budget:` produces the
    same results, events and texts as today, plus attestation fields whose
    absence is representable — so the nine built-in types and every existing
    caller are unaffected.

## 5. Non-goals

The A-6 boundaries from the research report §5.3, restated as commitments.

- **The response to a crossed limit.** Pre-emptive abort versus graceful drain,
  and partial-result preservation on abort, are SUBAGENT-08's, under the locked
  boundary M-1 (§5.4), quoted in the report's own emphasis: A-6 decides *when a
  budget is exceeded*; A-8 decides *what happens to the workers*. Abort is the
  same mechanism as cancellation, it operates on the same worker handles, and
  leaving it here would give the series two cancellation paths. This PRD may say
  that a limit was crossed; it may not stop a worker, drain a pool, or define
  what survives a stop.
- **The usage ledger.** PRD-157 is `Implemented` and owns canonical accounting,
  durability, cost provenance and the TUI projection
  (`src/agenthicc/runners/usage_ledger.py`). This PRD consumes records; it adds
  no meter, no pricing table and no reconciliation path.
- **Turn and tool-call budgets.** `max_turns` and `max_turn_time_s` already exist
  on the type record (`src/agenthicc/subagents/types.py:89`, `:91`); M-3 and R-8
  rejected them as an area, and this PRD does not redefine or rename them. A
  token budget is a different quantity from a turn budget and both may apply.
- **Per-type budgets on the definition record.** Budgets are per-pool policy,
  not a property of a role; adding a fifth definition key would be another
  unilateral AGX-09 extension with no evidence behind it. If per-type defaults
  are ever wanted they belong to PRD-233's profile system, which owns per-agent
  policy.
- **Model and context sizing.** Which model a type runs on and how large its
  memory is belong to SUBAGENT-02 (A-2). This PRD reads the cost those choices
  produce; it does not choose them. Until A-2 lands, every worker's usage is
  attributed to the parent model (`pool.py:940`), and the report says so rather
  than pretending the attribution is per-type.
- **Background and cross-turn pools.** A pool that outlives the turn needs
  durable identity and collection, which is SUBAGENT-07 (A-7). Goal 2's pool id
  is the minimum this PRD needs to attribute spend; A-7 owns what else it means.
- **The event and transcript schema.** SUBAGENT-09 (A-9) owns one schema for
  kernel events, transcripts and the TUI. This PRD adds fields to an existing
  event and states them; it does not define the schema.
- **Pricing.** No exchange rate, no vendor list, no budget forecasting. Cost is
  whatever the ledger recorded, with the ledger's provenance status attached.

## 6. Acceptance criteria

1. A pool's token total equals the sum of its workers' ledger records and the
   session total is unchanged by the aggregation, proving the pool number is a
   subset and not a second meter: `tests/unit/test_usage_ledger.py`.
2. Two `spawn_subagents` calls in one parent run produce disjoint pool totals,
   which fails today because both write `…:subagent:task-0`:
   `tests/unit/test_subagent_pool.py`.
3. A pool whose records carry `cost_status="unavailable"` reports cost as
   unknown rather than `$0.00`, and a `cost_usd` budget on such a pool is
   refused at declaration with the reason named:
   `tests/unit/test_usage_ledger.py`.
4. Provisional records are excluded from the pool total, so the total is
   monotone across snapshots taken while workers run and does not double-count a
   reconciled call: `tests/unit/test_usage_ledger.py`.
5. `spawn_subagents(budget={"tokens": 1000})` lowers the effective limit, and a
   call declaring more than `subagents.max_budget` is clamped to the ceiling
   with the clamp reported in the result: `tests/unit/test_subagent_pool.py`.
6. A malformed budget — zero, negative, non-integer tokens, non-finite cost —
   is refused before any worker starts, naming the offending field, and a call
   with no `budget:` behaves exactly as today:
   `tests/integration/test_subagent_tool_execution.py`.
7. Each `SubagentResult` reports its own tokens and duration, and
   `AggregatedResult` reports the pool total, so a pool of two unequal workers
   attributes spend to the right worker: `tests/unit/test_subagent_pool.py`.
8. The `SubagentPoolCompleted` payload and the tool's text digest both carry
   usage-versus-limit, and the text says *crossed* when a declared limit was
   passed, so the parent is told rather than left to infer:
   `tests/integration/test_subagent_tool_execution.py`.
9. `SubagentPoolState` publishes the pool's tokens, cost and declared limit, and
   the panel renders them through PRD-157's existing signals without a second
   widget: `tests/unit/test_subagent_panel_agx08.py`.
10. Crossing a declared limit produces a recorded crossing and no cancellation:
    the worker count, statuses and returned text are identical to the same run
    with no limit, which is the M-1 boundary asserted as a test:
    `tests/unit/test_subagent_pool.py`.
11. `pytest tests/unit/test_usage_ledger.py -q` and
    `pytest tests/unit/test_subagent_pool.py -q` pass unchanged for declarations
    that are absent, proving the default path is inert:
    `tests/unit/test_subagent_pool_runtime_coverage.py`.

## 7. Rollout, migration and security

**Defaults.** No budget is declared by default and
`subagents.max_budget` ships unset, so day-one behaviour is today's: no
aggregation requested, no clamp applied, no new text in the digest, attestation
fields present but empty. A pool opts in per call; a project opts in by setting
a ceiling.

**Migration.** Three ordered steps, each independently shippable:

1. Put the pool id in the worker run id (goal 2) and add attestation fields to
   `SubagentResult`/`AggregatedResult` (goal 6). Neither changes behaviour; both
   are pure information, and both are needed before a limit can mean anything.
2. Add `budget:` parsing, validation and the settings ceiling (goals 4, 5) plus
   the aggregation (goals 1, 3), without yet publishing the comparison.
3. Publish usage-versus-limit on the three surfaces and wire the panel to
   PRD-157's signals (goals 7, 8).

**No definition-format coordination.** This PRD adds no key to the type record,
so AGX-09 §5.1 and PRD-250's unknown-key validation need no change on its
account. That is deliberate: three of the first five PRDs in this series each
extend that key set, and a fourth unilateral extension in the weakest-evidenced
area would be the wrong way to spend the format's tolerance.

**Security and honesty.**

- *Unknown is not zero.* Cost is reported with the ledger's provenance
  (`AUTHORITATIVE`, `ESTIMATED`, `UNAVAILABLE`) and a cost budget cannot be
  declared against an unpriced model without that being an error. Reporting
  `$0.00` for unknown spend is the failure mode that makes a budget
  theatre.
- *The ceiling binds downward only.* A call may lower the configured limit and
  may never raise it (goal 5); the model-facing default is stated in the
  docstring the way `max_concurrent`'s is (`src/agenthicc/subagents/tool.py:206`),
  and the clamp is reported rather than silent.
- *A budget cannot be a denial-of-service.* Token and cost limits are integers
  and floats with positivity and finiteness checks; wall clock is compared
  against the pool's measured runtime, never used as a `sleep`, and never
  replaces `max_turn_time_s`.
- *No prompt text in the report.* Pool accounting carries counts, durations,
  labels and types — the same shape as `SubagentPoolCompleted` today — so a
  budget report can be logged without a redaction pass.
- *Detection is not enforcement, and the report must say so.* A language model
  reading "budget crossed" will ask to stop the pool. This PRD's text names
  SUBAGENT-08 as the owner of that action, so the parent asks for the right
  thing rather than assuming a limit already stopped anything.
- *Attribution is per model, not per type, until A-2.* Every worker's records
  carry `model=self._parent_model` (`pool.py:940`). The report says
  "attributed to the parent model" rather than implying per-type cost, because a
  budget that is computed on a false attribution is a wrong number with a
  trustworthy face.

## 8. Verification

Current suite that must stay green:

```console
$ pytest tests/unit/test_usage_ledger.py -q
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/unit/test_subagent_panel_agx08.py -q
$ pytest tests/integration/test_subagent_tool_execution.py -q
$ pytest tests/integration/test_subagent_executor_integration.py -q
```

Measured on 2026-10-29 at the commit in §3: 12 passed
(test_usage_ledger), 58 passed (test_subagent_pool), 3 passed
(test_subagent_pool_runtime_coverage), 22 passed (test_subagent_panel_agx08),
11 passed (integration test_subagent_tool_execution), 1 passed
(integration test_subagent_executor_integration).

New cases belong in `tests/unit/test_usage_ledger.py` (pool aggregation, subset
invariant, provisional exclusion, cost provenance), in
`tests/unit/test_subagent_pool.py` (pool-id attribution across two calls in one
run, per-worker attestation, the ceiling clamp, detection without cancellation),
and in `tests/integration/test_subagent_tool_execution.py` (budget argument
validation and the digest the parent actually receives).

The two tests this PRD most needs are criterion 2 and criterion 10: two calls in
one turn must not merge their spend, and a crossed limit must change the report
and nothing else. The first fails today for a structural reason (`task-{i}` run
ids); the second is the M-1 boundary asserted as behaviour, and it would have to
fail for this PRD to have quietly become a cancellation PRD.

## 9. Related and superseded PRDs

- **PRD-157 — extends.** PRD-157 is `Implemented` and owns canonical usage
  accounting and the TUI's token and cost observability. This PRD adds one
  aggregate over its records (per pool) and one reader of its projection (the
  panel); it adds no meter, no pricing table and no durable store.
- **SUBAGENT-08 — narrows against.** The abort-versus-drain response, and what
  partial output survives it, are SUBAGENT-08's by M-1. This PRD detects and
  reports; the two documents carry the boundary in the same words so that only
  one defines a cancellation path.
- **SUBAGENT-02 — narrows against.** Per-type model, effort and context sizing
  are A-2's; this PRD prices the outcome and states that attribution is to the
  parent model until A-2 lands.
- **SUBAGENT-09 — narrows against.** The canonical event and transcript schema
  is A-9's. This PRD adds fields to an existing event and to the pool state; it
  does not define their schema, redaction policy or retention.
- **PRD-233 — narrows against.** Per-agent profiles are PRD-233's, including any
  per-agent budget default. This PRD is per-pool and per-call only, and says so
  in §5 so the profile system does not inherit a competing notion of a limit.

## A. Competitor evidence

The weakest area of the nine on competitor evidence — the research report's own
verdict for A-6 is *Weak (T2 not met)* (§5.4 and the appendix table) — stated as
such rather than smoothed over. Concurrency caps are common; explicit
token-or-cost budgets are not clearly present in any of the six harnesses, and
two rows below are turn or context budgets being read as adjacent evidence, not
as spend budgets. Absence in a cloned repository is a bounded claim about a
commit; absence in docs is weaker, and is labelled.

| Harness | Evidence | Depth | What it does about spend and limits | Take or reject |
|---|---|---|---|---|
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | Per-subagent `maxTurns`, a run-level `--max-turns` for the whole headless query, a separate concurrency limit of 20 running subagents (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`) that fails the spawn and tells the model not to retry, and a 15k-token *description* budget — context, not spend | Take the distinction between per-child and per-run turn budgets and the "state the limit in the failure text" habit (goal 7); reject the description budget as budget evidence, and reject an env-var-only ceiling in favour of a declared one |
| Codex CLI | `codex-rs/core/src/session/multi_agents.rs:59` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | `max_concurrency: config.max_concurrent_threads_per_session` — fan-out is a configuration integer, with a TUI warning for high concurrency under high reasoning effort; no token or cost budget located in the multi-agent surface | Take a configured ceiling that the call cannot raise (goal 5) and the warning-before-the-wall instinct; reject nothing here, but note the ceiling is concurrency, not spend |
| opencode | `packages/schema/src/agent.ts:23-35` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | `steps` is a per-agent turn budget; no token, cost or wall-clock budget was located for subagents in the cloned tree | Take the declared-on-the-record shape as the precedent for *declaring* a limit at all; record the absence of a spend limit as absence, not as parity |
| Qoder CLI | `https://docs.qoder.com/cli/subagent` fetched 2026-10-29 | docs-only | `maxTurns` per subagent plus a run-level `--max-turns`, the same shape as Claude Code, documented without a cost model | Take only the corroboration that a per-child and a per-run bound are usually both present; reject as independent evidence — two docs describing one lineage is weaker than one code read (§2.1's caveat) |
| Grok CLI | `fb97af83f06dca873281d60168430f06c8de6324` | code | No fan-out cap, no depth cap and no budget located in the cloned tree; the delegate surface is a read-only single call | Record as the counter-example: a harness where the absence is real, which bounds how strongly "everyone budgets" can be claimed |
