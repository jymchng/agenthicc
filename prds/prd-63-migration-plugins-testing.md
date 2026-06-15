# PRD-63 — Migration Strategy, Plugin Architecture & Testing

## 1. Migration Strategy

### 1.1 Guiding Principle: No Big-Bang Rewrite

The migration proceeds in **vertical slices** — each slice delivers a working,
testable improvement without breaking existing functionality. Each slice
corresponds to one or two PRD subsystems.

```
Slice 0  (Foundation):  Reactive state graph alongside current code
Slice 1  (Rendering):   Always-on Live block, auto_refresh=False
Slice 2  (Events):      ConversationStore + ScrollBufferAppender
Slice 3  (Commands):    CommandBus + AgentRuntime
Slice 4  (Input):       UnifiedInputSession (one raw_mode context)
Slice 5  (Overlays):    OverlayHost + TriggerPickerOverlay
Slice 6  (Cleanup):     Delete all legacy code
```

### 1.2 Slice 0 — Reactive State Graph (Week 1)

**Goal**: Introduce `Signal`, `Computed`, `ConversationStore` alongside the
existing codebase. Wire it to the current `AgenthiccTUI` state setters.

**Changes**:
- Add `agenthicc/reactive.py` with `Signal` and `Computed`
- Add `agenthicc/tui/conversation_store.py` with `ConversationStore`
- In `AgenthiccTUI.__init__`: create `AppState` and wire existing setters as adapters:
  ```python
  # Existing: self.status_state.tool = e.name
  # New: self.app_state.conversation.set_tool(e.name)
  # Also: self.status_state.tool = e.name  ← kept for compatibility
  ```
- Add `ConversationStore.on_event()` subscriber (ScrollBufferAppender stub)

**Exits**: `_printed_count` is still there; `flush_from_model()` still runs.
Slice 0 adds parallel state, does not remove anything.

**Tests**: unit tests for Signal, Computed, ConversationStore.

### 1.3 Slice 1 — Always-On Live Block (Week 1–2)

**Goal**: Eliminate `live_panel.start()` / `live_panel.stop()` from the agent turn loop.

**Changes**:
- `LivePanel.start()` called ONCE in `AgenthiccTUI.run()` before the input loop
- `LivePanel.stop()` called ONCE in the `finally` block
- Remove `live_panel.start()` / `live_panel.stop()` from the agent turn path
- Add `auto_refresh=False` (already done; keep)
- `LivePanel._build()` subscribes to `AppState` signals for redraw

**Current code removed**:
```python
# BEFORE (in tui.py run loop):
self.live_panel.start()
...agent turn...
self.live_panel.stop()

# AFTER: nothing — live panel always on
```

**Risk**: The `transient=True` flag was used so the live block cleared on stop.
With always-on, we switch to `transient=False`. The live block is a persistent
bottom-of-screen fixture. Conversation content scrolls above it naturally.

**Tests**: integration test — 10 agent turns, verify no cursor corruption.

### 1.4 Slice 2 — ScrollBufferAppender (Week 2–3)

**Goal**: Replace `_on_tool_complete` / `_on_assistant_complete` /
`flush_from_model()` with the `ScrollBufferAppender` subscriber pattern.

**Changes**:
- Add `ScrollBufferAppender` class
- Wire it to `ConversationStore.on_event()`
- In `AgenthiccTUI._on_tool_complete`: append to `ConversationStore` instead of `console.print()`
- In `AgenthiccTUI._on_assistant_complete`: append text to `ConversationStore`
- In `AgenthiccTUI._flush_new_lines`: delegate to `ScrollBufferAppender.flush_pending()`
- Delete `TranscriptModel`-based flush path

**Current code deleted**:
```python
# DELETE:
self.transcript.flush_from_model()
self._printed_count
self._text_events_printed
ConsoleTranscriptView.flush_from_model()
```

**Tests**: verify all tool calls show args + correct status; verify LLM text appears.

### 1.5 Slice 3 — Command Bus & AgentRuntime (Week 3–4)

**Goal**: Replace ad-hoc `_run_agent` closure with typed `CommandBus` + `AgentRuntime`.

**Changes**:
- Add `CommandBus`, command dataclasses, `AgentRuntime`
- Wire `SendMessageCommand` → `AgentRuntime.handle_send_message`
- Wire `InterruptAgentCommand` → `AgentRuntime.interrupt`
- Replace `_run_agent` closure in `tui.py.run()` with `CommandBus.dispatch_async`
- Remove `_sigint_cancel` lambda (replaced by `InterruptAgentCommand`)
- Remove `_pending_queue` list (queued messages become queued commands)

**Tests**: command dispatch unit tests; agent turn integration test.

### 1.6 Slice 4 — Unified Input Session (Week 4–5)

**Goal**: Replace `IdleInputSession` + `StreamingSession` with `UnifiedInputSession`.

**Changes**:
- Add `UnifiedInputSession` with `InputMode` enum
- One `raw_mode` context for the application lifetime
- `AgentRuntime` calls `session.set_mode(STREAMING)` / `set_mode(IDLE)`
- Remove `StreamingSession`, `IdleInputSession`, and the `to_thread()` wrapping
- Remove all `asyncio.sleep(0)` race-fix calls
- Remove `_reset_terminal_on_exit()` (no longer needed — single raw_mode)
- Remove paranoid ECHO check from `cbreak_reader.raw_mode`

**Risk**: The entire `cbreak_reader` / `input/session.py` stack changes.
Must be done with careful incremental testing.

**Tests**: 
- Arrow keys work in idle mode
- Trigger picker works without mode switch
- Ctrl+C during streaming cancels agent
- Double Ctrl+C exits cleanly

### 1.7 Slice 5 — Overlay System (Week 5–6)

**Goal**: Replace `read_line_with_mention` + `InputSession._driver` with `OverlayHost`.

**Changes**:
- Add `OverlayHost`, `TriggerPickerOverlay`, `ConfigMenuOverlay`
- Triggers show as overlays within the always-on Live block
- Remove `ConfigurationMenu` widget (replaced by `ConfigMenuOverlay`)
- Remove `_pending_menu` from `AgenthiccTUI`
- Remove `_make_redraw_compat` / `_fn_redraw` compat layer
- Remove `mention_input.py` and `input_area.py` shims

**Tests**: trigger picker appears inline; config menu opens via /config.

### 1.8 Slice 6 — Delete Legacy Code (Week 6)

**Goal**: Remove all code that existed only for backward compatibility.

**Files deleted**:
```
src/agenthicc/tui/mention_input.py
src/agenthicc/tui/input_area.py
src/agenthicc/tui/input/streaming.py
src/agenthicc/tui/streaming_input.py  (shim)
src/agenthicc/tui/console_transcript.py
src/agenthicc/tui/transcript.py       (after migrating to ConversationStore)
src/agenthicc/runners/agent_turn.py   (logic moved to AgentRuntime)
```

**Symbols deleted**:
- `_printed_count`, `_text_events_printed`, `_pending_menu`
- `flush_from_model()`, `_flush_new_lines()`
- `SpinnerState`, `TranscriptModel`, `AgentTurnEntry`, `ToolCallEntry`
- `live_panel.start()`, `live_panel.stop()` in agent turn path
- `asyncio.sleep(0)` race-fix calls

---

## 2. Plugin Architecture

### 2.1 Plugin Protocol

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class Plugin(Protocol):
    """Minimal interface that all plugins must satisfy."""
    name: str
    version: str

    def register(self, registry: "PluginRegistry") -> None:
        """Called once at startup. Register commands, overlays, tools."""
```

### 2.2 Plugin Registry

```python
class PluginRegistry:
    """Plugins extend the system via this registry. Never modify core runtime."""

    def __init__(
        self,
        command_bus: CommandBus,
        event_bus: EventBus,
        overlay_host: OverlayHost,
    ) -> None:
        self._command_bus  = command_bus
        self._event_bus    = event_bus
        self._overlay_host = overlay_host
        self._plugins: list[Plugin] = []

    def register_command(self, command_type: type, handler: Callable) -> None:
        self._command_bus.register(command_type, handler)

    def register_event_handler(self, event_type: type, handler: Callable) -> Callable:
        return self._event_bus.subscribe(event_type, handler)

    def register_overlay(self, name: str, factory: Callable[[], Overlay]) -> None:
        # Allows plugins to add custom overlays (e.g. a git diff viewer)
        self._overlay_factories[name] = factory

    def load(self, plugin: Plugin) -> None:
        plugin.register(self)
        self._plugins.append(plugin)
        print(f"Plugin loaded: {plugin.name} v{plugin.version}")
```

### 2.3 Example Plugin

```python
class GitPlugin:
    name = "git"
    version = "1.0.0"

    def register(self, registry: PluginRegistry) -> None:
        # Add a /git-log command
        registry.register_command(GitLogCommand, self._handle_git_log)
        # Add a git diff overlay
        registry.register_overlay("git_diff", GitDiffOverlay)

    async def _handle_git_log(self, cmd: "GitLogCommand") -> None:
        result = await run_git_command(["git", "log", "--oneline", "-20"])
        # Publish as a conversation event
        ...
```

### 2.4 Plugin Boundaries

Plugins MUST NOT:
- Access `AppState` directly (use event/command APIs only)
- Modify core runtime classes
- Import from internal `_` prefixed modules
- Block the event loop

Plugins MAY:
- Register new commands
- Subscribe to events
- Contribute new overlays
- Add new tool implementations
- Add new theme tokens

---

## 3. Testing Strategy

### 3.1 Unit Tests

**Signal system**:
```python
def test_signal_notify():
    s = Signal(0)
    calls = []
    s.subscribe(lambda: calls.append(s()))
    s.set(1)
    assert calls == [1]

def test_computed_recalculates():
    a = Signal(2)
    b = Signal(3)
    c = Computed(lambda: a() + b(), a, b)
    assert c() == 5
    a.set(10)
    assert c() == 13
```

**ConversationStore**:
```python
def test_begin_end_turn():
    store = ConversationStore()
    turn = store.begin_turn("t1", "assistant")
    assert store.agent_state() == AgentState.THINKING
    store.end_turn()
    assert store.agent_state() == AgentState.IDLE

def test_event_subscriber():
    store = ConversationStore()
    events = []
    store.on_event(events.append)
    store.begin_turn("t1", "assistant")
    store.append_event("text", {"text": "hello"})
    assert len(events) == 1
    assert events[0].kind == "text"
```

**CommandBus**:
```python
def test_command_dispatch():
    bus = CommandBus()
    received = []
    bus.register(SendMessageCommand, lambda cmd: received.append(cmd.text))
    bus.dispatch(SendMessageCommand(text="hello"))
    assert received == ["hello"]
```

### 3.2 Component Tests

**ScrollBufferAppender**:
```python
def test_tool_complete_renders():
    console = FakeConsole()
    store   = ConversationStore()
    appender = ScrollBufferAppender(AppState(conversation=store), console)
    appender.mount()

    store.begin_turn("t1", "assistant")
    store.append_event("tool_complete", {
        "name": "read_file",
        "args_str": "(path='README.md')",
        "success": True,
        "dur_str": "  6ms",
        "output_lines": [],
    })

    assert any("read_file" in line for line in console.printed)
    assert any("✓" in line for line in console.printed)
    assert not any("⣾" in line for line in console.printed)  # no spinner
```

**StatusComponent**:
```python
def test_renders_thinking_animation():
    state = AppState()
    comp  = StatusComponent(state)
    comp.mount()
    state.conversation.agent_state.set(AgentState.THINKING)
    renderable = comp.render()
    text = _plain(renderable)
    assert "Thinking" in text or any(f in text for f in _FLOWERS)
```

### 3.3 Integration Tests

**Agent turn end-to-end**:
```python
async def test_full_agent_turn_no_corruption():
    app   = create_test_app()
    console = FakeConsole()

    # Run 10 turns with multiple tool calls each
    for i in range(10):
        await app.command_bus.dispatch_async(
            SendMessageCommand(text=f"test message {i}")
        )
        await asyncio.sleep(0.1)  # let events process

    # Verify: no ⣾ spinner in output (would mean RUNNING state rendered)
    assert not any("⣾" in line for line in console.printed)
    # Verify: all tool calls have ✓ or ✗
    tool_lines = [l for l in console.printed if "⎿" in l]
    assert all("✓" in l or "✗" in l for l in tool_lines)
    # Verify: LLM text present
    text_count = sum(1 for l in console.printed if "Based on" in l or "I'll" in l)
    assert text_count >= 10

async def test_cursor_no_desync():
    """Verify the Live block cursor never desynchronizes."""
    app = create_test_app()
    live_tracker = LiveCursorTracker(app.workspace._live)

    for i in range(50):
        await app.command_bus.dispatch_async(SendMessageCommand(text=f"msg {i}"))
        await asyncio.sleep(0.05)

    assert live_tracker.desync_count == 0
```

**Terminal state on exit**:
```python
def test_terminal_restored_after_exit():
    import subprocess
    result = subprocess.run(
        ["python", "-m", "agenthicc", "--headless", "--run-once", "hello"],
        capture_output=True,
        timeout=10,
    )
    # Verify ECHO is on after process exits
    import termios, sys
    settings = termios.tcgetattr(sys.stdin.fileno())
    assert settings[3] & termios.ECHO, "ECHO should be on after TUI exits"
```

### 3.4 Snapshot Tests

For the Rich rendering pipeline:

```python
def test_status_component_snapshot():
    state = AppState()
    state.conversation.agent_state.set(AgentState.THINKING)
    state.conversation.model_name.set("laguna-xs.2")
    state.conversation.elapsed_s.set(15.0)
    state.conversation.tokens_in.set(1200)
    state.conversation.tokens_out.set(800)

    comp   = StatusComponent(state)
    output = _render_to_text(comp.render(), width=80)

    # Snapshot assertion (uses approved snapshot file)
    assert_matches_snapshot(output, "status_thinking_15s")
```

---

## 4. Performance Considerations

| Concern | Mitigation |
|---|---|
| Signal notification O(N) per subscriber | Keep subscriber count <20 per signal; justified |
| `_redraw()` called on every signal change | Debounce with `asyncio.call_soon()` dedup |
| `console.print()` during rapid tool completions | `ScrollBufferAppender` batches rapid events (50ms window) |
| `Computed` recomputes on every dependency change | Cache value; skip if unchanged |
| `OverlayHost.render()` in hot path | Overlay renders are simple Rich Text; fast |

### 4.1 Event Batching (for rapid tool completions)

```python
class ScrollBufferAppender(Component):
    def __init__(self, ...) -> None:
        ...
        self._pending: list[ConversationEvent] = []
        self._flush_scheduled = False

    def _on_event(self, ev: ConversationEvent) -> None:
        self._pending.append(ev)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            asyncio.get_event_loop().call_soon(self._flush_batch)

    def _flush_batch(self) -> None:
        self._flush_scheduled = False
        for ev in self._pending:
            if not ev.rendered:
                ev.rendered = True
                self._render_one(ev)
        self._pending.clear()
```

---

## 5. Failure Modes & Mitigations

| Failure | Detection | Mitigation |
|---|---|---|
| Signal notification throws | `try/except` in `Signal.set()` | Log exception; continue; don't crash bus |
| `ScrollBufferAppender._render_one` throws | `try/except` in `_flush_batch` | Log; mark event rendered; skip |
| `console.print()` after terminal closed | `OSError` caught | Suppress silently |
| `UnifiedInputSession.run()` raises | `finally` in `Application.run()` | Terminal restored; user sees error |
| Overlay never dismissed | ESC always closes active overlay | Guaranteed by `FocusManager.route_key` fallthrough |
| `raw_mode` not restored on SIGKILL | `atexit._restore()` | Best-effort; SIGKILL is unrecoverable |
| Reactive cycle (signal A updates B updates A) | Infinite loop | Guard: `if new == old: return` in `Signal.set()` |
| Plugin blocks event loop | Async handler timeout | `asyncio.wait_for(handler(cmd), timeout=5.0)` |

---

## 6. Success Metrics for Full Migration

| Metric | Target |
|---|---|
| Terminal corruption incidents | 0 across 10,000 agent turns |
| Lines of code in `tui.py` | <300 (down from ~420) |
| `raw_mode` entry points | Exactly 1 |
| `console.print()` call sites | Exactly 1 (ScrollBufferAppender) |
| `live.start()` / `live.stop()` call sites | Exactly 2 (once each at app lifetime) |
| Test coverage (new runtime) | >90% line coverage |
| Agent turn latency (time to first tool call visible) | <100ms |
| Input latency (keystroke to Live redraw) | <16ms (1 frame at 60fps) |
