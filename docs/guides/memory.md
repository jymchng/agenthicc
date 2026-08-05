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

An explicit turn interruption preserves the valid assistant/tool exchanges
that completed before cancellation. If the interrupted tail contains an
unanswered tool call, it is healed with the provider's interruption result and
that repair is journaled, so a follow-up such as “what were you doing?” sees
the same context in the current process and after restart. A cancellation no
longer erases the whole turn by rolling memory back to its pre-turn count.

Automatic compaction is model-aware. Newer lauren-ai versions receive the
resolved `context_window` and use their exact-count compaction ladder. When an
older lauren-ai transport cannot provide that guard, agenthicc runs the same
bounded map-reduce fallback at the turn boundary. If the provider still
returns a context-length 400, the pre-turn memory snapshot is restored,
compacted once, and the turn is retried rather than submitting the same
oversized request again. If an OpenAI-compatible endpoint returns an empty
summary, a bounded local recent-history fallback is applied before retrying.

### Tool-exchange transactions

The provider conversation has one canonical transaction boundary for each
assistant tool-call batch. `lauren-ai` validates the history before every
provider request and after every result exchange. A batch must contain
non-empty, unique call IDs and exactly one immediately-following result for
each call. Approval filters and parallel execution therefore cannot leave a
missing result behind: omitted calls are recorded as explicit error results,
while a cancellation preserves completed results and synthesizes stable
interruption results for the rest.

`JournaledShortTermMemory` persists the exchange lifecycle (`started`, each
result recorded, `committed`, and `aborted/repaired`) using bounded metadata;
tool IDs are hashed in lifecycle records and prompts, arguments, outputs, and
credentials are never copied into diagnostics. On startup, journal replay
rehydrates the message projection and repairs a deterministic incomplete tail
before the first provider request. Repeating recovery is idempotent. Unknown,
duplicate, empty, or non-adjacent IDs are not guessed at: the shared runner
raises `ToolConversationIntegrityError` before network I/O.

The minimum supported integration is the transaction-capable `lauren-ai`
release exposing `ShortTermMemory.validate_tool_history()`,
`repair_tool_history()`, `begin_tool_exchange()`,
`commit_tool_exchange()`, and `abort_tool_exchange()`. Agenthicc fails closed
with an upgrade message when that contract is absent.

The manual `/compact` command uses the bounded map-reduce summarizer and
records a reset in the journal so the durable projection remains aligned with
the live messages. Automatic and manual compaction both emit a visible
`⎋ Compacting conversation…` event.

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
