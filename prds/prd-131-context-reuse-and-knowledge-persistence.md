---
title: "PRD-131: Context Reuse & Codebase Knowledge Persistence"
status: proposal
version: 0.1.0
created: 2026-06-22
related: [prd-101-semantic-index, prd-119-conversation-compaction, prd-129-conversation-durability]
---

# PRD-131 — Context Reuse & Codebase Knowledge Persistence

## Problem

When an agent does a task ("enhance the docs"), it reads many files to build
understanding — README, source modules, config, tests.  Today that work is
**thrown away twice**:

- **Within the session (re-pay):** every file the agent read sits in the
  conversation history and is **re-sent and re-billed at full input-token price
  on every subsequent turn**.  A turn that reads 20 files makes *every* later
  turn in that session expensive.
- **Across sessions (re-read):** the **next** agent — a new session, a different
  task, or a sub-agent — re-reads the same files from scratch, paying to acquire
  *and* to carry them in context again.

The request — *"reuse the conversation so the next agent doesn't read these files
again"* — actually spans **two distinct meanings of "don't re-read":**

1. **Don't re-PAY** for content already in context → *caching*.
2. **Don't re-ACQUIRE / re-load** content the system has already seen → *durable
   knowledge + retrieval*.

A robust answer addresses both, in layers, and must never serve **stale** content
(reusing an old file after it changed is a correctness bug, not an optimization).

## What agenthicc already has (grounding)

| Capability | State | Gap for this problem |
|---|---|---|
| **Conversation journal** (PRD-129) | Durable, fsync'd; `--resume` folds full history (incl. every file-read `tool_result`) back into memory | Reuse only for the **same** `session_id`; a new session/task starts cold |
| **Prompt caching** (lauren-ai `_anthropic.py:354-397`) | Implemented for **system prompt + tools** (`cache_control: ephemeral`); usage tracked (`cache_read/write_tokens`) | **Defaults OFF** (`_config.py:88-89`); **does not cache the message history** — the file reads are uncached |
| **Compaction** (PRD-119) | Summarises history past a token threshold | Lossy; within-session only; not a reuse mechanism |
| **3-tier memory** (`memory/layers.py`) | `ProjectMemoryLayer`/`GlobalMemoryLayer` = durable SQLite KV + **content-addressed (sha256) artifact store** | Substrate exists but is **not used** for file/codebase knowledge |
| **SemanticIndex** (PRD-101, `memory/vector.py`) | In-memory TF-IDF; indexes **completed turn text**; fresh per session (`tui_session.py:289`) | **Not durable** (lost on exit); does **not** index file contents |
| **fs `read_file` etc.** | Direct read each call | **No result cache / freshness tracking** |

**Net:** the durable substrate (journal + content-addressed artifacts) and the
caching hooks exist, but nothing connects "files this agent read" to "what the
next agent (or next turn) sees" — except a full same-session `--resume`.

## Survey of approaches (prior art)

| Approach | Solves | Used by | Trade-off |
|---|---|---|---|
| **Incremental conversation prompt caching** — put a rolling cache breakpoint on the conversation prefix, not just system+tools | Re-pay (P1) | Anthropic (explicit `cache_control`), OpenAI (automatic prefix cache) | Provider-specific; ~5-min TTL; must order messages prefix-stably |
| **Content-addressed file cache** — `read_file` returns cached bytes keyed by `(path, sha256/mtime)`; persisted | Re-acquire (P2) | Build systems; Bazel; Claude Code's file-state tracking | Saves disk read, not context tokens, unless paired with caching/retrieval; **staleness risk** |
| **Codebase map / "repo map"** — a compact, persisted file-tree + per-file purpose + key symbols, injected or tool-exposed | Re-load (P2) | aider (repo-map), Cursor (codebase index) | Must be refreshed on change; approximate |
| **RAG over file contents** — chunk + embed files in a durable vector store; agent queries instead of reading whole files | Re-load (P2) | Cursor, Sourcegraph Cody, Continue | Embedding cost; chunking quality; retrieval misses |
| **Session digest / handoff** — distil "what was learned/done" at session end into durable memory; next session loads it | Re-load (P2) | Claude Code (`/compact`, memory), Devin | Lossy; needs a good summariser |
| **Full session resume** (already PRD-129) | Both, for same session | Claude Code `--continue` | Brings *all* history — irrelevant + huge for a new task |

## Proposed architecture — a reuse stack (L0–L4)

Layers are independent and ordered by return-on-investment.  Each is useful
alone; together they cover both meanings of "don't re-read."

```
L4  Session digest / handoff   ── next session loads a distilled summary
L3  Durable codebase RAG        ── search_codebase(query) → relevant chunks
L2  Codebase map (repo map)     ── compact tree+symbols injected (and cached)
L1  Workspace file cache         ── content-addressed, mtime-validated read_file
L0  Conversation prompt caching  ── stop re-paying for history every turn
                                   (foundation; biggest within-session win)
```

### L0 — Conversation prompt caching (foundation; biggest, cheapest win)
- Turn `cache_system_prompt` / `cache_tools` **on by default** (config), and add
  **conversation-prefix caching**: place a rolling `cache_control` breakpoint on
  the last stable message (e.g. the most recent `tool_result`) each turn so the
  provider serves the file-heavy prefix from cache (~10% price) instead of
  re-billing it.
- Provider-agnostic shell: Anthropic = explicit breakpoints; OpenAI = automatic
  (no-op); others = graceful no-op.  Expose `cache_conversation` in
  `[execution]`.
- **This alone removes most of the within-session re-pay cost** and is a small,
  contained change in lauren-ai's `_anthropic` message builder.

### L1 — Workspace file cache (content-addressed + fresh)
- A `WorkspaceFileCache` keyed by `(abspath, sha256)` with the file's `mtime`,
  persisted as **project-memory artifacts** (the sha256 store already exists).
- `read_file` (and friends) record every read; on a re-read, if `mtime`+size are
  unchanged it serves the cached bytes and tags the result **cache-eligible** so
  L0 keeps it in the cached prefix.  Any change ⇒ miss ⇒ fresh read + re-index.
- Cross-session substrate: the cache survives process exit, so the *next* session
  can resolve a read without touching disk and (with L2/L3) without re-loading.

### L2 — Codebase map ("repo map")
- A durable, incrementally-maintained artifact: file tree + a one-line purpose +
  key exported symbols per file (cheaply derived — ctags/AST or distilled from
  prior reads).  Refreshed only for files whose hash changed (L1 drives
  invalidation).
- Injected into the system prompt (so L0 **caches it**) and/or exposed as a
  `codebase_map` tool, so a new agent **orients without reading every file**.

### L3 — Durable codebase RAG
- Promote the in-memory `SemanticIndex` to a **durable** per-project store
  (`sqlite-vec`, already a dependency) and index **file chunks** (not just turn
  text), keyed by `(path, chunk, hash)`.  Add a `search_codebase(query)` tool
  returning ranked chunks + line ranges, so agents read *targeted ranges* instead
  of whole files.
- Re-embed only changed files (L1 hashes).

### L4 — Session digest / handoff
- At turn/session end, distil a structured digest (goal, files touched + their
  roles, decisions, open threads) into project memory.  `--continue` (or an
  auto-load) seeds a new session with the digest + the codebase map — a *small*
  primer instead of a *full* `--resume`.

## Recommendation

**Build the stack bottom-up; ship L0 first.**

1. **L0 now (highest ROI, smallest change).** Enabling conversation prompt
   caching eliminates the dominant cost — re-paying for file contents every turn
   — for the common Anthropic path, with a graceful no-op elsewhere. It needs no
   new storage and rides existing cache hooks.
2. **L1 + L2 next — the direct answer to "the next agent shouldn't re-read."**
   A content-addressed, mtime-validated file cache plus a persisted, incrementally
   refreshed codebase map let a new session orient and resolve reads from durable
   project memory instead of re-acquiring files. Both reuse the existing
   content-addressed artifact store.
3. **L3 when scale demands it.** Durable file RAG is the most robust long-term
   reuse (read only what's relevant) but is the most work; do it once L1 supplies
   change-tracked invalidation.
4. **L4 as polish** — a cheap, lossy handoff that complements (not replaces)
   full `--resume`.

**Do not** make full-session `--resume` the cross-task mechanism: it carries an
entire prior conversation into an unrelated task — large, noisy, and often wrong.
The reuse should be *selective* (cache + map + retrieval), not wholesale.

## Cross-cutting requirements

| Requirement | Why it matters |
|---|---|
| **Freshness / invalidation** | The #1 correctness risk. Every cached byte, map entry, and embedding is keyed by content hash + mtime; a change invalidates it. Never serve stale code. |
| **Provider independence** | Caching is Anthropic-specific; L0 must no-op cleanly on OpenAI/Ollama/litellm. L1-L4 are provider-agnostic. |
| **Bounded size** | Caches/maps are per-project and size-capped (LRU on the artifact store); large/binary files are excluded. |
| **Opt-out + transparency** | Config flags per layer; surface cache hit-rate and `cache_read_tokens` (already tracked) so the win is measurable. |
| **Security** | Reuse stays within the workspace sandbox (`WorkspaceView`); no file leaves the project memory boundary. |

## Phased plan

| Phase | Scope | Deliverable |
|---|---|---|
| **1 (L0)** | Conversation prompt caching | `cache_conversation` config; rolling breakpoint in `_anthropic` builder; hit-rate surfaced; defaults sensible |
| **2 (L1)** | Workspace file cache | `WorkspaceFileCache` over project-memory artifacts; `read_file` consults it; mtime/hash invalidation |
| **3 (L2)** | Codebase map | Durable repo-map artifact, incrementally refreshed; injected (cached) + `codebase_map` tool |
| **4 (L3)** | Durable file RAG | `SemanticIndex` → sqlite-vec, file-chunk indexing, `search_codebase` tool |
| **5 (L4)** | Session digest | Structured digest at session end; `--continue` primes from digest + map |

## Acceptance criteria

| # | Criterion |
|---|---|
| 131.1 | With L0 on, a multi-file-read session shows `cache_read_tokens` > 0 on later turns; per-turn input cost drops materially vs. uncached |
| 131.2 | L0 is a clean no-op on non-Anthropic providers (no errors, no behaviour change) |
| 131.3 | `read_file` on an unchanged file (same session or new) resolves from the cache; a changed file (mtime/hash) misses and re-reads |
| 131.4 | A new session exposes a codebase map without having read the files this run |
| 131.5 | `search_codebase(query)` returns relevant chunks + line ranges from a durable, per-project index that survives restart |
| 131.6 | No layer ever serves content from a file whose hash changed since it was cached/indexed (freshness test) |
| 131.7 | Every layer is independently toggleable and measurable (hit-rate / token telemetry) |

## Open questions

- Is running agents against very large repos (where L3 RAG matters most) a target?
- Should the codebase map be **derived cheaply** (ctags/AST) or **distilled by the
  LLM** from reads (richer, costlier)? A hybrid (cheap structure + lazy LLM
  summaries for hot files) is likely best.
- Should L1/L2/L3 be **global** (shared across projects for shared deps) or strictly
  per-project? Per-project is safer for freshness; global helps monorepos/deps.
