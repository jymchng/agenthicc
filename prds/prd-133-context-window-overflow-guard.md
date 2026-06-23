---
title: "PRD-133: Context-Window Overflow — Bounded Tool Output + Model-Aware Pre-Send Guard"
status: implemented
version: 1.0.0
created: 2026-06-23
related: [prd-119-conversation-compaction, prd-126-transport-retry, prd-129-conversation-durability, prd-132-context-reuse]
---

> **Status:** All five layers shipped.  A + C (memory char-budget invariant +
> git-aware bounded tool output) landed first; B + D + E (model-aware budget
> registry, exact `count_tokens` pre-send guard, graceful `AgentContextOverflowError`)
> followed.  See Features PRD §45 for the consolidated expectations and the test
> suites (`test_context_window_guard.py`, `test_model_context_budget.py`).

# PRD-133 — Context-Window Overflow Guard

## Problem

A `code_plan` turn failed with a provider **400**:

```
This model's maximum context length is 1048565 tokens. However, you requested
1500035 tokens (1495939 in the messages, 4096 in the completion).
```

The agent had just run `list_directory(recursive=True)` + `search_files('*.md')`
+ `search_files('*.rst')` on the project root.  The conversation's **messages
alone (1.49M tokens)** exceeded the model's context window (1.048M) — the request
was assembled and sent anyway.  This is a hard failure: the turn (and the whole
`code_plan` workflow) aborts.

The root issue is architectural: **nothing in the pipeline guarantees the request
fits the model's context window.**  The existing safeguards (sliding-window trim,
PRD-119 compaction, lauren-ai summarisation) are heuristic and, in this scenario,
all miss.

## Root-cause analysis

### RC1 — Unbounded tool output (the trigger)
`list_directory` and `search_files` append every match from an unbounded
`rglob` with **no result cap and no noise-dir exclusion**
(`tools/fs/__init__.py:227-233, 309-318`).  On a repo containing `.venv`,
`.git`, `__pycache__`, `node_modules`, one recursive call emits hundreds of
thousands of tokens.  `read_file`/`read_lines` cap only at **10 MB ≈ 2.5M
tokens** (`:15,53` — and `read_lines` `read_text`s the whole file then slices,
`:410`).  A single turn can therefore add far more than any model can hold.

### RC2 — The last turn is un-trimmable
`ShortTermMemory.messages()` (and `trim_to_fit`) drop oldest turns but **never
trim past the last conversational user message**
(`_memory/__init__.py:802-810, 834-835`).  So when a *single* turn's tool
results exceed the budget, trimming returns it **in full** — the sliding window
cannot rescue an oversized current turn.

### RC3 — No model-aware budget
Nothing knows each model's context window.  `PROVIDER_DEFAULT_MODELS` is just
names (`config.py:87`); there is no `context_window`.  The thresholds are
hardcoded and unrelated to the real limit:
- memory trim budget = `session_memory_max_tokens` (32k),
- summarise at `memory_window_tokens * summarize_at` (≈800k via `agent_turn`),
- compact at `compact_threshold_tokens` (1M).

1M (+4096 completion + system + tools) already **exceeds** this 1.048M model — the
compaction threshold is set *above* the usable budget.

### RC4 — The token estimate under-counts
Trim/compaction use a **char/4 heuristic** (`_CHARS_PER_TOKEN = 4`,
`token_estimate`, `_memory/__init__.py:581,856-863`).  Code, JSON, and path-dense
listings tokenise nearer 3 chars/token, so the *real* count runs well above the
estimate — heuristic budgets fire late (here: estimate said "fine", reality was
1.5M).

### RC5 — No hard pre-send guard, despite accurate counting being available
Every transport already implements **`count_tokens`**
(`_anthropic.py:859`, `_openai.py:809`, `_ollama.py:608`, `_mock.py:451`), yet
the request is assembled in `_stream`/`complete` and **sent with no check** that
it fits the window.  Summarisation (`_should_summarize`) triggers on the
under-count estimate, keeps the *recent* messages (the huge tool results), and
runs pre-turn — so it does not bound a current-turn blow-up
(`_runner.py:1049-1074`).

**One-sentence root cause:** *The request sent to the provider is not guaranteed
to fit the model's context window — tool output is unbounded, the last turn is
un-trimmable, budgets are model-agnostic and based on an under-counting estimate,
and there is no pre-send guard at the choke point even though every transport can
count tokens exactly.*

## Design goal — a hard invariant

> **Every request handed to a provider is guaranteed ≤ the model's usable context
> budget.** A context-length 400 must be structurally impossible, not merely
> unlikely.

This is enforced at the **single `_stream`/`complete` choke point** (the same
place PRD-126/129 put transport retry), so it cannot be bypassed by any tool,
workflow, or code path.

## Proposed architecture (defense in depth)

```
E  Graceful failure        ── if a mandatory item alone exceeds the window,
                              fail with an actionable message, not a raw 400
D  Accurate accounting      ── use Transport.count_tokens (already present),
                              not char/4, for the guard + triggers
C  Pre-send guard (INVARIANT)── at the choke point, measure the assembled request;
                              if over budget: trim → summarise/truncate the last
                              turn (override the floor) → hard-truncate to fit
B  Model-aware budget        ── model→context_window registry; usable_budget =
                              window − max_output − reserve(system+tools+margin)
A  Bounded tool output       ── cap + truncate tool results; exclude noise dirs
                              (the common trigger never reaches memory)
```

### A — Bound tool output at the source (remove the trigger)
- A **central per-tool-result cap** (configurable, e.g. ~25k tokens) applied to
  every tool result before it enters memory: truncate with a clear
  `[truncated: showing N of M …]` marker + a hint to narrow the query.  This
  protects against *all* tools, present and future.
- `list_directory` / `search_files`: cap entries (e.g. 1000) and **default-exclude
  noise dirs** (`.venv`, `.git`, `node_modules`, `__pycache__`, `dist`, `build`,
  `.mypy_cache`, `.ruff_cache`, `.pytest_cache`), ideally honouring `.gitignore`.
- `read_file` / `read_lines`: cap by **token budget**, not 10 MB; for large files
  return a head/tail window (or require a range) with a truncation marker.

### B — Model-aware context budget (know the limit)
- A `MODEL_CONTEXT_WINDOWS` registry (per model id; configurable override
  `[execution] model_context_window` for unknown/proxied models).
- `usable_budget = context_window − max_tokens_per_turn − reserve`, where
  `reserve` covers the system prompt, tools, and a safety margin.
- Derive trim / summarise / compact thresholds **from `usable_budget`** instead
  of hardcoded constants.  This is what makes the band-aid (lowering
  `compact_threshold_tokens`) unnecessary and correct across models.

### C — Hard pre-send guard at the choke point (the invariant)
Before each provider call, measure the assembled request and, while it exceeds
`usable_budget`, apply in order:
1. Drop oldest turns (existing sliding-window).
2. If still over, the **last turn is itself too big** → summarise or truncate its
   oversized tool-result blocks (this *overrides the floor* — the one thing the
   current trim cannot do) and emit a system note so the agent knows.
3. Final guarantee: hard-truncate to fit.

Now the request is provably ≤ budget regardless of upstream behaviour.

### D — Accurate token accounting
Use `Transport.count_tokens` for the guard and (optionally) the summarise/compact
triggers.  Where a network round-trip per check is too costly, use a
**conservative local estimate** (≈3.5 chars/token + margin) and reserve the exact
`count_tokens` for the pre-send guard only.

### E — Graceful failure
If, after truncation, a single mandatory item (e.g. the user's own message)
exceeds the window, surface a clear, actionable error ("input too large — split
the request / compact the conversation") instead of a provider 400 that aborts
the workflow.

## Recommendation & priority

| Priority | Layer | Why |
|---|---|---|
| **1** | **C — pre-send guard** | The invariant. Makes the 400 structurally impossible at the choke point; small, contained, high-leverage. |
| **2** | **A — bounded tool output** | Removes the *common* trigger so the guard rarely has to truncate; also improves every turn's signal/noise and cost. |
| **3** | **B — model-aware budget** | Makes thresholds correct (not guessed) across models; needed for the guard's budget to be right. |
| 4 | **D — accurate counting** | Uses capability already present; sharpens A–C. |
| 5 | **E — graceful failure** | UX backstop for the irreducible case. |

Ship **C + A together** first (invariant + trigger removal). They make the failure
both impossible *and* rare. **B** follows so the budget is model-correct rather
than a hardcoded constant.

## Immediate mitigation (band-aid, not the fix)
Set `[execution] compact_threshold_tokens` well below the smallest model in use
(e.g. 150_000) and lower `session_memory_max_tokens`.  This reduces — but does not
eliminate — the risk: a single un-trimmable oversized turn (RC2) can still exceed
the window.  The architectural guard (C) is required for a guarantee.

## Phased plan

| Phase | Scope |
|---|---|
| 1 | **C** pre-send guard at the `_stream`/`complete` choke point (trim → last-turn truncate → hard cap), with `count_tokens`; tests that a synthetic oversized memory yields a within-budget request |
| 2 | **A** central tool-result cap + `list_directory`/`search_files` entry caps + noise-dir exclusion + token-budgeted `read_file`/`read_lines` |
| 3 | **B** model→context-window registry + `usable_budget` derivation + thresholds derived from it; config override |
| 4 | **D/E** accurate-count triggers + graceful over-budget error |

## Acceptance criteria

| # | Criterion |
|---|---|
| 133.1 | No request is ever sent whose measured size exceeds the model's usable budget (pre-send guard invariant), even with a synthetic 2M-token memory |
| 133.2 | When the last turn alone exceeds the budget, its tool results are truncated/summarised (floor overridden) and a system note is emitted — the request still fits |
| 133.3 | `list_directory(recursive=True)` / `search_files` are bounded (entry cap) and exclude `.venv`/`.git`/`__pycache__`/`node_modules` by default |
| 133.4 | `read_file`/`read_lines` cap output by token budget with a truncation marker; no single tool result exceeds the per-result cap |
| 133.5 | Budgets derive from a model context-window registry (with a config override), not hardcoded constants |
| 133.6 | The guard uses `Transport.count_tokens` (accurate) rather than the char/4 estimate |
| 133.7 | An irreducible over-budget input fails with an actionable error, not a provider 400 |
| 133.8 | Regression: the `code_plan` "concise the docs" scenario completes without a context-length 400 |

## Evidence index

| Claim | Location |
|---|---|
| `list_directory`/`search_files` unbounded, no noise-dir exclusion | `tools/fs/__init__.py:227-233, 309-318` |
| `read_file` 10 MB cap; `read_lines` reads whole file | `tools/fs/__init__.py:15,53,410` |
| Trim floor — never past last conversational user msg | `_memory/__init__.py:802-810, 834-835` |
| char/4 token estimate | `_memory/__init__.py:581, 856-863` |
| Summarise trigger on estimate, keeps recent, pre-turn | `_runner.py:69-80, 1049-1074` |
| `count_tokens` on every transport (unused as guard) | `_anthropic.py:859`, `_openai.py:809`, `_ollama.py:608`, `_mock.py:451` |
| No model context-window knowledge | `config.py:87` (`PROVIDER_DEFAULT_MODELS` = names only) |
| Hardcoded thresholds (32k/800k/1M) | `agent_turn.py` (`_window_tokens = 0.8 * compact_threshold`), `config.py` (`compact_threshold_tokens=1_000_000`) |
