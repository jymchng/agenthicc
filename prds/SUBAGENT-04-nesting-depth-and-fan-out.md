---
title: "SUBAGENT-04: Nesting depth and fan-out governance"
status: Proposed
version: 0.1.0
date: 2026-10-29
scope: "the bounds on how much work a pool may create and how deep delegation may go, and how both are enforced and reported to the parent and the TUI"
area: A-4
related_prds:
  - PRD-232
  - PRD-124
  - PRD-197
  - AGX-08
tags:
  - subagents
  - nesting-depth
  - fan-out
  - governance
  - limits
---

# SUBAGENT-04: Nesting depth and fan-out governance

## 1. Executive summary

agenthicc has no nesting and no fan-out governance, but for two different
reasons, and only one of them is a decision.

Nesting is impossible **by accident**. A worker's tools are filtered from the
parent's `all_tools`, and that list is copied *before* the spawn tool is
appended, so no worker ever sees `spawn_subagents`. Depth 1 is real, but it is
an invariant nothing names, nothing asserts and nothing tests.

Fan-out is not governed at all. `max_concurrent` is a model-supplied tool
argument with no upper bound, and the task list is unbounded, so the model
decides how much work a pool creates — the semaphore throttles *execution*, not
*work*.

This PRD makes both explicit: a declared `max_depth` (default 1, i.e. today),
a refusal to construct a child pool past the limit, the spawn tool withheld at
the limit, per-pool task and concurrency ceilings, and depth plus parent-pool
identity reported to the parent and the TUI.

It does not add recursion for its own sake. The evidence supports *governing*
depth, not *having* nesting; §5 and §7 make depth 1 a valid permanent outcome.

## 2. Problem statement

All claims verified at commit `ffbfa0da681fa76a52a8e49e5f9b5776cbb8f780`
(2026-10-29); the commands and their output are re-quoted in §3, and `src/` is
unchanged from that commit to the one this document is written on.

**No type can delegate.** The nine built-in types are declared at
`src/agenthicc/subagents/types.py:309-371`, and their tool ceilings are the
frozensets at `:182-306`, assigned through the `allowed_tools` field (`:88`).
`spawn_subagents` is a member of none of them. This is checked at runtime, not
inferred from a grep, in §3: **0 of 9**. The only occurrences of the name in
`types.py` are prose (`:29`, `:67`, `:82`).

**Depth 1 is held up by two guards, and the stronger one is invisible.** The pool
is handed `all_tools=list(visible_tools)`
(`src/agenthicc/runners/agent_turn.py:1576`), and only afterwards does the runner
register and append the spawn tool (`:1601`). Because `:1576` took a *copy*, the
later `append` cannot reach the pool, so `spawn_subagents` is never even a
candidate for a worker's tool set. A worker's tools are then
`[t for t in self._all_tools if tool_name(t) in self._effective_allowed …]`
(`src/agenthicc/subagents/pool.py:806-814`), and the second guard is that
ceiling: `_all_tools` does not contain the spawn tool *and* no type names it
(§3: 0 of 9).

Either guard alone blocks delegation today, so this is not one bug away from
recursion — it is one bug away from *relying on the other guard*, which is the
fragile one. The ceiling is at least visible in a type definition and greppable;
the copy is neither, is undocumented, and is asserted by no test (§3). Remove
both — tidy the `list()` at `:1576` away *and* let a type declare
`spawn_subagents` — and the result is unbounded recursion with nothing anywhere
to stop it recurring. §6 targets each guard separately for exactly that reason.

**Fan-out is the model's number, not the operator's.** The tool signature takes
`max_concurrent: int = max_concurrent` (`src/agenthicc/subagents/tool.py:190`)
and documents it to the model as "Maximum number of subagents running at once
(default 4)" (`:206`). Validation rejects a non-integer and nothing else
(`:217`); the value is clamped from *below* only, `max(1, max_concurrent)`
(`:286`). A model may therefore ask for one thousand workers, and the request is
honoured.

**The task list is unbounded and is materialised eagerly.** Nothing checks
`len(tasks)` anywhere in `tool.py` or `pool.py` (§3). `SubagentPool.run()`
constructs one `SubagentWorker` per task in a loop
(`src/agenthicc/subagents/pool.py:1225-1250`) *before* the semaphore is created
(`:1309`). The semaphore therefore bounds how many workers execute at once; it
does not bound how many exist, how much provider spend they commit, or how much
memory their construction costs. Fan-out is bounded in the dimension that is
least dangerous.

**The only existing bound is concurrency.** `max_concurrent: int = 4` at
`src/agenthicc/subagents/pool.py:1120` and in the module-level helper at
`:1643`; the semaphore at `:1309`; the limit echoed into the panel snapshot as
`limit=` (`:1178`). That is the whole of it.

**There is no limit on the number of pools.** Running pools register in a
process-global dict, `_ACTIVE_POOLS` (`src/agenthicc/subagents/pool.py:1099`),
added at `:1282` and removed at `:1286`. Nothing caps the dict. With one pool
per parent turn that is invisible; any concurrent or background orchestration
(SUBAGENT-07, A-7) makes it a fan-out dimension in its own right, and it is the
dimension no competitor appears to name.

**Depth does not exist as a value anywhere.** `WorkerState` carries `label`,
`agent_type`, `status`, `worker_id`, `activity`, timestamps, `tokens` and
`dismissed` (`src/agenthicc/subagents/pool.py:222-236`). `SubagentPoolState`
carries `pool_id`, `total`, `workers`, `limit`, `finished_at` and `transcripts`
(`:245-260`). Neither has a depth, and neither has a parent pool id. The TUI
renders those two records through `worker_line` and `panel_lines`
(`src/agenthicc/tui/workspace/subagent_panel.py:99`, `:139`), so the panel
cannot show nesting because the data it reads has no nesting to show.

**Consequence for users.** Today the answer to "how much work did that turn
authorise?" is "whatever the model typed". The answer to "how deep did this
tree go?" is "it cannot go deep, and one of the two things preventing it is a
`list()` call nobody documented". The ceiling is greppable; the copy is not.
Remove both — tidy the `list()` and let a type declare the tool — and the result
is not a failing test but unbounded recursion that spends money until something
times out.

## 3. Current-source investigation

Run on 2026-10-29. `git rev-parse HEAD` at the time of writing:

```console
$ git rev-parse HEAD
059253384a5292cb087c15f9e9d1cd229827db0b

$ git diff --stat ffbfa0da681fa76a52a8e49e5f9b5776cbb8f780 HEAD -- src/
(no output: src/ is byte-identical to the research baseline)
```

Every citation below therefore holds at both commits. The runtime checks, run
with the project interpreter:

```console
$ python -c "from agenthicc.subagents.types import _BUILTIN_SPECS as S; \
print(sum('spawn_subagents' in s.allowed_tools for s in S), 'of', len(S))"
0 of 9

$ python -c "from agenthicc.subagents.types import _BUILTIN_SPECS as S; \
u=set().union(*[s.allowed_tools for s in S]); \
print(len(u), sorted(n for n in u if 'agent' in n or 'spawn' in n))"
22 []
```

The union of every tool any built-in type may use is 22 names, and none of them
is a delegation tool. Quoting the source:

```console
$ sed -n '806,814p' src/agenthicc/subagents/pool.py
        filtered = [
            t
            for t in self._all_tools
            if tool_name(t) in self._effective_allowed
            and (
                self._policy is None
                or not self._policy.visible_tool_names
                or tool_name(t) in self._policy.visible_tool_names
            )
        ]

$ sed -n '1576p;1601p' src/agenthicc/runners/agent_turn.py
                all_tools=list(visible_tools),
                visible_tools.append(spawn_tool)

$ sed -n '190p;286p' src/agenthicc/subagents/tool.py
        max_concurrent: int = max_concurrent,
            max_concurrent=max(1, max_concurrent),
```

Absence claims, each with the pattern and the scope that establish it:

- **No depth, nesting, fan-out or recursion concept exists.** Pattern
  `max_depth|depth|fan_out|fanout|recurs`, scope `src/agenthicc/subagents/`:
  **no match at all**, in any file, including docstrings. There is no partly
  built mechanism here to finish; the vocabulary is absent.
- **No cap on the number of tasks.** Pattern `len(tasks)|len(subagent_tasks)`,
  scope `src/agenthicc/subagents/tool.py` and `pool.py`: five matches, every one
  of them reporting — `tool.py:268`, `:274`, `:275`, `:326`, and
  `pool.py:1178` passing the count into the panel snapshot. None is a
  comparison. Nothing validates the task count against a limit.
- **No depth or parent-pool field on the records the TUI reads.** Pattern
  `depth|nest|parent_pool`, scope
  `src/agenthicc/tui/workspace/subagent_panel.py`: no match. The panel imports
  `SubagentPoolState` and `WorkerState` and renders exactly their fields.
- **No test asserts that workers cannot delegate.** Pattern
  `spawn_subagents|depth|nest`, scope `tests/`: the name appears in
  `tests/integration/test_subagent_tool_execution.py` only as
  `make_spawn_subagents_tool` construction in the parent role. The tool
  *ceiling* is tested: `tests/integration/test_subagent_tool_execution.py:112`
  builds two tools, allows one, and asserts every provider call exposes only
  `lookup_fact`, so filtering in general is covered. What no test asserts is the
  narrower thing this PRD needs — that `spawn_subagents` is absent from a
  worker's tools, that depth is enforced, or that recursion is refused. The
  defensive copy at `agent_turn.py:1576` is asserted by nothing at all.

Diff against the research report's `f843040` snapshot: **nothing has moved** for
G-4. `types.py:182-306`, `types.py:309-371`, `pool.py:1309` and
`pool.py:1120` remain the accurate citations.

Four surprises, and they changed the design.

**The report's citation was right but its mechanism was understated.** The
report says `spawn_subagents` is in no `allowed_tools` set. That is true, and it
is only half of why no worker can delegate: the filter at `pool.py:806-814`
iterates `self._all_tools`, which is a *copy* taken at `agent_turn.py:1576`
before the spawn tool is appended at `:1601`. So the tool is not merely
unselected, it is not a candidate. Declaring `spawn_subagents` in a type's
`allowed_tools` today would be inert — the name is intersected against a list
that does not contain the tool, and the pool would report a worker with no
spawn tool and no explanation. There are two guards here with different
durability: one visible in a definition, one a `list()` call.

**Fan-out is model-controlled with no ceiling.** The report frames G-4 around
the semaphore, which invites the reading that concurrency is the bound. It is
not: `max_concurrent` is an input the model supplies (`tool.py:190`, `:206`)
and the only clamp is `max(1, …)` (`:286`). This is a sharper gap than "the
default is 4" and it is what goal 4 addresses.

**Eager construction makes unbounded tasks expensive, not merely untidy.**
Because `run()` builds every worker before the semaphore exists
(`pool.py:1225-1250` then `:1309`), a large task list costs memory and provider
setup up front even if it would have been throttled to four at a time. That is
why the fan-out ceiling in §4 is a task-count limit and not only a concurrency
limit.

**A process-global dict with no cap.** `_ACTIVE_POOLS`
(`pool.py:1099`, `:1282`, `:1286`) is a fan-out dimension the report does not
mention and no competitor appears to bound. It is harmless at one pool per turn
and load-bearing the moment pools overlap.

## 4. Goals

1. A pool declares `max_depth`, an integer, default `1`. Depth 1 means workers
   cannot delegate — which is today's behaviour, and which this PRD makes a
   decision instead of an accident. (Gap G-4)
2. Exceeding the limit is refused, not silently ignored: constructing a child
   pool whose depth would exceed `max_depth` fails with an error that names the
   current depth, the limit and the setting that raises it. The remedy text is
   the point; opencode's message is the model for it.
3. Delegation is **withheld, not merely unused**: at the limit the spawn tool is
   absent from the worker's tool set, so a model at depth `max_depth` cannot see
   a tool it must not call. Both the refusal (goal 2) and the withholding
   (goal 3) are required; either alone is a single point of failure. Today the
   withholding rests on a defensive copy (`agent_turn.py:1576`) plus an
   undeclared ceiling. The ceiling's effect is tested; the copy is asserted by
   no test.
4. Fan-out is bounded per pool by two declared ceilings: a maximum task count,
   checked before any worker is constructed, and a maximum for the model's
   `max_concurrent` argument. Both are declared **per depth**, so a nested pool
   can be given less work than the pool that spawned it; a depth with no entry
   inherits the nearest declared ancestor's ceiling, and the default declares
   one pair covering every depth, which reproduces today. The model may **lower**
   a ceiling per call and may never raise it.
5. A limit on how many pools may be running at once, since `_ACTIVE_POOLS` is a
   fan-out dimension with no bound today and overlapping pools are coming
   (SUBAGENT-07).
6. Runaway delegation is prevented by ancestry, not by a timer: each pool
   carries a bounded ancestry of pool ids and type names, and a child whose
   `(type, task)` signature already repeats in its ancestry is refused with the
   chain printed. Cause is reported; a wall-clock timeout is not a cycle
   detector.
7. Depth and parentage are visible. `WorkerState` and `SubagentPoolState` carry
   depth and parent pool id, the TUI renders them, the `subagent_pool_state`
   snapshot survives into the panel, and the parent's tool result states the
   depth and the task and concurrency actually used.
8. Depth's default reproduces today's behaviour exactly (`max_depth=1`, no
   delegation, no refusal ever reached). The fan-out ceilings are finite by
   default, which **is** a behaviour change; §7 argues it, states the number as
   a judgement rather than as evidence, and gives the migration.
9. Per-depth tiering may only narrow. A depth-tier policy may restrict a
   worker's model or tool profile relative to what its type declares, and may
   never grant more than the type, or more than the inherited policy (PRD-197).
10. No new definition-format keys. Depth and fan-out are properties of a *pool*,
    not of a *type* — a type is a role, a depth is a position — so this PRD adds
    nothing to AGX-09 §5.1's key set, which is already being extended by
    SUBAGENT-01, -02 and -03. §7 records that reasoning.

## 5. Non-goals

The A-4 boundaries from the research report §5.3, restated as commitments:

- **The concurrency semaphore itself.** It works and it stays
  (`pool.py:1309`). This PRD adds policy *above* it — ceilings, refusals and
  reporting — and does not replace, re-implement or move it.
- **Nesting itself, as a feature to ship.** The evidence supports governing
  depth, not having it. `max_depth` may stay `1` permanently and every goal above
  is still satisfied; if agenthicc never nests, this PRD has removed an accident
  and a runaway vector, which is a complete outcome rather than a partial one.
- **A second notion of a worker tree.** PRD-232 proposes a durable task DAG with
  its own "worker/depth/token/time budgets"
  (`prds/prd-232-durable-agent-team-coordination.md:49-50`) and already lists
  "unbounded nested spawning" as a non-goal (`:41`). If PRD-232 lands, its
  depth enforcement is the implementation and this PRD's limits become its
  inputs — there is one tree, not two. §9 states this.
- **Typed hierarchical identity as an addressing scheme.** Codex's `AgentPath`
  is a validated absolute path with prefix operations (research report §5.5
  R-1). A full path refactor is not proposed: this PRD stamps an integer depth
  and a parent pool id, which is the minimum that makes reporting honest, and
  leaves path-based addressing to A-9 if it is ever wanted.
- **Spend and wall-clock budgets.** Token, cost and time limits on a pool belong
  to A-6, and the abort-versus-drain response to a breach belongs to A-8
  (research report §5.4 M-1). This PRD bounds *how much work is admitted*, not
  *what it costs*; the two are different questions asked at different times.
- **Per-type permission narrowing and per-type model routing.** A-5 and A-2 own
  those. The depth-tier rule in goal 9 composes with them and may only narrow.
- **Mid-flight lifecycle control of nested workers.** Steering, cancellation and
  retry are A-8, including for nested pools.
- **Background and non-blocking pools.** A-7 owns fire-and-return, durable pool
  identity and collection. This PRD bounds how many pools may exist; A-7 decides
  what a pool that outlives its turn means.

## 6. Acceptance criteria

1. A pool constructed with a child depth greater than `max_depth` is refused
   with an error naming the current depth, the limit and the setting that raises
   it, and no worker is constructed: `tests/unit/test_subagent_pool.py`.
2. With `max_depth=1` (the default) every built-in type's worker tool set
   excludes `spawn_subagents`, asserted directly rather than inferred from a
   grep: `tests/unit/test_subagent_plugin.py`.
3. With `max_depth=2`, a `max_depth=1` worker's tool set still excludes
   `spawn_subagents`, and a worker one level below the limit has it available —
   proving withholding tracks depth and is not a constant:
   `tests/unit/test_subagent_pool.py`.
4. A synthetic type whose `allowed_tools` includes `spawn_subagents` still
   cannot delegate at `max_depth=1`: the pool refuses to construct a child, so
   neither guard is load bearing on its own and removing one does not silently
   produce recursion:
   `tests/integration/test_subagent_tool_execution.py`.
5. A task list longer than the declared maximum is rejected before any
   `SubagentWorker` is constructed, and the error states the limit and the
   configured source it came from. The same test declares a per-depth table in
   which depth 2 allows fewer tasks than depth 1, and asserts the depth-2 pool is
   held to its own smaller maximum: `tests/unit/test_subagent_pool.py`.
6. A `max_concurrent` argument above the declared ceiling is lowered to the
   ceiling, and the returned result reports the value actually used, so the
   model is told rather than silently obeyed. A depth with no entry inherits the
   nearest declared ancestor's ceiling rather than falling back to unlimited:
   `tests/integration/test_subagent_tool_execution.py`.
7. Exceeding the pool-count limit is refused with an error naming the limit and
   the pools currently running: `tests/unit/test_subagent_pool.py`.
8. A child pool whose `(type, task)` signature repeats in its ancestry is
   refused, and the error prints the ancestry chain:
   `tests/unit/test_subagent_pool.py`.
9. `WorkerState` and `SubagentPoolState` carry depth and parent pool id, the
   panel renders depth for a nested pool, and the flat-pool rendering is
   byte-identical to today's: `tests/unit/test_subagent_panel_agx08.py` and
   `tests/unit/test_subagent_tui.py`.
10. A depth-tier policy that tries to grant a model or tool the type does not
    declare is refused at load, and one that narrows is accepted:
    `tests/unit/test_subagent_plugin.py`.
11. `pytest tests/integration/test_subagent_tool_execution.py -q` and
    `pytest tests/unit/test_subagent_pool.py -q` pass unchanged, proving the
    default path (`max_depth=1`) admits exactly what it admits today.

## 7. Rollout, migration and security

**Defaults, and the one behaviour change.** `max_depth` defaults to `1`, which
is today's behaviour: no delegation, no refusal reachable, no new error path.

The fan-out ceilings are finite by default — a maximum task count of `64` and a
maximum `max_concurrent` of `16`, covering every depth — and that **is** a
change, because today the model may ask for unbounded work. It is deliberate and
it is a judgement, not a finding: no harness in the research report demonstrably
caps task count (§A records the same absence for Qoder), so `64` is chosen to sit
above any pool a human would read in a result digest and to be raisable in one
line. Stating it as evidence would be the overclaim §4 of the report warns
against; stating it as a default with a migration path is honest. The per-depth
form exists so that a project which does enable nesting can give a nested pool
less rope than its parent without touching code; it is not exercised by the
default configuration, which declares one ceiling for all depths.

**Migration.** Three ordered steps, each independently shippable:

1. Add `depth` and `parent_pool_id` with defaults to `SubagentPoolState` and
   `WorkerState`, and thread them through the snapshot. No behaviour change; the
   panel ignores them until step 3.
2. Add the refusals: depth, task count, concurrency ceiling and pool count, all
   defaulting to today's effective behaviour except the two fan-out ceilings.
   Announce the ceilings in release notes with the settings that raise them.
3. Render depth in the panel and report it to the parent. This is the only step
   that changes what a user sees.

**Why this PRD adds no definition-format keys.** AGX-09 §5.1 fixes the
frontmatter key set and makes unknown keys a validation error under PRD-250's
inert-field rule (`prds/agx-09-agent-library-and-switching.md:92`). `max_depth`,
the task ceiling and the pool ceiling are properties of a *pool*, and a pool is
created by a call, not by a definition; a type is a role and a depth is a
position. Putting them on the type would let two definitions disagree about the
tree they share, and would add a fourth unilateral extension to a key set that
already has three (SUBAGENT-01's `description` and `override:`, SUBAGENT-02's
`effort:` and `context:`, SUBAGENT-03's `output:`). They are run/project
settings instead. If a later PRD wants per-type depth ceilings, that is a
definition-format change and must go through the consolidation, not around it.

**Security, and where it fails closed.**

- *Fail closed at the boundary.* A pool over any limit is refused before workers
  are constructed. There is no "log and continue" mode: the limits exist to stop
  spend and recursion, and a warning that still spawns is not a limit.
- *The refusal is the feature.* Because the error names the setting that raises
  the limit, an operator who legitimately needs more work is given a path
  forward; an error that only says "denied" invites someone to widen the
  hard-coded constant instead.
- *Narrowing only.* A depth tier may restrict a worker's model or tools relative
  to its type and the inherited policy (PRD-197); a tier that widens is refused
  at load (criterion 10). Depth must never be a way to regain capability that
  the parent denied.
- *Ancestry is bounded.* The ancestry chain carried by a pool is length-capped
  by `max_depth`, so the cycle check cannot itself become unbounded memory, and
  the signature compared is the `(type, task)` pair already on the task record —
  no new free-text hashing of untrusted content.
- *Pool identity is not authorisation.* `parent_pool_id` and `depth` are
  reporting values; no permission decision may be made from them alone, or a
  forged parent id would become a capability. Permission decisions stay in
  `policy.py` and are A-5's.
- *The panel shows what exists.* Depth is rendered from the pool snapshot, not
  reconstructed by the TUI, so a nested pool cannot be displayed as flat.

## 8. Verification

Current suite that must stay green:

```console
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_plugin.py -q
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/unit/test_subagent_panel_agx08.py -q
$ pytest tests/unit/test_subagent_tui.py -q
$ pytest tests/integration/test_subagent_tool_execution.py -q
$ pytest tests/integration/test_subagent_executor_integration.py -q
```

New cases belong in `tests/unit/test_subagent_pool.py` (depth refusal, task and
concurrency ceilings, pool count, ancestry), in
`tests/unit/test_subagent_plugin.py` (worker tool sets per depth, depth-tier
narrowing), in `tests/unit/test_subagent_panel_agx08.py` and
`tests/unit/test_subagent_tui.py` (depth rendering, flat-pool parity), and in
`tests/integration/test_subagent_tool_execution.py` (the synthetic-agent path
that would otherwise let `agent_turn.py:1576` regress unnoticed).

The regression this PRD most needs is criterion 4: a synthetic type that
*declares* `spawn_subagents`, asserted to be refused rather than merely
filtered. That is the test that would fail today for the wrong reason — it would
pass because of a `list()` call nobody documented — and the one that converts
two accidents into one invariant.

## 9. Related and superseded PRDs

- **PRD-232 — narrows against, and defers to it if it lands.** PRD-232 proposes the durable task DAG
  and already requires enforcing "configured worker/depth/token/time budgets"
  (`prds/prd-232-durable-agent-team-coordination.md:49-50`), with "unbounded
  nested spawning" as a non-goal (`:41`). This PRD governs the synchronous pool
  that exists; if PRD-232 lands, its scheduler owns depth enforcement and these
  limits become its inputs. Whichever ships second adapts. Two worker trees
  would be the defect; there must be one.
- **PRD-124 — extends.** PRD-124 introduced `spawn_subagents` and left its
  fan-out to a `max_concurrent` argument with a default of 4. This PRD bounds
  the tool PRD-124 created; it does not change that tool's task shape, and a
  caller who passes no `max_concurrent` sees no difference.
- **PRD-197 — inherits.** PRD-197 is `Implemented` and owns the policy a worker
  inherits from its parent. A nested pool inherits through the same path, and
  goal 9's depth tiers can only narrow what PRD-197 already grants; this PRD
  adds no permission rule of its own.
- **AGX-08 — extends.** AGX-08 phase 1 shipped the panel with one flat row per
  worker (`src/agenthicc/tui/workspace/subagent_panel.py:99`, `:139`). This PRD
  adds depth and parentage to the records it renders; it does not redesign the
  panel's information architecture or navigation, and a flat pool renders
  exactly as it does today.

## A. Competitor evidence

| Harness | Evidence | Depth | What it does | Take or reject |
|---|---|---|---|---|
| opencode | `packages/core/src/tool/plugin/subagent.ts:124-139` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | Walks parent sessions to compute `depth` (`:124-134`), reads `experimental.subagent_depth` **defaulting to 1** (`:135`), and fails with "Subagent depth limit reached (N). Increase \"experimental.subagent_depth\" to allow nested subagents." (`:136-139`) | Take all of it: depth as an opt-in integer defaulting to today's behaviour, and an error that names the remedy. This is the closest analogue to agenthicc's situation |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | Three layers below the main conversation, changed by `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` (`1` disables nesting); at the limit the CLI **withholds the Agent tool** from every subagent *except a fork, whose call returns an error instead of spawning*; a spawning subagent waits for its nested subagent, so nesting does not multiply concurrency behind the parent's back | Take the withholding mechanism (goal 3) and the separation of depth from concurrency (goals 1 and 4). Reject the default of three: agenthicc has no evidence it needs nesting at all |
| Codex CLI | `codex-rs/protocol/src/agent_path.rs:19-23` and `codex-rs/core/src/session/multi_agents.rs:59` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | `AgentPath` is a validated absolute path with `ROOT = "/root"`, making identity a *tree position*; concurrency is a config integer, `max_concurrency: config.max_concurrent_threads_per_session` | Take the idea that identity carries tree position — minimally, as depth plus parent id (goal 7). Reject the full path refactor: §5.5 R-1 records that agenthicc already has `worker_id` and `PoolHandle` |
| Qoder CLI | `https://docs.qoder.com/cli/subagent` fetched 2026-10-29 | docs-only | Subagents are invoked with an `Agent(name)` expression; the documented surface describes `model`, `effort` and `maxTurns`, not a depth counter or a fan-out cap | Partial, and recorded as partial: take direct-child invocation as the mental model only. The *absence* of a documented depth or fan-out cap is weak evidence (§2.1's caveat), so it is not used to justify any requirement here |
