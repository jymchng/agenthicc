---
title: "PRD-136: Per-Model Context-Window Configuration + Model-Derived Live Window"
status: implemented
version: 1.0.0
created: 2026-06-23
related: [prd-133-context-window-overflow-guard, prd-135-automatic-llm-compaction-on-overflow]
---

> **Status:** Implemented (one-knob design). `[memory.context_windows]` replaces
> both `model_context_window` and `session_memory_max_tokens`; the live window
> derives from the configured window and `summarize_at` is the cost dial. See
> Features PRD §47 and `test_model_context_budget.py`.

# PRD-136 — Per-Model Context Windows (and making them matter for auto-compaction)

## The ask

Let the user declare each model's context window in the config file:

```toml
[memory.context_windows]
default            = 1_000_000   # fallback for unknown / proxied models
claude-opus-4-8    = 10_000_000
deepseek-v4-flash  = 250_000
```

## Is this a good idea for auto-compaction?

**Yes — it's the right *primitive*, and it fixes a real correctness gap — but on
its own it does NOT change when auto-compaction fires.** That second half is the
important architectural finding, and the revamp must address it or the config
will look like it does nothing.

Two separate things are in play, and today they are conflated/decoupled wrongly:

1. **Model context window** — the model's hard physical capacity. Today it comes
   from a single `[execution] model_context_window` int (applies only to the
   *active* model) plus a hardcoded lauren-ai `MODEL_CONTEXT_WINDOWS` registry
   (`config.py:139,173`; `_config.py:54`). It drives **(a)** the hard pre-send
   truncation/error guard (`usable_context_budget`) and **(b)** the map-reduce
   summariser's chunk budget.

2. **Live (working) window** — how much conversation we actively keep before
   compacting. Today this is a *fixed* `session_memory_max_tokens = 32_000`
   (`config.py:128`), and **the auto-compaction trigger fires on THIS, not the
   model window**: `_maybe_compact` uses `window = memory.max_tokens` →
   `trigger = window × summarize_at` (`_runner.py:373,379`).

So if you set `claude-opus-4-8 = 10_000_000` today, the hard guard and the
summariser chunking would honour it, **but compaction would still fire at
0.8 × 32k ≈ 25.6k tokens** — the 10M number would never affect *when* the agent
summarises. The config would feel inert for its headline use case.

### Why the primitive is still worth adding (two real wins)

- **Correctness for proxied/unknown models.** `deepseek-v4-flash` via a gateway
  isn't in the built-in registry → it falls back to a conservative 200k default.
  The real window was ~1.05M. A per-model entry lets the user state the truth, so
  the hard guard stops truncating far too early (or, with a too-high guess,
  stops a 400). This is exactly the failure that motivated PRD-133.
- **Multi-model ergonomics.** The single `model_context_window` int has to be
  re-set every time you switch models. A map is set once and covers your whole
  fleet.

### The catch worth stating plainly (cost/quality)

Making the live window = the model window by default means a 10M-window model
sends up to ~10M tokens **every turn**. Prompt caching (PRD-132) softens the
re-billing of the stable prefix, but cache writes + the growing tail are still
real cost and latency, and models attend *worse* to very long contexts
("lost in the middle"). So the model window should be a **ceiling**, and the live
working window a **policy** (default model-derived, but capped on request).

## Proposal

### 1. Per-model context-window map (the config) — the single source

The map **replaces** the single `[execution] model_context_window` int entirely
(no separate scalar field — one mechanism, no dual path). `default` carries the
exact semantics the old `model_context_window` had ("the window when I don't know
the model"); per-model keys extend it. A dedicated sub-table keeps arbitrary
model-id keys from colliding with the typed `[memory]` fields
(`project_memory_path`, etc.):

```toml
[memory.context_windows]
default            = 1_000_000   # was: [execution] model_context_window — now lives here
claude-opus-4-8    = 10_000_000
deepseek-v4-flash  = 250_000
"gpt-4.1"          = 1_000_000   # NOTE: ids containing a dot MUST be quoted in TOML
```

Parsed into `MemorySettings.context_windows: dict[str, int]` (keys lower-cased on
read). `default` is just a reserved key in that same dict — there is no second
field and no `[execution] model_context_window` anymore.

> **Migration:** `[execution] model_context_window = N` → `[memory.context_windows] default = N`.
> The old field is removed (project is pre-production; no shim).

### 2. Resolution order (most-specific wins)

`effective_context_window(model)` — driven entirely by the one map + the library
registry:

1. **Explicit map entry** — `context_windows[model]` (exact match on the resolved
   `effective_model()` id). This is also how you pin the active model.
2. **Built-in registry** — lauren-ai `context_window_for(model)` (family-prefix
   match; accurate for known Claude/OpenAI/Ollama models).
3. **Config `default`** — `context_windows["default"]`: the user's catch-all for
   unknown / proxied models (what replaces the conservative 200k for *your* fleet,
   and the home of the old `model_context_window`).
4. **Hardcoded `DEFAULT_CONTEXT_WINDOW`** (200k) — last resort.

> Rationale for "registry before config `default`": a `default = 1_000_000`
> should rescue genuinely-unknown models, but must NOT silently inflate a known
> `gpt-4o` (128k) into 1M and reintroduce overflow. An explicit map entry always
> wins if the user really means it. (This ordering is a documented choice — the
> alternative "config always wins" is available if you prefer it.)

**CLI / env override.** Without a scalar field, the active model is overridden by
writing into the map: `--set memory.context_windows.default=250000` (or a
per-model key, for dot-free ids). The TOML file remains the path for ids
containing dots. If a one-shot active-model flag is still wanted, a thin
`--context-window N` can inject `context_windows[effective_model] = N` at load —
but it is sugar over the single map, not a second stored field.

### 3. One knob: the live window IS the model window (delete `session_memory_max_tokens`)

There is no separate live-window setting. `session_memory_max_tokens` is
**removed**; the session memory is sized directly from the configured window:

```
session_memory.max_tokens = usable(effective_context_window(model))
                          = window − max_output − reserve
```

So the **per-model context window is the only configuration** for the whole
context system. Everything derives from it:

```
configured window (per-model, [memory.context_windows])
      │
      ├── hard pre-send guard ceiling      = usable(window)   (PRD-133, truncate/error backstop)
      ├── summariser map-reduce chunk size = usable(window)   (PRD-135 B)
      └── live working window  = usable(window)               (messages() trim budget)
                │
                └── auto-compaction fires at  summarize_at × usable(window)
```

#### Why one knob is enough — `summarize_at` is the working-set/cost dial

The reason you don't *need* a second "how much do I actually want to use" setting
is that the **existing** `summarize_at` fraction already plays that role, and it
does so without sacrificing the big ceiling:

- The **ceiling** stays at the full window, so a single legitimately-huge tool
  result (say 5M tokens on a 10M model) is **not** truncated — the hard guard
  only acts at ~`usable(window)`.
- The **steady-state working set** hovers around `summarize_at × window`, because
  compaction summarises older turns whenever the buffer crosses that line.

So `summarize_at` lets you have *"big ceiling, small everyday working set"* from
one window number — which a separate live-window **cap** could not do (a 200k cap
would truncate that 5M result). Concretely, on `claude-opus-4-8 = 10_000_000`:

| `summarize_at` | Steady-state working set | Tolerates a single huge input? |
|---|---|---|
| `0.8` (default) | ~7.6M (uses the window) | yes, up to ~9.96M |
| `0.05` | ~480k (cheap) | **still yes**, up to ~9.96M |

`deepseek-v4-flash = 250_000` → working set ~190k at the default `summarize_at`.

> **Honest trade-off:** `summarize_at` is currently **global** (one value for all
> models), so you can't say "opus small, deepseek large" in one run — only one
> active model runs at a time anyway, so this is rarely a constraint. Removing the
> 32k default also means the out-of-the-box working set grows to ~`0.8 × window`;
> if that default feels too costly we lower the default `summarize_at`, not add a
> field back.

## Implementation sketch

| Where | Change |
|---|---|
| `config.py` | **Remove** `[execution] model_context_window` **and** `[execution] session_memory_max_tokens` (both subsumed by the window). Add `MemorySettings.context_windows: dict[str,int]` (parsed from `[memory.context_windows]`, keys lower-cased; `default` is just a key). `effective_context_window(model)` resolves from the map → registry → `default` → hardcoded; add `effective_usable_budget(model)` (= `window − max_output − reserve`). |
| `runners/tui_session.py` | build `session_memory` with `max_tokens = cfg.execution.effective_usable_budget(model)` (replacing the fixed `32_000` at `tui_session.py:271`). |
| `runners/agent_turn.py` | keep passing `context_window=effective_context_window(model)` (already does); the live window now flows through `session_memory.max_tokens`, which `_maybe_compact` already reads — **no runner change needed** (PRD-135's trigger is already `summarize_at × memory.max_tokens`). |
| lauren-ai `_config.py` | unchanged — the registry stays the library default; agenthicc layers the user map on top. |
| docs / `llms-full.txt` | document `[memory.context_windows]`, the dot-quoting rule, and `summarize_at` as the working-set/cost dial. Remove `session_memory_max_tokens`. |

The pleasing part: because PRD-135 already triggers compaction on
`summarize_at × memory.max_tokens`, simply *sizing the session memory from the
configured window* wires the whole feature together — **one config value, no
change to the compaction loop, no second knob.**

## Recommendation

- **Ship the per-model map (1) + the one-knob live window (3) together.** The map
  alone is half a feature; (3) is what makes it control auto-compaction.
- **`[memory.context_windows]` is the single source of truth for the context
  system** — `model_context_window` *and* `session_memory_max_tokens` are both
  removed. One number per model.
- The live window = `usable(window)`; **`summarize_at` is the working-set/cost
  dial** (lower it for cheaper, more aggressive compaction). Consider lowering the
  default `summarize_at` if `0.8 × window` is too costly out of the box.

## Open choices to confirm

- **Section:** `[memory.context_windows]` (recommended, matches your instinct) vs
  `[execution.context_windows]` (next to `model`). 
- **`default` vs registry precedence** (registry-first recommended; config-first available).
- **Default `summarize_at`:** keep `0.8` (uses most of the window) vs a lower
  default (cheaper steady-state working set out of the box).

## Acceptance criteria

| # | Criterion |
|---|---|
| 136.1 | `[memory.context_windows]` maps model id → window; `default` is the unknown-model fallback |
| 136.2 | Resolution: explicit map entry → registry → config `default` → hardcoded |
| 136.3 | TOML keys with dots (`"gpt-4.1"`) are supported (documented quoting) |
| 136.4 | The configured window drives the hard guard, the summariser chunk budget, **and** the live window — from one value |
| 136.5 | `session_memory` is sized `max_tokens = usable(window)`; **no separate live-window config exists** (`session_memory_max_tokens` removed) |
| 136.6 | Auto-compaction fires at `summarize_at × usable(window)`, so a bigger configured window compacts later; lowering `summarize_at` shrinks the working set while the full window stays the hard ceiling (verified end-to-end) |
| 136.7 | Both `[execution] model_context_window` and `session_memory_max_tokens` are **removed**; the window map is the single source (no separate field/code path) |
| 136.8 | Docs + `llms-full.txt` updated; no `compact_threshold`-style hardcoded constant reintroduced |
