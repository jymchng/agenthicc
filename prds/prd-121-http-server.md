# PRD-121 — HTTP Server (lauren-framework)

## Problem

Agenthicc is TUI-first. Every agent turn flows through `TUISession` →
`ConversationStore` signals → `ScrollBufferAppender` → prompt_toolkit Live
block. There is no way to drive an agent turn from an HTTP client — a CI
pipeline, a web frontend, another service, or a headless test harness.

## Goal

Expose agenthicc as an ASGI HTTP service built on **lauren-framework**, with
streaming agent responses delivered via **Server-Sent Events (SSE)**. The HTTP
server is a new entrypoint *alongside* the TUI, not a replacement — all
existing code (`AgentRunnerBase`, `ShortTermMemory`, `ToolExecutor`) is reused
unchanged.

## Background: Lauren-Framework

| Feature | Detail |
|---|---|
| Routing | `@controller("/prefix")` + `@get`/`@post` on methods; radix-tree O(depth) |
| DI | `DIContainer` with `SINGLETON`/`REQUEST`/`TRANSIENT` scopes; `@injectable`; `@module(providers=[], controllers=[])` |
| SSE | `EventStream(async_generator)` returns `text/event-stream`; first-class |
| WebSockets | `@ws_controller`, `BroadcastGroup` fan-out; available for follow-on work |
| ASGI | Framework is ASGI-only; run with `uvicorn` or `hypercorn` (not bundled) |
| Agent wiring | No built-in AgentRunner integration — registered as a normal DI singleton |

## API Surface (v1 — Auto mode only)

```
POST   /v1/sessions                        Create a new session → {session_id}
POST   /v1/sessions/{id}/turns             Run one turn, stream response as SSE
GET    /v1/sessions/{id}/turns             List turn history (from memory snapshot)
DELETE /v1/sessions/{id}                   Clear session memory
```

### `POST /v1/sessions/{id}/turns` — request body

```json
{
  "message": "please concise the docs",
  "workflow": null
}
```

`workflow` is reserved for future use (PRD-121 v1 does not implement it).

### SSE event shape

Each turn streams a sequence of typed events:

```
event: delta
data: {"text": "hello "}

event: delta
data: {"text": "world"}

event: tool_call
data: {"name": "git_status", "tool_use_id": "tu_01..."}

event: tool_result
data: {"name": "git_status", "success": true, "duration_ms": 45}

event: done
data: {"stop_reason": "end_turn", "turns": 3, "input_tokens": 1200, "output_tokens": 340}

event: error
data: {"message": "TransportError: ..."}
```

The stream closes after `done` or `error`.

## Architecture

### The gap: TUI-specific scaffolding

The current `run_turn()` path writes to `ConversationStore` (TUI signals) and
`ScrollBufferAppender` (Rich console). Neither makes sense over HTTP.
However, `runner.run_stream()` itself is transport-agnostic — it takes a
`ShortTermMemory` object and yields `CompletionChunk` items. The TUI scaffolding
is downstream of the runner.

The HTTP path bypasses the TUI entirely and calls `runner.run_stream()`
directly, translating chunks into SSE events.

### New files

#### `src/agenthicc/server/session_store.py`

`SessionStore` — `@injectable(SINGLETON)`:

```python
class SessionStore:
    def get_or_create(self, session_id: str) -> ShortTermMemory: ...
    def delete(self, session_id: str) -> None: ...
    def snapshot(self, session_id: str) -> list[Any]: ...
```

In-memory implementation: `dict[str, ShortTermMemory]`. Optional persistence
via `SQLiteConversationStore` (from lauren-ai) for cross-process durability —
selected by config flag `server.persist_sessions`.

#### `src/agenthicc/server/agent_controller.py`

`@controller("/v1/sessions")` class. Injects `AgentRunnerBase` and
`SessionStore` via constructor DI.

```
POST /                  → create session
POST /{id}/turns        → run turn, return EventStream
GET  /{id}/turns        → return snapshot
DELETE /{id}            → delete session
```

The `POST /{id}/turns` handler:

1. Restores `ShortTermMemory` from `SessionStore`.
2. Builds a minimal `@agent`-decorated class with the configured tool set
   (same tool population as `AgentTurnRunner._build_agent()`).
3. Calls `runner.run_stream(agent, message, memory=mem)`.
4. Translates `CompletionChunk` items into SSE events:
   - `chunk.delta` → `event: delta`
   - `chunk.tool_call_delta` (name present, no prior delta) → `event: tool_call`
   - `chunk.stop_reason` → `event: done`
5. Saves updated memory snapshot back to `SessionStore`.
6. Returns `EventStream(generator)`.

On exception: emits `event: error` then closes the stream.

#### `src/agenthicc/server/app_module.py`

Root `@module` that wires the DI graph:

```python
@module(
    providers=[
        SessionStore,
        AgentControllerFactory,   # factory that resolves transport from config
    ],
    controllers=[AgentController],
)
class ServerAppModule:
    pass

app = LaurenFactory.create(ServerAppModule)
```

`AgentControllerFactory` reads `AgenthiccConfig` to build `LLMConfig` →
transport → `AgentRunnerBase`, identical to `_build_agent_runner()` in
`tui_session.py`.

#### `src/agenthicc/server/__main__.py`

```python
import uvicorn
from agenthicc.server.app_module import app
uvicorn.run(app, host="0.0.0.0", port=8000)
```

Invoked via `uv run python -m agenthicc.server` or a new CLI entry point
`agenthicc serve`.

### What does NOT change

| Component | Reused as-is |
|---|---|
| `AgentRunnerBase` | Yes — same instance, same `run_stream()` |
| `ShortTermMemory` | Yes — session state, same trim/compact logic |
| `ToolExecutor` | Yes — same tool dispatch |
| Compaction (`compact_memory`) | Yes — fires automatically via `should_compact` |
| TUI / `TUISession` | Unchanged — parallel entrypoint |

## Out of scope for v1

| Feature | Reason deferred |
|---|---|
| Workflow (code_plan) over HTTP | Requires headless `ApprovalService` — plan-review overlay replaced by `POST /v1/sessions/{id}/approvals/{tool_use_id}`. Scoped to PRD-122. |
| Authentication | Add a Bearer-token guard via `@use_guards(ApiKeyGuard)` in a follow-on. |
| WebSocket transport | SSE covers the streaming use case; WS adds bidirectional cancel which is a follow-on. |
| Multi-tenant tool isolation | Per-session `WorkspaceView` sandboxing is a follow-on. |

## Acceptance criteria

| # | Criterion |
|---|---|
| 121.1 | `POST /v1/sessions` returns a unique `session_id`. |
| 121.2 | `POST /v1/sessions/{id}/turns` streams SSE with `delta`, `tool_call`, `tool_result`, and `done` event types. |
| 121.3 | Conversation history persists across consecutive turns within the same session. |
| 121.4 | `DELETE /v1/sessions/{id}` clears the session; the next turn starts fresh. |
| 121.5 | `event: error` is emitted and the stream closes cleanly on any unhandled exception. |
| 121.6 | The TUI entrypoint (`uv run agenthicc`) is unaffected. |
| 121.7 | `uv run python -m agenthicc.server` starts the ASGI app on port 8000. |
| 121.8 | Auto-compact fires transparently mid-turn when memory exceeds threshold. |

## Files to create / modify

| File | Status | Change |
|---|---|---|
| `src/agenthicc/server/__init__.py` | New | Package root |
| `src/agenthicc/server/session_store.py` | New | `SessionStore` injectable |
| `src/agenthicc/server/agent_controller.py` | New | `AgentController` + SSE streaming |
| `src/agenthicc/server/app_module.py` | New | Root `@module` + `LaurenFactory.create` |
| `src/agenthicc/server/__main__.py` | New | `uvicorn.run` entrypoint |
| `pyproject.toml` | Modify | Add `lauren-framework` dependency; add `agenthicc serve` script |
| `tests/integration/test_http_server.py` | New | `httpx` integration tests against a live ASGI app |
