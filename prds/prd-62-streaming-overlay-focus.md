# PRD-62 — Streaming Architecture, Overlay System & Focus Management

## 1. Streaming Architecture

### 1.1 Streaming as a First-Class Primitive

Every long-running data source must be representable as an async generator.
The UI updates per-chunk, not per-completion. This eliminates the "flash"
pattern where a turn appears all at once after it finishes.

```python
from typing import AsyncGenerator

# All agent output conforms to this protocol
async def stream_turn(prompt: str) -> AsyncGenerator[dict, None]:
    """
    Yields events:
      {"type": "tool_start",    "tool_use_id": ..., "name": ..., "args": {...}}
      {"type": "tool_complete", "tool_use_id": ..., "success": ..., "duration_ms": ..., "output_lines": [...]}
      {"type": "text_chunk",    "text": "..."}
      {"type": "text_finalized","text": "full sub-turn text"}
      {"type": "tokens",        "input_tokens": ..., "output_tokens": ..., "cost_usd": ...}
    """
    ...
```

### 1.2 Streaming → Conversation Events

```python
async def _run_streaming_turn(
    agent_gen: AsyncGenerator[dict, None],
    conversation: ConversationStore,
    scroll: ScrollBufferAppender,
) -> None:
    """
    Maps agent generator events to ConversationStore mutations.
    All rendering happens via the ScrollBufferAppender subscriber pattern.
    """
    current_text: list[str] = []

    async for event in agent_gen:
        match event.get("type"):
            case "tool_start":
                # Flush any accumulated text before new tool call
                if current_text:
                    text = "".join(current_text).strip()
                    if text:
                        conversation.append_event("text", {"text": text})
                    current_text = []
                conversation.set_tool(event["name"])

            case "tool_complete":
                conversation.clear_tool()
                args_str = _format_args(event.get("args", {}))
                conversation.append_event("tool_complete", {
                    "name":         event["name"],
                    "tool_use_id":  event.get("tool_use_id", ""),
                    "success":      event.get("success", True),
                    "args_str":     f"[dim]({args_str})[/dim]" if args_str else "",
                    "dur_str":      _fmt_ms(event.get("duration_ms")),
                    "output_lines": event.get("output_lines", []),
                })

            case "text_chunk":
                # Accumulate streaming chunks; don't render yet
                current_text.append(event.get("text", ""))
                # Update status bar streaming preview (not scroll buffer)
                conversation.active_tool.set("")   # just show Thinking animation

            case "text_finalized":
                # Full sub-turn text finalized; render to scroll buffer
                text = event.get("text", "").strip()
                if text:
                    current_text = []
                    conversation.append_event("text", {"text": text})

            case "tokens":
                conversation.add_tokens(
                    event.get("input_tokens", 0),
                    event.get("output_tokens", 0),
                    event.get("cost_usd", 0.0),
                )

    # Flush any remaining text
    if current_text:
        text = "".join(current_text).strip()
        if text:
            conversation.append_event("text", {"text": text})
```

### 1.3 Streaming Cancellation

Cancellation is a first-class operation. The agent generator is cancelled
via the `TaskHandle.cancel()` path, which causes `asyncio.CancelledError`
to propagate through the generator. The `AgentRuntime` catches it and
publishes `AgentInterrupted`.

```python
# In AgentRuntime._run_turn():
try:
    async for event in agent_gen:
        await self._handle_stream_event(turn_id, event)
except asyncio.CancelledError:
    await agent_gen.aclose()  # allow generator to clean up
    raise
```

### 1.4 Rendering Streaming Text

During streaming (before `text_finalized`), the LLM is generating text.
This text is NOT printed to the scroll buffer yet (to avoid partial renders).
Instead, the status bar shows "Thinking" animation.

When `text_finalized` arrives, the complete sub-turn text is appended as
one `ConversationEvent`, which the `ScrollBufferAppender` renders atomically.

For very long responses (>30s), a "thinking" indicator may be added
incrementally — but this is a future enhancement.

---

## 2. Input Session: Always-On, Mode-Aware

### 2.1 Single Session Architecture

```python
from enum import Enum, auto

class InputMode(Enum):
    IDLE      = auto()  # Full features: triggers, history, cursor movement
    STREAMING = auto()  # Reduced: queue messages, interrupt, paste


class UnifiedInputSession:
    """Single CBREAK session for the application lifetime.

    Mode transitions:
        IDLE → STREAMING  when agent turn begins
        STREAMING → IDLE  when agent turn ends

    Only ONE raw_mode context, entered at startup and exited at shutdown.
    No nesting, no races.
    """

    def __init__(
        self,
        app_state: AppState,
        command_bus: CommandBus,
        trigger_registry: TriggerRegistry,
        cwd: Path,
    ) -> None:
        self._state   = app_state
        self._bus     = command_bus
        self._reg     = trigger_registry
        self._cwd     = cwd
        self._mode    = InputMode.IDLE
        self._buf     = InputBuffer()
        self._hist    = HistoryNavigator([])
        self._paste   = PasteState()
        self._trigger: _TriggerState | None = None

    def set_mode(self, mode: InputMode) -> None:
        self._mode = mode
        if mode == InputMode.IDLE:
            self._app_state.conversation.agent_state.subscribe(
                lambda: self._on_state_change()
            )

    async def run(self) -> None:
        """Blocking loop. Never returns during normal operation."""
        fd = sys.stdin.fileno()
        with raw_mode(fd):
            while True:
                key, ch = read_key(fd)
                await self._dispatch(key, ch)

    async def _dispatch(self, key: Key, ch: str) -> None:
        if self._mode == InputMode.STREAMING:
            await self._dispatch_streaming(key, ch)
        else:
            await self._dispatch_idle(key, ch)

    async def _dispatch_streaming(self, key: Key, ch: str) -> None:
        """Reduced key set during agent streaming."""
        match key:
            case Key.CTRL_C | Key.ESC:
                await self._bus.dispatch_async(InterruptAgentCommand())
            case Key.ENTER:
                text = self._buf.text.strip()
                self._buf.clear()
                self._push()
                if text:
                    # Queue for dispatch after current turn
                    self._queued.append(text)
                    self._notify_queued(text)
            case Key.PASTE if ch:
                self._paste.apply(self._buf, ch, _get_cols())
                self._push()
            case Key.BACKSPACE:
                if self._paste.condensed:
                    self._paste.backspace(self._buf)
                else:
                    self._buf.delete_before()
                self._push()
            case Key.CTRL_U:
                self._buf.clear()
                self._paste.condensed = False
                self._push()
            case Key.CHAR if ch:
                if self._paste.condensed:
                    self._paste.expand()
                # Check trigger activation
                if self._is_trigger_char(key, ch):
                    await self._handle_trigger(ch)
                else:
                    self._buf.insert(ch)
                self._push()

    async def _dispatch_idle(self, key: Key, ch: str) -> None:
        """Full feature set in idle mode."""
        # ... (existing IdleInputSession logic, now part of unified session)
        pass

    async def _handle_trigger(self, trigger_char: str) -> None:
        """Trigger handling works the same in both modes — no nesting."""
        # Since we're already in raw_mode, we need to handle the trigger
        # within the same context. We temporarily switch rendering to ANSI
        # mode for the picker.
        from agenthicc.tui.input.session import run_input_session
        initial = list(self._buf.buf) + [trigger_char]
        # We can't stop/start the Live block because it's always-on.
        # Instead, we render the picker ABOVE the Live block by
        # temporarily placing the cursor above it.
        # This is handled by the OverlayHost (see §3).
        await self._overlay_host.show_trigger_picker(
            initial_buf=initial,
            registry=self._reg,
            cwd=self._cwd,
            on_complete=self._on_trigger_complete,
        )
```

### 2.2 Mode Transitions

```python
# In AgentRuntime:
async def _run_turn(self, turn_id: str, text: str) -> None:
    self._input_session.set_mode(InputMode.STREAMING)
    try:
        ...
    finally:
        self._input_session.set_mode(InputMode.IDLE)
        # Dispatch any queued messages
        for queued_text in self._input_session.drain_queue():
            await self._bus.dispatch_async(SendMessageCommand(text=queued_text))
```

---

## 3. Overlay System

### 3.1 Overlay Philosophy

Overlays are **transient contextual projections** — they appear above the
workspace without navigating away from it. The workspace remains active
beneath every overlay.

```
Workspace (always rendered)
│
└── OverlayHost (z-layer above workspace)
    │
    └── Active overlay (zero or one at a time):
        ├── TriggerPickerOverlay    (@-mention / /command dropdown)
        ├── CommandPaletteOverlay   (Ctrl+P)
        ├── ConfigMenuOverlay       (/config)
        ├── ConfirmationOverlay     (destructive action confirmations)
        └── DiffViewerOverlay       (expanded tool output)
```

### 3.2 OverlayHost

```python
class OverlayHost:
    """Manages the single active overlay. Part of the Live region."""

    def __init__(self, app_state: AppState) -> None:
        self._state   = app_state
        self._overlay: Overlay | None = None

    @property
    def active(self) -> bool:
        return self._overlay is not None

    def show(self, overlay: "Overlay") -> None:
        self._overlay = overlay
        overlay.on_mount()
        self._state.overlay.set(overlay.name)
        self._state.modal_open.set(True)

    def hide(self) -> None:
        if self._overlay:
            self._overlay.on_unmount()
        self._overlay = None
        self._state.overlay.set("")
        self._state.modal_open.set(False)

    def render(self) -> Any:
        if self._overlay:
            return self._overlay.render()
        return None

    def handle_key(self, key: Key, ch: str) -> bool:
        """Returns True if the overlay consumed the key."""
        if self._overlay:
            return self._overlay.handle_key(key, ch)
        return False

    async def show_trigger_picker(
        self,
        initial_buf: list[str],
        registry: TriggerRegistry,
        cwd: Path,
        on_complete: Callable[[str | None], None],
    ) -> None:
        """Show the trigger picker as an inline overlay (no mode switch needed)."""
        overlay = TriggerPickerOverlay(
            initial_buf=initial_buf,
            registry=registry,
            cwd=cwd,
            on_complete=lambda result: (on_complete(result), self.hide()),
        )
        self.show(overlay)
```

### 3.3 Overlay Contract

```python
class Overlay(ABC):
    name: str

    def on_mount(self) -> None:
        """Called when overlay becomes active."""

    def on_unmount(self) -> None:
        """Called when overlay is dismissed."""

    @abstractmethod
    def render(self) -> Any:
        """Return Rich renderable for the Live region."""

    @abstractmethod
    def handle_key(self, key: Key, ch: str) -> bool:
        """Handle a keystroke. Return True if consumed."""
```

### 3.4 TriggerPickerOverlay

The trigger picker no longer requires pausing/restarting the Live block.
It renders as an overlay WITHIN the Live region:

```python
class TriggerPickerOverlay(Overlay):
    name = "trigger_picker"

    def __init__(self, initial_buf, registry, cwd, on_complete) -> None:
        self._buf      = InputBuffer(initial_buf)
        self._registry = registry
        self._cwd      = cwd
        self._complete = on_complete
        self._trigger  = None
        self._matches  = []
        self._selected = 0
        # Activate trigger immediately from initial_buf
        self._init_trigger()

    def render(self) -> Any:
        from rich.console import Group
        from rich.text import Text
        prompt = build_prompt(self._buf.buf, self._buf.cursor)
        lines = [Text.from_markup(prompt)]
        # Dropdown rows
        for i, item in enumerate(self._matches[:8]):
            style = "reverse" if i == self._selected else ""
            lines.append(Text.from_markup(
                f"  {'▶' if i == self._selected else ' '} {item.display}",
                style=style,
            ))
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        match key:
            case Key.ESC:
                self._complete(None)
            case Key.ENTER | Key.TAB:
                item = self._matches[self._selected] if self._matches else None
                result_buf = self._trigger.handler.on_select(item, self._trigger.fragment, self._buf.buf)
                self._complete("".join(result_buf))
            case Key.UP:
                self._selected = (self._selected - 1) % max(1, len(self._matches))
            case Key.DOWN:
                self._selected = (self._selected + 1) % max(1, len(self._matches))
            case Key.BACKSPACE:
                if self._trigger and self._trigger.fragment:
                    self._trigger.fragment = self._trigger.fragment[:-1]
                    self._update_matches()
                else:
                    self._complete(None)
            case Key.CHAR if ch:
                if self._trigger:
                    self._trigger.fragment += ch
                    self._update_matches()
        return True  # overlay consumes all keys
```

---

## 4. Focus System

### 4.1 FocusManager

Focus in a terminal application is simpler than in GUI — there is one "focused"
region at a time. The focus system makes this explicit and reactive.

```python
class FocusTarget(Enum):
    COMPOSER = auto()    # input bar (default)
    OVERLAY  = auto()    # any active overlay
    NONE     = auto()    # focus suspended (e.g. during agent run where input goes to streaming mode)


class FocusManager:
    """Tracks which region receives keyboard input."""

    def __init__(self, app_state: AppState) -> None:
        self._state  = app_state
        self.current = Signal(FocusTarget.COMPOSER)

    def focus_overlay(self) -> None:
        self.current.set(FocusTarget.OVERLAY)

    def focus_composer(self) -> None:
        self.current.set(FocusTarget.COMPOSER)

    def route_key(
        self,
        key: Key,
        ch: str,
        overlay_host: OverlayHost,
        input_session: UnifiedInputSession,
    ) -> None:
        """Route a keystroke to the focused target."""
        match self.current():
            case FocusTarget.OVERLAY:
                consumed = overlay_host.handle_key(key, ch)
                if not consumed:
                    self.focus_composer()
            case FocusTarget.COMPOSER:
                asyncio.create_task(input_session._dispatch(key, ch))
```

### 4.2 Focus Transitions

```
App startup                → COMPOSER focus
Overlay shown              → OVERLAY focus (modal trap)
Overlay dismissed (Esc)    → COMPOSER focus
Agent starts running       → COMPOSER focus (streaming mode)
Agent completes            → COMPOSER focus (idle mode)
```

Focus is reactive state — components subscribe to `FocusManager.current`
and adjust their visual appearance accordingly.

---

## 5. Resize Handling

```python
class ResizeHandler:
    """Publishes ResizeDetected events and triggers workspace re-render."""

    def __init__(self, event_bus: EventBus, workspace: Workspace) -> None:
        import signal
        signal.signal(signal.SIGWINCH, self._on_resize)
        self._bus       = event_bus
        self._workspace = workspace

    def _on_resize(self, signum: int, frame) -> None:
        import os, asyncio
        try:
            size = os.get_terminal_size()
            event = ResizeDetected(cols=size.columns, rows=size.lines)
            asyncio.get_event_loop().call_soon_threadsafe(
                self._bus.publish, event
            )
            asyncio.get_event_loop().call_soon_threadsafe(
                self._workspace._redraw
            )
        except OSError:
            pass
```

---

## 6. Summary: Three Systems Working Together

```
User types "@" in IDLE mode
    │
    ▼
UnifiedInputSession._dispatch_idle(Key.AT, "")
    │
    ▼
FocusManager.route_key → COMPOSER focused
    │
    ▼
_is_trigger_char → True
    │
    ▼
OverlayHost.show(TriggerPickerOverlay(initial_buf=buf+["@"]))
    │
    ├── FocusManager.focus_overlay()
    ├── AppState.overlay.set("trigger_picker")  → Workspace._redraw()
    │
    ▼
User types "docs/R" → TriggerPickerOverlay.handle_key
    │
    ├── fragment = "docs/R"
    ├── matches = AtMentionTrigger.get_matches("docs/R", ctx)
    ├── AppState.overlay.set("trigger_picker")  → Workspace._redraw()  ← re-renders dropdown
    │
    ▼
User presses Enter → on_complete("@docs/README.md")
    │
    ▼
OverlayHost.hide() → FocusManager.focus_composer()
InputBuffer updated with "@docs/README.md"
InputState signals → Workspace._redraw() → ComposerComponent renders new text
```

No `raw_mode` nesting. No Live block pause/restart. No cursor races.
