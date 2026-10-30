---
title: "AGX-14: CPU-time performance budgets in CI and perf diagnostics"
status: Implemented (weekly 10k search target unmet)
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P0
series: AGX
related_prds: [agx-00, agx-01, agx-02, agx-03, agx-04, agx-11, agx-12, prd-176, prd-209, prd-250]
tags: [performance, ci, benchmarks, diagnostics, process]
---

# AGX-14 — CPU-time performance budgets in CI and perf diagnostics

## Implementation status (2026-10-30)

The CPU-time and deterministic-count probe suite, checked budget ratchet,
`agenthicc perf report` command, required CI performance session, and scheduled
weekly profile are integrated. The `cli.agents_json.modules` exact-count budget
was lowered from 170 to the measured 167; the ratchet prevents the measured
improvement from going stale. `nox -s perf` and the coverage session pass; the
coverage run completed in 9 minutes. The full Nox run found one unmet weekly
target: `agents.search_10k.cpu_p95_ms` measured 220.379 ms against a 50 ms
budget (30% tolerance). The benchmark query includes the two-character term
`99`, below the FTS5 trigram index's three-character lookup threshold. This
weekly search target remains open; all other sessions in the recorded run
passed.

## 1. Summary

Agenthicc has written performance budgets before — PRD-176 for start-up,
PRD-209 for the agents screen — and both regressed or were never measured
where users feel them: the TUI needs 6–8 s of CPU to its first prompt against a
2 s budget, and a fresh agents screen needs 3.6–6 s for its first useful page at
10,000 sessions against 250 ms. The benchmarks that exist are opt-in, measure in
process, or time the wrong frame, and wall-clock timing on shared runners is
too noisy to gate on. Add a budget suite that measures **CPU time and
deterministic counts** for every AGX budget, gate pull requests on it, and give
users the same numbers through diagnostics.

## 2. Problem and evidence

| Budget | Where it is written | Measured today | Gap |
|---|---|---|---|
| First stable TUI frame p95 ≤ 2.0 s | PRD-176 NFR-1 | 6–8 s CPU before the first prompt | `scripts/benchmark_startup.py` never starts the TUI |
| First useful agents frame p95 ≤ 250 ms (10k sessions) | PRD-209 §5.1 | 3.6–6.0 s in a fresh process | `scripts/benchmark_agents_manager.py` measures in process and reports the placeholder frame (55–71 ms) |
| Listing commands | none | 1.25 s CPU, 440 modules for `agents --json` | no budget |
| Idle CPU | none | 3–6% of a core, idle agents screen | no budget |
| Agent-turn bookkeeping | none | 165 s CPU for the 510-step replay | no budget |

- `tests/performance/` contains one test (`test_reconstruct_site_prd177.py`).
- The agents benchmark is opt-in because its default fixture is 150 MB.
- On the development host, load moved between 3 and 41 during this study;
  wall-clock samples varied by 2–5× for the same command, while CPU time and
  module counts were stable.

## 3. Goals and non-goals

**Goals**

1. Every AGX budget has an automated measurement.
2. Pull requests are gated on metrics that are stable on shared runners: CPU
   time with headroom, and exact counts (modules imported, deep copies per
   step, markup parses per frame, bytes per frame, events appended while idle).
3. Budgets ratchet like the type-safety baseline: a regression fails; an
   improvement that leaves slack fails until the budget is lowered.
4. Users can produce the same report locally.

**Non-goals**

- Gating on wall-clock time on shared CI runners. Wall time is recorded for
  context, and a dedicated reference runner may gate it later.
- Measuring under coverage (tracing distorts every number).

## 4. Design

### 4.1 Suite

`scripts/perf_budget.py` runs named probes and writes one JSON report:

| Probe | Measures | Source PRD |
|---|---|---|
| `cli.<command>` | Best-of-5 CPU time, unique modules, deny-listed modules | AGX-01 |
| `tui.first_prompt` | CPU to first prompt and modules at first frame, in a pseudo-terminal; wall time recorded | AGX-02 |
| `agents.fresh_first_page.{1k,10k}` | Fresh-process CPU to first page with a current index | AGX-03 |
| `agents.search.1k` | Search CPU per query | AGX-03 |
| `agents.idle.1k` | CPU and bytes appended to `events.jsonl` over 30 s idle | AGX-04 |
| `render.footer`, `render.status`, `render.composer_200_lines` | CPU per render; markup parses per frame | AGX-11 |
| `render.spinner_frame` | Bytes written per animation frame; frames per idle minute | AGX-11 |
| `turn.replay_goal_flow` | CPU for the 510-step replay; deep copies and full validations per step | AGX-12 |

Counts come from test-only counters compiled into the code paths (for example a
counter around `copy.deepcopy` of the history, or around markup parsing), so
they are exact on every host.

### 4.2 Budgets file and ratchet

`docs/reference/performance-budgets.json` holds each metric's budget and
tolerance:

```json
{
  "cli.agents_json.cpu_s": {"budget": 0.30, "tolerance": 0.30},
  "cli.agents_json.modules": {"budget": 200, "tolerance": 0},
  "turn.replay_goal_flow.deepcopies_per_step": {"budget": 0, "tolerance": 0}
}
```

- `--check` fails when a metric exceeds `budget × (1 + tolerance)`.
- It also fails when a metric is better than `budget × (1 − tolerance)` for an
  exact count, so improvements are locked in with `--update` (the same
  two-directional ratchet `scripts/type_audit.py` uses).
- The file is owned in `.github/CODEOWNERS` with the other quality baselines;
  raising a budget needs the `perf-budget-increase` label, mirroring
  `type-baseline-increase`.

### 4.3 CI

- New Nox session `perf` (uninstrumented) runs every probe except the 10k
  fixtures; it becomes a job inside the aggregate **Required checks** in
  `.github/workflows/ci.yml`.
- A scheduled weekly workflow runs the 10k probes (`agents.fresh_first_page.10k`,
  the full `benchmark_agents_manager.py` profile) and uploads the report; a
  regression opens an issue rather than failing unrelated pull requests.
- Fixtures are generated deterministically by the existing benchmark helpers
  (`_write_fixture`), cached between runs.
- Reports record CPU model, CPU count, Python version and load average, so a
  noisy sample can be recognised.

### 4.4 Diagnostics for users

- `/perf` in the TUI (AGX-11) shows frame, render and write statistics for the
  session; `/startup` (AGX-02) shows start-up milestones.
- `agenthicc perf report` runs the local, fixture-free subset of the suite
  (CLI, first prompt, render probes) and prints results next to the budgets, so
  a bug report can include comparable numbers.
- `BackgroundManager.diagnostics()`'s metrics move to the hub (AGX-05) and are
  included in `agenthicc perf report` when the hub has been opened.

## 5. Acceptance criteria

1. Every budget in AGX-00 §6 maps to at least one probe and one budgets entry.
2. `nox -s perf` runs in under 10 minutes on a CI runner and is part of
   **Required checks**.
3. Introducing an extra module import into `agenthicc agents --json`, an extra
   deep copy per step, or an uncached markup parse per frame fails the `perf`
   session with a message naming the metric.
4. Lowering a metric's cost below its tolerance fails until `--update` lowers the
   budget.
5. `agenthicc perf report` works offline in a fresh `HOME`.

## 6. Test plan

- Unit tests for the ratchet logic (regression, stale budget, label override),
  modelled on `tests/unit/test_type_audit.py`.
- Probe self-tests with synthetic slow and fast functions.
- CI dry run with an intentionally regressed branch.

## 7. Risks

- **CPU time is not latency.** A blocked event loop costs no CPU. Mitigation:
  start-up and frame probes also record wall time and event-loop lag, reported
  but not gated on shared runners.
- **Counter maintenance.** Counters must not cost anything in production;
  they compile to no-ops unless an environment variable enables them in the
  perf session.

## Sources

- PRD-176 latency budgets: [prd-176](prd-176-fast-progressive-startup.md)
- PRD-209 interactive latency targets: [prd-209](prd-209-low-latency-agents-tui.md)
- PRD-250 two-directional type ratchet: [prd-250](prd-250-code-quality-evaluation-and-improvement-programme.md)
