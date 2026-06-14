# Textual Framework Architecture Research
## For an AI Coding Agent Terminal Interface
### Hard Constraint: No Alternate Screen — Terminal Scrollback Must Be Preserved

**Research date:** June 2026  
**Textual version range covered:** v0.56 – v8.x  
**Framework:** [Textual by Textualize](https://textual.textualize.io/)

---

## 1. Executive Summary

Textual is a mature, MIT-licensed Python TUI framework built by Textualize (Will McGugan et al.) on top of the Rich library. It targets sophisticated terminal applications with a modern widget model, CSS-based styling, reactive state management, and a full async event loop. As of mid-2025 it is the primary framework behind **Toad** (a production AI coding agent TUI), **Posting** (API client), **Harlequin** (database IDE), **Memray** (Bloomberg's memory profiler), and dozens of other real-world tools.

**For the agenthicc use case the decisive question is the alternate-screen constraint.** Textual's default mode (`app.run()`) uses the terminal's alternate screen buffer — the same mechanism used by `vim`, `htop`, and `less`. That completely blocks scrollback. Textual's **inline mode** (`app.run(inline=True)`) renders below the shell prompt inside the normal scrollback buffer and is the correct mode for this project.

### Key findings

| Dimension | Assessment |
|---|---|
| Inline mode maturity | Production-grade since v0.56 (April 2024); used by Toad in production |
| Streaming text for LLMs | Excellent: `Markdown.append()`, `MarkdownStream`, `RichLog.write()`, `Widget.anchor()` |
| Reactive state | First-class with `reactive()`, watch methods, compute methods, data binding |
| CSS layout | Full CSS-like system: grid, dock, layers, fr units, pseudo-classes |
| Async / threading | `@work` decorator, `call_from_thread()`, `post_message()` (thread-safe) |
| Performance | Spatial-map compositor; Rust speedups (`textual-speedups` package); batched rendering |
| Windows support | Inline mode **not supported on Windows** (works on WSL) |
| Testing | `run_test()`, `Pilot` API, `pytest-textual-snapshot` |
| Rich integration | Deep: any Rich renderable usable in widgets; `Syntax`, `Table`, `Text` all work |

**Recommendation:** Use Textual with `run(inline=True)` as the TUI layer for agenthicc. The framework has every capability needed: streaming Markdown, anchor-based auto-scroll, reactive state, async workers, and a robust widget ecosystem. The primary risks are the inline mode rendering complexity on edge-case terminals and the Windows limitation.

---

## 2. Inline Mode — Deep Dive

### 2.1 What inline mode is

Standard Textual apps call `app.run()`, which:
1. Sends the terminal escape sequence `\x1b[?1049h` to switch to the alternate screen buffer
2. Hides the cursor, takes over the full terminal area
3. On exit sends `\x1b[?1049l` to restore the original buffer

This is **incompatible with scrollback preservation** because alternate screen mode is a separate buffer that the terminal does not scroll.

Inline mode instead:
1. Renders directly into the **primary screen buffer** below the current cursor position
2. Uses cursor-repositioning escape codes to overwrite previous frames in-place
3. Terminates each line with `\n` except the last line (to avoid spurious blank lines)
4. Repositions the cursor upward using ANSI codes between frames

```python
# Activating inline mode
app.run(inline=True)

# With no-clear variant (keeps output on exit rather than erasing it)
app.run(inline=True, inline_no_clear=True)
```

### 2.2 How the rendering loop works

The inline rendering pipeline (from the [Behind the Curtain blog post](https://textual.textualize.io/blog/2024/04/20/behind-the-curtain-of-inline-terminal-applications/)):

1. **Cursor position query** — On startup, the app sends `\x1b[6n` (DSR) to ask the terminal for the current cursor row/column. The terminal responds with `\x1b[<row>;<col>R`. This anchor position is stored.
2. **First render** — Output is written from the anchor position downward. Lines use `\n` to advance, except the final line.
3. **Subsequent frames** — The app calculates how many lines tall the previous frame was and moves the cursor back up that many rows using `\x1b[<n>A`. Then it overwrites completely (no partial clears needed unless the new frame is shorter).
4. **Shrinking frame** — If the new frame has fewer lines than the previous, the remaining old lines are cleared with `\x1b[2K\x1b[1B` (clear line, move down) for each surplus line.
5. **Mouse coordinate translation** — The terminal's mouse events report coordinates relative to the top-left of the full terminal. The app subtracts the stored anchor row to translate into widget-local coordinates.
6. **Growing frame** — The app can grow taller as content is added, anchored at the top (the original cursor position) and extending downward.

### 2.3 Height configuration

```python
# In TCSS (Textual CSS)
Screen {
    height: 40;        # Fixed 40 lines
}

# Or dynamic
Screen {
    height: 50vh;      # 50% of terminal height
}

# Inline-mode-specific CSS via pseudo-selector
:inline Screen {
    height: 30;
    border: none;
}
```

The `INLINE_PADDING` class variable controls blank lines above the app:

```python
class MyApp(App):
    INLINE_PADDING = 0   # Default is 1 (one blank line above app)
```

### 2.4 Known inline mode limitations and issues

| Issue | Status | Workaround |
|---|---|---|
| **Windows not supported** | Unfixed (as of 2025) | Use WSL |
| **Laggy interactive widgets** (v0.56.2) | Fixed in later releases | Upgrade to v1.0+ |
| **Command palette UX** (v0.55.1) | Fixed via PR #4393, #4401 | Upgrade |
| **Scrollback duplication on resize** | Fixed in v2.1.116 | Upgrade; some edge cases remain in VSCode terminal |
| **`inline_no_clear` garbled output** | Fixed in v6.0.0 | Upgrade |
| **Mouse origin translation complexity** | Inherent to inline mode | No workaround needed; handled by framework |
| **Terminal compatibility** | Requires ANSI escape code support | Most modern terminals work; legacy terminals may not |

### 2.5 `inline_no_clear` mode

When `inline_no_clear=True`, the app does NOT erase its rendered output when it exits. This leaves the final frame visible in the terminal scrollback — useful for read-only output scenarios like displaying a result and returning control to the shell.

```python
# The app renders, user interacts, then on exit the content stays in scrollback
app.run(inline=True, inline_no_clear=True)
```

### 2.6 Sizing and resize behavior

Inline apps resize when the terminal resizes (SIGWINCH). The spatial map is invalidated and recalculated. As of v8.2.2–v8.2.3, resize handling was moved from idle-loop processing to timer-based, reducing lag on rapid resize events. The app can also be configured with a fixed height to avoid growing on resize.

### 2.7 Practical inline mode checklist

- Always test in the actual target terminals (iTerm2, Ghostty, Linux xterm, VSCode integrated terminal)
- Set `INLINE_PADDING = 0` to eliminate the default blank-line separator above the app
- Use `:inline` CSS pseudo-selector for mode-specific styling
- Avoid `height: auto` on the root Screen — set an explicit height or `vh`-based value
- For chat/streaming UIs, use `Widget.anchor()` on the scroll container — see section 6

---

## 3. Reactive State Management

### 3.1 The `reactive()` descriptor

Textual reactive attributes are Python descriptors with automatic side-effects:

```python
from textual.reactive import reactive, var

class ChatWidget(Widget):
    # Triggers re-render on change
    message_count = reactive(0)
    
    # Triggers full layout recalculation on change
    panel_width = reactive(40, layout=True)
    
    # Does NOT trigger render (just stores value reactively)
    internal_flag = var(False)
    
    # Callable default — called to generate each instance's default
    created_at = reactive(time.time)
```

### 3.2 The reactive superpower chain

When a reactive attribute changes, Textual runs in this order:

1. `compute_<attr>()` — recalculates derived values
2. `validate_<attr>(value)` — constrains the value, returns canonical form
3. `watch_<attr>(old, new)` — executes side effects
4. Schedule re-render (unless `var()` was used)

```python
class ProgressWidget(Widget):
    progress = reactive(0.0)
    status = reactive("idle")
    
    # Compute: derived from other reactives
    color = reactive(Color.parse("grey"))
    
    def compute_color(self) -> Color:
        if self.progress >= 1.0:
            return Color.parse("green")
        return Color.parse("yellow")
    
    # Validate: clamp to 0..1
    def validate_progress(self, value: float) -> float:
        return max(0.0, min(1.0, value))
    
    # Watch: side effects
    def watch_progress(self, old: float, new: float) -> None:
        if new >= 1.0:
            self.status = "complete"
```

### 3.3 Watch methods can be async

```python
async def watch_user_input(self, value: str) -> None:
    await self.trigger_search(value)
```

### 3.4 Data binding (parent → child)

Unidirectional binding from a parent reactive to a child widget attribute:

```python
class AppRoot(App):
    theme_name = reactive("dark")
    
    def compose(self) -> ComposeResult:
        yield Sidebar().data_bind(AppRoot.theme_name)
        yield MainPanel().data_bind(theme=AppRoot.theme_name)
```

### 3.5 Mutable reactive collections

Lists, dicts, and sets are not auto-detected by Python's descriptor protocol when mutated in place. Must call `mutate_reactive()` after in-place mutation:

```python
self.conversation_history.append(message)
self.mutate_reactive(MyChatApp.conversation_history)
```

### 3.6 Avoiding watcher invocation during `__init__`

```python
def __init__(self, initial_count: int = 0) -> None:
    super().__init__()
    self.set_reactive(MyWidget.message_count, initial_count)  # No watch triggered
```

### 3.7 `recompose=True` — full subtree refresh

When a reactive changes, rebuild the entire `compose()` subtree:

```python
messages = reactive([], recompose=True)
```

**Warning:** Any state stored on recomposed child widgets is lost. Query fresh references after recompose. This is expensive for large widget trees — prefer targeted mutations instead.

### 3.8 Dynamic watchers

Watch another widget's reactive attribute programmatically:

```python
def on_mount(self) -> None:
    input_widget = self.query_one(Input)
    self.watch(input_widget, "value", self._on_input_changed)
```

---

## 4. Layout System

### 4.1 Core layout types

Textual CSS supports three primary layout modes:

**Vertical** (default for most containers):
```css
.chat-panel {
    layout: vertical;
    overflow-y: auto;
}
```

**Horizontal**:
```css
.toolbar {
    layout: horizontal;
    height: 3;
}
```

**Grid**:
```css
.grid-container {
    layout: grid;
    grid-size: 3 2;           /* 3 columns, 2 rows */
    grid-columns: 2fr 1fr 1fr;
    grid-rows: 1fr 3fr;
    grid-gutter: 1 2;         /* vertical horizontal (2x horizontal because cells are 2x taller than wide) */
}
```

### 4.2 Docking — fixed edges

Docking removes a widget from normal flow and pins it to an edge:

```css
Header {
    dock: top;
    height: 3;
}

Footer {
    dock: bottom;
    height: 1;
}

Sidebar {
    dock: left;
    width: 30;
}
```

Docked widgets do not scroll with content and always remain visible. Multiple widgets docked to the same edge overlap in yield order (last yielded = topmost).

### 4.3 Layers — z-order control

Define a layer stack on a container; assign widgets to layers:

```css
#main-container {
    layers: base overlay modal;
}

.background-widget {
    layer: base;
}

.dropdown-menu {
    layer: overlay;
}

.dialog {
    layer: modal;
}
```

### 4.4 Fractional units and sizing

```css
/* fr units split available space proportionally */
.left-panel  { width: 1fr; }
.right-panel { width: 2fr; }   /* right gets twice the space */

/* Percentages relative to parent */
.full-width  { width: 100%; }
.half-height { height: 50%; }

/* auto — calculate from content */
.auto-sized  { height: auto; }

/* Viewport-relative */
:inline Screen { height: 40vh; }
```

### 4.5 Container widgets

Textual provides composable utility containers:

```python
from textual.containers import (
    Vertical, Horizontal, Grid,
    VerticalScroll, HorizontalScroll,
    ScrollableContainer, Container,
)

def compose(self) -> ComposeResult:
    with Horizontal():
        with VerticalScroll(id="sidebar"):
            yield FileTree()
        with Vertical(id="main"):
            with VerticalScroll(id="transcript"):
                yield TranscriptView()
            yield InputBar()
```

### 4.6 Overflow and scrolling

```css
.transcript-panel {
    overflow-y: auto;     /* vertical scrollbar when needed */
    overflow-x: hidden;   /* no horizontal scroll */
}

.wide-table {
    overflow-x: scroll;   /* always show horizontal scrollbar */
}
```

### 4.7 Recommended layout for an AI coding agent TUI

```
┌──────────────────────────────────────────────────┐
│ Header (dock: top, height: 1)                    │
├────────────┬─────────────────────────────────────┤
│            │ TranscriptScroll (overflow-y: auto) │
│  Sidebar   │   ConversationTurn (repeating)      │
│  (dock:    │     MarkdownWidget (LLM output)     │
│   left,    │     ToolCallWidget                  │
│   width:   ├─────────────────────────────────────┤
│   30)      │ StatusBar (height: 1)               │
│            ├─────────────────────────────────────┤
│            │ InputArea (height: auto, max: 10)   │
└────────────┴─────────────────────────────────────┘
```

CSS approximation:

```css
Screen {
    layout: horizontal;
}

#sidebar {
    dock: left;
    width: 30;
    display: none;  /* toggle with toggle_class() */
}

#main {
    layout: vertical;
    width: 1fr;
}

#transcript {
    height: 1fr;
    overflow-y: auto;
}

#status-bar {
    height: 1;
    dock: bottom;
}

#input-area {
    height: auto;
    max-height: 10;
}
```

---

## 5. Widget Architecture

### 5.1 Widget base class

Every Textual UI element is a `Widget`. Widgets are:
- Responsible for a rectangular region of the screen
- DOM nodes in a tree (parent → children)
- Each run by their own asyncio message queue task
- Styleable via Textual CSS
- Composable (can contain child widgets)

```python
from textual.widget import Widget
from textual.app import RenderResult

class MyWidget(Widget):
    DEFAULT_CSS = """
    MyWidget {
        border: round $accent;
        padding: 1 2;
    }
    """
    
    def render(self) -> RenderResult:
        return "Hello, [bold]World[/bold]!"
    
    def on_mount(self) -> None:
        self.set_interval(1.0, self.refresh)
```

### 5.2 Built-in widget catalogue

**Input widgets:**
- `Button` — clickable, semantic variants (primary, success, warning, error)
- `Input` — single-line text field with validation and masking
- `MaskedInput` — constrained text entry with template pattern
- `TextArea` — multi-line editor with syntax highlighting, undo/redo, Kitty key protocol
- `Checkbox`, `RadioButton`, `RadioSet`
- `Select` — dropdown
- `Switch` — toggle

**Display widgets:**
- `Label` — static text
- `Static` — content display with `update()` method; base class for custom display widgets
- `Header` / `Footer` — app chrome
- `Rule` — horizontal separator
- `Digits` — large numeric display
- `Markdown` — full Markdown renderer (tables, code fences with syntax highlighting, links)
- `MarkdownViewer` — Markdown + table of contents + navigation
- `RichLog` — scrolling rich text log (appends only, no full replace needed)
- `Log` — simpler text-only log

**Structural widgets:**
- `Collapsible` — expandable sections
- `ContentSwitcher` — show one of N children
- `Tabs` / `TabbedContent`
- `DataTable` — powerful configurable grid with cursors, sorting

**Specialized:**
- `Tree` / `DirectoryTree`
- `ProgressBar`, `LoadingIndicator`, `Sparkline`
- `Pretty` — pretty-printed Rich renderables
- `Link` — clickable URL

### 5.3 Custom widget patterns

**Simple render widget:**
```python
class StatusWidget(Widget):
    status = reactive("idle")
    
    def render(self) -> RenderResult:
        color = {"idle": "grey", "running": "green", "error": "red"}[self.status]
        return f"[{color}]● {self.status}[/]"
```

**Composable container widget:**
```python
class ConversationTurn(Widget):
    DEFAULT_CSS = """
    ConversationTurn {
        height: auto;
        margin-bottom: 1;
    }
    """
    
    def __init__(self, role: str, content: str = "") -> None:
        super().__init__()
        self.role = role
        self._content = content
    
    def compose(self) -> ComposeResult:
        yield Label(f"[bold]{self.role}[/bold]", classes="role-label")
        yield Markdown(self._content, id="content")
    
    async def stream_token(self, token: str) -> None:
        """Called from worker to append LLM tokens."""
        await self.query_one("#content", Markdown).append(token)
```

**Line API widget (high-performance, large content):**
```python
from textual.strip import Strip
from textual.geometry import Region

class VirtualListWidget(Widget):
    """Efficient rendering of thousands of items via line API."""
    
    def render_line(self, y: int) -> Strip:
        scroll_y = self.scroll_offset.y
        line_index = scroll_y + y
        if line_index >= len(self._items):
            return Strip.blank(self.size.width)
        return self._render_item(self._items[line_index])
```

### 5.4 DOM querying

```python
# Query by type
log = self.query_one(RichLog)

# Query by CSS selector
panel = self.query_one("#transcript-panel")

# Query multiple
all_turns = self.query(".conversation-turn")

# Query with type assertion
md = self.query_one("#response", Markdown)

# Query from app level
self.app.query_one("#status-bar").update("Thinking...")
```

### 5.5 Widget lifecycle

```python
class MyWidget(Widget):
    def on_mount(self) -> None:
        """Widget is in the DOM and sized."""
        self.set_interval(0.5, self._tick)
    
    def on_unmount(self) -> None:
        """Widget removed from DOM — cleanup."""
        pass
    
    def on_show(self) -> None:
        """Widget becomes visible."""
        pass
    
    def on_hide(self) -> None:
        """Widget hidden."""
        pass
    
    def on_resize(self, event: Resize) -> None:
        """Terminal or widget size changed."""
        pass
```

---

## 6. Async & Background Tasks

### 6.1 The `@work` decorator

Workers are the primary mechanism for background computation without blocking the UI:

```python
from textual.worker import Worker, WorkerState
from textual import work

class ChatApp(App):
    @work(exclusive=True, thread=False)  # async worker (default)
    async def send_message(self, prompt: str) -> None:
        """Runs in async context, can await I/O."""
        turn = ConversationTurn(role="assistant")
        await self.query_one("#transcript").mount(turn)
        
        stream = self.query_one("#transcript", Markdown).get_stream()
        async for chunk in llm_client.stream(prompt):
            await stream.write(chunk)
    
    @work(thread=True)  # thread worker for blocking code
    def run_subprocess(self, cmd: str) -> str:
        """Runs in thread pool for blocking subprocess calls."""
        result = subprocess.run(cmd, capture_output=True, text=True)
        # Thread-safe UI update:
        self.call_from_thread(self._update_output, result.stdout)
        return result.stdout
```

### 6.2 Worker configuration

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `exclusive` | bool | False | Cancel previous worker with same name before starting |
| `thread` | bool | False | Run in thread pool instead of async event loop |
| `exit_on_error` | bool | True | Crash app on unhandled exception |
| `name` | str | auto | Worker name for management |
| `group` | str | "default" | Group for bulk cancellation |
| `description` | str | "" | Human-readable description |

### 6.3 Worker state and lifecycle

```python
def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
    worker = event.worker
    match worker.state:
        case WorkerState.RUNNING:
            self.loading = True
        case WorkerState.SUCCESS:
            self.loading = False
        case WorkerState.ERROR:
            self.loading = False
            self.notify(str(worker.error), severity="error")
        case WorkerState.CANCELLED:
            self.loading = False
```

### 6.4 Thread-to-UI communication

```python
@work(thread=True)
def long_blocking_task(self) -> None:
    for item in process_items():
        if self.is_cancelled:  # check before each update
            return
        # Safe UI update from thread:
        self.call_from_thread(self._append_result, item)
    
    # post_message is also thread-safe:
    self.post_message(TaskComplete(result=results))
```

### 6.5 Cancellation

```python
# Cancel all workers in a group
await self.workers.cancel_group(self, "llm-requests")

# Cancel a specific worker
worker.cancel()

# Cancel all workers
await self.workers.cancel_all()
```

### 6.6 Scheduling timers

```python
def on_mount(self) -> None:
    # One-shot timer
    self.set_timer(2.0, self.hide_notification)
    
    # Repeating interval
    self.set_interval(5.0, self.poll_status)
```

### 6.7 `batch_update()` for atomic visual transitions

```python
async def _replace_content(self) -> None:
    with self.app.batch_update():
        await self.remove_children()
        await self.mount(NewWidget())
```

### 6.8 Streaming to Markdown — the complete pattern

This is the primary pattern for LLM token streaming in agenthicc:

```python
class ChatApp(App):
    @work(exclusive=True)
    async def handle_prompt(self, prompt: str) -> None:
        # 1. Create a new turn widget
        turn = AssistantTurn()
        await self.query_one("#transcript").mount(turn)
        
        # 2. Anchor the scroll container so it follows new content
        #    but releases if user scrolls up
        self.query_one("#transcript").anchor()
        
        # 3. Get a MarkdownStream for efficient buffered updates
        md_widget = turn.query_one(Markdown)
        stream = md_widget.get_stream()
        
        # 4. Stream tokens from LLM
        async for token in llm_api.stream(prompt):
            await stream.write(token)
        
        # 5. Flush remaining buffer
        await stream.finish()
```

---

## 7. Event & Message System

### 7.1 Architecture

Every `App` and `Widget` has its own asyncio message queue processed by a dedicated task. Events bubble up the DOM tree by default (when `bubble = True`). This means:

- A key press event on a focussed `Input` is first handled by that `Input`
- If not consumed, it bubbles to the parent, then grandparent, up to the `App`

### 7.2 Event handler naming convention

```python
class MyApp(App):
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handles ALL Button.Pressed events anywhere in the DOM."""
        pass
    
    def on_input_changed(self, event: Input.Changed) -> None:
        """Handles Input.Changed events."""
        pass
```

Nested widget messages use `<WidgetClassName>_<MessageName>` snake-cased:
- `Button.Pressed` → `on_button_pressed`
- `DataTable.RowSelected` → `on_data_table_row_selected`

### 7.3 The `@on()` decorator — selector-based routing

```python
from textual import on

class MyApp(App):
    @on(Button.Pressed, "#submit-btn")
    def handle_submit(self, event: Button.Pressed) -> None:
        """Only fires for the button with id='submit-btn'."""
        pass
    
    @on(Button.Pressed, ".action-button")
    def handle_any_action_button(self, event: Button.Pressed) -> None:
        pass
```

Multiple handlers for the same event run in decorator order before the naming-convention handler.

### 7.4 Custom messages

Define custom messages as nested classes on the widget that sends them:

```python
class ToolCallWidget(Widget):
    class Completed(Message):
        """Posted when a tool call finishes."""
        def __init__(self, result: str, tool_name: str) -> None:
            self.result = result
            self.tool_name = tool_name
            super().__init__()
    
    class Failed(Message):
        def __init__(self, error: str) -> None:
            self.error = error
            super().__init__()
    
    def complete(self, result: str) -> None:
        self.post_message(self.Completed(result, self._tool_name))
```

The parent then handles:
```python
def on_tool_call_widget_completed(self, event: ToolCallWidget.Completed) -> None:
    self.log(f"Tool {event.tool_name} completed: {event.result}")
```

### 7.5 Stopping propagation

```python
def on_key(self, event: Key) -> None:
    if event.key == "ctrl+c":
        event.stop()          # Stop bubbling entirely
        self._cancel_task()

def on_key(self, event: Key) -> None:
    event.prevent_default()   # Prevent base class handling, but still bubble
```

### 7.6 Suppressing messages programmatically

```python
# Prevent feedback loops when setting values programmatically
with self.query_one(Input).prevent(Input.Changed):
    self.query_one(Input).value = ""
```

### 7.7 Async event handlers

```python
async def on_button_pressed(self, event: Button.Pressed) -> None:
    # Long async work here blocks the widget's message queue
    # until this returns. Use workers for long operations:
    self.run_worker(self.process_request(), exclusive=True)
```

**Critical:** async event handlers block the widget's message queue for their duration. Long-running handlers must spawn workers or use `asyncio.create_task()`.

### 7.8 `call_after_refresh()`

Schedule a callback to run after the next render cycle completes:

```python
def on_mount(self) -> None:
    self.call_after_refresh(self._post_mount_setup)
```

---

## 8. Rich Integration

Textual is built on top of Rich and exposes full Rich integration throughout.

### 8.1 Rich renderables in widgets

Any Rich renderable can be returned from `Widget.render()`:

```python
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.panel import Panel

class CodeWidget(Widget):
    def render(self) -> RenderResult:
        return Syntax(
            self._code,
            self._language,
            theme="monokai",
            line_numbers=True,
            word_wrap=False,
        )

class TableWidget(Widget):
    def render(self) -> RenderResult:
        table = Table("Name", "Status", "Duration")
        for row in self._data:
            table.add_row(*row)
        return table
```

### 8.2 `RichLog` widget

The primary widget for streaming rich-formatted output:

```python
from textual.widgets import RichLog
from rich.syntax import Syntax

class OutputPanel(Widget):
    def compose(self) -> ComposeResult:
        yield RichLog(
            highlight=True,      # auto-highlight with ReprHighlighter
            markup=True,         # enable Rich markup tags
            wrap=True,           # word wrap
            max_lines=5000,      # cap memory usage
            auto_scroll=True,    # scroll to bottom on new content
        )
    
    def write_code(self, code: str, language: str = "python") -> None:
        log = self.query_one(RichLog)
        log.write(Syntax(code, language, theme="monokai"))
    
    def write_text(self, text: str) -> None:
        log = self.query_one(RichLog)
        log.write(text, markup=True)
```

**Scrollback behavior note:** `RichLog` has an issue (GitHub #6311) where auto-scroll behavior differs from `Log`. When `auto_scroll=True`, writing new content scrolls to the bottom. If the user scrolls up, the next `write()` call may jump them back to the bottom. Work around this by checking `is_vertical_scroll_end` before deciding whether to auto-scroll, or use the `scroll_end=False` parameter on individual `write()` calls.

### 8.3 `Markdown` widget with streaming

The preferred widget for LLM output:

```python
from textual.widgets import Markdown

class AssistantResponse(Widget):
    def compose(self) -> ComposeResult:
        yield Markdown("")
    
    async def stream(self, token_iterator) -> None:
        md = self.query_one(Markdown)
        stream = md.get_stream()  # Returns MarkdownStream
        async for token in token_iterator:
            await stream.write(token)
        await stream.finish()
```

`MarkdownStream` (`Markdown.get_stream()`) internally batches updates when tokens arrive faster than the display refresh rate, preventing UI lag at high token throughput.

### 8.4 Markup syntax

Textual uses Rich markup inline in strings:

```python
# In render() return values, Label text, notification messages, etc.
"[bold green]Success[/] — task completed in [cyan]3.2s[/cyan]"
"[red on white]Error:[/] file not found"
"[$accent]Use color variables from your theme[/]"
"[@click=app.bell]Click me[/]"       # Action link
"[link=https://example.com]URL[/]"   # Hyperlink
```

### 8.5 Content class (v6+)

The `Content` class provides programmatic rich text construction safer than raw markup strings:

```python
from textual.content import Content

# Safe variable substitution (user input with brackets won't break markup)
username = user_input  # may contain [ or ]
content = Content.from_markup("Hello $name!", name=username)

# Apply style to a range
content = content.stylize("bold", 0, 5)

widget.update(content)
```

### 8.6 `textual-speedups` — Rust acceleration

Install the optional Rust acceleration package:

```bash
pip install textual-speedups
```

This replaces `geometry.py` classes (`Offset`, `Size`, `Region`, `Spacing`) with Rust implementations. Textual auto-detects and uses them. Several orders of magnitude faster than pure-Python for layout computations. Disable with `TEXTUAL_SPEEDUPS=0`.

---

## 9. CSS Architecture Best Practices

### 9.1 File organization

```
src/agenthicc/tui/
  styles/
    app.tcss          # Root app styles, CSS variables, themes
    layout.tcss       # Layout-only rules (dock, grid, size)
    widgets.tcss      # Widget-specific component styles
    inline.tcss       # :inline pseudo-selector rules
```

Reference multiple files:

```python
class MyApp(App):
    CSS_PATH = [
        "styles/app.tcss",
        "styles/layout.tcss",
        "styles/widgets.tcss",
        "styles/inline.tcss",
    ]
```

### 9.2 CSS variables (design tokens)

```css
/* app.tcss */
$brand-primary: #6c5ce7;
$brand-secondary: #a29bfe;
$surface: $panel;
$chat-user-bg: $primary-muted;
$chat-assistant-bg: $surface;
$code-bg: $boost;

/* Use in rules */
.user-turn {
    background: $chat-user-bg;
}
```

Textual also exposes built-in theme variables: `$primary`, `$secondary`, `$accent`, `$warning`, `$error`, `$success`, `$surface`, `$panel`, `$boost`, `$background`.

### 9.3 Specificity order (low → high)

1. Type selectors: `Button {}`
2. Class selectors: `.action {}`
3. Pseudo-classes: `Button:hover {}`
4. ID selectors: `#submit {}`
5. Inline `!important` (avoid)

Widget `DEFAULT_CSS` has lower specificity than app CSS, allowing overrides.

### 9.4 Inline mode CSS isolation

```css
/* Normal mode */
Screen {
    height: 100%;
    border: none;
}

/* Inline mode overrides */
:inline Screen {
    height: 40;
    border: none;
    padding: 0;
}

:inline Header {
    display: none;  /* hide chrome in inline mode */
}
```

### 9.5 Dark/light theme detection

```css
/* Applies in dark themes (default) */
:dark .code-block {
    background: #1e1e1e;
}

/* Applies in light themes */
:light .code-block {
    background: #f5f5f5;
}
```

### 9.6 Nesting CSS

```css
/* Nested rules — ampersand refers to parent selector */
.conversation-turn {
    height: auto;
    padding: 1;
    
    &.user {
        background: $primary-muted;
        border-left: thick $primary;
    }
    
    &.assistant {
        background: $surface;
        border-left: thick $accent;
    }
    
    .role-label {
        color: $text-muted;
        text-style: bold;
    }
}
```

### 9.7 Scoped widget CSS via `DEFAULT_CSS`

Bundle styles with a widget class:

```python
class ToolCallWidget(Widget):
    DEFAULT_CSS = """
    ToolCallWidget {
        height: auto;
        border: round $panel;
        margin: 0 0 1 0;
        
        .tool-name {
            color: $accent;
            text-style: bold;
        }
        
        .tool-args {
            color: $text-muted;
        }
        
        &.running {
            border: round $warning;
        }
        
        &.complete {
            border: round $success;
        }
        
        &.error {
            border: round $error;
        }
    }
    """
```

---

## 10. Large Application Organization

### 10.1 Recommended directory structure

```
src/agenthicc/tui/
  __init__.py
  app.py                # Main App class (thin orchestrator)
  
  screens/
    __init__.py
    main_screen.py      # Primary interaction screen
    settings_screen.py  # Settings modal/screen
    history_screen.py   # Session history browser
  
  widgets/
    __init__.py
    transcript.py       # Conversation transcript widget
    input_bar.py        # Multi-line input with @ mentions, / commands
    tool_call.py        # Tool call display widget
    conversation_turn.py  # Single user/assistant exchange
    sidebar.py          # File/context sidebar
    status_bar.py       # Status line widget
    code_block.py       # Syntax-highlighted code block
    diff_viewer.py      # Unified diff display
  
  styles/
    app.tcss
    layout.tcss
    widgets.tcss
    inline.tcss
  
  messages.py           # Custom Message classes shared across widgets
  actions.py            # App-level action handlers
```

### 10.2 Screen-based organization

```python
# app.py
class AgenthiccTUI(App):
    CSS_PATH = ["styles/app.tcss", "styles/layout.tcss"]
    
    SCREENS = {
        "main": MainScreen,
        "settings": SettingsScreen,
        "history": HistoryScreen,
    }
    
    BINDINGS = [
        ("ctrl+comma", "push_screen('settings')", "Settings"),
        ("ctrl+h", "push_screen('history')", "History"),
        ("escape", "pop_screen", "Back"),
    ]
    
    def on_mount(self) -> None:
        self.push_screen("main")
```

### 10.3 Modal screens for transient UI

```python
from textual.screen import ModalScreen

class ConfirmDialog(ModalScreen[bool]):
    """Returns True if user confirms, False otherwise."""
    
    DEFAULT_CSS = """
    ConfirmDialog {
        align: center middle;
    }
    #dialog {
        width: 50;
        height: 11;
        border: thick $background 80%;
        background: $surface;
    }
    """
    
    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt
    
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._prompt)
            with Horizontal():
                yield Button("Yes", id="yes", variant="success")
                yield Button("No", id="no", variant="error")
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

# Usage:
async def maybe_cancel(self) -> None:
    confirmed = await self.push_screen_wait(ConfirmDialog("Cancel current task?"))
    if confirmed:
        await self.cancel_current_task()
```

### 10.4 Modes — independent screen stacks

For multi-mode apps (chat, files, settings) that each have their own navigation history:

```python
class AgenthiccTUI(App):
    MODES = {
        "chat": ChatScreen,
        "files": FilesBrowserScreen,
        "settings": SettingsScreen,
    }
    DEFAULT_MODE = "chat"
    
    BINDINGS = [
        ("ctrl+1", "switch_mode('chat')", "Chat"),
        ("ctrl+2", "switch_mode('files')", "Files"),
        ("ctrl+3", "switch_mode('settings')", "Settings"),
    ]
```

### 10.5 Shared message bus pattern

Define app-level messages in a central `messages.py`:

```python
# messages.py
from textual.message import Message

class NewAgentToken(Message):
    """LLM produced a new token."""
    def __init__(self, turn_id: str, token: str) -> None:
        self.turn_id = turn_id
        self.token = token
        super().__init__()

class TaskStarted(Message):
    def __init__(self, task_id: str, tool_name: str) -> None:
        self.task_id = task_id
        self.tool_name = tool_name
        super().__init__()

class TaskCompleted(Message):
    def __init__(self, task_id: str, result: str, error: str | None = None) -> None:
        self.task_id = task_id
        self.result = result
        self.error = error
        super().__init__()
```

Post from anywhere; the App handles routing:

```python
# In App:
def on_new_agent_token(self, msg: NewAgentToken) -> None:
    turn_widget = self.query_one(f"#turn-{msg.turn_id}", ConversationTurn)
    self.run_worker(turn_widget.stream_token(msg.token))
```

### 10.6 Compound widget pattern

Build reusable compound widgets with internal event handling:

```python
class ToolCallWidget(Widget):
    """Self-contained widget that manages tool execution display."""
    
    class Completed(Message):
        def __init__(self, result: str) -> None:
            self.result = result
            super().__init__()
    
    def __init__(self, tool_name: str, args: dict) -> None:
        super().__init__(classes="tool-call pending")
        self._tool_name = tool_name
        self._args = args
    
    def compose(self) -> ComposeResult:
        yield Label(f"[bold]⚙ {self._tool_name}[/bold]", classes="tool-name")
        yield Pretty(self._args, classes="tool-args")
        yield RichLog(id="output", max_lines=200)
        yield LoadingIndicator(id="spinner")
    
    async def run_tool(self, executor) -> None:
        self.add_class("running")
        self.remove_class("pending")
        try:
            async for output in executor.stream(self._tool_name, self._args):
                self.query_one("#output", RichLog).write(output)
            self.add_class("complete")
            self.post_message(self.Completed(result="done"))
        except Exception as e:
            self.add_class("error")
        finally:
            self.remove_class("running")
            self.query_one("#spinner").display = False
```

---

## 11. Performance Considerations

### 11.1 The compositor algorithm

Textual's rendering pipeline operates on segments (text + style pairs), not individual characters. The compositor:

1. Finds all cut-points where segment lists begin/end across overlapping widgets
2. Clips segments at those cuts
3. Discards occluded (invisible) segments
4. Combines visible segments into one output

This handles variable-width characters (CJK, emoji) correctly and enables **partial region updates** — only the changed region of the screen is redrawn. A button changing color does not repaint the entire terminal.

### 11.2 Spatial map for large widget counts

For apps with hundreds or thousands of widgets, Textual uses a grid-based spatial map:
- Widgets are indexed in a 100×20 tile grid
- Visibility checks only inspect widgets whose tiles overlap the query region
- Performance is **O(1)** regardless of widget count — scrolling 8 or 8000 widgets costs the same
- Map is rebuilt only when widget positions change (not on scroll)

### 11.3 Rendering performance knobs

**`@lru_cache` on hot paths:**
```python
from functools import lru_cache

@lru_cache(maxsize=1000)
def _render_segment(self, content: str, style: str) -> Strip:
    ...
```

**`batch_update()` for atomic changes:**
```python
async def refresh_all(self) -> None:
    with self.app.batch_update():
        for widget in self.query(".outdated"):
            widget.refresh()
```

**`layout=False` on reactive when layout doesn't change:**
```python
# Bad: triggers full layout recalculation
status = reactive("idle", layout=True)

# Good: only redraws content
status = reactive("idle")  # layout=False is the default
```

**`max_lines` on RichLog and Log:**
```python
RichLog(max_lines=5000)  # Prevent unbounded memory growth
```

### 11.4 Streaming text performance

For high-throughput LLM streaming:

**Use `Markdown.get_stream()`** for Markdown output:
- Internally buffers tokens
- Batch-updates the widget at display refresh rate (60fps)
- Re-parses only from the last incomplete block (sub-1ms)
- Handles table streaming, code fence accumulation, paragraph updates

**Use `RichLog.write()` for raw log output:**
- Simple append model
- Each write is O(1) (appends to lines list)
- Scrollback is efficient via virtual rendering

**Avoid `Markdown.update()` in a loop:**
```python
# BAD: replaces entire document each token — O(n²)
for token in stream:
    accumulated += token
    md.update(accumulated)

# GOOD: use append or get_stream()
stream_handle = md.get_stream()
async for token in llm_stream:
    await stream_handle.write(token)
```

### 11.5 Anchor-based auto-scroll

```python
# In the transcript scroll container, after mounting a new turn:
transcript_scroll = self.query_one("#transcript", VerticalScroll)
transcript_scroll.anchor()
# Now new content automatically scrolls to bottom
# But if user scrolls up, anchor is released
# Call anchor() again to re-engage when a new user prompt arrives
```

### 11.6 `textual-speedups` — Rust geometry module

```bash
pip install textual-speedups
```

Replaces pure-Python `Offset`, `Size`, `Region`, `Spacing` with Rust implementations. Several orders of magnitude faster. No code changes required — Textual detects and uses automatically. Disable with `TEXTUAL_SPEEDUPS=0` env var.

### 11.7 Synchronized output protocol

Textual uses the Synchronized Output protocol (`\x1b[?2026h`/`\x1b[?2026l`) when supported by the terminal. This instructs the terminal not to render intermediate frames, eliminating flicker at high update rates. Supported by Ghostty, Kitty, WezTerm, iTerm2, and modern VTE-based terminals.

### 11.8 Resize storm mitigation

Rapid terminal resizes (e.g., dragging a window) produce many SIGWINCH signals. As of v8.2.2–v8.2.3, Textual throttles resize handling via a timer (not idle loop) to prevent CPU spikes. No application-level workaround needed for normal use.

### 11.9 Emoji and wide character constraints

Emoji handling is inherently fragile across terminals:
- Character widths vary (single vs double width) across Unicode versions
- Multi-codepoint sequences (skin tones, ZWJ sequences) render inconsistently
- **Recommendation:** Stick to Unicode 9 emoji for reliable cross-terminal rendering
- Use `Rich`'s `Text.cell_len()` for correct width calculations on user content

### 11.10 Floating point precision in layout

Textual uses Python's `fractions.Fraction` internally for layout arithmetic to avoid rounding errors (e.g., a 1-character gap between panels from float accumulation). This is handled automatically — do not implement custom layout arithmetic with floats.

---

## 12. Anti-Patterns to Avoid

### 12.1 Blocking the async event loop

```python
# NEVER — blocks the event loop, freezes the entire UI
async def on_button_pressed(self, event: Button.Pressed) -> None:
    result = requests.get("https://api.example.com/data")  # BLOCKS
    self.update_display(result)

# CORRECT — use async I/O
async def on_button_pressed(self, event: Button.Pressed) -> None:
    self.run_worker(self._fetch_data())

@work
async def _fetch_data(self) -> None:
    async with httpx.AsyncClient() as client:
        result = await client.get("https://api.example.com/data")
    self.call_after_refresh(self.update_display, result)
```

### 12.2 Updating UI from threads without `call_from_thread`

```python
# NEVER — Textual is not thread-safe
@work(thread=True)
def background_task(self) -> None:
    result = blocking_operation()
    self.query_one(Label).update(result)  # RACE CONDITION

# CORRECT
@work(thread=True)
def background_task(self) -> None:
    result = blocking_operation()
    self.call_from_thread(self.query_one(Label).update, result)
    # OR: self.post_message(ResultReady(result))  # also thread-safe
```

### 12.3 `Markdown.update()` in a streaming loop (O(n²) pattern)

```python
# NEVER — each update replaces and re-renders the full document
buffer = ""
async for token in stream:
    buffer += token
    await md_widget.update(buffer)  # gets slower as document grows

# CORRECT — use append or get_stream()
stream = md_widget.get_stream()
async for token in stream_source:
    await stream.write(token)
```

### 12.4 Querying the DOM before mount

```python
# NEVER — widget not yet in DOM during __init__ or compose()
def __init__(self):
    super().__init__()
    self.query_one(Input).focus()  # NoMatches exception

# CORRECT — use on_mount()
def on_mount(self) -> None:
    self.query_one(Input).focus()
```

### 12.5 Storing references to recomposed widgets

```python
# NEVER — reference becomes stale after recompose
messages = reactive([], recompose=True)
self._input_ref = self.query_one(Input)  # may be replaced on next recompose
# Later:
self._input_ref.value = ""  # operates on detached widget

# CORRECT — always query fresh references
self.query_one(Input).value = ""
```

### 12.6 `reactive(layout=True)` when layout is stable

```python
# EXPENSIVE — triggers full layout pass on every change
status_text = reactive("", layout=True)  # layout rarely needed for text

# CORRECT — only use layout=True when size actually changes
status_text = reactive("")  # just redraws content
```

### 12.7 Unbounded RichLog / Log without `max_lines`

```python
# RISKY — unbounded memory for long-running sessions
yield RichLog()

# CORRECT
yield RichLog(max_lines=10000)  # Cap at 10k lines
```

### 12.8 Using alternate screen mode (violates hard constraint)

```python
# FORBIDDEN for agenthicc
app.run()  # Uses alternate screen — destroys scrollback

# REQUIRED
app.run(inline=True)
```

### 12.9 Ignoring RichLog auto-scroll race condition

When `RichLog.auto_scroll=True`, any `write()` call will scroll to bottom — even if the user has scrolled up to read earlier content. For streaming output where users may scroll up, either:

```python
# Option A: use Markdown widget with get_stream() — better auto-scroll behavior
# Option B: check position before writing
log = self.query_one(RichLog)
was_at_bottom = log.is_vertical_scroll_end
log.write(line, scroll_end=was_at_bottom)
```

### 12.10 Slow compute methods

Compute methods run whenever any reactive changes. Keep them fast (no I/O, no sleeping, no heavy computation):

```python
# BAD — runs on every reactive change
def compute_color(self) -> Color:
    return Color.parse(requests.get(...).text)  # NEVER

# GOOD — pure computation
def compute_color(self) -> Color:
    return Color(self.red, self.green, self.blue).clamped
```

---

## 13. Recommended Architecture for the AI Coding Agent TUI

### 13.1 Overall structure

```python
# app.py
class AgenthiccTUI(App):
    CSS_PATH = ["styles/app.tcss"]
    INLINE_PADDING = 0
    
    def run_inline(self) -> None:
        """Entry point respecting the no-alternate-screen constraint."""
        self.run(inline=True)
```

### 13.2 Screen layout

```
AgenthiccTUI (App)
└── MainScreen (Screen)
    ├── Header (dock: top, height: 1)
    ├── Sidebar (dock: left, width: 30, toggleable)
    │   ├── FileTree (DirectoryTree)
    │   └── ContextList (ListView)
    ├── MainPanel (Vertical, width: 1fr)
    │   ├── Transcript (VerticalScroll, height: 1fr)
    │   │   └── [ConversationTurn × N]  (dynamically mounted)
    │   │       ├── UserTurn (Markdown, classes="user-turn")
    │   │       └── AssistantTurn (Vertical)
    │   │           ├── [ToolCallWidget × N]
    │   │           └── ResponseMarkdown (Markdown, streaming)
    │   ├── StatusBar (height: 1, dock: bottom)
    │   └── InputArea (TextArea + controls, height: auto, max: 10)
    └── [ModalScreen overlays as needed]
```

### 13.3 Streaming LLM output architecture

```python
class MainScreen(Screen):
    
    @on(InputArea.Submitted)
    @work(exclusive=True)  # cancel any previous LLM request
    async def handle_user_input(self, event: InputArea.Submitted) -> None:
        prompt = event.value
        transcript = self.query_one("#transcript", VerticalScroll)
        
        # 1. Mount user turn (immediate)
        user_turn = UserTurn(prompt)
        await transcript.mount(user_turn)
        
        # 2. Mount assistant turn with empty Markdown
        assistant_turn = AssistantTurn()
        await transcript.mount(assistant_turn)
        
        # 3. Anchor scroll container — follows new content,
        #    but releases if user scrolls up manually
        transcript.anchor()
        
        # 4. Stream tokens via MarkdownStream
        md = assistant_turn.query_one(Markdown)
        stream = md.get_stream()
        
        try:
            async for event in self.app.kernel.stream_agent(prompt):
                match event:
                    case AgentToken(text=text):
                        await stream.write(text)
                    case ToolCallStart(tool=tool, args=args):
                        tool_widget = ToolCallWidget(tool, args)
                        await assistant_turn.mount(tool_widget)
                    case ToolCallEnd(result=result):
                        assistant_turn.query_one(
                            ToolCallWidget, last=True
                        ).complete(result)
        finally:
            await stream.finish()
```

### 13.4 Kernel integration pattern

The TUI should communicate with the agenthicc kernel exclusively through typed messages and `call_from_thread()` or `post_message()`:

```python
class AgenthiccTUI(App):
    def __init__(self, processor: EventProcessor) -> None:
        super().__init__()
        self._processor = processor
        self._kernel_subscription: asyncio.Task | None = None
    
    async def on_mount(self) -> None:
        # Subscribe to kernel events in a worker
        self.run_worker(self._kernel_event_loop(), thread=False)
    
    @work
    async def _kernel_event_loop(self) -> None:
        """Translates kernel AppState diffs into TUI messages."""
        async for app_state in self._processor.subscribe():
            # Map kernel events to TUI messages
            self.post_message(KernelStateChanged(app_state))
```

### 13.5 Input area design

```python
class InputArea(Widget):
    """Multi-line input with @ mention and / command support."""
    
    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()
    
    BINDINGS = [
        ("enter", "submit", "Submit"),
        ("shift+enter", "newline", "New line"),
        ("escape", "clear", "Clear"),
    ]
    
    def compose(self) -> ComposeResult:
        yield TextArea(id="input-field")
        with Horizontal(id="toolbar"):
            yield Button("@ Mention", id="at-mention", variant="default")
            yield Button("Send", id="send", variant="primary")
    
    def action_submit(self) -> None:
        ta = self.query_one("#input-field", TextArea)
        value = ta.text.strip()
        if value:
            self.post_message(self.Submitted(value))
            ta.clear()
    
    def action_newline(self) -> None:
        ta = self.query_one("#input-field", TextArea)
        ta.insert("\n")
```

### 13.6 Tool call display widget

```python
class ToolCallWidget(Widget):
    """Displays a single tool invocation with live output streaming."""
    
    DEFAULT_CSS = """
    ToolCallWidget {
        height: auto;
        border: round $panel;
        margin: 0 0 1 0;
        padding: 0 1;
        
        &.running { border: round $warning; }
        &.complete { border: round $success; }
        &.error { border: round $error; }
        
        .header { height: 1; }
        .output { max-height: 20; overflow-y: auto; }
    }
    """
    
    state = reactive("pending")
    
    def watch_state(self, state: str) -> None:
        self.set_classes(f"tool-call {state}")
    
    def compose(self) -> ComposeResult:
        with Horizontal(classes="header"):
            yield Label(f"⚙ {self._tool_name}", classes="tool-name")
            yield LoadingIndicator(id="spinner")
        yield RichLog(id="output", max_lines=500, classes="output")
    
    def complete(self, result: str | None = None) -> None:
        self.state = "complete"
        self.query_one("#spinner").display = False
    
    def fail(self, error: str) -> None:
        self.state = "error"
        self.query_one("#spinner").display = False
        self.query_one("#output", RichLog).write(f"[red]Error: {error}[/]")
```

### 13.7 Snapshot testing strategy

```python
# tests/tui/test_snapshots.py
from pytest_textual_snapshot import snap_compare

async def test_empty_transcript(snap_compare):
    async def setup(pilot):
        pass
    assert await snap_compare("src/agenthicc/tui/app.py", run_before=setup)

async def test_inline_mode_layout(snap_compare):
    assert await snap_compare(
        "src/agenthicc/tui/app.py",
        terminal_size=(120, 40),
    )
```

---

## 14. Risks and Mitigations

### 14.1 Windows inline mode — Unsupported

**Risk:** Inline mode does not work on native Windows (no POSIX terminal escape code support).  
**Impact:** Agenthicc cannot run on Windows except via WSL.  
**Mitigation:** Document the WSL requirement clearly. Consider a fallback headless JSON-lines mode for Windows.

### 14.2 Terminal incompatibility

**Risk:** Old terminals (Windows Console Host, some SSH clients, very old xterm) may not support all escape sequences used in inline mode.  
**Impact:** Garbled output, broken mouse support, incorrect cursor positioning.  
**Mitigation:** Test against Ghostty (recommended), iTerm2, Alacritty, WezTerm, Kitty, and VSCode integrated terminal. Add a `--check-terminal` flag that validates capabilities before starting.

### 14.3 Resize event storms

**Risk:** Rapid terminal resizing (window dragging) causes many SIGWINCH events, potentially saturating the layout engine.  
**Impact:** CPU spike, temporary lag.  
**Mitigation:** Textual v8.2.2+ handles this with timer-based throttling. Install `textual-speedups` to accelerate geometry calculations. Use a fixed `height` on the `Screen` in inline mode to reduce layout work on resize.

### 14.4 Scrollback duplication edge cases

**Risk:** On some terminal/multiplexer combinations (VSCode + tmux, Zellij + SSH), inline mode may produce duplicate history lines after large output bursts or resize events.  
**Impact:** Visual corruption in terminal scrollback.  
**Mitigation:** Fixed in Textual v2.1.116+. Use the latest Textual version. Test in CI against VSCode integrated terminal. Provide `--inline-no-clear` flag for a workaround when corruption is detected.

### 14.5 High-volume streaming causing UI jank

**Risk:** Extremely high token throughput from LLMs (>1000 tokens/sec) may outpace the 60fps render loop.  
**Impact:** Visible lag, missed frames.  
**Mitigation:** Use `MarkdownStream` (`Markdown.get_stream()`) which internally batches updates. Do not call `Markdown.update()` or `Markdown.append()` directly in a tight loop. Implement backpressure in the LLM streaming consumer if needed.

### 14.6 Memory growth in long sessions

**Risk:** Unbounded `RichLog` lines or `ConversationTurn` widgets accumulate over long sessions.  
**Impact:** Memory exhaustion, increasing GC pressure.  
**Mitigation:**  
- Set `max_lines=10000` on all `RichLog` and `Log` widgets  
- Implement conversation pagination — virtualize older turns (remove from DOM, keep in memory model)  
- Use the Line API (`render_line()`) for the transcript if widget count exceeds ~500

### 14.7 Complexity of inline mode cursor management

**Risk:** The inline rendering engine manages cursor position through escape sequences in ways that can interact badly with other terminal programs writing to the same stdout.  
**Impact:** Garbled display if anything else writes to the terminal during app execution.  
**Mitigation:** Redirect all application-level `print()` and `logging` to file during TUI operation. Use Textual's `log()` method instead of `print()`. Set `logging.basicConfig(filename=...)` before calling `app.run()`.

### 14.8 Textual version churn

**Risk:** Textual has made breaking changes at major versions (v4.0, v5.0, v6.0) affecting APIs used by the TUI (e.g., `Widget.anchor` semantics changed in v4.0).  
**Impact:** Framework upgrades require migration effort.  
**Mitigation:** Pin a specific Textual version in `pyproject.toml`. Write comprehensive snapshot tests. Subscribe to the Textual CHANGELOG. Dedicate one sprint per major version migration.

---

## 15. Conclusion

Textual is the right choice for the agenthicc terminal interface. It is the only Python TUI framework with:

1. **A production-tested inline mode** that preserves terminal scrollback — the hard constraint
2. **First-class LLM streaming support** via `Markdown.get_stream()` and `MarkdownStream` (introduced in v4.0 "The Streaming Release")
3. **Anchor-based auto-scroll** (`Widget.anchor()`) that follows streaming content while releasing when the user scrolls up to review history
4. **A complete async/worker system** that integrates naturally with the agenthicc MPSC event queue
5. **Rich integration** for syntax highlighting, tables, and styled text without extra work
6. **A real-world reference implementation** in Toad (built by the Textual author himself) for exactly this use case

The implementation path is clear:
- Entry point: `app.run(inline=True)` — non-negotiable
- Streaming output: `Markdown.append()` / `Markdown.get_stream()` for LLM tokens
- Scroll anchoring: `VerticalScroll.anchor()` on the transcript container
- Background work: `@work` decorator for all LLM calls and tool execution
- Kernel bridge: `post_message()` / `call_from_thread()` for thread-safe kernel→TUI events
- Performance: install `textual-speedups`, set `max_lines` on all logs
- Testing: `pytest-textual-snapshot` + `run_test()` / `Pilot`

The primary risk is terminal compatibility in edge environments (Windows, unusual multiplexers, old SSH clients). Mitigate by documenting requirements clearly, testing against a matrix of target terminals in CI, and providing a `--headless` (JSON-lines) fallback for environments where the TUI cannot render.

---

## References

- [Textual Documentation Home](https://textual.textualize.io/)
- [Behind the Curtain of Inline Terminal Applications](https://textual.textualize.io/blog/2024/04/20/behind-the-curtain-of-inline-terminal-applications/)
- [Style Inline Apps](https://textual.textualize.io/how-to/style-inline-apps/)
- [Algorithms for High-Performance Terminal Apps](https://textual.textualize.io/blog/2024/12/12/algorithms-for-high-performance-terminal-apps/)
- [Anatomy of a Textual User Interface](https://textual.textualize.io/blog/2024/09/15/anatomy-of-a-textual-user-interface/)
- [v4.0 The Streaming Release](https://github.com/Textualize/textual/releases/tag/v4.0.0)
- [Efficient Streaming Markdown in the Terminal](https://willmcgugan.github.io/streaming-markdown/)
- [Announcing Toad](https://willmcgugan.github.io/announcing-toad/)
- [Toad GitHub Repository](https://github.com/batrachianai/toad)
- [7 Things Learned Building a Modern TUI Framework](https://www.textualize.io/blog/7-things-ive-learned-building-a-modern-tui-framework/)
- [textual-speedups PyPI](https://pypi.org/project/textual-speedups/)
- [Textual GitHub Repository](https://github.com/Textualize/textual)
- [Textual Reactivity Guide](https://textual.textualize.io/guide/reactivity/)
- [Textual Workers Guide](https://textual.textualize.io/guide/workers/)
- [Textual Layout Guide](https://textual.textualize.io/guide/layout/)
