---
title: "PRD-132: Context Reuse — L0 Conversation Prompt Caching + L1 Workspace File Cache"
status: implementing
version: 0.1.0
created: 2026-06-22
implements: prd-131-context-reuse-and-knowledge-persistence (layers L0, L1)
---

# PRD-132 — Context Reuse L0 + L1

Implements the first two layers of the PRD-131 reuse stack:

- **L0 — conversation prompt caching:** stop re-paying full input price for the
  file-heavy conversation history on every turn.
- **L1 — workspace file cache:** a durable, freshness-validated record of file
  reads, so reads are consistent and cross-session, and a substrate for L2/L3.

## Background (current state)

- lauren-ai's Anthropic transport already caches the **system prompt**
  (`_build_system`) and **tools** (`_build_tools`), gated by `LLMConfig`
  `cache_system_prompt` / `cache_tools` — both **default `False`**
  (`_config.py:88-89`).  The **message history is never cached**, so every
  `tool_result` (every file an agent read) is re-billed at full input price on
  every subsequent turn.
- `read_file` reads from disk on every call; there is no durable record of what
  was read, so nothing can be reused across sessions.

## L0 — Conversation prompt caching

### Design
Anthropic caches the longest matching prefix up to a `cache_control` breakpoint.
Placing a breakpoint on the **last content block of the last message** each
request makes the whole conversation prefix a cache prefix: this turn writes it,
the next turn reads it (~90% cheaper).  We already use 2 of Anthropic's 4
breakpoints (system, tools); this adds a 3rd.

### Changes
- **lauren-ai `_config.py`:** add `cache_conversation: bool = field(default=False)`
  to `LLMConfig`.
- **lauren-ai `_anthropic.py`:** add `_apply_conversation_cache(messages)` — marks
  the last block of the last message with `cache_control: ephemeral` (normalising
  string content to a text block first).  Call it after building
  `anthropic_messages` in **both** the non-streaming (`:562`) and streaming
  (`:861`) paths, when `self._config.cache_conversation`.
- **agenthicc `config.py`:** add `ExecutionSettings.prompt_cache: bool = True`;
  parse from `[execution]`; in `build_llm_config`, `dataclasses.replace` the built
  config with `cache_system_prompt = cache_tools = cache_conversation =
  prompt_cache`.

### Provider independence
The cache flags are read **only** by the Anthropic transport; OpenAI/Ollama/
litellm transports ignore them, so `prompt_cache=True` is a clean no-op there.

## L1 — Workspace file cache

### Design
`WorkspaceFileCache` — a per-project SQLite store keyed by **absolute path**,
storing `(sha256, mtime, size, encoding, content)`.  A cached entry is served
**only** when the file's current `(mtime, size, encoding)` match what was stored
— so a changed file always misses and is re-read (freshness is a hard
correctness requirement; never serve stale code).  Content is content-addressed
(sha256) for integrity and future dedup (L2/L3).

### Changes
- **agenthicc `tools/fs/file_cache.py` (new):** `WorkspaceFileCache` +
  module-level `configure_file_cache()` / `get_file_cache()` (default: disabled).
- **agenthicc `tools/fs/__init__.py`:** `ReadFileTool.execute` consults the cache
  after path resolution — fresh hit returns cached content (tagged
  `cached: True`); miss/stale reads from disk and stores.
- **agenthicc `config.py`:** `ExecutionSettings.file_cache: bool = True`.
- **agenthicc `runners/tui_session.py`:** when `file_cache`, configure a
  `WorkspaceFileCache` at `~/.agenthicc`-style project path
  (`.agenthicc/cache/file-cache.db`) at session startup.

### Note on scope
L1 alone does not reduce context **tokens** (read content still enters the
conversation) — its wins are durable, freshness-validated read **consistency**
(which also improves L0 cache-hit stability) and the **substrate** L2 (repo map)
and L3 (RAG) build on.  Token savings in this PRD come from L0.

## Acceptance criteria

| # | Criterion |
|---|---|
| 132.1 | With `cache_conversation`, the last message of the Anthropic request carries `cache_control: ephemeral`; off → it does not |
| 132.2 | `_apply_conversation_cache` normalises string content to a text block before marking |
| 132.3 | `prompt_cache` (default `True`) enables system/tools/conversation caching via `build_llm_config`; settable from TOML + `--set` |
| 132.4 | Cache flags are a no-op on non-Anthropic providers (no error, no behaviour change) |
| 132.5 | `WorkspaceFileCache.get_fresh` returns content iff `(mtime, size, encoding)` match; a changed file misses |
| 132.6 | The cache is durable — a new `WorkspaceFileCache` on the same DB resolves a prior read |
| 132.7 | `ReadFileTool` serves a fresh cache hit (`cached: True`) and stores on a miss; disabled cache → unchanged behaviour |
| 132.8 | New suites green: `test_prompt_cache.py` (lauren-ai), `test_file_cache.py` (agenthicc) |
| 132.9 | L2 (repo map) and L3 (RAG) remain deferred per PRD-131 |
