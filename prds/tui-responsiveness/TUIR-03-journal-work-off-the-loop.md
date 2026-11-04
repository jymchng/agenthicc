---
title: "TUIR-03: Journal work off the event loop — an incremental subagent projection and a durable writer thread"
status: Implemented with amendments (2026-11-04; tools wave 5, pending merge)
version: 1.0.0
date: 2026-11-03
scope: "the cost of ConversationJournal on the event loop: subagent queries that re-read the whole file, and synchronous write+fsync per record — without weakening the write-before-expose durability rule"
related_prds:
  - TUIR-00
  - TUIR-04
  - SUBAGENT-07
  - SUBAGENT-11
  - PRD-157
tags:
  - performance
  - durability
  - journal
  - event-loop
---

# TUIR-03: Journal work off the event loop

## Implementation status (2026-11-04)

Delivered in tools wave 5 (branch `tools-wave-5-journal-and-lag`).

- **An incremental subagent projection (goals 1 and 2).**
  - `ConversationJournal` keeps the subagent and `agent_message_event` records
    in memory. They are built by one pass over the file on first query, which
    stops at the first torn line, and extended by every append.
  - Every subagent fold, `find_subagent_pool`,
    `completed_background_subagent_pools` and `subagent_pool_records` filter
    that view and never re-read the file.
  - `_fold_subagent_kind` is replaced by `_subagent_kind`, which reads the view.
- **One durability table (goal 4).** `RECORD_DURABILITY` declares every record
  kind. A test fails when a kind written by the journal is missing from it.
- **Advisory writes leave the loop.**
  - `usage_record` is the only advisory kind. It is queued and written by one
    writer thread, which fsyncs outside the lock.
  - Queued advisory records are written before the next durable record, so
    sequence order on disk is unchanged.
  - Every read drains the queue first, and `close()` flushes it.

Amendments:

- **Goal 3, durable writes.** Durable writes stay synchronous on the calling
  thread: write, fsync, then return. An awaited writer thread would have
  changed every caller to `await` and moved the write-before-expose ordering
  across a thread boundary. At 1.2 ms locally, the fsync was not what TUIR-00
  measured; the 240–300 ms folds were. Group commit for advisory records is
  one fsync per writer wake-up, not a 50 ms timer.
- **Goal 5, the resume read.** Not changed. Resume still reads the file once
  before the TUI accepts input, on the loop, as before.
- The property test lives in `tests/unit/test_journal_projection.py`, not
  `test_conversation_journal.py`.

Tests (`tests/unit/test_journal_projection.py`):

- with a 50 MB journal, each query takes under 1 ms (criterion 1);
- a Hypothesis test checks the projection equals a fresh read, including a
  torn tail and a reopen (criterion 3);
- with a 50 ms injected fsync, usage appends stay under 10 ms (criterion 5);
- sequence order is kept across queued and durable records;
- every kind is declared (criterion 6).

The existing worker-result ordering tests still pass (criterion 4).

## 1. Problem

TUIR-00 R4 and R5.

- **Whole-file folds.** `_fold_subagent_kind` (`src/agenthicc/memory/journal.py:1414`)
  opens the journal and `json.loads` every line to return one record kind.
  `fold_subagent_pool_lifecycle` (`:1236`) calls it for six kinds — six full
  passes. Its callers run on the loop:
  - `completed_background_subagent_pools` (`:1317`) at **every logical-turn
    boundary** (`subagents/delivery.py:101`, from `TUISession` and the
    background worker);
  - `find_subagent_pool` (`:1279`) on **every** `spawn_subagents` call
    (`subagents/tool.py:601`);
  - `list_subagent_pools` and the `collect_subagent_results` journal fallback
    (`subagents/communication.py:1099-1101`, `:1316`).
  At 9.2 MB they cost 240–300 ms each (TUIR-00 §3.4), and they grow linearly
  with the session.
- **Synchronous `fsync`.** `_write` (`:291-316`) writes and `fsync`s each record
  on the calling thread — the loop. Usage records (one per provider call, per
  worker), worker results and agent messages all pay it. 1.2 ms locally; 10–100
  ms on slow, encrypted or network storage, multiplied by six workers.

## 2. Goals

1. **An incremental subagent projection.** `ConversationJournal` keeps an
   in-memory `SubagentJournalProjection`: per pool, the folded lifecycle
   record, its worker results, its delivery flag and its fingerprint key. It is
   built by one pass when the journal opens (or lazily on first query) and
   updated by every subagent `_write` in the same call. `fold_subagent_pool_lifecycle`,
   `find_subagent_pool`, `completed_background_subagent_pools`,
   `delivered_subagent_pool_notifications`, `subagent_pool_records` and
   `fold_subagent_worker_results` read the projection: O(pools) or O(1), never
   O(journal). `_fold_subagent_kind` is deleted.
2. **A projection equals a re-read.** The projection and a fresh fold of the
   file are equal after any sequence of writes, a crash with a torn tail, a
   resume and a compaction (property-tested).
3. **Writes leave the loop, durability does not.** Journal appends go to one
   `JournalWriter` thread, in order. A write that later state depends on —
   `subagent_worker_result` before completion is exposed, the pool lifecycle
   before the tool result returns — is awaited (`await journal.write_durable(...)`)
   and completes only after its `fsync`. Usage and other advisory records are
   submitted without waiting and group-committed (one `fsync` per batch, at
   most every 50 ms). `close()` drains and syncs.
4. **One place decides.** Each record kind is declared `durable` or
   `advisory` in one table beside the record writers; a test fails when a new
   kind is not declared.
5. **Bounded reads stay bounded.** A resume that must read the whole file does
   so once, in a thread, before the TUI accepts input.

## 3. Non-goals

- Changing the journal's on-disk format or record schema.
- Asynchronous provider memory writes (`JournaledShortTermMemory` ordering is
  unchanged; its records are `durable`).

## 4. Acceptance criteria

1. With a 50 MB journal, `completed_background_subagent_pools()` and
   `find_subagent_pool()` take < 1 ms (today ~300 ms at 9 MB).
2. Turn-boundary delivery (`deliver_background_pool_notices`) costs < 10 ms
   regardless of journal size (TUIR-04 benchmark).
3. The projection equals a fresh fold after randomized write sequences,
   including a torn final line and a reopen (Hypothesis test in
   `tests/unit/test_conversation_journal.py`).
4. A worker's result record is on disk before `subagent_worker_done` /
   `SubagentCompleted` is emitted (existing ordering test still passes with the
   writer thread).
5. With an injected 50 ms `fsync`, six workers' usage records cause no loop
   stall > 10 ms (the writer thread absorbs them).
6. Every record kind is declared `durable` or `advisory`.
