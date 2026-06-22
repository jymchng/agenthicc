# Agenthicc

**A state-driven agent OS** — event-sourced kernel, parallel DAG execution,
tool-only inter-agent communication, and a full-screen TUI with a pinned input bar.

---

## TUI preview

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ● agent:planner  09:41:22                                                │
│   > parsing intent: "refactor the auth module to use JWT"                │
│   > identified 4 tasks, spawning workers                                 │
│     [tool] agent_spawn  ✓  12ms                                          │
│     [tool] task_create  ✓  38ms                                          │
│     [tool] task_create  ✓  41ms                                          │
│     [tool] task_create  ✓  39ms                                          │
│   → tokens: 892  cost: $0.002                                            │
│ ────────────────────────────────────────────────────                     │
│ ● agent:worker-1  09:41:24                                               │
│   > writing tests for AuthService                                        │
│     [tool] file_write  ✓  102ms                                          │
│ ────────────────────────────────────────────────────                     │
│ ● agent:worker-2  09:41:25                                               │
│   > refactoring AuthService._validate                                    │
│     [tool] file_write  ⣻ running...                                      │
├──────────────────────────────────────────────────────────────────────────┤
│  3 agents | $0.005 | 2,132 tok                                           │
├──────────────────────────────────────────────────────────────────────────┤
│ > _                                                                      │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Feature highlights

- **Event-sourced kernel** — every state change is an immutable `Event` appended
  to a JSON-lines log. Crash recovery replays the log via `restore_from_log`.
- **Three-tier memory** — session LRU/TTL, project SQLite, and global SQLite
  layers with a `MemoryRouter` that dispatches by key prefix.
- **Lifecycle hooks everywhere** — `LifecycleHook` ABC with `pre_execute`,
  `post_execute`, and `on_error` stages run in parallel via `asyncio.gather`.
- **Pure TUI rendering** — `TranscriptModel` has zero terminal dependencies
  and is fully testable headless. The prompt_toolkit `Application` is the only
  layer that touches the terminal.
- **Headless REST+WebSocket API** — `create_app` builds a FastAPI app with
  `POST /v1/intents`, `GET /v1/intents/{id}`, `GET /v1/state/summary`, and
  `WS /v1/ws`.
- **TOML configuration with deep-merge** — `agenthicc.toml` (project) and
  `~/.agenthicc.toml` (user) are merged; user settings win.

---

## Quick install

```bash
# Core only
pip install agenthicc

# With TUI (prompt_toolkit)
pip install "agenthicc[tui]"

# With headless API (FastAPI + uvicorn)
pip install "agenthicc[api]"

# Everything
pip install "agenthicc[tui,api]"
```

## Quick start

```python
import asyncio
from agenthicc.kernel import AppState, EventProcessor, Event

async def main():
    state = AppState.create()
    proc = EventProcessor(initial_state=state, persist=False)
    asyncio.create_task(proc.run())

    await proc.emit(Event.create("IntentCreated", {
        "intent_id": "i1",
        "raw_text": "refactor the auth module",
    }))
    await proc.drain()
    print(proc.get_state().intents)

asyncio.run(main())
```

### Start the TUI

```bash
python -m agenthicc
```

Or programmatically:

```python
from agenthicc.tui.app import build_app
from agenthicc.tui.transcript import TranscriptModel

model = TranscriptModel()
app = build_app(model, on_input=lambda text: print("intent:", text))
app.run()
```

### Start the headless API

```python
import uvicorn
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.api.server import create_app

proc = EventProcessor(AppState.create(), persist=True)
app = create_app(proc, api_key="secret")
uvicorn.run(app, host="127.0.0.1", port=8000)
```

---

## Core concepts at a glance

| Concept | Class | Description |
|---|---|---|
| Kernel state | `AppState` | Frozen, copy-on-write, event-sourced |
| Events | `Event` | Immutable records; appended to log |
| Event loop | `EventProcessor` | MPSC queue; applies reducer; fans out state |
| Memory | `MemoryRouter` | Session / project / global tier dispatch |
| Hooks | `HookRunner` | Pre/post/error stages; parallel gather |
| TUI model | `TranscriptModel` | Pure Python; renderable headlessly |
| TUI app | `build_app` | prompt_toolkit Application |
| API | `create_app` | FastAPI with REST + WebSocket |
| Config | `load_config` | TOML deep-merge with typed dataclasses |

---

## Links to guides

- [Getting Started](guides/quickstart.md) — install, first run, concepts
- [TUI Guide](guides/tui.md) — full layout reference, key bindings, slash commands
- [Writing Workflows](guides/workflows.md) — PhaseSpec, agent types, params, troubleshooting, ergonomics findings
- [Memory & Artifacts](guides/memory.md) — three-tier memory, TTL, artifact sharing
- [Lifecycle Hooks](guides/hooks.md) — LifecycleHook ABC, HookRunner, recovery
- [Configuration](guides/configuration.md) — complete TOML reference
- [Architecture](guides/architecture.md) — event-sourcing deep-dive, layer diagram

## Links to reference

- [Kernel reference](reference/kernel.md) — AppState, Event, EventProcessor, root_reducer
- [API reference](reference/api.md) — REST and WebSocket endpoints

## AI assistant files

- [`llms.txt`](../llms.txt) — ~2KB overview for AI assistants
- [`llms-full.txt`](../llms-full.txt) — full API reference with signatures
- [`skills/`](../skills/README.md) — six skill guides for specific tasks
