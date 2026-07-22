---
title: "PRD-137: Faithful Extended-Thinking Block Round-Trip"
status: implemented
version: 1.0.0
created: 2026-06-23
related: [prd-133-context-window-overflow-guard, prd-134-tool-call-validation, prd-135-automatic-llm-compaction]
---

> **Status:** Implemented in lauren-ai (A+B+C+D+E+F). Streaming captures the
> signature + redacted blocks; the runner reconstructs ordered `thinking_blocks`;
> memory stores them first (thinking → text → tool_use); the serializer
> round-trips them; the context guard never truncates them; heal/trim keep them
> attached. Tests: `lauren-ai/tests/unit/test_thinking_roundtrip.py` (16). See
> Features PRD §48.

# PRD-137 — Preserve & Round-Trip Extended-Thinking Blocks

## Problem

A `code_plan` run against `deepseek-v4-flash` (via the Anthropic-compatible
openmodel gateway) fails with a provider **400**:

```
The `content[].thinking` in the thinking mode must be passed back to the API.
```

This is Anthropic's **extended-thinking passback rule**: when a response is in
thinking mode and the assistant turn contains `tool_use`, the assistant's
`thinking` (and `redacted_thinking`) blocks — *with their cryptographic
signatures* — must be sent back **before** the `tool_use` blocks on the next
request. The model emitted thinking blocks; agenthicc dropped them; the next
turn (sending tool results) was rejected.

Note: agenthicc never set `thinking=True` — the model returns thinking blocks
*anyway* (deepseek is a reasoning model, and the gateway enforces the Anthropic
rule). So the fix is not "stop requesting thinking"; it is **faithfully
round-tripping whatever thinking the model emits.**

## Root-cause analysis

The thinking-block round-trip is broken at **every** stage of the streaming path
(the path agenthicc uses via `run_stream`):

| # | Gap | Location |
|---|-----|----------|
| RC1 | Streaming captures `thinking_delta` (text) but **not** `signature_delta`; no `redacted_thinking` handling → the signature (and redacted data) is lost | `_transport/_anthropic.py:710-755` (only `text_delta`/`thinking_delta`/`input_json_delta`) |
| RC2 | `run_stream`'s `synthetic_completion` is built with `content` + `tool_calls` only — **no `thinking_blocks`** | `_agents/_runner.py:~1463` |
| RC3 | `ShortTermMemory.add_assistant` stores only `text` + `tool_use` blocks — **drops `thinking_blocks`** | `_memory/__init__.py` `add_assistant` |
| RC4 | `_content_block_to_anthropic` has no `thinking`/`redacted_thinking` case and **raises `ValueError` on unknown types** → thinking can't be serialized back | `_transport/_anthropic.py:84-125` |

The non-streaming path *does* capture `thinking_blocks` with signatures
(`_extract_thinking_blocks`, `_anthropic.py:297-320`, set on the Completion at
`:673,682`) — but RC3 + RC4 still drop/reject them, so it is equally broken for
multi-turn tool use.

**One-sentence root cause:** *the model's `thinking`/`redacted_thinking` blocks
and their signatures are not preserved through stream-accumulation → memory →
request serialization, so a multi-turn tool-use request in thinking mode is sent
without the required thinking and is rejected.*

### Why truncation/compaction is NOT the cause (but is a constraint)
A thinking block's signature covers its **exact** text — truncating it would
invalidate the signature. The PRD-133 guard already happens to exempt thinking
(its text lives under a `"thinking"` key, not `"text"`/`"content"`, so
`_block_text_field` returns `None` and `_shrink_message` skips it). The fix must
**lock that invariant in**, not rely on it incidentally.

## Design goal

> Faithfully round-trip every `thinking` / `redacted_thinking` block the model
> emits: **capture** it (with signature) from the stream, **store** it on the
> assistant turn (before `tool_use`), **serialize** it back verbatim, and
> **never mutate** it. Independent of whether `thinking` was requested.

## Proposed architecture

### A — Capture thinking + signature during streaming (transport)
Extend `_anthropic._stream`'s content-block state machine:
- On a `thinking` `content_block_start`, begin a thinking block.
- Accumulate `thinking_delta` text **and** capture `signature_delta`
  (the signature arrives as its own delta type before `content_block_stop`).
- Handle `redacted_thinking` blocks (delivered whole, carry opaque `data`).
- Emit via `CompletionChunk` — `thinking_delta` already exists; add a
  `thinking_signature` field (and a redacted-data carrier), keyed so multiple
  thinking blocks in one turn stay distinct.

### B — Assemble `thinking_blocks` on the streamed Completion (runner)
`run_stream` accumulates the (text, signature) pairs / redacted blocks and sets
`synthetic_completion.thinking_blocks = [...]` so the downstream store receives
them. (Non-streaming already does this.)

### C — Persist thinking on the assistant turn, ordered first (memory)
`ShortTermMemory.add_assistant` prepends thinking blocks **before** text and
`tool_use` (Anthropic ordering requirement), serialised as dicts:
`{"type":"thinking","thinking":…,"signature":…}` /
`{"type":"redacted_thinking","data":…}`. Snapshot/restore + the journal
(PRD-129) carry them like any other block.

### D — Serialize thinking back into the request (transport)
Add `thinking` / `redacted_thinking` cases to `_content_block_to_anthropic`
(both dict and dataclass forms). Stop raising on these types.

### E — Lock the immutability invariant (context guard)
Make `_shrink_message` / `_enforce_char_budget` **explicitly** never touch
`thinking`/`redacted_thinking` blocks (today it's incidental). The signature is
verified against the exact text — any edit ⇒ 400. Summarisation removes whole
*older* turns (safe); the latest tool-using turn's thinking survives intact.

### F — Keep thinking attached through heal/trim (memory)
`_heal_dangling_tail` and the sliding-window trim must never yield an assistant
message that has `tool_use` but is missing the thinking that preceded it (when in
thinking mode). Thinking + its tool_use + the following tool_result move together.

### G — (Optional, later) prune resolved thinking to save tokens
Anthropic permits dropping thinking from **fully-resolved** older assistant turns
(no pending tool loop). A later optimisation may strip those to reduce input
tokens; correctness only requires preserving the **active** tool-using turn's
thinking. Ship correctness first.

## Recommendation & priority

| Priority | Item | Why |
|---|---|---|
| **1** | **A + B** | Without the signature captured + attached to the Completion there is nothing to round-trip (the streaming path has *nothing* today). |
| **1** | **C + D** | Store thinking (ordered) + serialize it back — the two ends of the round-trip. |
| **2** | **E** | Lock the no-truncation invariant so the guard can never invalidate a signature. |
| **2** | **F** | Heal/trim must not orphan thinking from its turn. |
| 3 | **G** | Token-cost optimisation; not needed for correctness. |

Ship **A+B+C+D together** (the round-trip is atomic — any missing stage still
400s), then **E+F** to harden.

## Phased plan

| Phase | Scope |
|---|---|
| 1 | **A**: `_stream` captures `signature_delta` + `redacted_thinking`; `CompletionChunk.thinking_signature`. Tests: a streamed thinking response yields text + signature. |
| 2 | **B+C**: `run_stream` assembles `thinking_blocks`; `add_assistant` stores them first (before text/tool_use). Tests: assistant message in memory begins with a thinking block carrying the signature. |
| 3 | **D**: `_content_block_to_anthropic` serializes `thinking`/`redacted_thinking`. Tests: round-trip a stored thinking block → Anthropic dict unchanged (text + signature preserved). |
| 4 | **E+F**: guard/heal invariants + regression: a multi-turn tool-use conversation in thinking mode replays without a 400; truncation never edits a thinking block. |

## Acceptance criteria

| # | Criterion |
|---|---|
| 137.1 | Streaming captures the thinking **signature** (and redacted data), not just the text |
| 137.2 | The streamed Completion carries `thinking_blocks`; `add_assistant` stores them **before** text/`tool_use` |
| 137.3 | `_content_block_to_anthropic` serializes `thinking`/`redacted_thinking` (dict + dataclass); never raises on them |
| 137.4 | A multi-turn **tool-use** conversation in thinking mode is accepted — no "thinking must be passed back" 400 (regression for the `code_plan` deepseek scenario) |
| 137.5 | Thinking blocks are never truncated/edited by the context guard (signature stays valid); summarisation drops whole old turns only |
| 137.6 | Heal/trim never produce an assistant turn with `tool_use` but missing its thinking |
| 137.7 | Round-trip is faithful even when `thinking` was not explicitly requested (model-emitted thinking is preserved) |
| 137.8 | Snapshot/restore + journal (PRD-129) carry thinking blocks across resume |

## Evidence index

| Claim | Location |
|---|---|
| Streaming handles `thinking_delta` only (no `signature_delta`/redacted) | `_transport/_anthropic.py:710-755` |
| `synthetic_completion` has no `thinking_blocks` | `_agents/_runner.py:~1463` |
| `add_assistant` stores only text + tool_use | `_memory/__init__.py` `add_assistant` |
| `_content_block_to_anthropic` lacks thinking case, raises on unknown | `_transport/_anthropic.py:84-125` |
| Non-streaming captures thinking_blocks (with signature) | `_transport/_anthropic.py:297-320, 673, 682` |
| `ThinkingBlock`/`RedactedThinkingBlock` carry `thinking`+`signature` / `data` | `_transport/__init__.py:399-427` |
| `Completion.thinking_blocks` field exists | `_transport/__init__.py:492` |
