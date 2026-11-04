---
title: "TUIR-02: Bounded filesystem discovery — prune before walking, filter before authorizing, share a limiter"
status: Proposed
version: 1.0.0
date: 2026-11-03
scope: "the cost and concurrency of discovery tools: what they walk, in which order they filter, how they stop, and how many run at once across a session and its subagents"
related_prds:
  - TUIR-00
  - TUIR-01
  - TUIR-04
  - PRD-133
tags:
  - performance
  - filesystem
  - subagents
  - concurrency
---

# TUIR-02: Bounded filesystem discovery

## 1. Problem

TUIR-00 R2 and R3.

- **Walk everything, filter last.** `search_files` (`src/agenthicc/tools/fs/__init__.py:671-707`)
  materialises `list(root.rglob(pattern))`, authorizes every entry, and only
  then applies `.gitignore` (`_git_keep_filter`, `:27-56`), `is_file()` and the
  1,000-result cap. `grep_files` (`:732-776`) does the same with `rglob("*")`,
  so it enumerates and authorizes every directory and file — including all of
  `node_modules/` — before it reads one line. In a directory that is not a git
  repository, `_git_keep_filter` returns `None` and nothing is pruned at all.
  The synthetic project's search authorized ~14,300 candidates to return 1,000.
- **`git ls-files` per call.** `_git_keep_filter` spawns `git` and builds a set
  of every tracked path and every parent prefix on each call — once per
  `search_files`, once per `grep_files`, per worker.
- **Unbounded concurrent walks.** Each worker's tool calls run with
  `parallel_tool_calls=True`, and each walk is its own `asyncio.to_thread`. Six
  workers run six CPU-heavy, pure-Python walks at once; they compete with the
  event loop for the GIL. With TUIR-01's offload alone, 12 stalls of
  100–415 ms remained and the pool got slower (TUIR-00 §3.1).

## 2. Goals

1. **Prune during the walk.** Discovery uses one `os.scandir`-based walker
   (shared with TUIR-01's `discover_authorized`) that skips ignored directories
   *before* descending:
   - in a git repository, the `.gitignore` rules (via one cached
     `git ls-files --cached --others --exclude-standard -z --directory` per
     root and index mtime, or a `pathspec`-equivalent matcher);
   - outside a repository, a documented default ignore set
     (`.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `.tox`, `.nox`,
     `dist`, `build`, `.mypy_cache`, `.pytest_cache`), overridable by an
     `include_ignored: bool = False` tool argument.
2. **Filter before anything expensive.** Order inside the walk: ignore rules →
   entry type (from `DirEntry`, no `stat`) → glob match → result cap → TUIR-01
   authorization → content read. `grep_files` reads only files that survive
   every earlier step and stops at `max_results`.
3. **Stop early and say so.** Every discovery tool has a candidate budget
   (default 50,000 entries examined) and a wall-clock budget (default 10 s).
   Reaching either returns what was found with `truncated: true` and the reason
   (`result_cap`, `examined_cap`, `time_cap`) instead of walking on.
4. **One limiter for heavy filesystem work.** A session-wide
   `FilesystemWorkLimiter` (an `asyncio.Semaphore`, default 2 permits, set by
   `execution.max_fs_walks`) bounds concurrent walks across the parent agent
   and every subagent. Subagents inherit the session's limiter; a nested pool
   does not get its own. Waiting for a permit suspends the coroutine, so the
   loop stays free.
5. **Cooperative chunks.** A walk that must run on the loop (none should after
   TUIR-01) yields every 2 ms of work; a threaded walk releases the GIL at
   least every 1,000 entries (`os.scandir` batches, `time.sleep(0)`), so the
   loop thread is scheduled.
6. **Cached ignore data.** The git file list is cached per repository root and
   invalidated by the index file's `mtime_ns` and size; workers in one pool
   share it.

## 3. Non-goals

- A content index or a search daemon. Bounded walking is enough for the
  measured sizes; an index is a separate decision.
- Changing `read_file` / `read_lines` limits (PRD-133 owns output bounds).

## 4. Acceptance criteria

1. In a git repository, `search_files` and `grep_files` never `scandir` a
   directory that `.gitignore` excludes (asserted with a spy on the walker).
2. Outside a repository, `node_modules/` is skipped by default and walked with
   `include_ignored=true`.
3. `search_files("*.py")` over TUIR-04's project returns the same first 1,000
   sorted matches as today (non-ignored files), at least 5× faster.
4. Reaching each budget returns `truncated: true` with its reason; nothing
   raises.
5. With six workers each calling `search_files` and `grep_files`, at most
   `execution.max_fs_walks` walks run at once (asserted by a counting
   limiter), and TUIR-04's benchmark meets max stall < 100 ms, p99 < 25 ms.
6. `_git_keep_filter` runs `git` at most once per repository per index change
   across a pool (asserted by counting subprocess calls).
