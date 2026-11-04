---
title: "SUBAGENT-10: Gap remediation for the SUBAGENT series"
status: Proposed
version: 0.2.0
date: 2026-11-03
scope: "the decisions and file-scoped work needed to close the gaps found when SUBAGENT-01…09 were evaluated against their own §4 goals and §6 acceptance criteria at the commit that put them on main; not a redesign of the series"
area: A-10
related_prds:
  - SUBAGENT-01
  - SUBAGENT-02
  - SUBAGENT-03
  - SUBAGENT-04
  - SUBAGENT-05
  - SUBAGENT-06
  - SUBAGENT-07
  - SUBAGENT-08
  - SUBAGENT-09
  - SUBAGENT-11
  - PRD-157
  - PRD-148
  - AGX-09
tags:
  - subagents
  - remediation
  - budgets
  - permissions
  - security
  - testing
---

# SUBAGENT-10: Gap remediation for the SUBAGENT series

## Revision 0.2.0 (2026-11-03)

Version 0.1.0 evaluated SUBAGENT-01…06 at `390cfc1` and explicitly deferred
SUBAGENT-07, -08 and -09. This revision:

1. **Re-runs every 0.1.0 check at `0a63bfb`**, the merge of PR #6 that put the
   series on `main`. No file under `src/agenthicc/subagents/`,
   `src/agenthicc/agents/`, `src/agenthicc/config.py`,
   `src/agenthicc/result_contracts.py` or `src/agenthicc/runners/agent_turn.py`
   changed between the two commits (`git diff --stat 390cfc1..0a63bfb` over
   those paths is empty), so every R1–R7 finding and every line number below
   still holds, and the §8 test counts are unchanged.
2. **Evaluates SUBAGENT-07, -08 and -09** (§3, R8–R16) and records the result in
   each of those documents' implementation-status sections.
3. **Takes the two decisions 0.1.0 left open** — SUBAGENT-04 criterion 6 and the
   M-1 ownership of the shipped budget governor — as D-1 and D-2 in §4, so they
   are no longer "must be decided".
4. **Separates what this document fixes from what it hands on.** Items that
   finish a delivered mechanism stay here as work packages WP-1…WP-9 (§7). The
   unfinished halves of SUBAGENT-07 and -08 — model-initiated background,
   non-blocking collection, background clarification and model-facing worker
   controls — are product scope rather than remediation and move to
   **SUBAGENT-11**. The two defects found in that code path that can mislead the
   parent today (B-1, B-2) are scheduled here because they are bugs in shipped
   behaviour, and SUBAGENT-11 builds on the fix.

Every implementation-status section added to SUBAGENT-01…09 in this revision
cites the same commit and points back at the work-package ids defined here.

## Delivery log

Work packages ship in waves, each as its own pull request to `main`. A wave's
items move to *delivered* here when its PR is opened and green; the owning
PRD's `status:` is promoted only when the PR is merged (research report §6.6).

| Wave | Branch | Work packages | Criteria pinned |
|---|---|---|---|
| 1 | `subagent-wave-1-redaction-and-fixes` | WP-1 (B-3 redaction and visible truncation in every scroll and kernel payload, including task text and errors), WP-2 (definition descriptions reach the spawn tool), WP-3 (D-1, documentation), WP-4 (armed operator ceilings pinned against the guide and SUBAGENT-06), WP-9 (refusals no longer report `max_concurrent_used`; guide corrected from "clamped" to "refused") | 11, 12, 17, 18, 23, 27 |
| 2 | `subagent-wave-2-background-delivery` | WP-5 (B-1: budget-ended pools journaled as `stopped` with counts, text, values and reason; delivery and turn-resume report the real cause; session-close `cancelled` records now keep their counts too), WP-6 (B-2: delivery composed by `agenthicc.subagents.delivery`, worker text framed as untrusted, redacted, bounded at 16,000 characters, frame-closing tag neutralised), WP-8 (SUBAGENT-05 criteria 4 and 8, SUBAGENT-08 criteria 5 and 8, test-location amendments in SUBAGENT-04/05/07/08) | 13, 14, 15, 19, 20, 21, 22, 25 |
| 3 | `subagent-wave-3-pool-budgets` | WP-7 (SUBAGENT-06 goals 1–8, 10: pool-scoped worker run ids, ledger-derived worker and pool usage, `budget:` argument with validation, `subagents.max_budget` downward clamp, cost metering and `max_cost_usd_exceeded`, usage-versus-limit report on the tool result, the completion events, the `results` digest and the panel); ledger fix for cost labelled `unavailable` on every non-streaming call; WP-4 criterion 24 | 1–10, 24 |
| 4 | `subagent-wave-4-collection-and-notices` | SUBAGENT-11 goals 2, 3, 4, 5, 9: supervised-only background pools, non-blocking idempotent `collect_subagent_results` with a bounded wait and journal fallback, notice-then-pull delivery (supersedes WP-6's framing), shared delivery for TUI and background sessions; R13 corrected | SUBAGENT-11 criteria 2–9, 17 (as delivery parity), 18 |
| 5 | `subagent-wave-5-model-pool-control` | SUBAGENT-11 goals 1, 6, 7, 8, 10: `spawn_subagents(background=True)`, `subagents.on_clarification` with id-only question notices, `list_subagent_pools` / `stop_subagents` / `retry_subagent` over the existing stop and retry functions, lineage-scoped control for nested workers, a bounded conversation registry that keeps older pools retryable; three shipped defects fixed (below) | SUBAGENT-11 criteria 1, 10–16 |

Wave 1 found a third leak site beyond §3 R15: `SubagentPoolCompleted.text`
carried the complete aggregate, unbounded and unredacted
(`src/agenthicc/subagents/pool.py:3143` at `0a63bfb`), and `SubagentStarted` /
`SubagentPoolStarted` carried the parent's task text unredacted. All are now
routed through one helper, `SubagentPool._display_fields`. WP-4's second
criterion (24, governor equals ledger) depends on the pool-scoped run id and
moves to WP-7.

Wave 2 found that the session-close `cancelled` record had the same defect as
B-1: `_record_unfinished_subagent_workers` computed the counts and text and the
record discarded them, so a pool cancelled at shutdown was delivered as
"0/0 succeeded". It now keeps them, as `interrupted` already did. It also found
that turn-resume (`tool.py:483-517`) read the same `cancelled` record, so a
re-driven turn after a budget stop reported every worker as failed with no
text; the `stopped` record fixes both paths.

Wave 5 found three defects in shipped behaviour. (1) A worker's first attempt
ignored its type's declared policy (`spec.policy`): only a retry passed it, so
a type that narrowed its tools ran unnarrowed the first time (SUBAGENT-05).
(2) `retry_worker` built the new attempt with a second, partial constructor
call that dropped the provider, model routing, pool id, depth, sinks and
communication, so a retried worker ran outside its pool's scope (SUBAGENT-08).
Both attempts now share `SubagentPool._make_worker`. (3) A pool whose workers
failed before asking anything returned `awaiting_clarification` with no
questions: closing the broker woke the question waiter first. The waiter now
counts only when a parent question is actually pending.

Wave 5 also closes a defect reported from a real session: two documenters were
reported as succeeded while their `write_file` calls had come back from a
DeepSeek gateway as DSML text, so no file landed and the parent had to discard
both replies. A worker whose reply holds call markup for one of its own tools
now gets one corrective turn and otherwise fails with `unexecuted_tool_call`
(`agenthicc.subagents.textual_tool_calls`); new integration tests also pin that
the built-in documenter's real `write_file` writes the file under a session
workspace policy.

## 1. Executive summary

Commit `733018f` (and its successors) announce *"all SUBAGENTS PRDs fully
implemented"*. Evaluation of all nine PRDs against their own §4 goals and §6
acceptance criteria at `0a63bfb` does not support that claim uniformly:

- **Delivered:** SUBAGENT-02 and -03 completely; SUBAGENT-05 in behaviour, with
  two criteria unproven; SUBAGENT-08 in behaviour for every goal, with one
  criterion (stop during an approval) unproven, every case in a file its
  criteria do not name, and every control reachable only from the panel.
- **Delivered with one defect each:** SUBAGENT-01 (declared types reach the
  model with "No description provided."), SUBAGENT-04 (criterion 6 said clamp,
  the code refuses, and the test was inverted to match; this revision amends the
  criterion to refusal, D-1).
- **Phase 1 only:** SUBAGENT-07 shipped *user*-initiated backgrounding with a
  durable lifecycle, restart recovery and exactly-once delivery, but not the
  model-facing half its goals describe.
- **Not implemented:** SUBAGENT-06 (accounting, declaration and report; what
  occupies its settings block is a governor that does the stopping its §5
  forbids) and SUBAGENT-09 (no event envelope, version, failure kind, replay,
  derived counters or degraded-transcript signal).

Three findings change the urgency order of 0.1.0:

- **B-3, a secret-leak path.** Worker text enters the scroll payload
  (`pool.py:2685`) and the `SubagentCompleted`/`SubagentFailed` kernel event
  (`:3104`) unredacted, while the transcript of the same worker is redacted
  (`transcripts.py:267-274`). This is SUBAGENT-09 goal 7's "first fix", and it is
  promoted ahead of every other SUBAGENT-09 item.
- **B-1, a misreport.** A backgrounded pool stopped by the governor is journaled
  as `cancelled` with no counts and no text, and is then delivered to the parent
  as "was cancelled when the session closed (0/0 succeeded, 0 failed)" — wrong
  reason, wrong counts, and the drained workers' results dropped.
- **B-2, a trust escalation.** A background result reaches the parent as a
  *user*-role message carrying the raw, unbounded aggregate, where the same
  result delivered in the foreground is a tool result.

This document is the remediation plan. It re-designs no mechanism the series
ships. Each item cites the source line that proves the gap and the test file
that must change to close it.

## 2. Problem statement

Evaluated at `0a63bfba1dca68e61a06fe3b9acb4f536637396b` (the merge of PR #6),
branch `main`, clean tree. D/P/M is
delivered/partial/missing.

| PRD | §4 goals (D/P/M) | §6 ACs (D/P/M) | Status after this revision | Remediation load |
|---|---|---|---|---|
| SUBAGENT-01 | 6 / 1 / 0 | 9 / 0 / 1 | `Implemented (phase 1)` | WP-2 (R5) |
| SUBAGENT-02 | 9 / 0 / 0 | delivered | `Implemented (2026-11-02)` | none |
| SUBAGENT-03 | delivered | delivered | `Implemented (2026-10-30)` | none (B-2 touches its envelope, see WP-6) |
| SUBAGENT-04 | 9 / 1 / 0 | 8 / 3 / 0 | `Implemented (phase 1)` | WP-3 (D-1), WP-8, WP-9 |
| SUBAGENT-05 | 11 / 0 / 0 | 7 / 3 / 1 | `Implemented (2026-11-02)` | WP-8 |
| SUBAGENT-06 | 0 / 1 / 9 | 0 / 1 / 10 | `Proposed` | WP-4 (D-2), WP-7 |
| SUBAGENT-07 | 2 / 5 / 2 | 4 / 4 / 5 | `Implemented (phase 1)` | WP-5, WP-6; rest → SUBAGENT-11 |
| SUBAGENT-08 | 11 / 0 / 0 | 12 / 1 / 0 | `Implemented (phase 1)` | WP-8; model-facing controls → SUBAGENT-11 |
| SUBAGENT-09 | 1 / 3 / 8 | 2 / 2 / 10 | `Proposed` | WP-1 (B-3 only) |

The -07, -08 and -09 rows were counted line by line in this revision and -01's
row was recounted, **by behaviour**: a criterion whose behaviour exists but whose
case lives in a file the criterion does not name counts as delivered here and is
tracked by R6/WP-8 instead. The -04 and -05 rows keep 0.1.0's counts, which
counted such a case as partial; their status sections mark which partials are
placement-only, so the two methods can be reconciled without a recount. -02
and -03 are recorded by what they leave behind, not restated as totals. The
per-goal and per-criterion verdicts behind every row are in each PRD's
implementation-status section.

**The pattern, not the instances.** Three of the 0.1.0 gaps and one of the new
ones are not missing functionality — they are missing *evidence*. A test was
written, it passes, and it lives somewhere the criterion does not name
(`tests/unit/test_subagent_permissions.py`, `tests/unit/test_subagent_lifecycle.py`,
`tests/unit/test_subagent_budgets.py`, `tests/unit/test_conversation_journal.py`),
so an auditor reading §6 concludes the work is absent. In a
specification-driven tree the document is the acceptance contract; a contract
that cannot be checked against the artefacts is not one.

**The ones that matter most.** In order: B-3 (secrets in kernel and scroll
payloads, `src/agenthicc/subagents/pool.py:2685`, `:3104`); B-1 and B-2 (the
background delivery misreports and escalates trust,
`src/agenthicc/memory/journal.py:1065-1087`,
`src/agenthicc/runners/tui_session.py:4344-4351`); then SUBAGENT-06, which asked
for measurement and reporting and explicitly refused enforcement
(`prds/SUBAGENT-06-pool-budgets-and-limits.md` §5: "it may not stop a worker,
drain a pool, or define what survives a stop"). What shipped is a governor that
*does* stop workers, armed for every pool by default
(`src/agenthicc/config.py:1251-1254`), metering with a private counter
(`pool.py:1579-1584`, `:2151-2154`) — so a parent still cannot learn what a pool
cost, and a pool can be drained by a limit the model never declared. In the
foreground the parent is at least told which limit (`status: "budget_exceeded"`
with the reason, `src/agenthicc/subagents/tool.py:763-778`); once the pool is
backgrounded it is told the wrong cause (B-1).

## 3. Current-source investigation

```console
$ git rev-parse HEAD
0a63bfba1dca68e61a06fe3b9acb4f536637396b
$ git merge-base --is-ancestor HEAD origin/main && echo "HEAD on main"
HEAD on main
$ git diff --stat 390cfc1..HEAD -- src/agenthicc/subagents src/agenthicc/agents \
      src/agenthicc/result_contracts.py src/agenthicc/config.py \
      src/agenthicc/runners/agent_turn.py
(no output)
```

Nothing in the evaluated paths moved since 0.1.0, so R1–R7 are re-confirmed
rather than re-derived; the commands that established them are kept below
unchanged because they still reproduce. R8–R16 are new in 0.2.0.

**R1 — no per-pool accounting, and the ledger key cannot support one.**

```console
$ grep -rn "UsageCategory.SUBAGENT\|category=\"subagent\"" src/ --include=*.py
src/agenthicc/subagents/pool.py:1604:                category="subagent",

$ grep -c "pool" src/agenthicc/runners/usage_ledger.py
0

$ sed -n '1601,1604p' src/agenthicc/subagents/pool.py
                run_id=f"{self._parent_run_id}:subagent:{self._task.task_id}",
```

The run id still carries `task-{i}` (`tool.py:383`), so two `spawn_subagents`
calls in one turn write identical run ids and no per-pool total is computable
even in principle. `UsageSnapshot` (`usage_ledger.py:269-286`) is session-scoped
and has no pool dimension.

**R1 — no budget argument, no ceiling, no attestation, no report.**

```console
$ grep -rn "max_budget" src/ tests/ --include=*.py
(no output)

$ sed -n '1251,1254p' src/agenthicc/config.py
    max_tokens: int = 200_000
    max_total_time_s: float = 600.0
    on_budget_exceeded: Literal["abort", "drain"] = "drain"
    stop_grace_s: float = 5.0
```

`SubagentResult` has `duration_ms` and `tool_calls` but no tokens or cost.
`AggregatedResult` has `budget_exceeded_reason` but no totals.
`SubagentPoolState` (`pool.py:347-366`) publishes `limit` — the concurrency
bound — and nothing about spend.

**R2 — the response half is implemented, in the block §5 of SUBAGENT-06
assigns to A-8.**

```console
$ sed -n '2126,2135p' src/agenthicc/subagents/pool.py
    def budget_exceeded(self, reason: str) -> tuple[WorkerLifecycle, ...]:
        """Apply the configured stop policy once when a pool budget is crossed."""
```

`budget_exceeded` calls `request_pool_stop(mode=self._on_budget_exceeded,
source="budget", …)`. This is a working, tested cancellation path
(`tests/unit/test_subagent_budgets.py:120-200`). It is not a bug; it is a scope
substitution that D-2 now settles.

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
$ grep -n "def test_nested_depth_tier" tests/integration/test_subagent_tool_execution.py
905:async def test_nested_depth_tier_refuses_concurrency_above_inherited_limit() -> None:
```

The criterion (`prds/SUBAGENT-04-nesting-depth-and-fan-out.md:340`) requires the
value be *lowered to the ceiling* with the used value reported. Commit
`a64848c` renamed the clamping test to `…_refuses_…` and inverted its
assertions, so the suite is green against behaviour the criterion does not
describe. D-1 resolves it.

**R4 — SUBAGENT-05 criterion 8 does not exist, and criterion 4 is asserted
against a hand-built gate.**

```console
$ grep -c "deny_tools\|deny_capabilities\|require_approval" \
      tests/unit/test_subagent_plugin.py tests/unit/test_subagent_pool.py \
      tests/unit/test_agent_permissions.py tests/unit/test_subagent_permissions.py
tests/unit/test_subagent_plugin.py:0
tests/unit/test_subagent_pool.py:0
tests/unit/test_agent_permissions.py:0
tests/unit/test_subagent_permissions.py:16
```

All permission cases live in `tests/unit/test_subagent_permissions.py`, while
the criteria name the other three. The `inherited.restrict(declared)` merge
table is absent; the capability-denial case constructs `ToolCapabilityGate(...)`
directly instead of observing a worker whose filtered tools still contain the
denied tool and whose own hook chain (`pool.py:1507-1519`) refuses it.

**R5 — SUBAGENT-01: every definition-loaded type reaches the model
undescribed.**

```console
$ sed -n '2001,2018p' src/agenthicc/runners/agent_turn.py   # SubagentTypeSpec(...) call
$ grep -n "description=" <(sed -n '2001,2018p' src/agenthicc/runners/agent_turn.py)
(no output)
$ sed -n '1093p' src/agenthicc/subagents/tool.py
            text = spec.description or "No description provided."
```

`SubagentTypeSpec.description` (`types.py:113`) keeps its `""` default because
no `description=` is passed; the definition's description is used only as a
fallback system prompt (`agent_turn.py:2011`). The model sees "No description
provided." for every declared type — the outcome SUBAGENT-01 criterion 2 exists
to prevent.

**R6 — new cases are systematically in files their criteria do not name.**
SUBAGENT-04 criterion 2 (named `test_subagent_plugin.py`, written in
`test_subagent_pool.py:709`); SUBAGENT-04 criterion 11 and SUBAGENT-05
criterion 10 both assert their named suites pass "unchanged", and both named
suites changed. (0.1.0 called the latter criterion 11; it is criterion 10.) 0.2.0 adds SUBAGENT-08: every lifecycle case is in
`tests/unit/test_subagent_lifecycle.py` (5 cases) or
`tests/unit/test_subagent_budgets.py` (9), neither of which any SUBAGENT-08
criterion names; and SUBAGENT-07: its journal cases are in
`tests/unit/test_conversation_journal.py:216-232`.

**R7 — behaviour the documents do not describe.** A second, distinct per-depth
table (`depth_limits` at `config.py:1255` vs `depth_tiers` at `:1256`, resolved
by different code in `config.py` and `governance.py:19`); the active-pool cap is
a rolling minimum across active pools (`pool.py:2383`), so one low-configured
pool tightens every other. 0.1.0 also said `max_concurrent` "clamps from below
(`tool.py:263`)"; that is wrong. A value below 1 is refused (`tool.py:333-334`)
and a value above the ceiling is refused (`:335`); line 263 only normalises the
metadata merged into those refusals, so a call refused for asking for 99
reports `max_concurrent_used: 99`. The field should be omitted or zero on a
refusal (WP-9).

**R8 — SUBAGENT-07: collection blocks and is single-shot.**

```console
$ sed -n '805,812p' src/agenthicc/subagents/communication.py
    async def collect(self, pool_id: str) -> object:
        handle = self._handles.get(pool_id)
        if handle is None:
            raise CommunicationError("unknown or expired subagent pool")
        try:
            return await handle.task
        finally:
            self._handles.pop(pool_id, None)
$ grep -n "async def collect_subagent_results" src/agenthicc/subagents/communication.py
987:    async def collect_subagent_results(pool_id: str) -> dict[str, object]:
```

`collect_subagent_results` takes only `pool_id`: no `wait`, no `timeout_s`. It
always joins the task (goal 3 missing) and pops the handle, so a second collect
raises `unknown or expired subagent pool` (goal 4 and criterion 5 missing).

**R9 — SUBAGENT-07: backgrounding exists, but only the user can ask for it.**

```console
$ grep -n "def request_background\|def wait_for_background_request" src/agenthicc/subagents/pool.py
2028:    def request_background(self) -> bool:
2035:    async def wait_for_background_request(self) -> None:
$ grep -n "class BackgroundSubagentsCommand" -A1 src/agenthicc/tui/runtime/commands.py
51:class BackgroundSubagentsCommand(Command):
52:    """Detach the active subagent pool from its foreground tool call (Ctrl+B)."""
$ grep -n "background" src/agenthicc/subagents/tool.py | grep -c "def spawn\|background: bool"
0
```

The spawn tool races the pool against `wait_for_background_request()`
(`tool.py:611-674`) and returns a `status="backgrounded"` receipt when the user
presses Ctrl+B; without a durable conversation journal it refuses with
`background_unavailable` (`:646-658`). There is no `background` argument, so
SUBAGENT-07 goal 1 is met for the user and missing for the model. The durable
lifecycle — `subagent_pool_started` written before the first worker
(`pool.py:2392-2400`, `journal.py:1002-1021`), `backgrounded`, `finished`,
`interrupted`, `cancelled` (`journal.py:1023-1087`, folded at `:1174-1213`) — and
exactly-once delivery through `subagent_pool_notification_delivered`
(`journal.py:1088-1111`, `tui_session.py:4311-4357`) are delivered.

**R10 — SUBAGENT-07: a backgrounded worker's question is surfaced nowhere.**

```console
$ grep -rn "pending_questions\|wait_for_pending" src/agenthicc --include=*.py \
      | grep -v "subagents/communication.py"
src/agenthicc/subagents/tool.py:616:            pending_task = asyncio.create_task(communication.wait_for_pending())
src/agenthicc/subagents/tool.py:676:                questions = communication.pending_questions(recipient="main")
src/agenthicc/subagents/tool.py:689:                    "pending_questions": questions,
$ grep -rn "on_clarification\|unanswerable_in_background" src/ --include=*.py
(no output)
```

The only reader of pending questions is the foreground tool call, which is
cancelled in its `finally` once the pool is backgrounded (`tool.py:719-726`).
A backgrounded worker that calls `ask_parent` therefore waits the full
`DEFAULT_MESSAGE_TIMEOUT_S = 300.0` (`communication.py:56`) with no surface able
to show or answer the question. Goal 6 is missing and its failure mode is
silent.

**R11 (B-1) — a budget-stopped background pool is misreported and loses its
results.**

```console
$ sed -n '2350,2364p' src/agenthicc/subagents/pool.py
            if self._journal is not None:
                if aggregate.budget_exceeded_reason:
                    self._journal.subagent_pool_cancelled(
                        pool_id=aggregate.pool_id,
                        reason=aggregate.budget_exceeded_reason,
                    )
                else:
                    self._journal.subagent_pool_finished(
$ sed -n '1078,1086p' src/agenthicc/memory/journal.py   # the cancelled record
                "kind": "subagent_pool_cancelled",
                "schema_version": 1,
                "pool_id": str(pool_id)[:128],
                "reason": str(reason)[:256],
```

A pool ended by the governor — whose `drain` let running workers *finish* — is
journaled as `cancelled` with a reason and **no** `total`, `succeeded`, `failed`
or `text`. The delivery path (`tui_session.py:4323-4343`) reads those fields with
`0` defaults and maps `cancelled` to "was cancelled when the session closed", so
the parent is told the wrong cause and "0/0 succeeded, 0 failed", and the
aggregate the drain preserved is not delivered. The reason the journal did
record is never shown. This crosses three PRDs (A-6 crossing, A-8 drain, A-7
delivery) and is owned by none of them; WP-5 fixes it.

**R12 (B-2) — background delivery escalates worker text to the user role,
unbounded.**

```console
$ sed -n '4344,4351p' src/agenthicc/runners/tui_session.py
            results = record.get("text")
            if isinstance(results, str) and results:
                message = f"{message}\n\n{results}"
            message_id = f"subagent-completion:{pool_id}"
            memory.add_user_once(message, message_id=message_id)
$ sed -n '1059p' src/agenthicc/memory/journal.py
            "text": str(text),
```

In the foreground the aggregate returns to the parent as a *tool result*; once
backgrounded the same text is appended as a *user* message. Worker output is
untrusted — workers read web pages, files and tool output — and the user role
is the most trusted position in the parent's context. The journaled text is
also unbounded (`journal.py:1059`) where every other subagent record is
truncated (`journal.py:1016-1018`). WP-6 bounds and frames it; SUBAGENT-11
replaces the delivery channel.

**R13 — restart recovery and background delivery exist only in the TUI
session.**

```console
$ grep -rn "recover_interrupted_subagent_pools\|completed_background_subagent_pools" \
      src/ --include=*.py | grep -v "def "
src/agenthicc/runners/tui_session.py:1535:        await asyncio.to_thread(session_conversation.journal.recover_interrupted_subagent_pools)
src/agenthicc/runners/tui_session.py:4323:        for record in journal.completed_background_subagent_pools():
$ grep -c "recover_interrupted_subagent_pools" src/agenthicc/runners/headless.py
0
```

*(Corrected in wave 4.)* The grep above is right but the inference drawn from
it was not. `tui_session.py:1535` sits inside `_build_session_context_impl`, the
shared session builder that the headless workflow runner
(`runners/headless.py:727-731`) and background workers
(`background/worker.py:1889-1925`) also call, so restart recovery already runs
for every runner that resumes a session. Only *delivery* of finished background
pools was TUI-specific: background workers run turns through `_run_agent_turn`
directly and never delivered them. Wave 4 moves delivery into one shared
function that both call. The stdin JSON-lines runner keeps no conversation
journal, so it has neither.

**R14 — SUBAGENT-08: delivered in behaviour, with one unproven criterion and a
user-only surface.**

```console
$ grep -n "class WorkerStopRequest\|class WorkerLifecycle\|def request_stop\|def request_pool_stop\|def retry_worker\|def dismiss_worker" src/agenthicc/subagents/pool.py
292:class WorkerStopRequest:
307:class WorkerLifecycle:
2043:    def request_stop(
2114:    def request_pool_stop(
2192:    def dismiss_worker(self, worker_id: str) -> bool:
2237:    async def retry_worker(self, worker_id: str) -> SubagentResult | None:
$ grep -n "\.acknowledge(" src/agenthicc/subagents/pool.py
1743:                    await self._communication.acknowledge(self._task.task_id, message_id)
$ grep -ln "approval" tests/unit/test_subagent_lifecycle.py tests/integration/test_subagent_panel_agx08.py
tests/integration/test_subagent_panel_agx08.py
```

Every goal has a mechanism: idempotent stop requests with mode, source and
reason; abort salvage with `partial` and `stop_reason` on every unsuccessful
ending (`pool.py:1242-1348`); drain that never starts pending workers; retry as
a new attempt; parent cancellation through the same path with
`source="parent"` (`:2327`, `:2824`); acknowledged steering. Criterion 5 (a stop
while the worker waits on an approval withdraws only that worker's prompt) has
no test: the one approval case in the named panel suite
(`test_a_workers_approval_names_it_and_waits_on_its_row`) does not stop the
worker. The controls are reachable only from the panel
(`tui_session.py:3011-3063`); the parent model's subagent tools are
`answer_subagent`, `send_subagent_message` and `collect_subagent_results`
(`communication.py:1004`). The PRD's goals do not require a model-facing
control, so this is phase-2 scope (SUBAGENT-11), not a defect. One retry
constraint is worth recording because SUBAGENT-11 must change it: a finished
pool is retryable only through the process-global `_RETRYABLE_POOLS` map while
it is the pool the panel displays, and any new pool in the same conversation
clears it (`pool.py:1819-1840`, `:2408-2411`).

**R15 (B-3) — SUBAGENT-09: not implemented, and two of its sinks leak.**

```console
$ grep -rn "class SubagentEvent\|subagent_worker_failed\|def replay_pool\|stuck_workers\|transcript_write_failures\|subagent_transcript_degraded" src/ --include=*.py
(no output)
$ sed -n '2685p;3104p' src/agenthicc/subagents/pool.py
                    "summary": result.text[:_MAX_DISPLAY_RESULT_CHARS],
            "text": result.text[:_MAX_DISPLAY_RESULT_CHARS],
$ sed -n '267,274p' src/agenthicc/subagents/transcripts.py   # transcripts redact
```

The scroll payload (`subagent_worker_done`, emitted for success *and* failure at
`pool.py:2707`) and the `SubagentCompleted`/`SubagentFailed` kernel payload
carry the first display-bound characters of worker text with no redaction,
while the transcript line for the same worker passes through the session-export
redactor and structured values pass through `_redact_result_value`
(`pool.py:669-676`). A token a worker read from a `.env` file reaches the kernel
event stream and the scroll buffer in clear. The scroll payload also carries no
`pool_id` and carries `worker_id` only on a stopped or partial worker
(`pool.py:2680-2706`), which is goal 4. What *is* present: journal records carry
`schema_version: 1` (`journal.py:1015`, `:1054`, `:1082`), a precedent goal 3 can
follow.

**R16 — the governor is a second meter, armed by default and documented as
such.**

```console
$ sed -n '1578,1584p' src/agenthicc/subagents/pool.py
            @signals.on(ModelCallComplete)
            async def _record_usage(event: ModelCallComplete) -> None:
                usage = event.usage
                progress.usage(
                    _token_count(_usage_field(usage, "input_tokens"))
                    + _token_count(_usage_field(usage, "output_tokens"))
$ grep -n "max_tokens\|max_total_time_s" docs/guides/subagents.md docs/guides/configuration.md
docs/guides/subagents.md:441:| `subagents.max_tokens` | 200,000 | Input plus output tokens reported by worker model calls |
docs/guides/subagents.md:442:| `subagents.max_total_time_s` | 600 seconds | Elapsed wall-clock time before the pool budget action triggers |
docs/guides/configuration.md:282:max_tokens = 200000         # aggregate token budget per pool
docs/guides/configuration.md:283:max_total_time_s = 600.0    # pool wall-clock budget
```

The governor's count is input plus output from the worker's own signal bus,
accumulated in `self._token_count` (`pool.py:2151-2154`) and nowhere else; the
ledger records the same calls with cache tokens and cost
(`pool.py:1599-1606`). Input tokens are re-sent on every sub-turn, so a pool of
four ten-turn workers whose context grows toward the 8,000-token default can
approach or cross 200,000 in the normal course of work; and the wall-clock
watcher is started for every admitted pool (`pool.py:2310`, `:2534`), so every
pool is drained at ten minutes regardless of the per-worker one-hour timeout. The user-facing guides document both defaults as
armed; SUBAGENT-06 §7 ("No budget is declared by default … day-one behaviour is
today's") says the opposite. D-2 decides which is true.

## 4. Goals

### Decisions

- **D-1 — SUBAGENT-04 criterion 6 is amended to refusal.** Refusal is stricter
  than clamping, already shipped, already tested, and it is what opencode does at
  its depth limit ("error with remedy text", Appendix A). The criterion is
  rewritten to: *a requested `max_concurrent` above the depth-tier ceiling is
  refused before any worker starts, and the refusal names the ceiling and its
  source*; `tool.py:335-343` stays. The test at
  `tests/integration/test_subagent_tool_execution.py:905` already asserts the
  refusal, `max_concurrent_limit`, `max_concurrent_limit_source` and ancestor
  inheritance (a depth-3 pool resolving to `depth_limits.2`), so no test change
  is needed. No value is clamped in either direction: below 1 is refused too
  (`tool.py:333-334`). **Applied in this revision**: SUBAGENT-04 §6 criterion 6
  now states refusal.
- **D-2 — M-1 ownership of the shipped governor.** The series keeps one
  cancellation path and one limit vocabulary, and the shipped code is
  reclassified rather than removed:
  - `on_budget_exceeded` and `stop_grace_s` are **SUBAGENT-08's** response keys
    (as SUBAGENT-08 §7 already says), and `budget_exceeded()` is the M-1
    response A-8 goal 9 describes. SUBAGENT-08's status section attributes it.
  - `max_tokens`, `max_total_time_s`, `max_tool_calls` and `max_iterations` are
    **SUBAGENT-06's operator ceilings**, a category SUBAGENT-06 did not
    anticipate: limits the operator sets for every pool, distinct from the
    per-call `budget:` declaration it specifies. They stay armed by default —
    removing a shipped safety limit is a regression — and SUBAGENT-06 §7's
    "inert by default" claim is corrected to say so, matching the guides.
    **Applied in this revision**: SUBAGENT-06 §7 now names the four armed
    ceilings and their values.
  - The governor's private counter is replaced by the ledger-derived pool total
    as soon as WP-7 makes one computable, so there is one meter. Until then a
    test pins that the two agree on input-plus-output for the same pool.
  - A crossing is **reported** on the same surfaces as any A-6 report, including
    the background path (WP-5), so "drained by a limit nobody declared" is
    always visible to the parent.

### Substance

1. **Per-pool accounting, read from the ledger, not from a second meter.** The
   worker run id gains the pool id (`…:{pool_id}:subagent:{task_id}`,
   replacing `pool.py:1601`, `:1673`) so a pool's records are selectable; a pool
   total sums input, output, cache-read and cache-write tokens, and sums cost
   only over records whose `cost_status` is known, propagating unknown cost as
   *some unknown*, never as zero. Aggregation reads completed ledger records and
   excludes provisional ones (`usage_ledger.py:196-202`), so the total is
   monotone and never disagrees with the session total it is a subset of.
   (SUBAGENT-06 goals 1–3.)
2. **A declared limit the call may lower and never raise.** `spawn_subagents`
   accepts an optional `budget:` mapping of `tokens`, `cost_usd` and
   `wall_clock_s`. A settings ceiling `subagents.max_budget` caps it by clamping
   downward and reporting the clamp (`src/agenthicc/worktrees/agent_tool.py:55`,
   `:100`). A malformed budget is refused before any worker is constructed,
   naming the field. Per D-2, a declaration also lowers — never raises — the
   operator ceiling of the same unit. (SUBAGENT-06 goals 4, 5.)
3. **Attestation and reporting on the surfaces that already exist.** Each
   `SubagentResult` carries its own tokens, cost and cost status;
   `AggregatedResult` carries pool totals; the `SubagentPoolCompleted` payload,
   the tool's text digest and `SubagentPoolState` carry usage-versus-limit, and
   the digest says *crossed* when a limit (declared or operator) was passed and
   which one. The panel renders pool spend through PRD-157's signals.
   (SUBAGENT-06 goals 6–8.)
4. **SUBAGENT-01 description threading.** `SubagentTypeSpec` is constructed
   with `description=definition.description` at `runners/agent_turn.py:2001-2018`.
   (SUBAGENT-01 goal 2, criterion 2.)

### Shipped-behaviour defects (new in 0.2.0)

5. **B-3: redact before any sink.** The scroll `summary` and the kernel `text`
   pass through the same session-export redactor the transcript uses, via one
   helper that also returns `truncated`/`text_length` (SUBAGENT-09 goals 6–7,
   first fix). This lands ahead of, and independently of, the rest of
   SUBAGENT-09.
6. **B-1: a terminal record always carries its counts, its text and its cause.**
   A pool ended by a budget crossing is journaled with a distinct terminal kind
   (`subagent_pool_stopped`, folded to status `stopped`) carrying `total`,
   `succeeded`, `failed`, the aggregate `text` and the stop `reason`; the
   delivery message names the reason ("stopped by the pool budget:
   max_tokens_exceeded") and the counts. `cancelled` remains reserved for a
   session close. `_record_unfinished_subagent_workers` runs for the stopped
   kind as it does for the others, so a drained pool's never-started workers are
   recorded as such.
7. **B-2: bound and frame background delivery.** Until SUBAGENT-11 replaces the
   channel, the delivered message wraps worker text in an explicit
   untrusted-content frame that names the pool and states that the content was
   produced by subagents, not by the user, and the framed text is redacted and
   bounded with a visible truncation note. *(Amended in wave 2.)* The bound and
   redaction are applied at the **delivery boundary**, not at journal write:
   the pool-level journal text is also what turn-resume replays as the tool
   result (`tool.py:483-517`), and `pool.py:109-112` states the journal keeps
   complete worker answers. Redacting it at write would make a re-driven turn
   return different text from the original one.

### Divergence and conformance

8. **D-1, applied** (see Decisions).
9. **SUBAGENT-05 criterion 8**: the `inherited.restrict(declared)` merge table in
   `tests/unit/test_agent_permissions.py`, extending `TestAgentCapabilityScope`.
10. **The worker-level capability-denial case**, through the pool's own hook
    chain (`pool.py:1507-1519`), not a hand-built `ToolCapabilityGate`.
11. **Put each case in the file its criterion names, or amend the criterion in
    the same commit** — for SUBAGENT-04, -05, -07 and -08. For SUBAGENT-08 the
    recommended direction is to amend the criteria to name
    `tests/unit/test_subagent_lifecycle.py` and `tests/unit/test_subagent_budgets.py`,
    which are coherent homes, and to add the missing criterion-5 case there.
12. **Make the "unchanged" claims checkable**: either the suite is untouched or
    the criterion names the cases that were added (SUBAGENT-04 criterion 11 and
    SUBAGENT-05 criterion 10 are false as written).
13. **Document the undocumented**: `depth_limits` vs `depth_tiers` and the
    rolling-minimum active-pool cap in SUBAGENT-04 §7 and
    `docs/guides/subagents.md`; the operator ceilings in SUBAGENT-06 §7 per D-2
    (the SUBAGENT-06 half is done in this revision).

## 5. Non-goals

- **The model-facing half of SUBAGENT-07 and -08.** Model-initiated background
  spawn, non-blocking and idempotent collection, background clarification
  policy, a model-facing list/stop/retry surface and headless recovery parity are
  product scope and live in **SUBAGENT-11**. This document fixes only the two
  shipped defects (B-1, B-2) in that path.
- **SUBAGENT-09 beyond B-3.** The envelope, version, lifecycle table,
  `worker_failed` split, replay, derived counters and degraded-transcript signal
  remain SUBAGENT-09's and are sequenced after WP-7 per the research report §5.7
  ("A-9 comes last … once the events of A-6, A-7 and A-8 exist").
- **Removing the governor.** D-2 reclassifies it; deleting a tested safety limit
  to satisfy a boundary between two documents is the wrong repair.
- **A second meter.** No new token counter, pricing table or reconciliation
  path. PRD-157 owns accounting; this document aggregates its records and, per
  D-2, retires the one private counter that exists.
- **Per-type budgets on the definition record.** Budgets remain per-pool policy;
  the AGX-09 key set is not extended.
- **Re-litigating delivered mechanisms.** SUBAGENT-04's two depth guards,
  SUBAGENT-05's narrow-only invariant, SUBAGENT-03's submission tool and
  SUBAGENT-08's lifecycle stay as they are.
- **Pricing, forecasting or vendor tables.** Cost is whatever the ledger
  recorded, with its provenance attached.

## 6. Acceptance criteria

Criteria 1–16 are carried from 0.1.0 unchanged except criterion 12, which now
states D-1. Criteria 17–27 are new.

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
   usage-versus-limit, and the text says *crossed* — naming the limit and
   whether it was declared or an operator ceiling — when one was passed:
   `tests/integration/test_subagent_tool_execution.py`.
9. `SubagentPoolState` publishes the pool's tokens, cost and limit, and the panel
   renders them through PRD-157's existing signals:
   `tests/unit/test_subagent_panel_agx08.py`.
10. *(Amended in wave 3.)* Crossing a *declared* `budget:` limit is answered
    exactly like crossing an operator ceiling — by `on_budget_exceeded` — and
    the report names the limit and that it was declared:
    `tests/integration/test_subagent_tool_execution.py::test_crossing_a_declared_token_limit_is_reported_with_its_source`.
    The original wording ("cancels nothing") predates D-2 and contradicted
    SUBAGENT-08 goal 9.
11. A definition-loaded type appears in the spawn tool's description with its own
    description text, not "No description provided":
    `tests/unit/test_subagent_plugin.py`.
12. SUBAGENT-04 criterion 6 states refusal (D-1), `tool.py:335-343` refuses, and
    `test_nested_depth_tier_refuses_concurrency_above_inherited_limit` asserts
    the refusal, the ceiling and `max_concurrent_limit_source`:
    `tests/integration/test_subagent_tool_execution.py`.
13. The `inherited.restrict(declared)` merge table exists in
    `tests/unit/test_agent_permissions.py` and asserts against
    `AgentCapabilityScope.restrict()` rather than a reimplementation.
14. A worker whose filtered tool list still contains a denied-capability tool is
    refused by the worker's own `ToolCapabilityGate`, asserted through the pool:
    `tests/unit/test_subagent_pool.py`.
15. Every case named by a §6 criterion of SUBAGENT-04, -05, -07 and -08 lives in
    the file that criterion names, and every "passes unchanged" claim is true of
    a byte-identical suite: verified by the §8 command list plus `git diff` over
    the named files.
16. `pytest tests/unit/test_usage_ledger.py tests/unit/test_subagent_pool.py
    tests/unit/test_subagent_plugin.py tests/unit/test_agent_permissions.py
    tests/unit/test_subagent_permissions.py tests/unit/test_subagent_panel_agx08.py
    tests/integration/test_subagent_tool_execution.py -q` passes, and every
    pre-existing case still passes unedited except where a criterion requires a
    change.
17. **B-3:** a worker answer containing a secret the session-export redactor
    masks is masked identically in the `subagent_worker_done` scroll payload,
    the `SubagentCompleted`/`SubagentFailed` kernel payload and the transcript
    line, and a payload whose text was cut carries `truncated: true` and the
    untruncated `text_length`: `tests/unit/test_subagent_pool.py`.
18. **B-3:** redaction is idempotent — applying the helper to its own output
    changes nothing: `tests/unit/test_subagent_pool.py`.
19. **B-1:** a backgrounded pool stopped by `max_tokens` with `drain` journals a
    `subagent_pool_stopped` record carrying non-zero `total`, the succeeded
    workers' `text` and `reason="max_tokens_exceeded"`; it folds to status
    `stopped`: `tests/unit/test_conversation_journal.py`.
20. **B-1:** delivering that record tells the parent the pool was stopped by the
    pool budget, names the reason, reports the real counts and includes the
    drained workers' text; a session-close cancellation still reads "cancelled
    when the session closed": `tests/unit/test_subagent_resume.py`.
21. **B-2:** *(amended in wave 2, see goal 7)* the delivered background message
    carries worker text redacted and bounded, and an oversized aggregate is
    marked truncated rather than cut silently, while the journal keeps the
    complete text: `tests/unit/test_subagent_resume.py`.
22. **B-2:** the delivered background message frames worker text as untrusted
    subagent output with the pool id, and an instruction-shaped string inside
    worker text ("ignore previous instructions") appears only inside that frame:
    `tests/unit/test_subagent_resume.py`.
23. **D-2:** with no configuration, `SubagentSettings()` arms `max_tokens`,
    `max_total_time_s`, `max_tool_calls` and `max_iterations` at the values the
    guides document, and SUBAGENT-06 §7 and `docs/guides/subagents.md` state the
    same numbers: `tests/unit/test_config.py`.
24. **D-2:** for one pool the governor's token count equals the input-plus-output
    sum of that pool's ledger records. *(Amended in wave 3.)* The counter is
    kept as the real-time trigger — reading the ledger on every model-call
    signal would order the governor after the ledger's own sink — and is pinned
    equal to the ledger total:
    `tests/unit/test_subagent_pool.py::test_each_worker_reports_its_own_spend_and_the_pool_reports_the_total`.
25. **SUBAGENT-08 criterion 5:** stopping a worker that waits on an approval
    leaves no answerable prompt for it and leaves a sibling's pending approval
    answerable: `tests/unit/test_subagent_lifecycle.py` (with the criterion
    amended to name that file, per goal 11).
26. Every SUBAGENT-01…09 document carries an implementation-status section that
    cites `0a63bfb` (or a later commit) and the WP ids that close its remaining
    items, and its `status:` uses the research report §6.6 vocabulary:
    `grep -L "## Implementation status" prds/SUBAGENT-0[1-9]-*.md` prints
    nothing.
27. A `spawn_subagents` call refused for its `max_concurrent` value (below 1, or
    above the ceiling) does not report the refused value as
    `max_concurrent_used`: `tests/integration/test_subagent_tool_execution.py`.

## 7. Rollout, migration and security

### Work packages and ordering

Ordering is by harm, then by dependency. Each work package is one reviewable
change with its criteria; none spans two PRDs' code without naming both.

| WP | Closes | Criteria | Depends on | Why this position |
|---|---|---|---|---|
| WP-1 | B-3 (SUBAGENT-09 goals 6–7, first fix) | 17, 18 | — | Secrets reach kernel and scroll payloads today |
| WP-2 | R5 (SUBAGENT-01 goal 2) | 11 | — | One-line fix with a model-visible effect |
| WP-3 | R3 / D-1 (SUBAGENT-04 criterion 6) | 12 | — | **Done in 0.2.0** (documentation only; the test already pinned refusal) |
| WP-4 | R16 / D-2 documentation and pinning | 23, 24 | — | Makes the armed defaults a stated contract. The SUBAGENT-06 §7 correction is **done in 0.2.0**; the two tests remain |
| WP-5 | B-1 (stopped-pool record and delivery) | 19, 20 | WP-4 | Parent is told the wrong cause and loses results |
| WP-6 | B-2 (bound, redact, frame background text) | 21, 22 | WP-1 | Trust escalation; reuses WP-1's helper |
| WP-7 | R1 / SUBAGENT-06 goals 1–8 | 1–10 | WP-4, WP-5 | Accounting needs the pool run id; reporting needs WP-5's record |
| WP-8 | R4, R6, R14 / conformance | 13, 14, 15, 16, 25 | — | No behaviour change; may land in any order |
| WP-9 | R7 documentation, and `max_concurrent_used` on refusals | 26 (part), 27 | WP-3 | Describes behaviour after D-1 settles it; one small payload fix |

SUBAGENT-11 depends on WP-1, WP-5 and WP-6 (it replaces the background delivery
channel and must not inherit B-1 or B-2), and SUBAGENT-09's remaining goals
depend on WP-7 and SUBAGENT-11 so its lifecycle table is written once.

### Migration

- **WP-5's new terminal kind is additive.** Older journals contain no
  `subagent_pool_stopped` record and fold as before. A journal written after
  WP-5 and read by an older build folds the pool to its last known state
  (`running`/`backgrounded`) and is then recovered as `interrupted` — a
  conservative misreport, never a crash, because the fold ignores unknown kinds
  (`journal.py:1176-1183`). No migration step rewrites old records.
- **WP-7's run-id change is additive in meaning.** `run_id` is a string key in
  the ledger, not a join, so existing records keep resolving and only future
  records are pool-selectable.
- **WP-6 changes stored text.** Journaled aggregates written before WP-6 are
  unbounded and unredacted; delivery applies the bound, redaction and frame at
  read time as well as write time, so an old record delivered after the upgrade
  is also safe.
- `budget:` is optional and defaults to absent; `subagents.max_budget` ships
  unset; per D-2, *unset* means "no per-call clamp", not "no limits", and a test
  (criterion 23) pins what is armed.

### Security and honesty

- *Redact at the boundary, not at the reader.* WP-1 redacts where the payload is
  built, so every present and future consumer of the kernel event inherits it.
- *Untrusted text never gains a role it was not produced in.* WP-6 is a stopgap
  that frames text inside the user role; SUBAGENT-11 removes the role change.
- *Unknown is not zero.* Cost carries the ledger's provenance; a `cost_usd`
  budget on an unpriced model is a declaration error.
- *The ceiling binds downward only*, and every clamp is reported.
- *A crossing is always visible.* After WP-5 no pool can be stopped by a limit
  without the parent reading which limit, in the foreground or background.

## 8. Verification

```console
$ git rev-parse HEAD   # record the remediation baseline
$ uv run pytest tests/unit/test_usage_ledger.py -q
$ uv run pytest tests/unit/test_subagent_pool.py -q
$ uv run pytest tests/unit/test_subagent_plugin.py -q
$ uv run pytest tests/unit/test_agent_permissions.py -q
$ uv run pytest tests/unit/test_subagent_permissions.py -q
$ uv run pytest tests/unit/test_subagent_panel_agx08.py -q
$ uv run pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ uv run pytest tests/unit/test_subagent_lifecycle.py -q
$ uv run pytest tests/unit/test_subagent_budgets.py -q
$ uv run pytest tests/unit/test_subagent_resume.py -q
$ uv run pytest tests/unit/test_subagent_communication.py -q
$ uv run pytest tests/unit/test_conversation_journal.py -q
$ uv run pytest tests/unit/test_config.py -q
$ uv run pytest tests/integration/test_subagent_tool_execution.py -q
$ uv run pytest tests/integration/test_subagent_executor_integration.py -q
$ uv run pytest tests/integration/test_subagent_panel_agx08.py -q
$ grep -L "## Implementation status" prds/SUBAGENT-0[1-9]-*.md
```

Green at `0a63bfb`, before any remediation — measured, one file per run. The
first nine rows are identical to 0.1.0's measurement at `390cfc1`, which is the
evidence that nothing moved:

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
| `tests/unit/test_subagent_lifecycle.py` | 5 |
| `tests/unit/test_subagent_budgets.py` | 9 |
| `tests/unit/test_subagent_resume.py` | 17 |
| `tests/unit/test_subagent_communication.py` | 10 |
| `tests/unit/test_conversation_journal.py` | 13 |
| `tests/integration/test_subagent_panel_agx08.py` | 5 |

The counts are recorded so that a later reader can tell an added case from a
moved one — the distinction R6 turns on.

## 9. Related and superseded PRDs

- **SUBAGENT-01 — narrows against.** WP-2 is a threading fix, not a revision.
- **SUBAGENT-02, SUBAGENT-03 — unrelated** to any work package; recorded as
  delivered. WP-6 *extends* SUBAGENT-03's redaction-before-delivery rule to the
  background text path its envelope does not cover.
- **SUBAGENT-04 — narrows against.** D-1 amends criterion 6; WP-9 documents R7.
- **SUBAGENT-05 — extends.** WP-8 adds the merge table its §8 already asked for.
- **SUBAGENT-06 — completed by this document.** WP-7 is its goals 1–8 restated as
  implementable work; D-2 corrects its §7 default claim and introduces the
  operator-ceiling category it did not anticipate.
- **SUBAGENT-07 — narrows against.** WP-5 and WP-6 fix shipped defects in its
  delivery path; its unfinished goals are handed to SUBAGENT-11.
- **SUBAGENT-08 — narrows against.** D-2 attributes the shipped budget response
  to its goal 9; WP-8 adds its criterion-5 case and re-homes its criteria.
- **SUBAGENT-09 — narrows against.** WP-1 ships its goal 7 "first fix" early;
  everything else stays its own.
- **SUBAGENT-11 — extends.** Takes the model-facing half of SUBAGENT-07 and -08
  and depends on WP-1, WP-5 and WP-6.
- **PRD-157 — inherits.** Accounting stays canonical there; WP-7 aggregates its
  records and adds no meter.
- **PRD-148 — inherits.** The stop vocabulary WP-5's record carries
  (`source`, `reason`) is PRD-148's, reused at pool scope as SUBAGENT-08 does.
- **AGX-09 — unrelated.** No definition key is added.

## A. Competitor evidence

Remediation rests on the series' own criteria rather than on competitors, so
this appendix carries only the rows the two decisions lean on. Rows and depth
labels are copied from the research report §2 and from SUBAGENT-04, -06 and -07
appendix A.

| Harness | Evidence | Depth | What it does | Take or reject |
|---|---|---|---|---|
| opencode | `packages/core/src/tool/plugin/subagent.ts:135-139` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | At `experimental.subagent_depth` the spawn fails with "Subagent depth limit reached (N). Increase \"experimental.subagent_depth\" to allow nested subagents." rather than silently reducing what was asked | Take for D-1: refusal with the limit and its source named is a recognised shape, so amending the criterion to refusal is not a regression |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | Nesting depth is bounded by default (three layers, `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`), and the research report's matrix records a default 20-subagent concurrency cap | Take as corroboration for D-2 that a default-armed operator limit is normal; docs-only, so D-2 does not rest on it alone — it rests on the shipped, tested code |
| Codex CLI | `codex-rs/core/src/session/multi_agents.rs:59` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | `max_concurrency: config.max_concurrent_threads_per_session` — fan-out is a session-wide configuration integer, not a per-call declaration | Take as the precedent for D-2's split between *operator ceilings* (always present) and *per-call declarations* (optional, may only lower) |
| Codex CLI | `codex-rs/core/src/config/mod.rs:1404`, `:1428` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | `wait_agent` is an explicit, disableable tool (`multi_agent_v2.wait_agent_enabled`): a model waits for another agent only by calling a tool, not by the harness blocking its turn | Take for SUBAGENT-11: a completed result can reach the parent as the output of a tool the parent called, which keeps worker text in the tool role. Note the limit of the evidence: the research report does not establish which role opencode's or Claude Code's background *notifications* use, so B-2 rests on agenthicc's own foreground/background asymmetry (§3 R12), not on a competitor claim |
