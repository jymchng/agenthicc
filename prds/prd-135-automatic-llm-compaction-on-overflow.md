---
title: "PRD-135: Automatic LLM Conversation Compaction on Context Overflow"
status: implemented
version: 1.0.0
created: 2026-06-23
related: [prd-119-conversation-compaction, prd-129-conversation-durability, prd-132-context-reuse, prd-133-context-window-overflow-guard]
---

> **Status:** Implemented — all phases (A exact-count trigger, B map-reduce, the
> ladder ordering, C one representation, D notice + convergent error).  See
> Features PRD §46 and the suites `test_skill_memory_summarization.py`
> (lauren-ai) and `test_compactor.py` (agenthicc).

# PRD-135 — Automatic LLM Compaction on Context Overflow

## Problem

PRD-133 made a context-length 400 *structurally impossible*, but its response to
"the request doesn't fit" is **lossy**:

- **Layer C** truncates the middle out of oversized tool-result/text blocks.
- **Layer E** raises `AgentContextOverflowError` for an irreducible item.

Neither uses the LLM to *preserve the meaning* of the conversation. Truncation
silently discards the middle of a large tool result; the error aborts the turn.
What the agent actually wants when it runs out of room is what a human would do:
**summarise the older history and keep going.**

agenthicc already *has* that capability — and it still failed. Two LLM
compaction mechanisms exist, but both are wired to the wrong trigger and neither
is reached by PRD-133's accurate overflow signal:

- **PRD-119 `compact_memory`** fires when `token_estimate` (char/4) ≥
  `compact_threshold_tokens` (**1,000,000**). On a 200k model that threshold is
  *above the window*, so it never fires; and char/4 under-counts code/JSON
  (RC4), so even on a 1M model it fires late. This is precisely why the original
  `code_plan` turn 400'd *despite compaction being enabled*.
- **lauren-ai `_summarize_memory`** fires at `summarize_at × memory_window_tokens`
  using the same char/4 estimate — same blind spot.

**One-sentence root cause:** *the only mechanism that measures context
accurately (PRD-133's send-time guard) responds with lossy truncation, while the
LLM-compaction mechanisms trigger on an under-counting char/4 estimate and so
fire late or never — and the summariser's own input is unbounded, so it can
overflow too.*

## Root-cause analysis

| # | Mechanism | Trigger | Representation | Gap |
|---|---|---|---|---|
| M1 | `compact_memory` (PRD-119) | char/4 ≥ 1M, **pre-run** | whole history → 2 messages | trigger never fires on small models; one LLM call over the *whole* transcript can itself exceed the window |
| M2 | `_summarize_memory` (lauren-ai) | char/4 ≥ `summarize_at×window`, **per-turn** | older turns → summary in system prompt, keep recent 6 | char/4 under-count; one LLM call; disjoint from M1's representation |
| M3 | `_fit_to_context` (PRD-133 D) | **exact `count_tokens`**, send-time | lossy block truncation | accurate, but compresses by *deleting characters*, not summarising |
| M4 | overflow error (PRD-133 E) | irreducible, send-time | — | aborts; and (below) is swallowed into a re-run loop |

Three further structural problems:

- **RC-A — Accurate signal not wired to LLM compaction.** M3 has the only
  trustworthy measurement but never invokes M1/M2; it truncates instead.
- **RC-B — The compactor can overflow.** `compact_memory` feeds the entire
  transcript to a single `transport.complete` call (`compactor.py:81-90`). For a
  conversation larger than the window, that call 400s — auto-compaction fails
  exactly when it is most needed. (It previews tool results to 500 chars, which
  helps a single huge result but not a long *many-turn* history.)
- **RC-C — Two compaction representations.** M1 replaces memory with two
  messages; M2 keeps recent turns + a system-prompt summary. Two shapes, two code
  paths, two triggers — a dual-path muddle.
- **RC-D — The overflow error degrades to a blind re-run.** `AgentContextOverflowError`
  is neither a transient network error (`retry.py:112`) nor an HTTP 4xx
  (`_is_permanent_error` → `_http_status_code` is `None`), so it is **swallowed**
  (`agent_turn.py:628`) and the phase loop re-runs the *same* overflowing turn —
  wasteful and non-converging.

## Design goal

> When the assembled request exceeds the model's usable budget, **compress the
> conversation with an LLM first** (semantic, meaning-preserving), measured by
> the *exact* token count, with a compaction step that can never itself overflow.
> Lossy truncation and the hard error remain only as last-resort backstops for a
> single irreducible item.

## Proposed architecture — one accurate-count-driven compaction ladder

Replace M3's "truncate-or-error" response at the send-time choke point with an
escalating ladder. Each rung is tried in order, re-measuring with the exact count
between rungs:

```
1  Drop oldest whole turns        ── lossless within the kept window (exists)
2  LLM-compact older turns        ── the headline: summarise everything older than
                                     keep_recent into a dense summary; re-measure
3  Map-reduce compaction          ── if the slice to summarise is itself > window,
                                     chunk → summarise each → summarise the summaries
                                     (the compaction call's own input always fits)
4  Lossy block truncation (C)     ── only if a single *recent* turn still overflows
5  Graceful overflow error (E)    ── only if an irreducible mandatory item remains
```

Rungs 2–3 are the LLM compaction the user is asking for; rungs 4–5 are PRD-133's
existing backstops, now demoted *behind* compaction instead of being the answer.

### A — Trigger on the exact count, not char/4 (highest impact)

Drive compaction from the **exact `count_tokens`** budget that PRD-133 already
computes, not the char/4 estimate. Fire **proactively** at ~80–85 % of
`usable_context_budget` (before the 100 % wall) so the extra LLM call amortises
across the turn rather than blocking the final send. Reuse PRD-133's cheap-estimate
gate so the exact count (and thus the compaction check) is skipped on turns that
are comfortably small. This single change is why compaction will now fire when it
must — it removes RC-A and RC-D's root (the under-counting trigger).

### B — Make the compaction step overflow-proof (map-reduce)

Replace `compact_memory`'s single whole-transcript call with bounded map-reduce:
partition the to-summarise messages into chunks each ≤ the window (measured with
the accurate count), summarise each chunk, then summarise the concatenation of
chunk-summaries (recursing if needed). Now compaction is robust at *any*
conversation size — it can compress a history several times the window, which the
current one-shot call cannot (RC-B).

### C — One compaction representation

Collapse M1 and M2 into a single mechanism: **a dense rolling summary carried in
the system prompt + the last `keep_recent` turns verbatim** (lauren-ai's shape).
Rationale: it keeps recent tool context *live* (the agent can still act on the
last few tool results), the summary survives `snapshot()`/`restore()` (PRD-129),
and it composes with prompt caching (PRD-132). Retire `compact_memory`'s
two-message replacement and route agenthicc's PRD-119 path through the same
summariser (no dual paths; RC-C). The rolling summary is *append-merged* each time
(summary + newly-aged turns → new summary) so nothing is silently lost.

### D — Reactive backstop: compact-then-retry

If overflow is still raised (a proactive pass was bypassed, or a borderline item),
catch `AgentContextOverflowError` in `_stream_with_retry`, run one compaction
pass, and retry the turn once — reusing PRD-126's snapshot-rollback so the retry
starts from a clean pre-turn history. This replaces today's blind swallow-and-rerun
(RC-D) with a *converging* response. It is a thin safety net once A–C are in place.

### E — Boundary & correctness safety

`keep_recent` must snap to a turn boundary so a `tool_use` is never split from its
`tool_result`, and the summarised prefix must leave no dangling `tool_use`
(reuse `_heal_dangling_tail`). The summary text is data, not instructions — it is
injected exactly where M2 already injects it (`_build_system_prompt`), so the
threat surface is unchanged.

## Recommendation & priority

| Priority | Item | Why |
|---|---|---|
| **1** | **A — exact-count trigger** | The reason compaction didn't fire. Small, decisive. |
| **2** | **B — map-reduce compaction** | Without it, auto-compaction fails on exactly the large histories that need it. |
| **3** | **C — one representation** | Removes the dual-path muddle; one summariser, one shape. |
| **4** | **Ladder ordering** | Compaction *before* truncation/error, at the accurate choke point. |
| 5 | **D — compact-then-retry** | Backstop; converts the blind re-run into a converging one. |

Ship **A + B + the ladder ordering** first — that delivers "agenthicc auto-compacts
with an LLM when context doesn't fit," robustly and at the right moment. **C** and
**D** follow to retire the legacy dual paths and harden the residual case.

## Phased plan

| Phase | Scope |
|---|---|
| 1 | **A**: exact-count compaction trigger (lauren-ai `_should_summarize` + agenthicc `should_compact`) derived from `usable_context_budget`, gated by the cheap estimate. Tests: compaction fires at the budget, not at char/4×1M. |
| 2 | **B**: map-reduce summariser (`_summarize_memory` / `compact_memory`) that chunks oversized slices so the compaction call never overflows. Tests: a 3×-window synthetic history compacts without a single over-window LLM call. |
| 3 | **Ladder**: at the send choke point, escalate drop → LLM-compact → map-reduce → truncate (C) → error (E), re-measuring with the exact count between rungs. Tests: an oversized turn is *summarised* (not truncated) when summarisation suffices; truncation only when a single recent turn is itself too big. |
| 4 | **C**: unify to the rolling-summary representation; retire `compact_memory`'s 2-message replacement; route PRD-119 through the shared summariser. Update PRD-119 tests. |
| 5 | **D**: catch `AgentContextOverflowError` in `_stream_with_retry`, compact + retry once; emit a user-visible "⎋ Compacting to fit…" event. |

## Acceptance criteria

| # | Criterion |
|---|---|
| 135.1 | Compaction fires based on the **exact `count_tokens`** budget, not char/4 — it triggers on a 200k model (where the 1M threshold never could) |
| 135.2 | When the request exceeds the budget, older turns are **LLM-summarised** (meaning preserved) *before* any lossy block truncation |
| 135.3 | The compaction LLM call **never itself overflows** — a history several times the window is summarised via map-reduce |
| 135.4 | Recent `keep_recent` turns stay verbatim; `tool_use`/`tool_result` pairing is never split by the compaction boundary |
| 135.5 | There is **one** compaction representation (rolling summary + recent turns); `compact_memory`'s 2-message path is retired (no dual paths) |
| 135.6 | Lossy truncation (PRD-133 C) runs only when a single recent turn still overflows after compaction; the overflow error (E) only for a truly irreducible item |
| 135.7 | `AgentContextOverflowError` triggers a compact-then-retry (PRD-126 rollback), not a blind phase re-run |
| 135.8 | The user sees a clear "⎋ Compacting conversation…" event when auto-compaction fires |
| 135.9 | Regression: the `code_plan` "concise the docs" scenario auto-compacts and completes — no truncated tool output, no overflow error |

## Evidence index

| Claim | Location |
|---|---|
| `compact_memory` triggers on char/4 ≥ 1M | `memory/compactor.py:47-48` |
| `compact_memory` feeds the whole transcript to one call (can overflow) | `memory/compactor.py:81-90` |
| `compact_memory` replaces memory with 2 messages | `memory/compactor.py:93-96` |
| `_summarize_memory` triggers on char/4 × window | `_runner.py:_should_summarize` |
| `_summarize_memory` keeps recent 6, summary → system prompt | `_runner.py:_summarize_memory`, `_build_system_prompt` |
| PRD-133 guard truncates / raises (no LLM compaction) | `_runner.py:_fit_to_context`, `_exceptions.AgentContextOverflowError` |
| Overflow error is not transient-network → not retried | `runners/retry.py:112` |
| Overflow error is not HTTP-4xx → swallowed → phase re-run | `runners/agent_turn.py:622-628`, `_is_permanent_error` |
| Per-turn summarisation already wired into the run loop | `runners/agent_turn.py` (`summarize_at`, `summary_model`, `context_window`) |
