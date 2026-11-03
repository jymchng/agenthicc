---
title: "TUIR-04: An event-loop lag budget — a benchmark gate and a live watchdog"
status: Proposed
version: 1.0.0
date: 2026-11-03
scope: "make loop stalls measurable and enforced: the TUIR-00 harness as a permanent benchmark with a CI budget, and an in-session watchdog that names the stall in /perf"
related_prds:
  - TUIR-00
  - TUIR-01
  - TUIR-02
  - TUIR-03
  - AGX-11
  - AGX-14
tags:
  - performance
  - testing
  - observability
  - event-loop
---

# TUIR-04: An event-loop lag budget

## 1. Problem

Nothing measures loop lag. The AGX-11 frame statistics measure render and write
cost, which were fine; the stall happened *between* frames, in tool code, and
no test or metric saw it. A 93.7 s freeze shipped because no gate watched for
one (TUIR-00).

## 2. Goals

1. **A permanent benchmark.** `tests/performance/test_subagent_loop_lag.py`
   generates TUIR-00's synthetic project (33,000 files, 3,415 directories,
   a `node_modules/` tree, generated into `tmp_path` and cached per session),
   runs a real `SubagentPool` of six `explorer` workers with the real
   filesystem tools, policy, journal and transcripts, and a scripted transport,
   and samples loop lag with a 10 ms sleeper.
2. **A budget that fails CI.** The benchmark asserts max stall < 100 ms and
   p99 < 25 ms, and that one main-loop `search_files("*.py")` stalls < 50 ms.
   It runs in the existing `Performance budgets` CI job (`nox -s
   tests_performance`, uninstrumented); budgets live beside the other AGX-14
   budgets and changing them needs the performance-budget approval label.
   Until TUIR-01 and TUIR-02 land, it is marked `xfail(strict=True)` with the
   measured values, so the fix flips it.
3. **A live watchdog.** The TUI runs a `LoopLagMonitor` task (10 ms sleeper,
   negligible cost) that keeps bounded samples and, for any stall over 250 ms,
   records the coroutine or callback that was running. That comes from
   `loop.slow_callback_duration` in a debug run, or from a sampled
   `sys._current_frames()` snapshot of the loop thread, taken by a watchdog
   thread when the loop has not ticked for 250 ms.
4. **Visible in `/perf`.** `/perf` shows loop-lag p50/p95/max and the last five
   stalls with their culprit frames (file:line), so a user report carries the
   cause.
5. **No new dependency.** Standard library only.

## 3. Non-goals

- Production telemetry export (SUBAGENT-09 non-goal stands).
- Profiling every session: the watchdog records frames only on a stall.

## 4. Acceptance criteria

1. The benchmark reproduces today's failure (max stall > 10 s) on `79984e0`
   and passes after TUIR-01 and TUIR-02.
2. `/perf` lists a stall injected by a test (`time.sleep(0.4)` in a tool) with
   the injecting function's file and line.
3. The monitor's own overhead keeps the TUI idle CPU < 1 % (measured over 60 s).
4. The watchdog thread never touches loop state; it only reads frames and
   writes to a lock-protected ring buffer.
