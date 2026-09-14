# Memory

agenthicc provides tiered memory with durable conversation journaling.

## Tiers

| Tier | Scope | Lifetime |
|---|---|---|
| **Session** | The current session | The process |
| **Project** | The current project / working directory | Durable |
| **Global** | The user | Durable |

`MemoryRouter` (`src/agenthicc/memory/router.py`) is the supported dispatch
point; the three layers live in `src/agenthicc/memory/layers.py`. Reaching into
a layer directly is reserved for that layer's own implementation and tests.

!!! warning "A session-scoped write does not survive the process"
    Use `scope="project"` or `scope="global"` for anything that must outlive
    the run, and use a namespace to keep unrelated values apart in the global
    tier.

## Conversation journaling

The memory package owns these modules:

| Module | Role |
|---|---|
| `journal.py` | The durable conversation journal |
| `journaled.py` | Journaled memory operations |
| `tool_history.py` | Tool-call history, used for replay |
| `compactor.py` | Bounds context when the window grows |
| `vector.py` | Semantic search over stored entries |
| `router.py` | Routes a request to the right tier |
| `layers.py` | The session / project / global tiers |

The journal is authoritative and the live projection is a fold over it.

## Automatic behaviour

- Journaling records turns durably so an interrupted or resumed turn can replay
  its tool results instead of losing them.
- Compaction summarises older context to stay inside the model window. Manual
  `/compact` uses the same bounded map-reduce summariser, retries an empty
  final response, then falls back to bounded local recent history.
- Semantic search surfaces relevant stored facts.

## From the TUI

Memory is a **tool** surface, not a slash command — there is no `/memory`
command in the registry. The agent reads and writes memory during a turn, and
you can inspect the result through the transcript and `/usage`.

!!! danger "Never store credentials or unbounded tool output"
    Keep project memory inside the project's `.agenthicc/` directory, treat
    global memory as user data when collecting diagnostics, and redact before
    writing. A record with a credential-shaped value means something upstream
    failed to sanitise it.

## Next

- [Tools →](09-tools.md)
- [Configuration →](02-configuration.md)
- Depth: [Memory guide](../guides/memory.md)
