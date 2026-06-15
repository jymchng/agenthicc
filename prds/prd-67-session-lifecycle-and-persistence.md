# PRD-67 — Session Lifecycle, Persistence & Headless Mode

## 1. Session Lifecycle Overview

A **session** is a single conversation with the agent that spans from
`agenthicc` startup to shutdown. Sessions are persisted so they can be
resumed. The session system is orthogonal to the conversation runtime —
it handles file I/O and CLI flags, not UI rendering.

```
CLI startup
    │
    ├── --resume <id>       → load specific session
    ├── --continue          → load latest session for cwd
    └── (no flag)           → create new session
    │
    ▼
Session created / loaded
    │
    ▼
ConversationStore initialised with session data
    │
    ▼
Application runtime runs
    │
    ▼
CLI shutdown
    │
    ▼
Session saved / index updated
```

---

## 2. Session Identity

### 2.1 Session ID

A session ID is a UUID4 string. It is created at startup if no `--resume` is
provided.

```python
import uuid

def create_session_id() -> str:
    return str(uuid.uuid4())
```

### 2.2 Session Directory

```
~/.agenthicc/
  sessions/
    <session-id>/
      conversation.jsonl    ← append-only event log
      metadata.json         ← session metadata (cwd, model, created_at)
    index.json              ← maps session_id → metadata for quick lookup
```

---

## 3. Session Index

The index file provides fast lookup without scanning every session directory.

```python
import json
from pathlib import Path

_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"
_SESSION_INDEX = _SESSIONS_DIR / "index.json"


def _load_index() -> dict:
    if _SESSION_INDEX.exists():
        try:
            return json.loads(_SESSION_INDEX.read_text())
        except Exception:
            return {}
    return {}


def _save_index(data: dict) -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _SESSION_INDEX.write_text(json.dumps(data, indent=2))


def register_session(session_id: str, cwd: str, model: str) -> None:
    """Create a new session entry in the index."""
    import time
    index = _load_index()
    index[session_id] = {
        "cwd": cwd,
        "model": model,
        "created_at": time.time(),
        "last_active": time.time(),
    }
    _save_index(index)
    # Create session directory
    session_dir = _SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "metadata.json").write_text(json.dumps(index[session_id]))


def touch_session(session_id: str) -> None:
    """Update last_active timestamp for a session."""
    import time
    index = _load_index()
    if session_id in index:
        index[session_id]["last_active"] = time.time()
        _save_index(index)


def find_latest_session_for_cwd(cwd: str | None = None) -> str | None:
    """Return the most recently active session ID for the given cwd."""
    import os
    cwd = cwd or os.getcwd()
    index = _load_index()
    candidates = [
        (sid, meta) for sid, meta in index.items()
        if meta.get("cwd") == cwd
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda x: x[1].get("last_active", 0))
    return latest[0]


def get_session_log_path(session_id: str) -> Path:
    return _SESSIONS_DIR / session_id / "conversation.jsonl"
```

---

## 4. Session Persistence: Event Log

Each session has an append-only `conversation.jsonl` file. Every
`ConversationEvent` is appended when it is created.

```python
import json
from pathlib import Path

class SessionEventLog:
    """Appends ConversationEvents to a JSONL file for session persistence."""

    def __init__(self, session_id: str) -> None:
        self._path = get_session_log_path(session_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8")

    def append(self, ev: ConversationEvent) -> None:
        import dataclasses
        record = {
            "event_id":  ev.event_id,
            "kind":      ev.kind,
            "payload":   ev.payload,
            "timestamp": ev.timestamp,
        }
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    @staticmethod
    def load(session_id: str) -> list[ConversationEvent]:
        """Load all events from the log for session restoration."""
        path = get_session_log_path(session_id)
        if not path.exists():
            return []
        events: list[ConversationEvent] = []
        for line in path.read_text().splitlines():
            try:
                data = json.loads(line)
                events.append(ConversationEvent(
                    event_id=data["event_id"],
                    kind=data["kind"],
                    payload=data["payload"],
                    timestamp=data["timestamp"],
                    rendered=True,   # already displayed; skip rendering on restore
                ))
            except Exception:
                pass
        return events
```

### 4.1 Wiring to ConversationStore

```python
# In AppRuntime / startup:
session_log = SessionEventLog(session_id)

# Subscribe to all new events
conversation.on_event(
    lambda ev: session_log.append(ev)
)

# On shutdown:
session_log.close()
```

---

## 5. Session Restoration

When `--resume <id>` or `--continue` is used, the previous session's events
are loaded and replayed into `ConversationStore`. **Events are loaded with
`rendered=True`** so they do not get re-printed to the scroll buffer.

The scroll buffer shows the previous conversation as a visual context header,
not by re-printing all events.

```python
async def restore_session(session_id: str, conversation: ConversationStore) -> None:
    """Restore a previous session into ConversationStore."""
    events = SessionEventLog.load(session_id)
    if not events:
        return

    # Restore metrics from the last tokens event
    for ev in events:
        if ev.kind == "tokens":
            conversation.tokens_in.set(ev.payload.get("total_in", 0))
            conversation.tokens_out.set(ev.payload.get("total_out", 0))
            conversation.cost_usd.set(ev.payload.get("total_cost", 0.0))

    # Reconstruct turns (for history and context)
    current_turn: ConversationTurn | None = None
    for ev in events:
        if ev.kind == "turn_start":
            current_turn = ConversationTurn(
                turn_id=ev.payload.get("turn_id", ev.event_id),
                agent_name=ev.payload.get("agent_name", "assistant"),
                timestamp=ev.timestamp,
            )
            conversation.turns.set(conversation.turns.get() + [current_turn])
        elif current_turn is not None:
            current_turn.events.append(ev)

    # Print a visual "resumed session" notice to the scroll buffer
    # (NOT via append_event — this is a one-off UI note, not stored)
    conversation.notification.set(
        f"Resumed session {session_id[:8]}… with {len(events)} events"
    )
```

---

## 6. `resume_id` in Exit Hints

When the user presses Ctrl+C twice to exit, the exit hint shows how to resume.
The `resume_id` must be threaded through to `UnifiedInputSession`.

```python
# In UnifiedInputSession:
def _ctrl_c_sequence(self) -> object:
    self._ctrl_c_count += 1
    if self._ctrl_c_count == 1:
        ...
        return None
    # Second press — show exit hint
    resume_id = self._state.conversation.session_id()
    self._renderer.show_exit_hint(resume_id)
    return _EXIT

# PromptRenderer.show_exit_hint (unchanged from current):
def show_exit_hint(self, resume_id: str = "") -> None:
    if resume_id:
        hints = [
            f"  To resume:  agenthicc --resume {resume_id}",
            f"  Or in this directory:  agenthicc --continue",
        ]
    else:
        hints = [f"  To resume:  agenthicc --continue  (in this directory)"]
    ...
```

---

## 7. Application Startup / Shutdown

### 7.1 Startup Sequence

```python
async def run_tui_session(
    resume_id: str | None = None,
    continue_session: bool = False,
    cli_overrides: list[str] | None = None,
) -> None:
    import os
    from agenthicc.config import load_config

    # 1. Load configuration
    cfg = load_config(cli_overrides or [])

    # 2. Resolve session
    if continue_session:
        resume_id = find_latest_session_for_cwd()
        if resume_id is None:
            print("No previous session found for this directory. Starting fresh.")
    session_id = resume_id or create_session_id()
    cwd = os.getcwd()

    # 3. Create reactive state
    app_state = AppState.create()
    app_state.conversation.session_id.set(session_id)
    app_state.conversation.model_name.set(cfg.execution.model or "")

    # 4. Restore previous session if resuming
    if resume_id:
        await restore_session(resume_id, app_state.conversation)
    else:
        register_session(session_id, cwd, cfg.execution.model or "")

    # 5. Create session event log (appends all new events)
    session_log = SessionEventLog(session_id)
    app_state.conversation.on_event(session_log.append)

    # 6. Build the runtime
    mode_registry  = build_default_registry()
    mode_manager   = ModeManager(mode_registry)
    event_bus      = EventBus()
    command_bus    = CommandBus()
    task_manager   = TaskManager()
    kernel_proc    = _create_kernel_processor(cfg)
    agent_runtime  = AgentRuntime(
        conversation=app_state.conversation,
        event_bus=event_bus,
        task_manager=task_manager,
    )

    # 7. Build the workspace
    from rich.console import Console
    console   = Console(highlight=False, markup=True, force_terminal=True)
    workspace = Workspace(app_state, console)
    overlay   = workspace.overlay_host

    # 8. Wire commands
    wire_commands(command_bus, agent_runtime, app_state.conversation, overlay, event_bus)

    # 9. Build input session
    trigger_registry = _build_trigger_registry(command_bus)
    input_session = UnifiedInputSession(
        app_state=app_state,
        command_bus=command_bus,
        trigger_registry=trigger_registry,
        mode_manager=mode_manager,
        overlay_host=overlay,
        cwd=Path(cwd),
        cfg=cfg,
    )

    # 10. Start kernel bridge
    kernel_bridge = KernelBridge(kernel_proc, app_state.conversation, mode_manager)
    kernel_bridge.start()

    # 11. Start workspace (begins always-on Live block)
    workspace.start()

    try:
        # 12. Run input session (blocks until exit)
        await input_session.run()
    finally:
        # 13. Shutdown
        kernel_bridge.stop()
        workspace.stop()
        session_log.close()
        touch_session(session_id)
        _reset_terminal_on_exit()  # belt-and-suspenders
```

### 7.2 Config Loading

`_loaded_config` (currently set as a dynamic attribute on `AgenthiccTUI`) is
replaced by passing `cfg` explicitly through the startup sequence. The `cfg`
object is passed to:

- `UnifiedInputSession` → for `/config` overlay
- `AgentRuntime` → for model/provider settings
- `KernelBridge` → for kernel processor creation

```python
# In UnifiedInputSession:
async def _handle_builtin_command(self, cmd: RunBuiltinCommand) -> None:
    match cmd.name:
        case "config":
            self._overlay_host.show(
                ConfigMenuOverlay(cfg=self._cfg, on_close=self._overlay_host.hide)
            )
        case "model":
            ...
```

---

## 8. Headless Mode

Headless mode outputs a JSON-lines stream to stdout — no TUI, no Rich Live.
It is used for programmatic integration, CI/CD, and testing.

### 8.1 Activation

```
agenthicc --headless
```

### 8.2 JSON-lines Protocol

Each line is a JSON object:

```json
{"type": "session_start",  "session_id": "abc123", "timestamp": 1718000000.0}
{"type": "turn_start",     "agent_name": "laguna-xs.2", "timestamp": 1718000010.0}
{"type": "tool_complete",  "name": "list_directory", "args": {"path": "."}, "success": true, "duration_ms": 6.0}
{"type": "text",           "text": "The repository contains…"}
{"type": "turn_complete",  "tokens_in": 1200, "tokens_out": 800, "cost_usd": 0.015}
{"type": "session_end",    "timestamp": 1718000060.0}
```

### 8.3 HeadlessRuntime

```python
import json, sys

class HeadlessRuntime:
    """Outputs JSON-lines to stdout. No TUI, no Live block."""

    def __init__(self, conversation: ConversationStore) -> None:
        self._conv = conversation
        conversation.on_event(self._on_event)

    def _emit(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def _on_event(self, ev: ConversationEvent) -> None:
        if ev.rendered:
            return
        ev.rendered = True
        self._emit({"type": ev.kind, **ev.payload, "timestamp": ev.timestamp})

    def emit_session_start(self, session_id: str) -> None:
        import time
        self._emit({"type": "session_start", "session_id": session_id,
                    "timestamp": time.time()})

    def emit_session_end(self) -> None:
        import time
        self._emit({"type": "session_end", "timestamp": time.time()})
```

### 8.4 Headless Startup Path

```python
async def run_headless(cli_overrides: list[str] | None = None) -> None:
    cfg        = load_config(cli_overrides or [])
    app_state  = AppState.create()
    session_id = create_session_id()
    app_state.conversation.session_id.set(session_id)

    headless   = HeadlessRuntime(app_state.conversation)
    headless.emit_session_start(session_id)

    agent_runtime = AgentRuntime(
        conversation=app_state.conversation,
        event_bus=EventBus(),
        task_manager=TaskManager(),
    )

    # Read prompts from stdin (one per line)
    import asyncio, sys
    for line in sys.stdin:
        prompt = line.strip()
        if prompt:
            await agent_runtime.handle_send_message(SendMessageCommand(text=prompt))
            # Wait for turn to complete
            while app_state.conversation.is_running():
                await asyncio.sleep(0.1)

    headless.emit_session_end()
```

---

## 9. ConversationStore Additions (extends PRD-59)

Add these signals to `ConversationStore.__init__`:

```python
# Session
self.session_id:   Signal[str]   = Signal("")
self.model_name:   Signal[str]   = Signal("")

# Mode (added here to avoid circular import with ModeManager)
self.active_mode_name:  Signal[str] = Signal("Auto")
self.active_mode_badge: Signal[str] = Signal("⏵⏵")
self.mode_str:          Signal[str] = Signal(
    "⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵"
)

# Transient footer notification (cleared after display)
self.notification: Signal[str | None] = Signal(None)
```

---

## 10. Acceptance Criteria

| Criterion | Test |
|---|---|
| New session creates index entry | `test_register_session()` |
| `--continue` finds latest session for cwd | `test_find_latest_session()` |
| Session events persisted to JSONL | `test_session_event_log_append()` |
| Restored session has correct turn count | `test_restore_session_turn_count()` |
| Restored events have `rendered=True` | `test_restored_events_not_reprinted()` |
| `resume_id` shown in Ctrl+C exit hint | `test_exit_hint_shows_resume_id()` |
| Headless mode outputs valid JSON-lines | `test_headless_json_lines()` |
| `cfg` accessible in `/config` overlay | `test_config_overlay_has_cfg()` |
| Session log closed cleanly on exit | `test_session_log_closed_on_exit()` |
| `touch_session` updates last_active | `test_touch_session()` |
