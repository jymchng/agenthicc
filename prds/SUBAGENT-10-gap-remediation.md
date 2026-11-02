---
title: "SUBAGENT-10: Gap remediation for the SUBAGENT series"
status: Proposed
version: 0.1.0
date: 2026-10-31
scope: "the concrete, file-scoped work needed to close the gaps found when SUBAGENT-01…06 were evaluated against their own §4 goals and §6 acceptance criteria; not a redesign of the series"
area: A-10
related_prds:
  - SUBAGENT-01
  - SUBAGENT-04
  - SUBAGENT-05
  - SUBAGENT-06
  - SUBAGENT-08
  - PRD-157
  - AGX-09
tags:
  - subagents
  - remediation
  - budgets
  - permissions
  - testing
---

# SUBAGENT-10: Gap remediation for the SUBAGENT series

## 1. Executive summary

Commit `733018f` (and its successors) announce *"all SUBAGENTS PRDs fully
implemented"*. Evaluation at `390cfc1` of the first six PRDs against their own
§4 goals and §6 acceptance criteria does not support that claim uniformly. Two
PRDs are essentially complete (SUBAGENT-02, -03), two are complete in behaviour
but not in the checks their criteria name (SUBAGENT-04, -05), one has a single
undelivered field threading goal (SUBAGENT-01), and **one — SUBAGENT-06 — is not
implemented at all**: nine of ten goals and ten of eleven criteria are missing,
and the subsystem that occupies its settings block does the thing its §5
forbids.

This document is the remediation plan. It is deliberately narrow: it does not
re-design any mechanism the series already ships, and it does not restate the
series' goals. Each item below cites the source line that proves the gap and the
test file that must change to close it.

Three tiers, in the order they should ship:

1. **Substance** — SUBAGENT-06's accounting, declaration and reporting surface,
   plus the SUBAGENT-01 description threading bug. These change behaviour and
   are the only items that can lose data or mislead a user today.
2. **Divergence** — SUBAGENT-04 criterion 6, where the code refuses where the
   criterion requires a clamp, and the test was inverted to match. One decision
   (amend the criterion or restore the clamp) must be taken, not left standing.
3. **Conformance** — SUBAGENT-05 criterion 8 (absent), the missing worker-level
   capability-denial test, and the systematic placement of new cases in files
   their criteria do not name.

## 2. Problem statement

Evaluated at `390cfc1`, branch `integrate-agx-sequence-20261030`, clean tree.

| PRD | §4 goals (D/P/M) | §6 ACs (D/P/M) | Remediation load |
|---|---|---|---|
| SUBAGENT-01 | partially delivered | — | R5: `description` never threaded |
| SUBAGENT-02 | delivered | — | none raised here |
| SUBAGENT-03 | delivered | — | none raised here |
| SUBAGENT-04 | 9 / 1 / 0 | 8 / 3 / 0 | R3 (AC-6), R6 (AC-2, AC-11) |
| SUBAGENT-05 | 11 / 0 / 0 | 7 / 3 / 1 | R4 (AC-8, AC-4), R6 |
| SUBAGENT-06 | 0 / 1 / 9 | 0 / 1 / 10 | R1, R2, R6 |

SUBAGENT-07, -08 and -09 have not been evaluated and contribute no items here;
§5 states that boundary and §9 says what happens when they are. The §6 column
is filled only for the three PRDs whose criteria were counted line by line in
the current pass (-04, -05, -06); -01, -02 and -03 are recorded by the item they
leave behind, not restated as totals.

**The pattern, not the instances.** Three of the six gaps are not missing
functionality — they are missing *evidence*. A test was written, it passes, and
it lives somewhere the criterion does not name, so an auditor reading §6
concludes the work is absent. That is a real defect in a specification-driven
tree: the document is the acceptance contract, and a contract that cannot be
checked against the artefacts is not one.

**The one that matters most.** SUBAGENT-06 asked for measurement and reporting,
and explicitly refused enforcement (`prds/SUBAGENT-06-pool-budgets-and-limits.md`
§5: "it may not stop a worker, drain a pool, or define what survives a stop").
What shipped is a pool resource governor that *does* stop workers, with an
`on_budget_exceeded: abort|drain` knob in `subagents.*`. So a parent still
cannot learn what a pool cost, and a pool can now be cancelled by a limit the
model never declared. The measurement half of the PRD exists in embryo and is
thrown away: `self._token_count` is accumulated at `pool.py:2152` and compared
at `:2153`, and appears nowhere else in the module.

## 3. Current-source investigation

Run at `390cfc1`. Each block is the evidence for the item that follows it.

**R1 — no per-pool accounting, and the ledger key cannot support one.**

```console
$ grep -rn "UsageCategory.SUBAGENT\|category=\"subagent\"" src/ --include=*.py
src/agenthicc/subagents/pool.py:1604:                category="subagent",

$ grep -c "pool" src/agenthicc/runners/usage_ledger.py
0

$ sed -n '1601,1604p' src/agenthicc/subagents/pool.py
                run_id=f"{self._parent_run_id}:subagent:{self._task.task_id}",
```

The run id still carries `task-{i}` (`tool.py:245`), so two `spawn_subagents`
calls in one turn write identical run ids and no per-pool total is computable
even in principle. `UsageSnapshot` (`usage_ledger.py:269-286`) is session-scoped
and has no pool dimension.

**R1 — no budget argument, no ceiling, no attestation, no report.**

```console
$ grep -rn "max_budget" src/ tests/ --include=*.py
(no output)

$ sed -n '1249,1254p' src/agenthicc/config.py
    max_tokens: int = 200_000
    max_total_time_s: float = 600.0
    on_budget_exceeded: Literal["abort", "drain"] = "drain"
    stop_grace_s: float = 5.0
```

`SubagentResult` (`pool.py:318-333`) has `duration_ms` and `tool_calls` but no
tokens or cost. `AggregatedResult` (`:601-611`) has `budget_exceeded_reason` but
no totals. `SubagentPoolState` publishes `limit` — the concurrency bound — and
nothing about spend. The `SubagentPoolCompleted` payload is
`pool_id/total/succeeded/failed/text/budget_exceeded_reason`
(`pool.py:3135-3147`).

**R2 — the response half is implemented, in the block §5 assigns to A-8.**

```console
$ sed -n '2126,2141p' src/agenthicc/subagents/pool.py
    def budget_exceeded(self, reason: str) -> tuple[WorkerLifecycle, ...]:
        """Apply the configured stop policy once when a pool budget is crossed."""
        ...
        self._budget_stop_event.set()
        return self.request_pool_stop(
            mode=self._on_budget_exceeded,
            source="budget",
            reason=reason_value,
        )
```

This is a working, tested cancellation path (`tests/unit/test_subagent_budgets.py`
asserts `stop_request.mode == "drain"`, `source == "budget"`). It is not a bug.
It is a scope substitution: SUBAGENT-08 has not been evaluated yet and will
otherwise appear to own a feature already shipped under another PRD's name.

**R3 — SUBAGENT-04 criterion 6: the code refuses what the criterion says to
lower.**

```console
$ sed -n '335,343p' src/agenthicc/subagents/tool.py
        if max_concurrent > concurrency_ceiling:
            return {
                "ok": False,
                "status": "refused",
                "error": (
                    f"requested max_concurrent={max_concurrent} exceeds configured "
                    f"max_concurrent={concurrency_ceiling} from {concurrency_limit_source}"
                ),
```

The criterion (`prds/SUBAGENT-04-nesting-depth-and-fan-out.md:340`) requires the
value be *lowered to the ceiling* with the used value reported. Commit
`a64848c` renamed the test
`test_nested_depth_tier_clamps_concurrency_and_reports_inherited_limit` to
`…_refuses_concurrency_above_inherited_limit` and inverted its assertions
(`tests/integration/test_subagent_tool_execution.py:905`), so the suite is green
against behaviour the criterion does not describe.

**R4 — SUBAGENT-05 criterion 8 does not exist, and criterion 4 is asserted
against a hand-built gate.**

```console
$ grep -c "deny_tools\|deny_capabilities\|require_approval" \
      tests/unit/test_subagent_plugin.py tests/unit/test_subagent_pool.py \
      tests/unit/test_agent_permissions.py
0

$ grep -n "declared\|restrict(declared)" tests/unit/test_agent_permissions.py
(no output)

$ git diff --stat b8c3625^ HEAD -- tests/unit/test_agent_permissions.py
(no output: unchanged)
```

All 14 permission cases live in a new file, `tests/unit/test_subagent_permissions.py`,
while the criteria name `test_subagent_plugin.py`, `test_subagent_pool.py` and
`test_agent_permissions.py`. The merge-table case the criterion requires is
absent; the capability-denial case (`:134-158`) constructs
`ToolCapabilityGate(...)` directly instead of observing a worker whose
`_filtered_tools()` still contains the denied tool.

**R5 — SUBAGENT-01: every definition-loaded type reaches the model
undescribed.**

```console
$ sed -n '2001,2019p' src/agenthicc/runners/agent_turn.py
            result.register(
                SubagentTypeSpec(
                    name=definition.name,
                    allowed_tools=allowed_names,
                    ...
                    output_schema=definition.output_schema,
                    permissions=permissions,
                )
            )

$ sed -n '1093,1093p' src/agenthicc/subagents/tool.py
            text = spec.description or "No description provided."
```

No `description=` argument is passed, so `SubagentTypeSpec.description` keeps its
`""` default and the model sees "No description provided." for every declared
type — the exact outcome SUBAGENT-01 criterion 2 exists to prevent.

**R6 — new cases are systematically in files their criteria do not name.**
SUBAGENT-04 criterion 2 (named `test_subagent_plugin.py`, written in
`test_subagent_pool.py:709`); SUBAGENT-04 criterion 11 and SUBAGENT-05
criterion 11 both assert their named suites pass "unchanged", and both named
suites changed (`test_subagent_pool.py` +787, `test_subagent_tool_execution.py`
+389/−4, `test_subagent_pool_runtime_coverage.py` +82/−6).

**R7 — behaviour the documents do not describe.** A second, distinct per-depth
table (`depth_limits` at `config.py:1255` vs `depth_tiers` at `:1256`, resolved
by different code at `:1345-1359` and `governance.py:38-84`); the pool cap is a
rolling minimum across active pools (`pool.py:2382-2383`), so one
low-configured pool tightens every other; `max_concurrent` clamps from below
(`tool.py:263`) and refuses from above (`:335`).

## 4. Goals

**Substance**

1. **Per-pool accounting, read from the ledger, not from a second meter.** The
   worker run id gains the pool id
   (`…:{pool_id}:subagent:{task_id}`, replacing `pool.py:1601`, `:1673`) so a
   pool's records are selectable; a pool total sums input, output, cache-read
   and cache-write tokens, and sums cost only over records whose `cost_status`
   is known, propagating unknown cost as *some unknown*, never as zero.
   Aggregation reads the ledger's completed records and excludes provisional
   ones by its own lifecycle flag (`usage_ledger.py:196-202`), so the total is
   monotone while workers run and never disagrees with the session total it is a
   subset of. (SUBAGENT-06 goals 1, 2, 3.)
2. **A declared limit the call may lower and never raise.** `spawn_subagents`
   accepts an optional `budget:` mapping of `tokens`, `cost_usd` and
   `wall_clock_s`, any subset; absent means today's behaviour plus empty
   attestation. A settings ceiling — `subagents.max_budget`, which does not
   exist today — caps the declaration and is applied by *clamping downward and
   reporting the clamp*, following the sibling idiom at
   `src/agenthicc/worktrees/agent_tool.py:55`, `:100`. A malformed budget
   (zero, negative, non-integer tokens, non-finite cost) is refused before any
   worker is constructed, naming the field. (SUBAGENT-06 goals 4, 5.)
3. **Attestation and reporting on the surfaces that already exist.** Each
   `SubagentResult` carries its own tokens, cost and cost status;
   `AggregatedResult` carries the pool totals; the `SubagentPoolCompleted`
   payload, the tool's text digest and `SubagentPoolState` each carry
   usage-versus-limit as a superset of today's fields, and the digest says
   *crossed* when a declared limit was passed. The panel renders pool spend
   through PRD-157's existing signals — no second widget, no second pricing
   table. (SUBAGENT-06 goals 6, 7, 8.)
4. **SUBAGENT-01 description threading.** `SubagentTypeSpec` is constructed with
   `description=definition.description` at
   `runners/agent_turn.py:2001-2019`, so the spawn tool's description block and
   schema text carry each declared type's own description instead of "No
   description provided." (SUBAGENT-01 criterion 2.)

**Divergence**

5. **Resolve SUBAGENT-04 criterion 6 one way or the other.** Because refusal is
   stricter than clamping and already tested, the recommended resolution is to
   **amend the criterion** to specify refusal, keep `tool.py:335-343`, and
   restore the test name
   `test_nested_depth_tier_refuses_concurrency_above_inherited_limit`'s
   assertions to include the limit source it already checks. The alternative —
   restoring clamping — is acceptable only if the clamp value is reported in
   `max_concurrent_used` and a refusal test is kept for the *task* ceiling so
   the two behaviours stay distinguishable. Either way, **the criterion and the
   code must not be left disagreeing while the suite reads green.**

**Conformance**

6. **Add SUBAGENT-05 criterion 8**: a table of declarations asserted against
   `inherited.restrict(declared)` — in `tests/unit/test_agent_permissions.py`,
   extending the existing `TestAgentCapabilityScope` cases rather than
   duplicating them, as that PRD's §8 already instructs.
7. **Add the worker-level capability-denial case**: a worker whose
   `_filtered_tools()` still contains a denied-capability tool, refused through
   the worker's own hook chain (`pool.py:1507-1519`), not through a hand-built
   `ToolCapabilityGate`.
8. **Put each case in the file its criterion names**, or amend the criterion in
   the same commit. New permission cases move to (or are mirrored in)
   `test_subagent_plugin.py` / `test_subagent_pool.py`; SUBAGENT-04 criterion 2
   gains a `test_subagent_plugin.py` case.
9. **Make the "unchanged" claims checkable.** Where a criterion asserts a suite
   passes unchanged, either the suite is genuinely untouched or the criterion is
   reworded to name the cases that were added. SUBAGENT-04 criterion 11 and
   SUBAGENT-05 criterion 11 are both false as written.
10. **Document the undocumented.** §7 of SUBAGENT-04 gains the
    `depth_limits`/`depth_tiers` distinction and the rolling-minimum pool cap;
    SUBAGENT-06 gains a note that its settings block is occupied by the
    resource governor.

## 5. Non-goals

- **SUBAGENT-07, -08, -09 remediation.** Not evaluated yet. R2 deliberately
  does *not* remove `budget_exceeded`, `on_budget_exceeded`, `stop_grace_s` or
  `test_subagent_budgets.py`: they are working behaviour, and deleting a tested
  mechanism to satisfy a boundary between two documents is the wrong repair.
  §9 states how the ownership question is settled instead.
- **A second meter.** No new token counter, no pricing table, no reconciliation
  path. PRD-157 owns accounting; this document aggregates its records.
- **Per-type budgets on the definition record.** Budgets remain per-pool
  policy; the AGX-09 key set is not extended.
- **Re-litigating delivered mechanisms.** SUBAGENT-04's two depth guards,
  SUBAGENT-05's narrow-only invariant and SUBAGENT-03's submission tool are
  working and stay as they are.
- **Pricing, forecasting or vendor tables.** Cost is whatever the ledger
  recorded, with its provenance attached.

## 6. Acceptance criteria

1. A pool's token total equals the sum of its workers' ledger records and the
   session total is unchanged by the aggregation: `tests/unit/test_usage_ledger.py`.
2. Two `spawn_subagents` calls in one parent run produce disjoint pool totals,
   because the run id carries the pool id: `tests/unit/test_subagent_pool.py`.
3. A pool whose records carry `cost_status="unavailable"` reports cost as
   unknown rather than `$0.00`, and declaring a `cost_usd` budget against it is
   refused with the reason named: `tests/unit/test_usage_ledger.py`.
4. Provisional records are excluded, so a total taken mid-run is monotone and a
   reconciled call is not counted twice: `tests/unit/test_usage_ledger.py`.
5. `spawn_subagents(budget={"tokens": 1000})` lowers the effective limit; a
   declaration above `subagents.max_budget` is clamped to the ceiling and the
   clamp is reported in the result: `tests/unit/test_subagent_pool.py`.
6. A malformed budget is refused before any worker starts, naming the field, and
   a call with no `budget:` behaves exactly as today:
   `tests/integration/test_subagent_tool_execution.py`.
7. Each `SubagentResult` reports its own tokens and duration and
   `AggregatedResult` the pool total, so a pool of two unequal workers attributes
   spend to the right worker: `tests/unit/test_subagent_pool.py`.
8. The `SubagentPoolCompleted` payload and the tool's text digest both carry
   usage-versus-limit, and the text says *crossed* when a declared limit was
   passed: `tests/integration/test_subagent_tool_execution.py`.
9. `SubagentPoolState` publishes the pool's tokens, cost and declared limit, and
   the panel renders them through PRD-157's existing signals:
   `tests/unit/test_subagent_panel_agx08.py`.
10. Crossing a declared limit records the crossing and cancels nothing: worker
    counts, statuses and returned text are identical to the same run without a
    limit: `tests/unit/test_subagent_pool.py`.
11. A definition-loaded type appears in the spawn tool's description with its own
    description text, not "No description provided":
    `tests/unit/test_subagent_plugin.py`.
12. SUBAGENT-04 criterion 6 and `tool.py:335-343` state the same behaviour, and
    the test that pins it asserts what the criterion says:
    `tests/integration/test_subagent_tool_execution.py`.
13. The `inherited.restrict(declared)` merge table exists in
    `tests/unit/test_agent_permissions.py` and asserts against
    `AgentCapabilityScope.restrict()` rather than a reimplementation.
14. A worker whose filtered tool list still contains a denied-capability tool is
    refused by the worker's own `ToolCapabilityGate`, asserted through the pool:
    `tests/unit/test_subagent_pool.py`.
15. Every case named by a §6 criterion lives in the file that criterion names,
    and every "passes unchanged" claim is true of a byte-identical suite:
    verified by the §8 command list plus `git diff` over the named files.
16. `pytest tests/unit/test_usage_ledger.py tests/unit/test_subagent_pool.py
    tests/unit/test_subagent_plugin.py tests/unit/test_agent_permissions.py
    tests/unit/test_subagent_permissions.py tests/unit/test_subagent_panel_agx08.py
    tests/integration/test_subagent_tool_execution.py -q` passes, and every
    pre-existing case in those files still passes unedited except where a
    criterion above requires a change.

## 7. Rollout, migration and security

**Ordering.** Ship in the tier order of §1: R1–R4 (accounting and declaration)
first, because they are the only items that change what a user sees; then R5
(description threading, a one-line behaviour fix with a visible model-facing
effect); then the divergence decision; then the conformance work, which changes
no behaviour at all and can land in any order.

**Migration.** R1's run-id change is additive in meaning: `run_id` is a string
key in the ledger, not a join, so existing records keep resolving and the new
segment only makes future records pool-selectable. R2's `budget:` argument is
optional and defaults to absent; `subagents.max_budget` ships unset, and — the
lesson of SUBAGENT-06 §7 — **"unset" must mean a code path in which nothing is
armed**, asserted by a test, not a sentence in a document.

**Default-path honesty.** SUBAGENT-06's own claim that "an undeclared budget is
inert" is false of the shipped tree, because `max_tokens=200_000`,
`max_total_time_s=600.0` and `on_budget_exceeded="drain"` are armed for every
pool (`config.py:1249-1254`). Either those limits are documented as defaults
that do stop a pool, or they ship disarmed for the pool case. A criterion in
§6 must pin whichever answer is chosen.

**Security and honesty.**

- *Unknown is not zero.* Cost carries the ledger's provenance
  (`AUTHORITATIVE`, `ESTIMATED`, `UNAVAILABLE`); a `cost_usd` budget on an
  unpriced model is a declaration error, not a silent no-op.
- *The ceiling binds downward only.* A call may lower `subagents.max_budget` and
  never raise it, and the clamp is reported rather than silent.
- *No prompt text in the report.* Pool accounting carries counts, durations,
  labels and types — the same shape `SubagentPoolCompleted` has today — so a
  budget report can be logged without a redaction pass.
- *Detection is not enforcement.* The digest must name SUBAGENT-08 as the owner
  of any stop action, so a parent asks for the right thing instead of assuming a
  limit already stopped something.

## 8. Verification

```console
$ git rev-parse HEAD   # record the remediation baseline
$ pytest tests/unit/test_usage_ledger.py -q
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_plugin.py -q
$ pytest tests/unit/test_agent_permissions.py -q
$ pytest tests/unit/test_subagent_permissions.py -q
$ pytest tests/unit/test_subagent_panel_agx08.py -q
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/integration/test_subagent_tool_execution.py -q
$ pytest tests/integration/test_subagent_executor_integration.py -q
```

Green at `390cfc1`, before any remediation — measured, one file per run:

| File | Passed |
|---|---|
| `tests/unit/test_usage_ledger.py` | 12 |
| `tests/unit/test_subagent_pool.py` | 79 |
| `tests/unit/test_subagent_plugin.py` | 20 |
| `tests/unit/test_agent_permissions.py` | 21 |
| `tests/unit/test_subagent_permissions.py` | 14 |
| `tests/unit/test_subagent_panel_agx08.py` | 25 |
| `tests/unit/test_subagent_pool_runtime_coverage.py` | 3 |
| `tests/integration/test_subagent_tool_execution.py` | 23 |
| `tests/integration/test_subagent_executor_integration.py` | 1 |

The counts are recorded so that a later reader can tell an added case from a
moved one — the distinction three of the gaps above turn on.

## 9. Related and superseded PRDs

- **SUBAGENT-06 — completed by this document.** R1–R3 are its goals 1–8
  restated as implementable work. Its goal 9 ("detection, not response") is
  honoured by criterion 10 and by leaving the existing stop path where it is.
- **SUBAGENT-08 — owns the response.** When it is evaluated, the shipped
  `budget_exceeded()` / `on_budget_exceeded` path must be attributed to it
  explicitly, and either SUBAGENT-06 §5 or SUBAGENT-08 §4 must be amended so the
  series has one owner for "what happens when a limit is crossed". This
  document does not decide which; it records that the two must not both claim
  it.
- **SUBAGENT-04 — criterion 6 entry point.** R3's decision amends either that
  criterion or `tool.py:335-343`; the change lands in one commit with the test.
- **SUBAGENT-05 — criterion 8 entry point.** R6 adds the merge table its §8
  already asked for.
- **PRD-157 — supersedes nothing.** Accounting stays canonical there; this
  document aggregates its records and adds no meter.
- **SUBAGENT-01 — one field.** R4 is a threading fix, not a revision of that
  document.
