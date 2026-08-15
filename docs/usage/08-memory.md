# Memory

agenthicc provides tiered memory with durable conversation journaling.

## Tiers

Verified in `src/agenthicc/memory/layers.py`:

| Tier | Backing | Scope |
|---|---|---|
| **Session** | In-process LRU cache | Current session |
| **Project** | SQLite-backed | Current project / working directory |
| **Global** | Persistent | User home |

## Conversation journaling

- `journal.py` — durable event journal for conversation history.
- `journaled.py` — journaled memory operations.
- `tool_history.py` — history of tool calls (used for replay).
- `compactor.py` — compacts old context when the window grows.
- `vector.py` — semantic search over stored entries.
- `router.py` — routes memory requests to the right tier.

## Auto behavior

- Conversation journaling records turns durably so interrupted/resumed turns
  can replay tool results.
- Compaction summarizes older context to stay within the model window.
- Semantic search (vector) surfaces relevant stored facts.

## From the TUI

- `/memory`-related functionality is exposed through the memory system; the
  agent can `remember`/`recall` facts across turns.

## Next

- [Tools →](09-tools.md)
- [Configuration →](02-configuration.md)
