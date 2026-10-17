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
reset, logical-turn marker, provider-step receipt, partial-fragment diagnostic,
and durable tool record is written to `conversation-journal.jsonl` and flushed.
On resume the journal is folded back into the live memory; an incomplete turn
can be re-driven from its latest committed provider step with already-complete
tools replayed from the durable ledger.

### Failed mid-turn recovery

One submitted message is a logical turn, but a single `lauren-ai` streaming
run may contain several provider requests:

```text
logical turn T
├─ provider step T:0 → assistant/tool messages → step_committed
├─ provider step T:1 → transport failure → step_interrupted
└─ turn_failed(T, last_committed_step=T:0)
```

The retry unit is the provider step, not the whole logical turn. A retry may
discard only the uncommitted provider attempt; it must never restore a snapshot
from before `T:0`. This is the key difference between a safe retry and the old
whole-stream rollback that made a later provider error erase earlier assistant
and tool-result messages.

Agenthicc owns this retry boundary so the guarantee also holds with older
lauren-ai releases that do not emit step receipts: those retries retain the
current memory projection and use the idempotency ledger to avoid repeating
completed side effects.

`AgentStepStarted`, `AgentStepRetryScheduled`, `AgentStepInterrupted`, and
`AgentStepCommitted` are emitted by the provider runner. The session adapter
projects those receipts into the journal and advances the safe checkpoint only
after the memory append and tool exchange have completed. Partial streamed
text is stored as an `assistant_partial` transcript event and a bounded
`partial_fragment` diagnostic; it is never folded into provider memory as a
normal assistant message.

For a failed turn, the live memory therefore remains the valid prefix
`H, U, committed steps`, and the next user message is appended to that same
session conversation exactly once. `turn_failed` is distinct from
`turn_aborted`: the former means a provider/tool error while preserving
context, while the latter is reserved for explicit cancellation. When the
installed runner exposes provider-step recovery, the session adapter persists
and restores the latest committed checkpoint. When it does not, the retry
keeps the current projection rather than rolling back to the turn start; the
durable idempotency ledger protects already-completed tools. In both cases the
logical user message is sent exactly once per retry attempt.

When a provider failure occurs during a re-drive of an already interrupted
turn, the journal writes the non-terminal `turn_recovery_started` marker
instead of `turn_failed`. The original turn ID and seeded tool ledger therefore
remain discoverable if the re-drive fails again; a fresh turn is not started
against that partial conversation. A successful re-drive writes the normal
completion marker.

An explicit turn interruption preserves the valid assistant/tool exchanges
that completed before cancellation. If the interrupted tail contains an
unanswered tool call, it is healed with the provider's interruption result and
that repair is journaled, so a follow-up such as “what were you doing?” sees
the same context in the current process and after restart. A cancellation no
longer erases the whole turn by rolling memory back to its pre-turn count.

Automatic compaction is owned by Agenthicc at the shared agent-turn boundary
for foreground, detached, direct, and workflow turns. Agenthicc disables
lauren-ai's independent proactive summarizer in these sessions so a token-count
or journal error cannot bypass the durable-session recovery policy. Near the
conservative 80% input threshold, the runner attempts a bounded model-aware
token count; a timeout, unsupported counter, or invalid result falls back to a
stricter local estimate. It does not multiply this decision into the provider
turn retry loop.

Compaction first builds a replacement without changing live memory. A generated
summary includes any prior rolling summary, then replaces the previous summary
and conversation projection in one journal transaction. The journal writes a
prepare record, a candidate reset, and a commit receipt. If the process stops
before commit, journal folding restores the previous projection; if a write or
fsync fails, an incomplete record is truncated where possible and the live
buffer is not published. Manual `/compact` uses this same replacement boundary.

When the summary call fails or returns no final text, Agenthicc records a
bounded classification (`summary_result`, attempt count, and exception class)
and attempts a clearly labeled local fallback. It first drops only complete
oldest user-turn groups; it never truncates a tool call away from its result.
If the retained exchange is still too large, the fallback may excerpt only
plain-text content inside recognized tool-result blocks, preserving tool-use
IDs, result IDs, message order, assistant provider metadata, and all
user-authored content. The excerpt includes an explicit omission marker. Opaque
or multimodal payloads are not stringified or clipped.

Before publishing a local candidate, Agenthicc includes the current user input,
system prompt, and tool schemas in its token-budget check when the transport
supports counting. It permits one deterministic tighter candidate and recount;
it does not add another summary/provider retry. A candidate that remains over
the hard usable budget is not committed or sent. If the latest user input is
itself too large, no protocol-safe candidate exists, or the journal cannot
commit, the session reports a specific recoverable compaction/context failure
instead of retrying the unchanged oversized request or declaring the work
irrecoverably failed. The failed transaction leaves the existing conversation
projection intact. Direct-turn recovery keeps the original turn ID discoverable;
workflow recovery retains the existing checkpoint and phase. Cancellation
continues to propagate as cancellation, with no partial summary committed.

The final `compaction_status` record is cumulative: it carries the summary
classification and attempts together with the local fallback result and
recovery reason. Records contain only bounded enums/counts and exception
classes—not transcript text, provider payloads, credentials, or headers.

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
before the first provider request. If cancellation raced with a queued
continuation and left a known result behind that continuation, the session
memory moves that result back beside its matching assistant call and journals
one reset. Repeating recovery is idempotent. Unknown, duplicate, empty, or
ambiguous result IDs remain fail-closed: the shared runner raises
`ToolConversationIntegrityError` before network I/O.

The minimum supported integration is the transaction-capable `lauren-ai`
release exposing `ShortTermMemory.validate_tool_history()`,
`repair_tool_history()`, `begin_tool_exchange()`,
`commit_tool_exchange()`, and `abort_tool_exchange()`. Agenthicc fails closed
with an upgrade message when that contract is absent.

Automatic and manual compaction emit a visible
`⎋ Compacting conversation…` event and a bounded `compaction_status` record.
The status record includes a start marker and distinguishes successful summary
compaction, local complete-turn reduction, summary unavailability, journal
failure, and irreducible context. The lossy local outcome is never described
as an LLM summary.

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

## Try it

The three tiers and the router are importable, and each reports the module that
actually implements it — useful when a traceback names a layer instead of the
router:

```bash
PYTHONPATH=src python -c "
from agenthicc.memory import MemoryRouter, SessionMemoryLayer, ProjectMemoryLayer, GlobalMemoryLayer
print('router:', MemoryRouter.__module__)
print('session:', SessionMemoryLayer.__module__)
print('project:', ProjectMemoryLayer.__module__)
print('global:', GlobalMemoryLayer.__module__)
"
```

```text
router: agenthicc.memory.router
session: agenthicc.memory.layers
project: agenthicc.memory.layers
global: agenthicc.memory.layers
```

`MemoryRouter` is the only supported dispatch point. Reaching into a layer
directly is reserved for that layer's own implementation and tests.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| A value written in the TUI is gone in the next process | It was stored in the session tier, whose lifetime is the process | Use `scope="project"` or `scope="global"` for anything that must outlive the run |
| A value is gone after the TTL you set | The session tier applies per-entry TTL | Re-read before use, or store in a durable tier; check `[memory] session_ttl_seconds` |
| Two projects see each other's values | It was written to the global tier | Use a namespace, or write to the project tier |
| Artifacts appear duplicated | Artifacts are content-addressed by hash | Identical content intentionally maps to one object; do not expect a new object per publish |
| An unrelated artifact was overwritten | You bypassed the content-addressed publish API | Publish through the router; content addressing is what prevents silent clobbering |
| `SemanticIndex` returns poor results | It fell back to bag-of-words because no lauren-ai store was available | Expected for tests and offline use; configure a real vector backend for production |
| A resumed session replays an incomplete turn | `conversation-journal.jsonl` has a trailing partial turn | Expected. The journal is folded to the last committed provider step and the incomplete turn is re-driven |
| A retried turn erases earlier assistant or tool messages | Something restored a pre-turn snapshot | That is the old whole-stream rollback. Retry discards only the *uncommitted* attempt; never a snapshot from before the last committed step |
| `ToolConversationIntegrityError` before any network call | Unknown, duplicate, empty, or ambiguous tool-result IDs | Correct fail-closed behavior. Fix the tool-call batch; a batch needs unique non-empty call IDs and exactly one result each |
| Resume fails with an upgrade message | The installed lauren-ai lacks the transaction contract | Upgrade lauren-ai; agenthicc requires `validate_tool_history()`, `repair_tool_history()`, `begin_tool_exchange()`, `commit_tool_exchange()`, and `abort_tool_exchange()` |
| `read_file` returns stale content | The file changed but the cache entry still matched | It cannot: the cache is used only when path, mtime, size, and encoding all match. If this happens, report it — the cache must never serve changed content |
| An empty compaction summary | The provider split its output budget and returned no final text | Agenthicc already retries the summary with a larger completion budget, then falls back to bounded local recent history |
| Compaction happened at a surprising moment | The provider returned a context-length 400 | The current committed projection is compacted once and the request retried, preserving earlier provider steps |

### The journal is durable, the projection is not

The live memory projection is a fold over `conversation-journal.jsonl`. When
the two disagree, the journal wins. That is why a corrupt trailing line is
ignored after the durable prefix rather than failing the resume.

### Memory grew without a visible cause

Set the context budget from the model-aware source of truth
(`ExecutionSettings.effective_usable_budget()`) rather than a second scalar
token limit. Adding an independent cap without reconciling it against
`[memory.context_windows]` is how trimming and compaction drift apart.
