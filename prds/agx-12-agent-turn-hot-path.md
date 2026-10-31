---
title: "AGX-12: Agent-turn hot path — copy-on-write memory and incremental validation"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P1
series: AGX
related_prds: [agx-00, agx-14, prd-169, prd-182, prd-242, prd-247, prd-250]
tags: [performance, memory, agent-turn, lauren-ai, replay-safety]
---

# AGX-12 — Agent-turn hot path: copy-on-write memory and incremental validation

## Implementation status (2026-10-23)

The agenthicc side — §4.1 and §4.3 — is implemented in
`JournaledShortTermMemory`. §4.2 (incremental tool-history index) and §4.4
(cached token estimates) are implemented in lauren-ai's `ShortTermMemory` and
runner, released as 1.6.1, and consumed by agenthicc's dependency pin.

- **Validate first (§4.1).** `ensure_valid_and_persist()` returns `False` before
  copying when `validate_tool_history()` is OK; the repair path is unchanged.
- **Copy-on-write snapshots (§4.3).** `snapshot()` keeps the current message
  references (plus the summary and a deep copy of the small active tool
  exchange). lauren-ai's `_stream_loop` and the agent turn's safe-step
  checkpoints both go through it.

### Upstream remainder release evidence (2026-10-29)

The §4.2 incremental tool-history index and §4.4 cached token estimates are
upstream lauren-ai changes, not agenthicc-owned source. Upstream PR #5 merged
as `af94fcad`; release `lauren-ai==1.6.1` was published to PyPI on 2026-10-29.
The upstream release validation passed 1,421 unit tests (1 skipped) and 1,010
integration tests; lint, format, typecheck, build, build-check, and the PR CI
matrix passed. agenthicc now requires `lauren-ai>=1.6.1,<2`, with `uv.lock`
resolving 1.6.1.

With only agenthicc's `src/` on `PYTHONPATH` and no upstream source overlay, the
510-step cassette replay passed (5 passed) against the published package. The
combined copy-on-write and replay tests passed 11 tests. The performance
measurements and replay oracle below remain local diagnostic evidence; do not
attribute their source changes to agenthicc when they belong to lauren-ai.

Replay oracle (§4.5), 510-step hermetic `goal_flow` replay, each side run three
times on the development host (load average 35–55):

| Measure | Before | After |
|---|---:|---:|
| Provider request digest (IDs and temp paths normalized) | `b484c1c7…` | `b484c1c7…` (identical, every run) |
| Journal digest (IDs normalized; concurrent tool results ordered) | `269584b8…` | `269584b8…` (identical, every run) |
| Full-history deep copies | 2,245 | 35 (one per turn, outside the step loop) |
| Full `_validate_tool_history` scans | 2,935 | 1,771 |
| Snapshots checked against shadow deep copies | — | 1,091, 0 mismatches |
| CPU, uninstrumented | 344–357 s | 253–261 s |

The CPU budgets in §5 assumed all four changes; the remaining per-step cost
is lauren-ai's full validation scans and token estimates. Property tests in
`tests/unit/test_agent_turn_hot_path.py` check 10,000 seeded random histories
for the validate-first soundness condition, compare the new
`ensure_valid_and_persist` with the replaced implementation (returned flag,
errors, messages and journal records) and compare copy-on-write with deep
snapshots across random append/snapshot/restore/repair/trim sequences; a
counting test asserts that a valid history is not copied.

## 1. Summary

Every provider step deep-copies the whole conversation about seven times and
re-validates its whole tool history about six times, so per-step bookkeeping
grows with conversation length and a long session spends most of its CPU on
copies. The 510-step `goal_flow` replay costs 165 s of CPU, about 70% of it in
`copy.deepcopy` and `_validate_tool_history`. Validate before copying, keep an
incremental tool-call index, take copy-on-write snapshots and cache token
estimates — proven equivalent against the recorded replay — so bookkeeping is
O(new messages) per step.

## 2. Problem and evidence

### 2.1 Measured (AGX-00 §4.4; PRD-250 status section)

510-step hermetic `goal_flow` replay
(`tests/integration/test_goal_flow_cassette_replay.py`):

| Measure | Value |
|---|---:|
| CPU, uninstrumented | 165 s |
| CPU, under branch coverage | 481 s |
| Full-history deep copies | 3,381 (22 million recursive `deepcopy` calls) |
| — from lauren-ai `ShortTermMemory.snapshot()` | 2,179 |
| — from `JournaledShortTermMemory.ensure_valid_and_persist()` | 1,164 (two per call) |
| Full `_validate_tool_history` scans | 2,935 (4.4 million `extract_tool_result_ids` calls) |
| Share of CPU in deep copies and validation (sampling profile) | ≈ 70% |

Per step that is ≈ 6.6 full copies and ≈ 5.8 full scans; both are O(messages),
so a session's total bookkeeping is O(n²) in its length.

### 2.2 Call sites

- **Preflight, every provider step.** lauren-ai's
  `_validate_memory_before_provider` calls agenthicc's
  `JournaledShortTermMemory.ensure_valid_and_persist`
  (`src/agenthicc/memory/journaled.py:339`):

  ```python
  before = copy.deepcopy(self._messages)
  candidate = copy.copy(self)
  candidate._messages = copy.deepcopy(before)
  candidate._active_exchange = None
  candidate.ensure_valid()                 # validates twice inside lauren-ai
  report = candidate.validate_tool_history()
  ...
  changed = candidate._messages != before  # deep equality over the history
  ```

- **Safe-step snapshots.** `AgentTurnRunner._stream._advance_safe_step`
  (`src/agenthicc/runners/agent_turn.py:2507`) calls
  `session_memory.snapshot()` on step signals, and lauren-ai's `_stream_loop`
  takes its own snapshot; `ShortTermMemory.snapshot()` returns
  `copy.deepcopy(self._messages)`.
- **Context fitting.** `_fit_to_context` → `_cheap_token_estimate` →
  `estimate_message_tokens` walks every message every step (≈ 8% of CPU).

### 2.3 The common case needs no copy

On a valid history, lauren-ai's `ensure_valid()` changes nothing:
`_repair_tool_history` re-validates and returns `list(messages)` — the same
message objects — when the report is OK. In that case `ensure_valid_and_persist`
copies the history twice, validates it three times and compares it deeply only
to conclude `changed = False`.

## 3. Goals and non-goals

**Goals**

1. Per-step bookkeeping is O(messages added since the last step), not O(history).
2. Results are identical: same provider requests, journal bytes, receipts and
   recovery behaviour, proven against the recorded replay.
3. Repair paths (PRD-169, PRD-247) keep their exact semantics.

**Non-goals**

- Changing what is persisted or replayed.
- Compaction policy (PRD-242).

## 4. Design

### 4.1 Validate first (agenthicc, no upstream change)

```python
def ensure_valid_and_persist(self) -> bool:
    if self.validate_tool_history().ok:
        return False
    ...existing copy, repair, validate and commit path...
```

Equivalent by construction (§2.3); removes two deep copies, two validations
and one deep comparison from every step.

### 4.2 Incremental tool-history index (lauren-ai)

`ShortTermMemory` maintains, as messages are appended or committed:

- the set of tool-call IDs awaiting results and the index of the assistant
  message that issued them;
- whether every result so far is adjacent to its call.

`validate_tool_history()` returns from the index in O(1) when no call is open
and adjacency holds; it falls back to the full scan after operations that
rewrite history (restore, repair, compaction, rollback), which rebuild the
index once. agenthicc's journal subclass updates the index through the same
append and commit hooks.

### 4.3 Copy-on-write snapshots (lauren-ai and agenthicc)

- Treat messages as values: every code path that changes a message replaces it
  rather than mutating it in place (repair already deep-copies before editing).
  A debug-mode test wraps messages in read-only views and runs the full suite to
  find in-place mutations.
- `snapshot()` then stores a tuple of message references plus the summary and
  active exchange — O(n) pointer copy, no deep copy — and restoring rebuilds the
  list from that tuple. A persistent sequence can make it O(1) later if
  profiles call for it.

### 4.4 Cached token estimates

Each message's estimate is computed once and cached by message identity;
`_fit_to_context` keeps a running total updated on append, replace and
truncate.

### 4.5 Proving equivalence

- **Replay oracle.** Run the 510-step replay before and after each step of this
  PRD with request capture enabled: provider request payloads, journal files and
  receipts must be byte-identical.
- **Property tests.** For random histories — including interrupted tool calls,
  non-adjacent results, unknown IDs and the PRD-247 recovery cases — the
  incremental index and the full scan return identical reports, and validate-first
  returns the same `changed` flag and persisted state as the old path.

## 5. Budgets and acceptance criteria

| Measure | Budget |
|---|---:|
| Bookkeeping per provider step at 500 messages | ≤ 5 ms |
| 510-step replay CPU, uninstrumented | ≤ 40 s (from 165 s) |
| 510-step replay under branch coverage | ≤ 150 s (from 481 s) |
| Deep copies of the full history per step, valid history | 0 |

1. The replay oracle passes byte-for-byte.
2. Property tests pass for at least 10,000 generated histories per run.
3. `ensure_valid_and_persist` performs no copy when the history is valid
   (asserted with a counting wrapper).
4. AGX-14 records the replay CPU budget, so regressions fail CI.

## 6. Rollout

1. §4.1 in agenthicc alone (largest single win, no upstream dependency).
2. §4.4 token-estimate cache.
3. §4.2 and §4.3 in lauren-ai, released and pinned, with the replay oracle run
   against the new pin.

## 7. Risks

- **Hidden in-place mutation** would make shallow snapshots unsafe. Mitigation:
  the read-only-view test mode; snapshots stay deep until that test passes.
- **Index drift** after an unusual history operation. Mitigation: every
  history-rewriting method rebuilds the index; debug builds cross-check the
  index against a full scan on each validation.

## Sources

- PRD-250 status section (profile and counts): [prd-250](prd-250-code-quality-evaluation-and-improvement-programme.md)
