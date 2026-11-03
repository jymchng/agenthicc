---
title: "TUIR-00: Study — why the TUI stops responding while subagents run"
status: Study
version: 1.0.0
date: 2026-11-03
scope: "measure, not guess: reproduce the unresponsive TUI with a real subagent pool, find every source of event-loop stall, rank them by measured cost, and route each to a PRD"
related_prds:
  - TUIR-01
  - TUIR-02
  - TUIR-03
  - TUIR-04
  - TUIR-05
  - SUBAGENT-09
  - AGX-11
tags:
  - performance
  - tui
  - subagents
  - event-loop
---

# TUIR-00: Study — why the TUI stops responding while subagents run

## 1. Report

A user running six `explorer` subagents in `/root/youtube_projects` (33,105
files) saw the TUI stop responding: keys did not echo and the panel did not
update for long stretches, and the footer read `◆ 7 need you | ● 1 running`.

## 2. Method

Everything in the TUI — key handling, frame rendering, the subagent panel,
and every worker's agent loop — runs on one asyncio event loop. The TUI is
unresponsive exactly when that loop is blocked. The study therefore measures
**loop lag**: a task that sleeps 10 ms and records how late it wakes. Any lag
over ~50 ms is visible as a stalled key echo; over 1 s it is a frozen screen.

Harness ([`study/loop_lag_harness.py`](study/loop_lag_harness.py), project
generator [`study/make_tree.py`](study/make_tree.py); TUIR-04 makes them a
permanent benchmark). Reproduce with:

```bash
python prds/tui-responsiveness/study/make_tree.py /tmp/probe 33000
uv run python prds/tui-responsiveness/study/loop_lag_harness.py /tmp/probe /tmp/probe-session 6
```

It uses:

- a synthetic project of 33,000 files in 3,415 directories, including a
  `node_modules/` tree, matching the user's project size;
- a **real** `SubagentPool` with the built-in `explorer` type, the real
  filesystem tools (`FS_AGENT_TOOLS`), a real `WorkspaceAccessPolicy` in Yolo
  mode (the user's mode), a real `ConversationJournal`, transcripts and a
  `ConversationStore`;
- a scripted transport (50 ms latency) that makes every worker call
  `search_files("*.py")`, `grep_files("def handler", "src")`,
  `list_directory("src")` and `read_file`, then answer;
- the lag sampler above, `cProfile`, and per-tool timing.

Commit measured: `79984e0` (SUBAGENT wave 6), Python 3.13, Linux.

## 3. Measurements

### 3.1 The whole pool

| Run | Workers | Wall time | Max single stall | Time stalled > 50 ms | Lags > 500 ms |
|---|---:|---:|---:|---:|---:|
| As shipped | 6 | 154 s | **93.7 s** | **135.6 s** (88 % of the run) | 2 |
| As shipped | 2 | 127 s | 90.0 s | 121.7 s | 2 |
| Experiment: authorization canonicalised in a thread | 6 | 167 s | **0.41 s** | 3.3 s | 0 |

The experiment replaced `_authorize_discovered_paths` with a version that
canonicalises candidates in `asyncio.to_thread` and classifies them by
string-prefix containment, falling back to the full policy only for candidates
outside the scope. One change removed 97.6 % of the stall time. The 12
remaining stalls of 100–415 ms are GIL contention from six walking threads
(§4, R3), and the run got *slower* in wall time — so moving the work is
necessary but not sufficient.

### 3.2 One tool call, on the main loop, no subagents

| Tool | Duration | Max loop stall |
|---|---:|---:|
| `search_files("*.py", ".")` | 14.7 s | **14.3 s** |
| `grep_files("def handler", "src")` | 5.9 s | **4.7 s** |
| `list_directory("src")` | 0.0 s | 2 ms |

The parent agent has the same problem on its own; subagents multiply it,
because six workers' searches serialise on the one loop.

### 3.3 Profile of the shipped run (2 workers, 125 s)

| Function | Calls | Cumulative |
|---|---:|---:|
| `WorkspaceAccessPolicy.authorize` | 85,928 | 133.2 s |
| `WorkspaceScope.resolve` | 257,784 | **123.7 s** |
| `fs._authorize_discovered_paths` | 6 | 121.5 s |
| `os.path.realpath` (`_realpath`) | 257,784 | 41.1 s |
| `_contains` (`root in candidate.parents`) | 257,784 | 34.9 s |
| `_display_path` (`relative_to`) | 257,784 | 31.7 s |
| `posix.lstat` | 2,829,438 | 11.0 s (self) |

Every discovered candidate is resolved **three times** — once by
`authorize_tool`'s preflight, once by `authorize`, once by `revalidate` — and
each resolution makes one `lstat` per path component, builds every
`Path.parents` entry, and computes a display path nobody reads. `await
policy.authorize(...)` never suspends in Yolo or for in-scope paths, so a
6-call search holds the loop for its entire 85,928-call loop.

### 3.4 Journal costs (synthetic 9.2 MB session journal)

| Operation | Cost | Called |
|---|---:|---|
| `completed_background_subagent_pools()` | 296 ms | every logical-turn boundary (TUI and background sessions) |
| `find_subagent_pool()` | 241 ms | every `spawn_subagents` call |
| `fold_subagent_pool_lifecycle()` | 238 ms | `list_subagent_pools`, `collect_subagent_results` journal fallback |
| `_write` (write + `fsync`) | 1.2 ms each | every journal record: usage per provider call, worker results, agent messages |

These are linear in journal size and run on the loop. They are small next to
R1 today and grow without bound with session length.

## 4. Root causes, ranked

| ID | Cause | Where (at `79984e0`) | Measured | PRD |
|---|---|---|---|---|
| **R1** | Every discovered path is authorized on the event loop, three canonicalisations each, before any filtering | `src/agenthicc/tools/fs/__init__.py:159-191` (`_authorize_discovered_paths`); `src/agenthicc/tools/workspace_access.py:138-188` (`resolve`), `:500` (`authorize_tool`), `:711` (`authorize`), `:223` (`revalidate`), `:900-921` | 93.7 s single stall; 97.6 % of stall time | TUIR-01 |
| **R2** | Discovery walks and authorizes everything, then filters: `rglob("*")` descends `node_modules/`, the `.gitignore` filter and `is_file()` run after authorization, and `search_files`' 1,000-result cap is applied after all of it | `fs/__init__.py:671-707` (`search_files`), `:732-776` (`grep_files`), `:27-56` (`_git_keep_filter`) | 14,300 candidates authorized per search for ≤ 1,000 results | TUIR-02 |
| **R3** | Concurrent workers' filesystem walks run in unbounded threads and contend for the GIL with the loop | `fs/__init__.py:671`, `:736`; worker tools run with `parallel_tool_calls=True` | 12 stalls of 100–415 ms after R1 is offloaded | TUIR-02 |
| **R4** | Subagent journal queries re-read and re-parse the whole journal, per kind | `src/agenthicc/memory/journal.py:1414` (`_fold_subagent_kind`), `:1236`, `:1279`, `:1317` | 240–300 ms per call at 9 MB | TUIR-03 |
| **R5** | Every journal record is written and `fsync`ed synchronously on the loop | `journal.py:291-316` (`_write`) | 1.2 ms locally per record; 10–100 ms on slow or network disks | TUIR-03 |

### Findings that are not causes

- **F1 — the `◆ 7 need you` badge.** It counts *background sessions* (PRD-141)
  waiting for approval or input or orphaned, published by
  `src/agenthicc/tui/attention.py:146-183`, not subagents. It is event-driven
  (a file watcher) and does not poll on the loop. Shown beside a running pool it
  reads as "seven subagents need you", which is wrong. TUIR-05.
- **Rendering.** Signals request frames from `FrameScheduler`
  (`src/agenthicc/tui/workspace/frames.py`), which coalesces every request
  before the next frame. Pool-state publishes per progress event are cheap
  frozen-dataclass replacements; rendering did not appear in any profile.
- **Redaction.** `_display_fields` redacts bounded worker text a handful of
  times per worker; it did not appear in the profile.
- **Transcripts.** Written on their own thread; 0.17 s cumulative over a run.

## 5. Requirements every fix must keep

- **The security boundary does not weaken.** A discovered path that resolves
  outside the workspace through a symlink is still decided by the full policy,
  including Safe-mode approval and Plan-mode denial. A fast path may only skip
  work whose outcome is already known to be "in scope".
- **Durability does not weaken.** A journal record is on disk before the state
  it describes is exposed (the worker-result-before-completion rule).
- **Results do not change** for in-scope projects: the same matches, order and
  caps.

## 6. Success criteria for the series

With six workers over the 33,000-file project, measured by TUIR-04's
benchmark:

- maximum loop stall < 100 ms, p99 < 25 ms;
- one `search_files("*.py")` on the main loop stalls it < 50 ms;
- `search_files` and `grep_files` finish at least 5× faster than today;
- turn-boundary journal work < 10 ms regardless of journal size.
