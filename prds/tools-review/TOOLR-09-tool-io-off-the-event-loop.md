---
title: "TOOLR-09: Tool I/O off the event loop, and a race-free file cache"
status: Proposed
version: 1.0.0
date: 2026-11-04
scope: "the tools that still do blocking file, hash, SQLite or tree-walk work on the asyncio loop, and the read-cache store race"
related_prds: [TOOLR-00, TOOLR-01, TUIR-01, TUIR-02, TUIR-03, TUIR-04, PRD-132]
tags: [tools, performance, event-loop, correctness]
---

# TOOLR-09: Tool I/O off the event loop

## 1. Problem

TOOLR-00 F8, F9 and P5. TUIR-00 showed that tool work on the event loop
freezes the TUI, and that six subagents multiply it. TUIR-01 and TUIR-02 cover
discovery. These blocking sites remain:

- `grep_file`, `apply_diff`, `checksum_file`, `truncate_file` and `touch_file`
  call `read_text`, `read_bytes`, `open` and `write_text` directly
  (`tools/fs/agent_tools.py:265-575`). `checksum_file` hashes a whole file
  (up to any size) on the loop.
- `batch_read`, `batch_write`, `batch_delete`, `batch_move` and `batch_copy`
  call the synchronous backend methods directly, for any number of files.
- `read_file` calls `WorkspaceFileCache.get_fresh`, which does a stat and a
  SQLite query on the loop. After the threaded read it calls `store`, which
  does a stat, a hash, an `INSERT` and a `commit` (an fsync) on the loop
  (`tools/fs/file_cache.py:63,89`).
- `search_agenthicc_source` and `search_agenthicc_docs` walk and read
  agenthicc's tree synchronously (`tools/introspect/__init__.py:751`).

There is also a correctness race in the cache. `store` stats the file *after*
the content was read. A write that lands between the read and the stat
records the new `(mtime, size)` against the old content, and the cache then
serves that stale content as fresh until the file changes again. This is read
from the code; a local run of 200 rapid write/read pairs did not reproduce it,
because the window is narrow, but concurrent subagents and editors widen it.

## 2. Goals

1. **No blocking I/O in a tool coroutine.** Every filesystem, hash, SQLite and
   subprocess-free walk in a tool runs in `asyncio.to_thread` (or the TUIR-02
   limiter for walks). The rule is enforced by a test. It runs each built-in
   tool against a 50 MB file and a 5,000-file tree under a loop-lag sampler,
   and asserts no stall above 50 ms (TUIR-04's harness).
2. **The cache stamps what it read.**
   - `ReadFileTool` stats the file, reads it, and stats again in the same
     thread.
   - It stores the content only when both stats agree, with the stamp taken
     *before* the read.
   - A disagreement returns the content uncached.
   - `get_fresh` and `store` run in the worker thread, on that thread's own
     connection.
3. **Writes invalidate.** Every TOOLR-01 write updates the cache entry for its
   path with the bytes it wrote and their stamp, so the next read is a hit
   with the right content rather than a stat-dependent guess.
4. **A content check where mtime is coarse.** On filesystems whose mtime
   granularity is ≥ 1 ms, detected once per root, a cache hit also compares
   the stored SHA-256 with a hash of the first and last 64 KB of the file.

## 3. Non-goals

- Making the cache distributed or shared across machines.
- Discovery cost, which belongs to TUIR-01 and TUIR-02.

## 4. Acceptance criteria

1. `checksum_file` on a 500 MB file stalls the loop for less than 50 ms.
2. `batch_read` of 2,000 files stalls the loop for less than 50 ms.
3. A test that writes between the read and the stat, through an injected hook
   in `ReadFileTool`, never gets a later cache hit with the old content.
4. After `write_file`, the next `read_file` is a cache hit with the new
   content.
5. `search_agenthicc_source("def ")` stalls the loop for less than 50 ms.
