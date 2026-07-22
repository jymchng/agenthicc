# Memory

agenthicc has two related forms of memory: conversation memory used to build
LLM turns, and a three-tier key/value/artifact memory router used by tools and
workflows.

## Three tiers

| Tier | Implementation | Lifetime | Use |
|---|---|---|---|
| Session | `SessionMemoryLayer` | Process | Fast LRU values with per-entry TTL |
| Project | `ProjectMemoryLayer` | Project | SQLite namespaced values and content-addressed artifacts |
| Global | `GlobalMemoryLayer` | User | SQLite values shared across projects |

`MemoryRouter` is the dispatch point. Callers should not reach into a layer
unless they are implementing or testing that layer. The current contract keeps
reads lock-free and serializes writes per owning tier.

## Artifacts

Project artifacts are stored by content hash. A publish/read round trip should
be stable across process instances and should not silently overwrite unrelated
content. Use a temporary project directory in integration tests.

## Semantic index

`SemanticIndex` offers asynchronous add/search over short text documents. It
uses the available lauren-ai store when present and a bag-of-words fallback
otherwise. The fallback is useful for tests and local operation but is not a
replacement for a production vector database.

## Conversation memory

The session runner creates a journal-backed short-term memory. Each append,
reset, turn marker, and durable tool record is written to
`conversation-journal.jsonl` and flushed. On resume the journal is folded back
into the live memory; an incomplete turn can be re-driven with already-complete
tools replayed from the durable ledger.

Automatic compaction is model-aware. The manual `/compact` command uses a
bounded map-reduce summarizer and records a reset in the journal so the durable
projection remains aligned with the live messages.

## Context budgeting

The active provider/model resolves a context window from:

1. exact `[memory.context_windows]` model entry;
2. lauren-ai's known model registry;
3. the configured `default` entry;
4. the library fallback.

`ExecutionSettings.effective_usable_budget()` reserves output and headroom.
This budget drives trimming and compaction; do not reintroduce a second scalar
token limit without reconciling it with the model-aware source of truth.

## File cache

When enabled, `WorkspaceFileCache` stores file content with freshness metadata.
`read_file` uses the cache only when path, mtime, size, and encoding still match.
The cache is a performance layer, not the source of truth; a changed file must
never be served stale content.

## Operational guidance

- Keep project memory inside the project-specific `.agenthicc/` directory.
- Treat global memory as user data when collecting diagnostics.
- Do not put credentials or unbounded tool output into durable memory.
- Add schema versions and retention before changing journal or SQLite formats.
- Test crash-at-write, corrupt trailing JSONL, resume, compaction, and repeated
  side-effecting tool calls.

See the [storage reference](../reference/storage.md) for paths and recovery
guarantees.
