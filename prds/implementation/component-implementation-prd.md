# AgentHICC TUI — Component Implementation PRD

**Version:** 1.0  
**Architecture:** Committed-transcript (raw stdout) + Textual inline bottom block  
**Hard constraints:**
- No alternate screen (`App.run(inline=True)` only)
- Textual inline mode for bottom block widgets only
- Rich for transcript rendering (committed lines)
- Python 3.11+, full type hints, mypy-clean (`from __future__ import annotations` on every file)
- All display-width via `wcwidth.wcswidth()`, never `len()`
- All ANSI output respects `NO_COLOR` env var

**Framework versions (minimum):**
- `textual >= 0.56` (for `MarkdownStream`)
- `rich >= 13.7`
- `wcwidth >= 0.2.13`

---

## Architecture Overview

```
Layer 1 — Committed Transcript (raw stdout, Rich rendering)
  AgentMessage, UserMessage, ToolCallBlock, DiffViewer,
  ConversationDivider, ExpandableOutput, ErrorBlock

Layer 2 — Bottom Block (Textual App inline=True, max 12 rows)
  StreamingText, AgentStatusBar, TokenMeter, ModeIndicator,
  InputBar, TriggerDropdown, ThinkingIndicator, ProgressIndicator,
  CommandPalette, MentionChip (preview row)

Layer 3 — Float Overlays (Textual Float, above bottom block)
  TriggerDropdown, CommandPalette, NotificationToast

Layer 4 — In-flow blocking (mounted in ChatTranscript or bottom block)
  ApprovalRequest
```

**File layout:**
```
src/agenthicc/tui/
  __init__.py
  terminal.py          # Terminal, Frame, Size, TerminalCapabilities
  frame_composer.py    # FrameComposer, render helpers
  render_loop.py       # RenderLoop
  input_state.py       # InputState, DropdownState, TriggerType
  transcript.py        # TranscriptModel, AgentTurnEntry, ToolCallEntry, enums
  adapter.py           # TUIEventAdapter
  app.py               # BottomApp (Textual App, inline=True)
  widgets/
    __init__.py
    chat_transcript.py
    agent_message.py
    user_message.py
    tool_call_block.py
    diff_viewer.py
    streaming_text.py
    agent_status_bar.py
    token_meter.py
    mode_indicator.py
    input_bar.py
    trigger_dropdown.py
    approval_request.py
    progress_indicator.py
    notification_toast.py
    session_header.py
    thinking_indicator.py
    command_palette.py
    mention_chip.py
    error_block.py
    context_summary.py
    conversation_divider.py
    expandable_output.py
  messages.py          # All Textual Message subclasses
  styles/
    app.tcss           # CSS design tokens and widget styles
```

---

## Shared Types and Enums

Every file begins with `from __future__ import annotations`.

```python
# src/agenthicc/tui/messages.py
from __future__ import annotations
from dataclasses import dataclass
from textual.message import Message


# --- ChatTranscript messages ---
class ScrollPaused(Message):
    def __init__(self, offset: int) -> None:
        super().__init__()
        self.offset = offset

class ScrollResumed(Message):
    pass

class ItemFocused(Message):
    def __init__(self, item_id: str) -> None:
        super().__init__()
        self.item_id = item_id

# --- AgentMessage messages ---
class TurnComplete(Message):
    def __init__(self, turn_id: str) -> None:
        super().__init__()
        self.turn_id = turn_id

class TurnErrored(Message):
    def __init__(self, turn_id: str, error: str) -> None:
        super().__init__()
        self.turn_id = turn_id
        self.error = error

# --- UserMessage messages ---
class MentionActivated(Message):
    def __init__(self, mention: "Mention") -> None:
        super().__init__()
        self.mention = mention

# --- ToolCallBlock messages ---
class ToolExpanded(Message):
    def __init__(self, tool_id: str) -> None:
        super().__init__()
        self.tool_id = tool_id

class ToolCollapsed(Message):
    def __init__(self, tool_id: str) -> None:
        super().__init__()
        self.tool_id = tool_id

class ApprovalRequested(Message):
    def __init__(self, tool_id: str) -> None:
        super().__init__()
        self.tool_id = tool_id

# --- DiffViewer messages ---
class DiffCopied(Message):
    def __init__(self, diff_text: str) -> None:
        super().__init__()
        self.diff_text = diff_text

class DiffToggled(Message):
    def __init__(self, collapsed: bool) -> None:
        super().__init__()
        self.collapsed = collapsed

# --- AgentStatusBar / ModeIndicator messages ---
class ModeChangeRequested(Message):
    pass

class ModeActivated(Message):
    def __init__(self, mode: "Mode") -> None:
        super().__init__()
        self.mode = mode

class ModeCycleRequested(Message):
    def __init__(self, direction: int) -> None:  # +1 or -1
        super().__init__()
        self.direction = direction

class ModePickerRequested(Message):
    pass

# --- TokenMeter messages ---
class BudgetWarning(Message):
    def __init__(self, fraction: float) -> None:
        super().__init__()
        self.fraction = fraction

class BudgetExceeded(Message):
    pass

# --- InputBar messages ---
class MessageSubmitted(Message):
    def __init__(self, text: str, mentions: "list[Mention]") -> None:
        super().__init__()
        self.text = text
        self.mentions = mentions

class TriggerDetected(Message):
    def __init__(self, kind: str, fragment: str, cursor_pos: int) -> None:
        super().__init__()
        self.kind = kind
        self.fragment = fragment
        self.cursor_pos = cursor_pos

class TriggerDismissed(Message):
    pass

class InputChanged(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

# --- TriggerDropdown messages ---
class CompletionAccepted(Message):
    def __init__(self, value: str, kind: str) -> None:
        super().__init__()
        self.value = value
        self.kind = kind

class CompletionDismissed(Message):
    pass

class CompletionHighlighted(Message):
    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value

# --- ApprovalRequest messages ---
class ApprovalGranted(Message):
    def __init__(self, request_id: str) -> None:
        super().__init__()
        self.request_id = request_id

class ApprovalDenied(Message):
    def __init__(self, request_id: str, reason: str) -> None:
        super().__init__()
        self.request_id = request_id
        self.reason = reason

class ApprovalAllGranted(Message):
    def __init__(self, tool_name: str) -> None:
        super().__init__()
        self.tool_name = tool_name

# --- NotificationToast messages ---
class ToastDismissed(Message):
    def __init__(self, toast_id: str) -> None:
        super().__init__()
        self.toast_id = toast_id

# --- ContextSummary messages ---
class ContextExpanded(Message):
    pass

class ContextCollapsed(Message):
    pass

class FileRemoved(Message):
    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

# --- ExpandableOutput messages ---
class OutputExpanded(Message):
    def __init__(self, widget_id: str) -> None:
        super().__init__()
        self.widget_id = widget_id

class OutputCollapsed(Message):
    def __init__(self, widget_id: str) -> None:
        super().__init__()
        self.widget_id = widget_id

# --- ErrorBlock messages ---
class ErrorRetried(Message):
    def __init__(self, error_id: str) -> None:
        super().__init__()
        self.error_id = error_id

class ErrorDismissed(Message):
    def __init__(self, error_id: str) -> None:
        super().__init__()
        self.error_id = error_id

class ErrorCopied(Message):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

# --- MentionChip messages ---
class ChipActivated(Message):
    def __init__(self, mention: "Mention") -> None:
        super().__init__()
        self.mention = mention

class ChipRemoved(Message):
    def __init__(self, mention: "Mention") -> None:
        super().__init__()
        self.mention = mention

# --- CommandPalette messages ---
class CommandSelected(Message):
    def __init__(self, command: "Command", args: str) -> None:
        super().__init__()
        self.command = command
        self.args = args

class PaletteDismissed(Message):
    pass
```

---

## Design Tokens (CSS)

```css
/* src/agenthicc/tui/styles/app.tcss */

/* Semantic colours — Catppuccin Mocha-inspired, 256-color compatible */
$color-agent-bg:     #1e1e2e;
$color-user-bg:      #181825;
$color-tool-border:  #313244;
$color-tool-running: #cba6f7;
$color-tool-success: #a6e3a1;
$color-tool-error:   #f38ba8;
$color-tool-pending: #6c7086;

/* Mode badge colours */
$mode-auto:    #a6e3a1;
$mode-plan:    #f9e2af;
$mode-ask:     #89dceb;
$mode-review:  #89b4fa;
$mode-safe:    #f38ba8;
$mode-debug:   #cba6f7;

/* Status colours */
$status-idle:     #6c7086;
$status-thinking: #f9e2af;
$status-running:  #89dceb;
$status-error:    #f38ba8;
$status-approval: #fab387;

/* Diff colours */
$diff-added:   #a6e3a1;
$diff-removed: #f38ba8;
$diff-hunk:    #89dceb;
$diff-context: #cdd6f4;

/* Toast colours */
$toast-info:    #89b4fa;
$toast-success: #a6e3a1;
$toast-warning: #f9e2af;
$toast-error:   #f38ba8;

/* Animation constants (used in Python via CSS class toggling) */
/* SPINNER_FRAMES = ["◐", "◑", "◒", "◓"] */
/* SPINNER_INTERVAL_MS = 125 */
/* CURSOR_BLINK_MS = 500 */
/* TOAST_DEFAULT_MS = 3000 */
/* TOKEN_UPDATE_THROTTLE_MS = 200 */

ChatTranscript {
    height: 1fr;
    overflow-y: scroll;
    overflow-x: hidden;
    background: $color-agent-bg;
    padding: 0 1;
}

AgentMessage {
    padding: 0 0 1 0;
    background: $color-agent-bg;
}

UserMessage {
    padding: 0 0 1 0;
    background: $color-user-bg;
}

ToolCallBlock {
    border: round $color-tool-border;
    padding: 0 1;
    margin: 0 0 1 0;
}

ToolCallBlock.--running {
    border: round $color-tool-running;
}

ToolCallBlock.--success {
    border: round $color-tool-success;
}

ToolCallBlock.--error {
    border: round $color-tool-error;
}

AgentStatusBar {
    dock: bottom;
    height: 1;
    background: $color-user-bg;
    padding: 0 1;
}

InputBar {
    dock: bottom;
    height: auto;
    max-height: 8;
    min-height: 1;
    border: round $color-tool-border;
    padding: 0 1;
}

TriggerDropdown {
    layer: above;
    max-height: 10;
    border: round $color-tool-border;
    background: $color-user-bg;
}

NotificationToast {
    layer: notification;
    dock: top;
    height: auto;
}

ModeIndicator {
    width: auto;
    min-width: 6;
}

ModeIndicator.--auto   { color: $mode-auto; }
ModeIndicator.--plan   { color: $mode-plan; }
ModeIndicator.--ask    { color: $mode-ask; }
ModeIndicator.--review { color: $mode-review; }
ModeIndicator.--safe   { color: $mode-safe; }
ModeIndicator.--debug  { color: $mode-debug; }

MentionChip {
    width: auto;
    padding: 0 1;
    margin: 0 1 0 0;
}

MentionChip.--file        { border-left: solid $mode-review; }
MentionChip.--directory   { border-left: solid $mode-ask; }
MentionChip.--url         { border-left: solid $mode-auto; }
MentionChip.--glob        { border-left: solid $mode-plan; }
MentionChip.--unresolved  { border-left: solid $mode-safe; }

ErrorBlock {
    border: round $color-tool-error;
    padding: 0 1;
    margin: 0 0 1 0;
}

ApprovalRequest {
    border: round $status-approval;
    padding: 1;
    margin: 0 0 1 0;
}

ConversationDivider {
    color: $status-idle;
    margin: 1 0;
}

ExpandableOutput {
    padding: 0 1;
}

ProgressIndicator {
    height: 1;
}

ContextSummary {
    background: $color-user-bg;
    border: round $color-tool-border;
    padding: 0 1;
    margin: 0 0 1 0;
}

SessionHeader {
    dock: top;
    height: 1;
    background: $color-user-bg;
    color: $status-idle;
    padding: 0 1;
}
```

---

## Component 1: ChatTranscript

**Purpose:** Primary scrollable viewport owning the ordered list of all conversation items; manages auto-scroll during streaming and keyboard navigation.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/chat_transcript.py
from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.scroll_view import ScrollView
from textual.reactive import reactive
from textual.widget import Widget

from ..messages import ScrollPaused, ScrollResumed, ItemFocused

if TYPE_CHECKING:
    from ..transcript import TranscriptItem


class ChatTranscriptState(Enum):
    IDLE = auto()
    STREAMING = auto()
    SCROLL_PAUSED = auto()
    AWAITING_APPROVAL = auto()


class ChatTranscript(ScrollView):
    """Primary scrollable conversation viewport.

    Constraints:
    - Used in Layer 1 (committed transcript) rendered via Rich to stdout,
      OR as a Textual VerticalScroll in the bottom block for short sessions.
    - In committed-transcript architecture, this widget is not mounted;
      transcript is stdout. This widget is used when Textual manages the
      full layout (e.g. small terminal or accessibility mode).
    """

    COMPONENT_CLASSES = {"chat-transcript--item"}
    DEFAULT_CSS = """
    ChatTranscript {
        height: 1fr;
        overflow-y: scroll;
    }
    """

    state: reactive[ChatTranscriptState] = reactive(ChatTranscriptState.IDLE)
    auto_scroll: reactive[bool] = reactive(True)

    def __init__(
        self,
        *,
        max_items: int = 500,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._max_items = max_items
        self._items: list[Widget] = []
        self._scroll_paused_by_user: bool = False

    # --- Public API ---

    def append_item(self, widget: Widget) -> None:
        """Mount a new conversation item and optionally scroll to bottom."""
        self._items.append(widget)
        self.mount(widget)
        if len(self._items) > self._max_items:
            self._virtualise_oldest()
        if self.auto_scroll and self.state != ChatTranscriptState.SCROLL_PAUSED:
            self.scroll_end(animate=False)

    def scroll_to_bottom(self) -> None:
        """Jump to bottom and re-enable auto-scroll."""
        self.scroll_end(animate=False)
        self._scroll_paused_by_user = False
        if self.state == ChatTranscriptState.SCROLL_PAUSED:
            self.state = ChatTranscriptState.STREAMING
        self.auto_scroll = True
        self.post_message(ScrollResumed())

    def set_streaming(self, streaming: bool) -> None:
        if streaming:
            self.state = ChatTranscriptState.STREAMING
        else:
            self.state = ChatTranscriptState.IDLE
            self._scroll_paused_by_user = False

    def set_awaiting_approval(self, waiting: bool) -> None:
        if waiting:
            self.state = ChatTranscriptState.AWAITING_APPROVAL
        elif self.state == ChatTranscriptState.AWAITING_APPROVAL:
            self.state = ChatTranscriptState.IDLE

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        yield from self._items

    def on_scroll_changed(self) -> None:
        """Detect user scroll-up during streaming."""
        if self.state == ChatTranscriptState.STREAMING:
            if not self._is_at_bottom():
                self._scroll_paused_by_user = True
                self.state = ChatTranscriptState.SCROLL_PAUSED
                self.post_message(ScrollPaused(offset=int(self.scroll_y)))

    # --- Key bindings ---

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "end":
            self.scroll_to_bottom()
            event.stop()
        elif event.key == "home":
            self.scroll_home(animate=False)
            event.stop()

    # --- Private helpers ---

    def _is_at_bottom(self) -> bool:
        return self.scroll_y >= self.max_scroll_y - 1

    def _virtualise_oldest(self) -> None:
        """Unmount the oldest item to keep DOM size bounded."""
        if self._items:
            oldest = self._items.pop(0)
            oldest.remove()
```

### Visual Mockup

```
╔══════════════════════════════════════════════════════════════╗
║  [IDLE state — full scroll freedom]                          ║
║  ── Turn 3 ────────────────────────────────────── 11:42 ──  ║
║  YOU  11:42                                                   ║
║  Fix the auth bug in [src/auth.py]                           ║
║                                                               ║
║  AGENT  11:42  [claude-opus-4-8]                              ║
║  Let me read the file first…                                  ║
║  ┌─ read_file ────────────────────── ✓ 0.3s ──────────────┐  ║
║  │ src/auth.py (342 lines)                                 │  ║
║  └─────────────────────────────────────────────────────────┘  ║
╚══════════════════════════════════════════════════════════════╝

╔══════════════════════════════════════════════════════════════╗
║  [SCROLL_PAUSED — indicator shown]                           ║
║  ...earlier content...                                        ║
║                                      ▼ 3 new messages        ║
╚══════════════════════════════════════════════════════════════╝
```

### State Model

```python
class ChatTranscriptState(Enum):
    IDLE             = auto()  # No streaming; free scroll
    STREAMING        = auto()  # Auto-scroll locked to bottom
    SCROLL_PAUSED    = auto()  # User scrolled up during streaming
    AWAITING_APPROVAL = auto() # ApprovalRequest is bottommost item
```

### Rendering Spec

- Background: `$color-agent-bg` (`#1e1e2e`)
- Items appended via `mount()` with `scroll_end(animate=False)` when `auto_scroll=True`
- Scroll-paused indicator: `▼ N new messages` rendered as a `Static` overlay at bottom-right, dim cyan
- All items are `Widget` subclasses with `aria-label` set from type + speaker + timestamp
- Virtualised items (past `max_items`) are unmounted but their committed-line equivalents remain in stdout

### Events Emitted

| Message | When |
|---|---|
| `ScrollPaused(offset)` | User scrolls up during STREAMING |
| `ScrollResumed()` | User presses End or scrolls to bottom |
| `ItemFocused(item_id)` | Keyboard navigation selects an item |

### Events Consumed

| Event | Action |
|---|---|
| `TurnComplete` | `set_streaming(False)` |
| `AgentMessage` mounted | `scroll_end()` if auto_scroll |
| `ApprovalRequested` | `set_awaiting_approval(True)` |

### Error Handling

| Failure | Handling |
|---|---|
| `mount()` raises during virtualisation | Log warning; continue without mount |
| `scroll_end()` on zero-height widget | Guard with `if self.content_size.height > 0` |
| `max_items` exceeded | `_virtualise_oldest()` unmounts gracefully |

### Accessibility

- `aria-label` per item: synthesised as `"{type} from {speaker} at {timestamp}"`
- `role="feed"` on the container (ARIA live region for sequential posts)
- Scroll-paused state announced via `notify("Scroll paused — new messages below")`
- High-contrast borders use box-drawing characters not CSS colour alone
- `End` key always restores auto-scroll (announced via `notify`)

### Unit Tests

```python
# tests/unit/test_chat_transcript.py

# test_CT_01: append_item_mounts_widget_and_scrolls_to_bottom
#   Setup: ChatTranscript in IDLE, auto_scroll=True
#   Action: append_item(Static("hello"))
#   Assert: widget is mounted, scroll_y == max_scroll_y

# test_CT_02: scroll_up_during_streaming_sets_scroll_paused_state
#   Setup: set_streaming(True), simulate scroll_y < max_scroll_y
#   Action: on_scroll_changed()
#   Assert: state == SCROLL_PAUSED, ScrollPaused message posted

# test_CT_03: scroll_to_bottom_resumes_auto_scroll
#   Setup: SCROLL_PAUSED state
#   Action: scroll_to_bottom()
#   Assert: state == STREAMING, auto_scroll == True, ScrollResumed posted

# test_CT_04: virtualise_oldest_removes_item_when_max_exceeded
#   Setup: max_items=3, append 3 items
#   Action: append_item(fourth)
#   Assert: _items has 3 items, oldest item is removed from DOM

# test_CT_05: awaiting_approval_state_set_and_cleared
#   Setup: IDLE
#   Action: set_awaiting_approval(True), then set_awaiting_approval(False)
#   Assert: state transitions IDLE→AWAITING_APPROVAL→IDLE

# test_CT_06: end_key_calls_scroll_to_bottom
#   Setup: SCROLL_PAUSED
#   Action: on_key(KeyEvent("end"))
#   Assert: scroll_to_bottom called, event stopped

# test_CT_07: no_scroll_when_auto_scroll_false
#   Setup: auto_scroll=False
#   Action: append_item(widget)
#   Assert: scroll_y unchanged

# test_CT_08: scroll_paused_not_triggered_when_idle
#   Setup: IDLE state
#   Action: simulate scroll_y < max_scroll_y, on_scroll_changed()
#   Assert: state remains IDLE (no pause in non-streaming state)
```

---

## Component 2: AgentMessage

**Purpose:** Renders a single agent turn — speaker label, timestamp, Markdown body streamed token-by-token, and child ToolCallBlock/ExpandableOutput widgets.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/agent_message.py
from __future__ import annotations

import datetime
from enum import Enum, auto
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Markdown, Static

from ..messages import TurnComplete, TurnErrored

if TYPE_CHECKING:
    from .tool_call_block import ToolCallBlock
    from .expandable_output import ExpandableOutput
    from .error_block import ErrorBlock


class AgentMessageState(Enum):
    PENDING    = auto()  # Placeholder; no output yet
    STREAMING  = auto()  # Text arriving; cursor visible
    COMPLETE   = auto()  # Final text committed; cursor removed
    ERROR      = auto()  # Turn ended with error


class AgentMessage(Widget):
    """Single agent turn block rendered in ChatTranscript."""

    DEFAULT_CSS = """
    AgentMessage {
        padding: 0 0 1 0;
    }
    AgentMessage .agent-header {
        color: $mode-debug;
        text-style: bold;
    }
    AgentMessage .agent-header--dim {
        color: $status-idle;
    }
    AgentMessage .agent-divider {
        color: $status-idle;
    }
    """

    state: reactive[AgentMessageState] = reactive(AgentMessageState.PENDING)
    text: reactive[str] = reactive("")

    def __init__(
        self,
        turn_id: str,
        model_id: str,
        timestamp: datetime.datetime,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.turn_id = turn_id
        self.model_id = model_id
        self.timestamp = timestamp
        self._error: str | None = None
        self._cursor_widget: Static | None = None

    # --- Public API ---

    def append_token(self, token: str) -> None:
        """Append a streaming token. Transitions PENDING→STREAMING on first call."""
        if self.state == AgentMessageState.PENDING:
            self.state = AgentMessageState.STREAMING
            self._remove_thinking_indicator()
            self._show_cursor()
        self.text += token
        self.query_one(".agent-body", Markdown).update(self.text)

    def complete(self) -> None:
        """Mark turn as complete. Removes cursor, transitions to COMPLETE."""
        self.state = AgentMessageState.COMPLETE
        self._hide_cursor()
        self.post_message(TurnComplete(self.turn_id))

    def set_error(self, error: str) -> None:
        """Transition to ERROR state. Mounts ErrorBlock."""
        self._error = error
        self.state = AgentMessageState.ERROR
        self._hide_cursor()
        from .error_block import ErrorBlock  # avoid circular at module level
        self.mount(ErrorBlock(
            error_id=f"{self.turn_id}-error",
            title="Turn failed",
            message=error,
            retryable=True,
            source="llm",
        ))
        self.post_message(TurnErrored(self.turn_id, error))

    def append_tool_block(self, block: "ToolCallBlock") -> None:
        self.mount(block)

    def append_expandable(self, output: "ExpandableOutput") -> None:
        self.mount(output)

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        ts = self.timestamp.strftime("%H:%M")
        yield Static(
            f"AGENT  {ts}  [{self.model_id}]",
            classes="agent-header",
        )
        yield Static("─" * 40, classes="agent-divider")
        from .thinking_indicator import ThinkingIndicator
        yield ThinkingIndicator(classes="thinking-indicator")
        yield Markdown("", classes="agent-body")

    # --- Private helpers ---

    def _remove_thinking_indicator(self) -> None:
        try:
            self.query_one(".thinking-indicator").remove()
        except Exception:
            pass

    def _show_cursor(self) -> None:
        from .streaming_text import StreamingCursor
        self._cursor_widget = StreamingCursor()
        self.mount(self._cursor_widget)

    def _hide_cursor(self) -> None:
        if self._cursor_widget is not None:
            try:
                self._cursor_widget.remove()
            except Exception:
                pass
            self._cursor_widget = None

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "c":
            import pyperclip  # type: ignore[import]
            try:
                pyperclip.copy(self.text)
            except Exception:
                pass
            event.stop()
        elif event.key == "e":
            for output in self.query("ExpandableOutput"):
                output.expand()
            event.stop()
```

### Visual Mockup

```
AGENT  11:43  [claude-opus-4-8]         ← bold model dim
──────────────────────────────────────
  ◐ Thinking…                          ← ThinkingIndicator (PENDING→STREAMING)

AGENT  11:43  [claude-opus-4-8]
──────────────────────────────────────
I have fixed the authentication bug. The root cause was a
missing `await` on the token validation call.

  • src/auth.py line 87 — added `await`
  • tests/test_auth.py   — added regression test
▌                                       ← StreamingCursor (blinks)

AGENT  11:43  [claude-opus-4-8]         ← COMPLETE state
──────────────────────────────────────
I have fixed the authentication bug. [full text]
```

### State Model

```python
class AgentMessageState(Enum):
    PENDING   = auto()  # ThinkingIndicator visible; no text yet
    STREAMING = auto()  # Markdown updating each token; StreamingCursor shown
    COMPLETE  = auto()  # Final render; cursor removed
    ERROR     = auto()  # ErrorBlock injected at bottom
```

### Rendering Spec

- Header line: `AGENT  HH:MM  [model_id]`
  - "AGENT" in `$mode-debug` bold
  - timestamp in `$status-idle` dim
  - model_id in `$status-idle` dim, bracketed
- Horizontal rule: `─` × 40 (or terminal width), `$status-idle`
- Body: `textual.widgets.Markdown` widget, incremental `update(text)` per token (do NOT remount)
- Cursor: `StreamingCursor` widget appended after Markdown, removed on complete
- Error: `ErrorBlock` appended at bottom of widget subtree
- `aria-live="polite"` on `.agent-body` Markdown widget

### Events Emitted

| Message | When |
|---|---|
| `TurnComplete(turn_id)` | `complete()` called |
| `TurnErrored(turn_id, error)` | `set_error()` called |

### Events Consumed

| Source | Triggers |
|---|---|
| `TUIEventAdapter` streaming token | `append_token(token)` |
| `TUIEventAdapter` turn end | `complete()` |
| `TUIEventAdapter` turn error | `set_error(error)` |

### Error Handling

| Failure | Handling |
|---|---|
| `Markdown.update()` raises on malformed text | Catch, log, render as plain Static |
| `ThinkingIndicator.remove()` fails (already removed) | Silent `except Exception: pass` |
| `StreamingCursor` mount fails | Log warning; continue without cursor |
| `pyperclip` unavailable | Catch `ImportError`; skip copy silently |

### Accessibility

- "AGENT" label always present as text, never icon-only
- `aria-live="polite"` on streaming text region
- Timestamp in `title` attribute for hover
- Model label in `aria-describedby` of the header

### Unit Tests

```python
# tests/unit/test_agent_message.py

# test_AM_01: initial_state_is_pending_with_thinking_indicator
#   Assert: state == PENDING, ThinkingIndicator in DOM

# test_AM_02: first_token_transitions_to_streaming_removes_thinking
#   Action: append_token("Hello")
#   Assert: state == STREAMING, ThinkingIndicator removed, cursor shown

# test_AM_03: subsequent_tokens_accumulate_text
#   Action: append_token("Hello"), append_token(" world")
#   Assert: text == "Hello world", Markdown updated twice

# test_AM_04: complete_removes_cursor_and_emits_message
#   Setup: STREAMING state
#   Action: complete()
#   Assert: state == COMPLETE, cursor removed, TurnComplete posted

# test_AM_05: set_error_mounts_error_block_and_emits
#   Setup: STREAMING state
#   Action: set_error("API timeout")
#   Assert: state == ERROR, ErrorBlock in DOM, TurnErrored posted

# test_AM_06: header_contains_model_id_and_timestamp
#   Setup: model_id="claude-opus-4-8", timestamp=datetime(2026,6,13,11,43)
#   Assert: header Static text contains "11:43" and "claude-opus-4-8"

# test_AM_07: append_tool_block_mounts_it
#   Action: append_tool_block(ToolCallBlock(...))
#   Assert: ToolCallBlock is mounted as child

# test_AM_08: complete_in_pending_state_transitions_cleanly
#   Setup: PENDING state (no tokens arrived)
#   Action: complete()
#   Assert: state == COMPLETE, no error raised
```

---

## Component 3: UserMessage

**Purpose:** Renders a single committed user turn with raw text and inline MentionChip widgets; non-editable after submission.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/user_message.py
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ..messages import MentionActivated

if TYPE_CHECKING:
    from ..transcript import Mention
    from .mention_chip import MentionChip


class UserMessage(Widget):
    """Single committed user turn. Static post-submission."""

    DEFAULT_CSS = """
    UserMessage {
        padding: 0 0 1 0;
        background: $color-user-bg;
    }
    UserMessage .user-header { color: $mode-ask; text-style: bold; }
    UserMessage .user-body   { padding: 0 0 0 2; }
    """

    def __init__(
        self,
        turn_id: str,
        text: str,
        mentions: "list[Mention]",
        timestamp: datetime.datetime,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.turn_id = turn_id
        self.raw_text = text
        self.mentions = mentions
        self.timestamp = timestamp

    def compose(self) -> ComposeResult:
        ts = self.timestamp.strftime("%H:%M")
        yield Static(f"YOU  {ts}", classes="user-header")
        yield Static("─" * 40, classes="user-divider")
        yield from self._build_body()

    def _build_body(self) -> "ComposeResult":
        """Interleave text segments with MentionChip widgets."""
        from textual.app import ComposeResult
        from .mention_chip import MentionChip
        # Replace @mention tokens in raw_text with chip placeholders
        # Render plain text segments and chip widgets in order
        text = self.raw_text
        if not self.mentions:
            yield Static(text, classes="user-body")
            return

        # Build ordered segments
        # Each mention has .raw_token (e.g. "@src/auth.py") and .start_pos, .end_pos
        last_end = 0
        segments: list[tuple[str, "Mention | None"]] = []
        for mention in sorted(self.mentions, key=lambda m: m.start_pos):
            if mention.start_pos > last_end:
                segments.append((text[last_end:mention.start_pos], None))
            segments.append((mention.raw_token, mention))
            last_end = mention.end_pos
        if last_end < len(text):
            segments.append((text[last_end:], None))

        from textual.containers import Horizontal
        row_widgets: list[Widget] = []
        for seg_text, mention in segments:
            if mention is None:
                row_widgets.append(Static(seg_text))
            else:
                row_widgets.append(MentionChip(mention=mention))
        yield Horizontal(*row_widgets, classes="user-body")

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "c":
            import pyperclip  # type: ignore[import]
            try:
                pyperclip.copy(self.raw_text)
            except Exception:
                pass
            event.stop()
```

### Visual Mockup

```
YOU  11:42
──────────────────────────────────────
Fix the auth bug in [src/auth.py]     ← MentionChip inline
and add tests for [tests/]
```

### State Model

Single state: `DISPLAYED`. No FSM; message is appended whole.

### Rendering Spec

- Header: `YOU  HH:MM`, cyan bold
- Rule: `─` × 40, dim
- Body: text with `@mention` tokens replaced by `MentionChip` widgets in a `Horizontal` flow
- Raw `@token` text is NOT shown; chip replaces it

### Events Emitted

| Message | When |
|---|---|
| `MentionActivated(mention)` | User presses Enter/click on a child MentionChip |

### Events Consumed

None. Purely display.

### Error Handling

| Failure | Handling |
|---|---|
| Mention position out of range | Clamp to text length; render as plain text |
| `MentionChip` mount failure | Render raw `@token` as plain text |

### Accessibility

- "YOU" label always text, never icon
- `MentionChip` children expose `aria-label="file: <path>"`
- Tab order reaches each chip

### Unit Tests

```python
# tests/unit/test_user_message.py

# test_UM_01: compose_renders_header_with_timestamp
#   Assert: "YOU  11:42" in Static text

# test_UM_02: no_mentions_renders_plain_text
#   Setup: text="Hello world", mentions=[]
#   Assert: single Static with "Hello world"

# test_UM_03: mention_replaced_by_chip_in_body
#   Setup: text="Fix @src/auth.py now", mentions=[Mention(raw_token="@src/auth.py", ...)]
#   Assert: MentionChip in compose result; plain text "Fix " and " now" as Statics

# test_UM_04: multiple_mentions_ordered_correctly
#   Setup: text with two mentions at different positions
#   Assert: three text segments, two chips, in correct order

# test_UM_05: copy_key_copies_raw_text
#   Action: on_key(KeyEvent("c"))
#   Assert: pyperclip.copy called with raw_text

# test_UM_06: mentions_sorted_by_start_pos
#   Setup: mentions in reverse order by position
#   Assert: rendered in correct textual order

# test_UM_07: tab_focuses_first_mention_chip
#   Setup: two MentionChip children
#   Assert: first chip receives focus on Tab

# test_UM_08: empty_text_renders_empty_body
#   Setup: text="", mentions=[]
#   Assert: body Static is empty string, no crash
```

---

## Component 4: ToolCallBlock

**Purpose:** Displays one tool invocation lifecycle (pending/running/success/error/approval) with status-aware header, optional DiffViewer, and ExpandableOutput for long results.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/tool_call_block.py
from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from ..messages import ToolExpanded, ToolCollapsed, ApprovalRequested

if TYPE_CHECKING:
    from .diff_viewer import DiffViewer
    from .expandable_output import ExpandableOutput
    from .progress_indicator import ProgressIndicator


class ToolStatus(Enum):
    PENDING          = auto()
    RUNNING          = auto()
    SUCCESS          = auto()
    ERROR            = auto()
    APPROVAL_NEEDED  = auto()


_STATUS_ICON: dict[ToolStatus, str] = {
    ToolStatus.PENDING:         "○",
    ToolStatus.RUNNING:         "●",
    ToolStatus.SUCCESS:         "✓",
    ToolStatus.ERROR:           "✗",
    ToolStatus.APPROVAL_NEEDED: "⚠",
}

_STATUS_CSS_CLASS: dict[ToolStatus, str] = {
    ToolStatus.PENDING:         "--pending",
    ToolStatus.RUNNING:         "--running",
    ToolStatus.SUCCESS:         "--success",
    ToolStatus.ERROR:           "--error",
    ToolStatus.APPROVAL_NEEDED: "--running",
}


class ToolCallBlock(Widget):
    """Renders a single tool invocation with full lifecycle."""

    COMPONENT_CLASSES = {"tool-call-block--header", "tool-call-block--body"}

    status: reactive[ToolStatus] = reactive(ToolStatus.PENDING)

    def __init__(
        self,
        tool_id: str,
        tool_name: str,
        args_summary: str,
        *,
        duration_ms: int | None = None,
        output: str | None = None,
        is_diff: bool = False,
        error: str | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.args_summary = args_summary[:200]  # hard cap 200 chars
        self.duration_ms = duration_ms
        self.output = output
        self.is_diff = is_diff
        self.error = error
        self._expanded = False

    # --- Public API ---

    def set_running(self) -> None:
        self.status = ToolStatus.RUNNING
        self.add_class("--running")
        self._update_header()

    def set_success(self, output: str | None, duration_ms: int) -> None:
        self.status = ToolStatus.SUCCESS
        self.duration_ms = duration_ms
        self.output = output
        self.remove_class("--running")
        self.add_class("--success")
        self._update_header()
        if output:
            self._mount_output(output)

    def set_error(self, error: str, duration_ms: int) -> None:
        self.status = ToolStatus.ERROR
        self.error = error
        self.duration_ms = duration_ms
        self.remove_class("--running")
        self.add_class("--error")
        self._update_header()

    def set_approval_needed(self) -> None:
        self.status = ToolStatus.APPROVAL_NEEDED
        self.post_message(ApprovalRequested(self.tool_id))
        self._update_header()

    def toggle_expand(self) -> None:
        self._expanded = not self._expanded
        try:
            collapsible = self.query_one(Collapsible)
            if self._expanded:
                collapsible.collapsed = False
                self.post_message(ToolExpanded(self.tool_id))
            else:
                collapsible.collapsed = True
                self.post_message(ToolCollapsed(self.tool_id))
        except Exception:
            pass

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        icon = _STATUS_ICON[self.status]
        dur_text = f"{self.duration_ms}ms" if self.duration_ms else "…"
        header = f"─ {self.tool_name}  {icon} {dur_text}"
        yield Static(header, classes="tool-call-block--header", id="tcb-header")
        yield Static(self.args_summary, classes="tool-call-block--args")

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key in ("space", "enter"):
            self.toggle_expand()
            event.stop()
        elif event.key == "c":
            if self.output:
                import pyperclip  # type: ignore[import]
                try:
                    pyperclip.copy(self.output)
                except Exception:
                    pass
            event.stop()

    # --- Private helpers ---

    def _update_header(self) -> None:
        icon = _STATUS_ICON[self.status]
        dur_text = f"{self.duration_ms}ms" if self.duration_ms else "…"
        try:
            header_widget = self.query_one("#tcb-header", Static)
            header_widget.update(f"─ {self.tool_name}  {icon} {dur_text}")
        except Exception:
            pass

    def _mount_output(self, output: str) -> None:
        if self.is_diff:
            from .diff_viewer import DiffViewer
            collapsible = Collapsible(DiffViewer(diff_text=output), collapsed=True)
        else:
            from .expandable_output import ExpandableOutput
            collapsible = Collapsible(
                ExpandableOutput(content=output, preview_lines=5),
                collapsed=True,
            )
        self.mount(collapsible)
```

### Visual Mockup

```
┌─ read_file ───────────────────── ✓ 0.3s ─┐   SUCCESS
│ path: "src/auth.py"                        │
│ → 342 lines returned                       │
└────────────────────────────────────────────┘

┌─ write_file ──────────────────── ● running ─┐  RUNNING
│ path: "src/auth.py"                          │
│ [████████░░░░░░░░░░░░]  writing…            │
└──────────────────────────────────────────────┘

┌─ patch_file ──────────────────── ✓ 1.1s ─┐   DIFF output
│ ┌─ diff ────────────────────────────────┐ │
│ │ - return verify(token)                │ │
│ │ + return await verify(token)          │ │
│ └───────────────────────────────────────┘ │
└────────────────────────────────────────────┘

┌─ run_bash ────────────────────── ✗ error ─┐   ERROR
│ cmd: "pytest tests/test_auth.py"           │
│ [Show 47 lines]                             │
└────────────────────────────────────────────┘

┌─ write_file ──────────────────── ⚠ approval ─┐  APPROVAL_NEEDED
│ path: "src/auth.py"                            │
│ [Y] Allow  [N] Deny  [A] Allow all             │
└────────────────────────────────────────────────┘
```

### State Model

```python
class ToolStatus(Enum):
    PENDING          = auto()  # Queued; not started
    RUNNING          = auto()  # Executing; ProgressIndicator shown
    SUCCESS          = auto()  # Complete; green ✓ + duration
    ERROR            = auto()  # Failed; red ✗ + message
    APPROVAL_NEEDED  = auto()  # Blocked on user confirmation
```

Transitions: `PENDING→RUNNING→SUCCESS|ERROR`, `RUNNING→APPROVAL_NEEDED→RUNNING|ERROR`

### Rendering Spec

- Border: `round` style, colour from `_STATUS_CSS_CLASS` map
- Header row: `─ {tool_name}  {icon} {duration}` in tool-border colour
- Args summary: max 2 lines (200 chars hard cap), dim style
- Output: `Collapsible` wrapping `DiffViewer` (if `is_diff`) or `ExpandableOutput`
- Icons: `○` pending, `●` running (cyan), `✓` success (green), `✗` error (red), `⚠` approval (yellow)
- Duration: dim, format `{ms}ms` under 1s, `{s:.1f}s` over 1s

### Events Emitted

| Message | When |
|---|---|
| `ToolExpanded(tool_id)` | User expands output |
| `ToolCollapsed(tool_id)` | User collapses output |
| `ApprovalRequested(tool_id)` | Status → APPROVAL_NEEDED |

### Events Consumed

| Source | Action |
|---|---|
| `TUIEventAdapter` tool start | `set_running()` |
| `TUIEventAdapter` tool success | `set_success(output, duration_ms)` |
| `TUIEventAdapter` tool error | `set_error(error, duration_ms)` |
| `TUIEventAdapter` approval required | `set_approval_needed()` |

### Error Handling

| Failure | Handling |
|---|---|
| `args_summary` > 200 chars | Truncated at `__init__` |
| `_mount_output` fails | Log, continue without output widget |
| `pyperclip` unavailable | Silent except |
| Query for `#tcb-header` fails | Log, skip header update |

### Accessibility

- Status conveyed by icon + colour dual coding
- Duration exposed as `aria-label="completed in 0.3 seconds"`
- Collapsed output: `aria-expanded="false"` on Collapsible
- `role="status"` on header Static

### Unit Tests

```python
# tests/unit/test_tool_call_block.py

# test_TCB_01: initial_state_pending_icon_circle
#   Assert: header contains "○", status == PENDING

# test_TCB_02: set_running_updates_header_to_bullet
#   Action: set_running()
#   Assert: header contains "●", status == RUNNING, class --running added

# test_TCB_03: set_success_updates_header_checkmark_and_duration
#   Action: set_success("result", 312)
#   Assert: header contains "✓", "312ms", status == SUCCESS

# test_TCB_04: set_error_updates_header_cross
#   Action: set_error("No such file", 5)
#   Assert: header contains "✗", error stored, status == ERROR

# test_TCB_05: set_approval_needed_posts_approval_requested
#   Action: set_approval_needed()
#   Assert: ApprovalRequested message posted, status == APPROVAL_NEEDED

# test_TCB_06: toggle_expand_posts_tool_expanded
#   Setup: set_success("output", 100)
#   Action: toggle_expand()
#   Assert: ToolExpanded posted, _expanded == True

# test_TCB_07: args_summary_truncated_at_200_chars
#   Setup: args_summary = "x" * 300
#   Assert: self.args_summary == "x" * 200

# test_TCB_08: diff_output_mounts_diff_viewer
#   Setup: is_diff=True
#   Action: set_success("@@ -1,1 +1,1 @@\n-old\n+new", 50)
#   Assert: DiffViewer in widget tree
```

---

## Component 5: DiffViewer

**Purpose:** Renders unified diff with syntax highlighting — red for removed, green for added, dim cyan for hunk headers, dim for context.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/diff_viewer.py
from __future__ import annotations

import re
from enum import Enum, auto

from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import RichLog, Static

from ..messages import DiffCopied, DiffToggled


class DiffViewerState(Enum):
    COLLAPSED = auto()
    EXPANDED  = auto()
    LOADING   = auto()


class DiffViewer(Widget):
    """Renders a unified diff with Rich syntax colouring."""

    state: reactive[DiffViewerState] = reactive(DiffViewerState.EXPANDED)

    _HUNK_RE = re.compile(r"^@@.*@@")

    def __init__(
        self,
        diff_text: str,
        file_path: str | None = None,
        max_lines: int = 40,
        collapsed: bool = False,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.diff_text = diff_text
        self.file_path = file_path
        self.max_lines = max_lines
        self.state = DiffViewerState.COLLAPSED if collapsed else DiffViewerState.EXPANDED
        self._parsed_lines: list[tuple[str, str]] = []  # (line, kind)
        self._parse()

    # --- Public API ---

    def toggle(self) -> None:
        if self.state == DiffViewerState.COLLAPSED:
            self.state = DiffViewerState.EXPANDED
        else:
            self.state = DiffViewerState.COLLAPSED
        self._update_display()
        self.post_message(DiffToggled(self.state == DiffViewerState.COLLAPSED))

    def set_loading(self) -> None:
        self.state = DiffViewerState.LOADING
        self._update_display()

    def expand(self) -> None:
        self.state = DiffViewerState.EXPANDED
        self._update_display()

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        label = f"diff: {self.file_path}" if self.file_path else "diff"
        yield Static(f"┌─ {label} " + "─" * 20, classes="diff-header")
        yield RichLog(highlight=False, markup=False, id="diff-log")
        stats = self._compute_stats()
        yield Static(stats, classes="diff-footer")

    def on_mount(self) -> None:
        self._update_display()

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key in ("space", "enter"):
            self.toggle()
            event.stop()
        elif event.key == "c":
            import pyperclip  # type: ignore[import]
            try:
                pyperclip.copy(self.diff_text)
            except Exception:
                pass
            self.post_message(DiffCopied(self.diff_text))
            event.stop()

    # --- Private helpers ---

    def _parse(self) -> None:
        for line in self.diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                self._parsed_lines.append((line, "added"))
            elif line.startswith("-") and not line.startswith("---"):
                self._parsed_lines.append((line, "removed"))
            elif self._HUNK_RE.match(line):
                self._parsed_lines.append((line, "hunk"))
            else:
                self._parsed_lines.append((line, "context"))

    def _render_line(self, line: str, kind: str) -> Text:
        t = Text(no_wrap=True)
        if kind == "added":
            t.append(line, style="green")
        elif kind == "removed":
            t.append(line, style="red")
        elif kind == "hunk":
            t.append(line, style="dim cyan")
        else:
            t.append(line, style="dim")
        return t

    def _update_display(self) -> None:
        try:
            log = self.query_one("#diff-log", RichLog)
            log.clear()
            if self.state == DiffViewerState.LOADING:
                log.write(Text("Loading diff…", style="dim"))
                return
            lines = self._parsed_lines
            if self.state == DiffViewerState.COLLAPSED:
                lines = lines[:self.max_lines]
            for line_text, kind in lines:
                log.write(self._render_line(line_text, kind))
        except Exception:
            pass

    def _compute_stats(self) -> str:
        added = sum(1 for _, k in self._parsed_lines if k == "added")
        removed = sum(1 for _, k in self._parsed_lines if k == "removed")
        hunks = sum(1 for _, k in self._parsed_lines if k == "hunk")
        return f"{hunks} hunks · +{added} / -{removed} lines"
```

### Visual Mockup

```
┌─ diff: src/auth.py ────────────────────────────────┐
│ @@ -85,7 +85,7 @@ async def verify_jwt(token):    │  (dim cyan)
│   try:                                              │  (dim)
│ -     return verify(token)                          │  (red)
│ +     return await verify(token)                    │  (green)
│   except JWTError:                                  │  (dim)
│       raise AuthenticationError()                   │  (dim)
└─────────────────────────────────────────────────────┘
  3 hunks · +4 / -4 lines  [Space to collapse]

[COLLAPSED state]
┌─ diff: src/auth.py ────────────────────────────────┐
│ @@ -85,7 ... (40 lines, Space to expand)           │
└─────────────────────────────────────────────────────┘
  3 hunks · +4 / -4 lines
```

### State Model

```python
class DiffViewerState(Enum):
    COLLAPSED = auto()  # max_lines shown, expand affordance
    EXPANDED  = auto()  # full diff shown
    LOADING   = auto()  # computing diff
```

### Rendering Spec

- Header: `┌─ diff: {file_path} ─...` border character
- Body: `RichLog` widget; each line coloured per kind:
  - `"added"` → `style="green"`
  - `"removed"` → `style="red"`
  - `"hunk"` → `style="dim cyan"`
  - `"context"` → `style="dim"`
- Footer: `{N} hunks · +{A} / -{R} lines`
- NO_COLOR mode: strip colour styles; prefix `[+]` / `[-]` / `[@@]` text markers

### Events Emitted

| Message | When |
|---|---|
| `DiffCopied(diff_text)` | User presses `c` |
| `DiffToggled(collapsed)` | `toggle()` called |

### Error Handling

| Failure | Handling |
|---|---|
| Malformed diff (no `@@` markers) | Parse as all-context lines; no crash |
| `RichLog` query fails | Log, render as Static fallback |
| `pyperclip` unavailable | Silent except |

### Accessibility

- Removed lines prefixed `[-]` in addition to red (NO_COLOR safe)
- Added lines prefixed `[+]` in addition to green
- Hunk headers prefixed `[@@]` for screen readers
- `aria-expanded` toggled by `toggle()`

### Unit Tests

```python
# tests/unit/test_diff_viewer.py

# test_DV_01: parse_correctly_classifies_added_removed_hunk_context
#   Input: standard unified diff
#   Assert: _parsed_lines has correct kinds

# test_DV_02: render_added_line_uses_green_style
#   Assert: Text for "+  return await..." has green style

# test_DV_03: render_removed_line_uses_red_style
#   Assert: Text for "-  return verify..." has red style

# test_DV_04: render_hunk_header_uses_dim_cyan
#   Assert: Text for "@@ -85,7..." has dim cyan style

# test_DV_05: collapsed_state_shows_only_max_lines
#   Setup: 100-line diff, max_lines=40, collapsed=True
#   Assert: RichLog has 40 entries

# test_DV_06: toggle_from_collapsed_expands_and_posts_message
#   Setup: collapsed=True
#   Action: toggle()
#   Assert: state==EXPANDED, DiffToggled(collapsed=False) posted

# test_DV_07: compute_stats_counts_correctly
#   Input: diff with 3 hunks, 4 added, 4 removed
#   Assert: stats == "3 hunks · +4 / -4 lines"

# test_DV_08: malformed_diff_no_crash
#   Input: "not a diff at all"
#   Assert: no exception, renders as context lines
```

---

## Component 6: StreamingText (StreamingCursor)

**Purpose:** Blinking block cursor appended at insertion point of actively streaming text; removed from DOM when turn completes.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/streaming_text.py
from __future__ import annotations

from enum import Enum, auto

from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static


class CursorPhase(Enum):
    ON  = auto()
    OFF = auto()


class StreamingCursor(Widget):
    """Blinking block cursor. One instance in DOM at a time."""

    DEFAULT_CSS = """
    StreamingCursor {
        height: 1;
        width: auto;
        color: $mode-auto;
    }
    """

    phase: reactive[CursorPhase] = reactive(CursorPhase.ON)

    def __init__(
        self,
        blink_interval_ms: int = 500,
        char: str = "▌",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.blink_interval_ms = blink_interval_ms
        self.char = char
        self._timer: Timer | None = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(
            self.blink_interval_ms / 1000.0,
            self._blink,
        )

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _blink(self) -> None:
        self.phase = CursorPhase.OFF if self.phase == CursorPhase.ON else CursorPhase.ON

    def watch_phase(self, phase: CursorPhase) -> None:
        char = self.char if phase == CursorPhase.ON else " "
        try:
            self.query_one(Static).update(char)
        except Exception:
            pass

    def compose(self):
        yield Static(self.char)


class StreamingText(Widget):
    """
    MarkdownStream-based streaming text widget for the bottom block.
    Used during active agent turn in Layer 2 (Textual inline bottom block).
    """

    DEFAULT_CSS = """
    StreamingText {
        height: auto;
        max-height: 3;
        overflow-y: hidden;
    }
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._buffer: str = ""

    def append_token(self, token: str) -> None:
        """Append a new token to the streaming display."""
        self._buffer += token
        try:
            from textual.widgets import Markdown
            self.query_one(Markdown).update(self._buffer)
        except Exception:
            pass

    def clear(self) -> None:
        self._buffer = ""
        try:
            from textual.widgets import Markdown
            self.query_one(Markdown).update("")
        except Exception:
            pass

    def compose(self):
        from textual.widgets import Markdown
        yield Markdown("")
```

### Visual Mockup

```
…and the fix is applied to line 87.▌   ← CursorPhase.ON

…and the fix is applied to line 87.    ← CursorPhase.OFF (blink)
```

### State Model

```python
class CursorPhase(Enum):
    ON      = auto()  # char visible
    OFF     = auto()  # char blank
    # HIDDEN is represented by removal from DOM (no instance exists)
```

### Rendering Spec

- `StreamingCursor` renders single `Static` toggling between `self.char` ("▌") and `" "`
- Interval: `blink_interval_ms / 1000.0` via `set_interval` (stoppable in tests)
- Timer paused on `on_blur` / resumed on `on_focus` to reduce battery use
- One instance maximum in DOM; parent `AgentMessage.complete()` calls `.remove()`
- `StreamingText` in bottom block: `Markdown` widget updated each token, `max-height: 3`

### Events Emitted

None.

### Error Handling

| Failure | Handling |
|---|---|
| `Static` query fails in `watch_phase` | Silent except |
| `set_interval` during unmounted state | Guard with `on_mount` / `on_unmount` |

### Accessibility

- `aria-label="streaming"` on the parent `AgentMessage` container
- Cursor is a text character, not CSS animation
- Screen reader announced via `AgentMessage` `aria-live="polite"`

### Unit Tests

```python
# tests/unit/test_streaming_cursor.py

# test_SC_01: initial_phase_is_on
#   Assert: phase == CursorPhase.ON

# test_SC_02: blink_toggles_phase
#   Action: _blink() called once
#   Assert: phase == CursorPhase.OFF

# test_SC_03: blink_toggles_back_on_second_call
#   Action: _blink() called twice
#   Assert: phase == CursorPhase.ON

# test_SC_04: watch_phase_updates_static_to_char
#   Setup: compose(), watch_phase(ON)
#   Assert: Static.renderable == "▌"

# test_SC_05: watch_phase_off_updates_static_to_space
#   Action: watch_phase(OFF)
#   Assert: Static.renderable == " "

# test_SC_06: timer_stopped_on_unmount
#   Action: on_unmount()
#   Assert: _timer is None

# test_SC_07: custom_char_used_instead_of_default
#   Setup: StreamingCursor(char="█")
#   Assert: Static initial text == "█"

# test_SC_08: streaming_text_append_token_updates_markdown
#   Setup: StreamingText with mounted Markdown
#   Action: append_token("Hello"), append_token(" world")
#   Assert: Markdown updated with "Hello world"
```

---

## Component 7: AgentStatusBar

**Purpose:** Single-line persistent bar showing agent FSM state icon, mode badge, model label, session ID; always visible above InputBar.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/agent_status_bar.py
from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from textual.containers import Horizontal

from ..messages import ModeChangeRequested

if TYPE_CHECKING:
    from .mode_indicator import ModeIndicator, Mode
    from .token_meter import TokenMeter, TokenStats


class AgentFSMState(Enum):
    IDLE              = auto()
    THINKING          = auto()
    RUNNING_TOOLS     = auto()
    AWAITING_APPROVAL = auto()
    ERROR             = auto()
    STREAMING         = auto()


_STATE_ICON: dict[AgentFSMState, str] = {
    AgentFSMState.IDLE:               "○",
    AgentFSMState.THINKING:           "●",
    AgentFSMState.RUNNING_TOOLS:      "▶",
    AgentFSMState.AWAITING_APPROVAL:  "⚠",
    AgentFSMState.ERROR:              "✗",
    AgentFSMState.STREAMING:          "~",
}

_STATE_STYLE: dict[AgentFSMState, str] = {
    AgentFSMState.IDLE:               "dim",
    AgentFSMState.THINKING:           "yellow",
    AgentFSMState.RUNNING_TOOLS:      "cyan",
    AgentFSMState.AWAITING_APPROVAL:  "bold yellow",
    AgentFSMState.ERROR:              "red",
    AgentFSMState.STREAMING:          "green",
}


class AgentStatusBar(Horizontal):
    """Persistent status bar docked above InputBar."""

    DEFAULT_CSS = """
    AgentStatusBar {
        dock: bottom;
        height: 1;
        background: $color-user-bg;
        padding: 0 1;
    }
    """

    agent_state: reactive[AgentFSMState] = reactive(AgentFSMState.IDLE)

    def __init__(
        self,
        model_id: str,
        session_id: str,
        mode: "Mode",
        token_stats: "TokenStats",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.model_id = model_id
        self.session_id = session_id[:8]
        self._mode = mode
        self._token_stats = token_stats

    # --- Public API ---

    def update_state(self, state: AgentFSMState) -> None:
        self.agent_state = state
        self._update_state_label()

    def update_model(self, model_id: str) -> None:
        self.model_id = model_id
        self._update_state_label()

    def update_mode(self, mode: "Mode") -> None:
        self._mode = mode
        try:
            from .mode_indicator import ModeIndicator
            self.query_one(ModeIndicator).mode = mode
        except Exception:
            pass

    def update_token_stats(self, stats: "TokenStats") -> None:
        self._token_stats = stats
        try:
            from .token_meter import TokenMeter
            meter = self.query_one(TokenMeter)
            meter.input_tokens = stats.input_tokens
            meter.output_tokens = stats.output_tokens
            meter.cost_usd = stats.cost_usd
        except Exception:
            pass

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        from .mode_indicator import ModeIndicator
        from .token_meter import TokenMeter
        icon = _STATE_ICON[self.agent_state]
        yield Static(f"{icon} {self._state_label()}", id="state-label")
        yield ModeIndicator(mode=self._mode)
        model_short = self.model_id[:24]
        yield Static(f"  {model_short}", id="model-label")
        yield TokenMeter(
            input_tokens=self._token_stats.input_tokens,
            output_tokens=self._token_stats.output_tokens,
            cost_usd=self._token_stats.cost_usd,
        )
        yield Static(f"  [{self.session_id}]", id="session-label", classes="dim")

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "ctrl+m":
            self.post_message(ModeChangeRequested())
            event.stop()

    # --- Private helpers ---

    def _state_label(self) -> str:
        names = {
            AgentFSMState.IDLE:               "Idle",
            AgentFSMState.THINKING:           "Thinking",
            AgentFSMState.RUNNING_TOOLS:      "Running",
            AgentFSMState.AWAITING_APPROVAL:  "Approval needed",
            AgentFSMState.ERROR:              "Error",
            AgentFSMState.STREAMING:          "Streaming",
        }
        return names[self.agent_state]

    def _update_state_label(self) -> None:
        try:
            icon = _STATE_ICON[self.agent_state]
            label = self.query_one("#state-label", Static)
            label.update(f"{icon} {self._state_label()}")
        except Exception:
            pass
```

### Visual Mockup

```
 ● Thinking   [AUTO]  claude-opus-4-8   1,234 tok · $0.0031  ──  Session abc12345
 ○ Idle        [PLAN]  claude-opus-4-8   4,890 tok · $0.0124  ──  Session abc12345
 ▶ Running     [SAFE]  claude-sonnet-4-6 2,100 tok · $0.0041  ──  Session abc12345
 ✗ Error       [ASK]   claude-opus-4-8   1,100 tok · $0.0028  ──  Session abc12345

[narrow terminal < 80 cols]:
 ● [AUTO] claude-s… 1.2k $0.003 [abc1]
```

### State Model

```python
class AgentFSMState(Enum):
    IDLE              # ○ dim white
    THINKING          # ● yellow
    RUNNING_TOOLS     # ▶ cyan
    AWAITING_APPROVAL # ⚠ bold yellow
    ERROR             # ✗ red
    STREAMING         # ~ green
```

### Rendering Spec

- Single row: `{icon} {state_label}  {mode_badge}  {model_id}  {token_meter}  [{session_id}]`
- Icon colour from `_STATE_STYLE`
- Never wraps: model_id truncated at 24 chars on narrow terminals
- In headless mode: state changes emitted as JSON-line events
- `role="status"` for screen reader live region

### Events Emitted

| Message | When |
|---|---|
| `ModeChangeRequested()` | `Ctrl-M` pressed |

### Events Consumed

| Source | Action |
|---|---|
| `TUIEventAdapter` agent state change | `update_state()` |
| `TUIEventAdapter` token stats | `update_token_stats()` |
| `ModeActivated` | `update_mode()` |

### Error Handling

| Failure | Handling |
|---|---|
| `ModeIndicator` query fails | Log, skip mode update |
| `TokenMeter` query fails | Log, skip token update |
| Terminal too narrow for full bar | Truncate components right-to-left |

### Accessibility

- State conveyed by icon + text label (never icon-only)
- `role="status"` announces changes via `aria-live`
- `Ctrl-M` hint visible in mode footer row

### Unit Tests

```python
# tests/unit/test_agent_status_bar.py

# test_ASB_01: initial_state_idle_shows_circle_icon
#   Assert: state_label Static contains "○"

# test_ASB_02: update_state_thinking_shows_bullet_yellow
#   Action: update_state(AgentFSMState.THINKING)
#   Assert: label contains "●", "Thinking"

# test_ASB_03: update_state_error_shows_cross
#   Action: update_state(AgentFSMState.ERROR)
#   Assert: label contains "✗", "Error"

# test_ASB_04: model_id_truncated_to_24_chars
#   Setup: model_id = "a" * 30
#   Assert: model label shows "a" * 24

# test_ASB_05: session_id_shows_first_8_chars
#   Setup: session_id = "abc12345678"
#   Assert: session label shows "[abc12345]"

# test_ASB_06: ctrl_m_posts_mode_change_requested
#   Action: on_key(KeyEvent("ctrl+m"))
#   Assert: ModeChangeRequested posted

# test_ASB_07: update_mode_updates_mode_indicator
#   Action: update_mode(Mode.PLAN)
#   Assert: ModeIndicator.mode == Mode.PLAN

# test_ASB_08: update_token_stats_delegates_to_token_meter
#   Action: update_token_stats(TokenStats(input=100, output=50, cost=0.002))
#   Assert: TokenMeter.input_tokens == 100
```

---

## Component 8: TokenMeter

**Purpose:** Displays running token count (input + output) and estimated USD cost; warns at budget thresholds; updates at most once per 200ms.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/token_meter.py
from __future__ import annotations

import time
from enum import Enum, auto
from dataclasses import dataclass

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ..messages import BudgetWarning, BudgetExceeded


class BudgetState(Enum):
    NORMAL   = auto()  # < 80% of budget
    WARNING  = auto()  # 80–95%
    CRITICAL = auto()  # > 95%


@dataclass
class TokenStats:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class TokenMeter(Widget):
    """Compact token/cost display with budget warning states."""

    DEFAULT_CSS = """
    TokenMeter { width: auto; }
    TokenMeter.--warning  { color: $mode-plan; }
    TokenMeter.--critical { color: $mode-safe; }
    """

    input_tokens:  reactive[int]   = reactive(0)
    output_tokens: reactive[int]   = reactive(0)
    cost_usd:      reactive[float] = reactive(0.0)

    def __init__(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        budget_tokens: int | None = None,
        show_detail: bool = False,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.budget_tokens = budget_tokens
        self.show_detail = show_detail
        self._budget_state = BudgetState.NORMAL
        self._last_update_time: float = 0.0
        self._warned_80 = False
        self._warned_100 = False

    # --- Public API ---

    def update_stats(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        """Throttled update: at most once per 200ms."""
        now = time.monotonic()
        if now - self._last_update_time < 0.200:
            return
        self._last_update_time = now
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self._check_budget()

    def toggle_detail(self) -> None:
        self.show_detail = not self.show_detail
        self._refresh_display()

    # --- Textual lifecycle ---

    def compose(self):
        yield Static(self._render_text(), id="token-text")

    def watch_input_tokens(self, value: int) -> None:
        self._refresh_display()

    def watch_output_tokens(self, value: int) -> None:
        self._refresh_display()

    def watch_cost_usd(self, value: float) -> None:
        self._refresh_display()

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key in ("enter", "space"):
            self.toggle_detail()
            event.stop()

    # --- Private helpers ---

    def _render_text(self) -> str:
        total = self.input_tokens + self.output_tokens
        if self.show_detail:
            text = (
                f"{self.input_tokens:,} in · "
                f"{self.output_tokens:,} out · "
                f"${self.cost_usd:.4f}"
            )
        else:
            text = f"{total:,} tok · ${self.cost_usd:.4f}"

        suffix = ""
        if self._budget_state == BudgetState.WARNING:
            suffix = " ⚠"
        elif self._budget_state == BudgetState.CRITICAL:
            suffix = " ⛔ near limit"
        return text + suffix

    def _refresh_display(self) -> None:
        try:
            self.query_one("#token-text", Static).update(self._render_text())
        except Exception:
            pass

    def _check_budget(self) -> None:
        if self.budget_tokens is None:
            return
        total = self.input_tokens + self.output_tokens
        fraction = total / self.budget_tokens
        prev = self._budget_state
        if fraction >= 0.95:
            self._budget_state = BudgetState.CRITICAL
            if not self._warned_100:
                self._warned_100 = True
                self.post_message(BudgetExceeded())
        elif fraction >= 0.80:
            self._budget_state = BudgetState.WARNING
            if not self._warned_80:
                self._warned_80 = True
                self.post_message(BudgetWarning(fraction))
        else:
            self._budget_state = BudgetState.NORMAL
        if prev != self._budget_state:
            self.remove_class("--warning", "--critical")
            if self._budget_state == BudgetState.WARNING:
                self.add_class("--warning")
            elif self._budget_state == BudgetState.CRITICAL:
                self.add_class("--critical")
            self._refresh_display()
```

### Visual Mockup

```
Normal:   1,234 tok · $0.0031
Detail:   1,234 in · 567 out · $0.0031
Warning:  9,800 tok · $0.24  ⚠
Critical: 20,000 tok · $0.62 ⛔ near limit
```

### State Model

```python
class BudgetState(Enum):
    NORMAL   = auto()  # < 80% — no warning colour
    WARNING  = auto()  # 80–95% — amber/yellow
    CRITICAL = auto()  # > 95% — red + ⛔ near limit
```

### Rendering Spec

- Normal: `{total:,} tok · ${cost:.4f}`
- Detail mode: `{in:,} in · {out:,} out · ${cost:.4f}`
- Warning: append ` ⚠`, apply `--warning` CSS class (yellow)
- Critical: append ` ⛔ near limit`, apply `--critical` CSS class (red)
- Updates throttled to once per 200ms via `time.monotonic()`
- `aria-label` expands to full text: "1234 input tokens, 567 output tokens, cost 3 cents"

### Events Emitted

| Message | When |
|---|---|
| `BudgetWarning(fraction)` | First crossing of 80% threshold |
| `BudgetExceeded()` | First crossing of 95% threshold |

### Error Handling

| Failure | Handling |
|---|---|
| `budget_tokens=0` | Division by zero guarded with `if self.budget_tokens` |
| `Static` query fails | Silent except in `_refresh_display` |

### Unit Tests

```python
# tests/unit/test_token_meter.py

# test_TM_01: initial_render_shows_zero_tokens
#   Assert: text contains "0 tok"

# test_TM_02: update_stats_refreshes_display
#   Action: update_stats(1000, 500, 0.003)
#   Assert: text shows "1,500 tok · $0.0030"

# test_TM_03: throttle_ignores_rapid_updates
#   Action: update_stats twice within 100ms
#   Assert: Static updated only once (first call)

# test_TM_04: budget_warning_posted_at_80_percent
#   Setup: budget_tokens=10000
#   Action: update_stats(8001, 0, 0.01)
#   Assert: BudgetWarning posted with fraction >= 0.80

# test_TM_05: budget_exceeded_posted_at_95_percent
#   Setup: budget_tokens=10000
#   Action: update_stats(9501, 0, 0.01)
#   Assert: BudgetExceeded posted

# test_TM_06: budget_warning_posted_only_once
#   Action: update_stats to 82%, then 85%, both beyond throttle
#   Assert: BudgetWarning posted exactly once

# test_TM_07: toggle_detail_shows_split_view
#   Action: toggle_detail()
#   Assert: text contains " in · " and " out · "

# test_TM_08: critical_state_adds_css_class
#   Setup: budget_tokens=1000
#   Action: update_stats(960, 0, 0.01)
#   Assert: has_class("--critical") == True
```

---

## Component 9: ModeIndicator

**Purpose:** Coloured badge showing the active operating mode; acts as hit-target for mode picker and keyboard mode cycling.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/mode_indicator.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button

from ..messages import ModeActivated, ModeCycleRequested, ModePickerRequested


class Mode(Enum):
    AUTO   = "AUTO"
    PLAN   = "PLAN"
    ASK    = "ASK"
    REVIEW = "REVIEW"
    SAFE   = "SAFE"
    DEBUG  = "DEBUG"

    @property
    def css_class(self) -> str:
        return f"--{self.value.lower()}"

    @property
    def label(self) -> str:
        return f"[{self.value}]"

    @property
    def colour_style(self) -> str:
        return {
            Mode.AUTO:   "bold green",
            Mode.PLAN:   "bold yellow",
            Mode.ASK:    "bold cyan",
            Mode.REVIEW: "bold blue",
            Mode.SAFE:   "bold red",
            Mode.DEBUG:  "bold magenta",
        }[self]


_MODE_ORDER = [Mode.AUTO, Mode.PLAN, Mode.ASK, Mode.REVIEW, Mode.SAFE, Mode.DEBUG]


class ModeIndicator(Widget):
    """Coloured mode badge. Cycling via Ctrl-M; picker via click."""

    DEFAULT_CSS = """
    ModeIndicator {
        width: auto;
        min-width: 8;
        height: 1;
    }
    """

    mode: reactive[Mode] = reactive(Mode.AUTO)

    def __init__(
        self,
        mode: Mode = Mode.AUTO,
        all_modes: list[Mode] | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.mode = mode
        self.all_modes = all_modes or _MODE_ORDER

    # --- Public API ---

    def cycle_forward(self) -> None:
        idx = self.all_modes.index(self.mode)
        new_mode = self.all_modes[(idx + 1) % len(self.all_modes)]
        self.mode = new_mode
        self.post_message(ModeActivated(new_mode))
        self.post_message(ModeCycleRequested(+1))

    def cycle_backward(self) -> None:
        idx = self.all_modes.index(self.mode)
        new_mode = self.all_modes[(idx - 1) % len(self.all_modes)]
        self.mode = new_mode
        self.post_message(ModeActivated(new_mode))
        self.post_message(ModeCycleRequested(-1))

    # --- Textual lifecycle ---

    def compose(self):
        from textual.widgets import Static
        yield Static(self.mode.label, id="mode-badge")

    def watch_mode(self, new_mode: Mode) -> None:
        for m in Mode:
            self.remove_class(m.css_class)
        self.add_class(new_mode.css_class)
        try:
            from textual.widgets import Static
            self.query_one("#mode-badge", Static).update(new_mode.label)
        except Exception:
            pass

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "ctrl+m":
            self.cycle_forward()
            event.stop()
        elif event.key == "shift+ctrl+m":
            self.cycle_backward()
            event.stop()

    def on_click(self) -> None:
        self.post_message(ModePickerRequested())
```

### Visual Mockup

```
[AUTO]      ← green bold
[PLAN]      ← yellow bold
[ASK]       ← cyan bold
[REVIEW]    ← blue bold
[SAFE]      ← red bold
[DEBUG]     ← magenta bold
```

### State Model

Single reactive `mode: Mode`. CSS class applied per mode for colour.

### Rendering Spec

- Badge: `[{MODE}]` in brackets, 8 char min-width
- Colour via CSS class `--auto`, `--plan`, etc. → maps to colour tokens
- Max label 6 chars; plugin modes truncated with `…`
- NO_COLOR: brackets preserved, colour stripped

### Events Emitted

| Message | When |
|---|---|
| `ModeActivated(mode)` | Mode changes |
| `ModeCycleRequested(+1/-1)` | Cycle key pressed |
| `ModePickerRequested()` | Click or tooltip key |

### Unit Tests

```python
# test_MI_01: initial_mode_auto_shows_auto_badge
# test_MI_02: cycle_forward_advances_auto_to_plan
# test_MI_03: cycle_forward_wraps_debug_to_auto
# test_MI_04: cycle_backward_from_auto_goes_to_debug
# test_MI_05: watch_mode_updates_badge_text
# test_MI_06: watch_mode_swaps_css_class
# test_MI_07: ctrl_m_calls_cycle_forward
# test_MI_08: click_posts_mode_picker_requested
```

---

## Component 10: InputBar

**Purpose:** Multi-line CBREAK text entry; detects @mention and /command triggers; submits on Enter.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/input_bar.py
from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import TextArea, Static

from ..messages import (
    MessageSubmitted,
    TriggerDetected,
    TriggerDismissed,
    InputChanged,
)

if TYPE_CHECKING:
    from ..transcript import Mention
    from .mode_indicator import Mode


class InputBarState(Enum):
    IDLE             = auto()
    TYPING           = auto()
    MENTION_TRIGGER  = auto()
    COMMAND_TRIGGER  = auto()
    SUBMITTING       = auto()
    BLOCKED          = auto()
    MULTILINE        = auto()


class InputBar(Widget):
    """Primary text entry widget with trigger detection."""

    BINDINGS = [
        Binding("enter",       "submit",         "Send",        show=True),
        Binding("shift+enter", "newline",        "New line",    show=False),
        Binding("escape",      "dismiss_or_clear","Dismiss/Clear",show=False),
        Binding("ctrl+k",      "kill_line",      "Clear input", show=False),
        Binding("up",          "history_up",     "History up",  show=False),
        Binding("down",        "history_down",   "History down",show=False),
        Binding("tab",         "accept_completion","Accept",     show=False),
    ]

    DEFAULT_CSS = """
    InputBar {
        dock: bottom;
        height: auto;
        max-height: 8;
        min-height: 3;
        border: round $color-tool-border;
    }
    InputBar.--blocked {
        border: round $status-idle;
        opacity: 0.5;
    }
    InputBar Static.prompt { color: $status-idle; width: 2; }
    """

    state: reactive[InputBarState] = reactive(InputBarState.IDLE)
    agent_ready: reactive[bool] = reactive(True)

    def __init__(
        self,
        placeholder: str = "Message AgentHICC…",
        agent_ready: bool = True,
        current_mode: "Mode | None" = None,
        max_height_lines: int = 8,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.placeholder = placeholder
        self.agent_ready = agent_ready
        self.current_mode = current_mode
        self.max_height_lines = max_height_lines
        self._history: list[str] = []
        self._history_index: int = -1
        self._mentions: list[Mention] = []

    # --- Public API ---

    def set_blocked(self, blocked: bool) -> None:
        self.agent_ready = not blocked
        if blocked:
            self.state = InputBarState.BLOCKED
            self.add_class("--blocked")
            try:
                self.query_one(TextArea).disabled = True
            except Exception:
                pass
        else:
            self.state = InputBarState.IDLE
            self.remove_class("--blocked")
            try:
                self.query_one(TextArea).disabled = False
                self.query_one(TextArea).focus()
            except Exception:
                pass

    def clear(self) -> None:
        try:
            ta = self.query_one(TextArea)
            ta.load_text("")
        except Exception:
            pass
        self._mentions = []
        self.state = InputBarState.IDLE

    def insert_completion(self, value: str, kind: str) -> None:
        """Replace trigger text with completion value."""
        try:
            ta = self.query_one(TextArea)
            text = ta.text
            cursor_pos = ta.cursor_location
            # Find trigger start and replace
            if kind == "mention":
                trigger_char = "@"
            else:
                trigger_char = "/"
            # Walk back from cursor to find trigger
            flat_pos = self._location_to_offset(text, cursor_pos)
            at_idx = text.rfind(trigger_char, 0, flat_pos)
            if at_idx >= 0:
                new_text = text[:at_idx] + value + text[flat_pos:]
                ta.load_text(new_text)
        except Exception:
            pass

    def add_mention(self, mention: "Mention") -> None:
        self._mentions.append(mention)

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        yield Static("> ", classes="prompt")
        yield TextArea(
            "",
            language=None,
            theme="monokai",
            id="input-textarea",
            classes="input-area",
        )
        mode_label = f"[{self.current_mode.value}]" if self.current_mode else ""
        yield Static(
            f"{mode_label}  ↵ to send",
            id="input-hint",
            classes="dim",
        )

    def on_text_area_changed(self, event: "TextArea.Changed") -> None:
        text = event.text_area.text
        self.post_message(InputChanged(text))
        self._detect_triggers(text, event.text_area.cursor_location)
        if "\n" in text:
            self.state = InputBarState.MULTILINE
        elif self.state not in (InputBarState.BLOCKED, InputBarState.SUBMITTING):
            self.state = InputBarState.TYPING

    # --- Action handlers ---

    def action_submit(self) -> None:
        if not self.agent_ready:
            return
        try:
            ta = self.query_one(TextArea)
            text = ta.text.strip()
        except Exception:
            return
        if not text:
            return
        self._history.append(text)
        self._history_index = -1
        self.state = InputBarState.SUBMITTING
        from ..transcript import parse_mentions
        mentions = parse_mentions(text)
        self.post_message(MessageSubmitted(text=text, mentions=mentions))
        self.clear()

    def action_newline(self) -> None:
        try:
            ta = self.query_one(TextArea)
            ta.insert("\n")
        except Exception:
            pass

    def action_dismiss_or_clear(self) -> None:
        if self.state in (InputBarState.MENTION_TRIGGER, InputBarState.COMMAND_TRIGGER):
            self.post_message(TriggerDismissed())
            self.state = InputBarState.TYPING
        else:
            self.clear()

    def action_kill_line(self) -> None:
        try:
            ta = self.query_one(TextArea)
            ta.load_text("")
        except Exception:
            pass

    def action_history_up(self) -> None:
        if not self._history:
            return
        try:
            ta = self.query_one(TextArea)
        except Exception:
            return
        if ta.text.strip():
            return  # Only recall when input is empty
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            ta.load_text(self._history[-(self._history_index + 1)])

    def action_history_down(self) -> None:
        try:
            ta = self.query_one(TextArea)
        except Exception:
            return
        if self._history_index > 0:
            self._history_index -= 1
            ta.load_text(self._history[-(self._history_index + 1)])
        elif self._history_index == 0:
            self._history_index = -1
            ta.load_text("")

    def action_accept_completion(self) -> None:
        # Delegate to parent App which coordinates with TriggerDropdown
        pass  # App.on_tab_in_input handles this

    # --- Private helpers ---

    def _detect_triggers(self, text: str, cursor_location: tuple[int, int]) -> None:
        if not text:
            return
        flat = self._location_to_offset(text, cursor_location)
        stripped = text.lstrip()
        if stripped.startswith("/") and flat > 0:
            after_slash = stripped[1:flat]
            self.state = InputBarState.COMMAND_TRIGGER
            self.post_message(TriggerDetected("command", after_slash, flat))
            return
        if flat > 0 and text[flat - 1] == "@":
            before = text[:flat - 1]
            if not before or not before[-1].isalnum():
                self.state = InputBarState.MENTION_TRIGGER
                self.post_message(TriggerDetected("mention", "", flat))
                return

    @staticmethod
    def _location_to_offset(text: str, loc: tuple[int, int]) -> int:
        row, col = loc
        lines = text.split("\n")
        offset = sum(len(lines[i]) + 1 for i in range(min(row, len(lines))))
        return offset + col
```

### Visual Mockup

```
╔══════════════════════════════════════════════════════════════╗
║ > Fix the lint errors in [src/utils.py] and [tests/]        ║
║   then run the test suite                                    ║
║                                          [Auto] ↵ to send   ║
╚══════════════════════════════════════════════════════════════╝

[BLOCKED state]:
╔══════════════════════════════════════════════════════════════╗  ← dim border
║ > (agent is working…)                                       ║
╚══════════════════════════════════════════════════════════════╝
```

### State Model

```python
class InputBarState(Enum):
    IDLE             # Empty; no trigger active
    TYPING           # User entering text
    MENTION_TRIGGER  # @ detected; dropdown shown
    COMMAND_TRIGGER  # / at position 0; dropdown shown
    SUBMITTING       # Enter pressed; sending
    BLOCKED          # Agent busy; input disabled
    MULTILINE        # Text contains newline(s)
```

### Rendering Spec

- Prompt: `> ` in dim (`$status-idle`)
- `TextArea` with `language=None` (no syntax highlighting)
- Hint footer: `[{mode}]  ↵ to send` dim
- BLOCKED: `opacity: 0.5`, `disabled=True` on TextArea, `aria-disabled="true"`
- `@` trigger: only when preceding char is not alphanumeric (avoids email false positives)

### Events Emitted

| Message | Payload | When |
|---|---|---|
| `MessageSubmitted` | `text, mentions` | Enter (agent_ready) |
| `TriggerDetected` | `kind, fragment, cursor_pos` | `@` or `/` typed |
| `TriggerDismissed` | — | Escape while trigger active |
| `InputChanged` | `text` | Any keystroke |

### Unit Tests

```python
# test_IB_01: enter_key_submits_when_agent_ready
# test_IB_02: enter_key_blocked_when_not_agent_ready
# test_IB_03: at_trigger_posts_trigger_detected_mention
# test_IB_04: at_trigger_ignored_in_email_context
# test_IB_05: slash_trigger_at_position_zero_posts_command_trigger
# test_IB_06: set_blocked_disables_textarea
# test_IB_07: history_up_recalls_previous_message
# test_IB_08: escape_dismisses_trigger_then_clears
# test_IB_09: shift_enter_inserts_newline
# test_IB_10: ctrl_k_clears_text
```

---

## Component 11: TriggerDropdown

**Purpose:** Floating overlay providing type-ahead completions for @mention (files/dirs) and /command; keyboard-navigable; appears above InputBar.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/trigger_dropdown.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option, Separator

from ..messages import CompletionAccepted, CompletionDismissed, CompletionHighlighted

if TYPE_CHECKING:
    pass


class DropdownMode(Enum):
    HIDDEN   = auto()
    MENTION  = auto()
    COMMAND  = auto()
    LOADING  = auto()


@dataclass
class Completion:
    value: str
    label: str
    kind: str       # "file" | "directory" | "glob" | "command" | "group"
    description: str = ""
    group: str = ""


class TriggerDropdown(Widget):
    """Floating completion overlay for @ and / triggers."""

    DEFAULT_CSS = """
    TriggerDropdown {
        layer: above;
        display: none;
        max-height: 10;
        border: round $color-tool-border;
        background: $color-user-bg;
        width: 60;
    }
    TriggerDropdown.--visible { display: block; }
    """

    mode: reactive[DropdownMode] = reactive(DropdownMode.HIDDEN)

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._completions: list[Completion] = []
        self._selected_index: int = 0

    # --- Public API ---

    def show_mention(self, fragment: str, completions: list[Completion]) -> None:
        self.mode = DropdownMode.MENTION
        self._completions = completions[:8]
        self._selected_index = 0
        self.add_class("--visible")
        self._populate()

    def show_command(self, fragment: str, completions: list[Completion]) -> None:
        self.mode = DropdownMode.COMMAND
        self._completions = completions[:8]
        self._selected_index = 0
        self.add_class("--visible")
        self._populate()

    def show_loading(self) -> None:
        self.mode = DropdownMode.LOADING
        self.add_class("--visible")

    def hide(self) -> None:
        self.mode = DropdownMode.HIDDEN
        self.remove_class("--visible")

    def navigate(self, direction: int) -> None:
        """Move selection up (-1) or down (+1)."""
        if not self._completions:
            return
        self._selected_index = (
            (self._selected_index + direction) % len(self._completions)
        )
        self._highlight_selected()
        self.post_message(CompletionHighlighted(self._completions[self._selected_index].value))

    def accept(self) -> None:
        if self._completions:
            c = self._completions[self._selected_index]
            self.post_message(CompletionAccepted(c.value, c.kind))
            self.hide()

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        yield Static("", id="dd-header")
        yield OptionList(id="dd-list")

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "escape":
            self.post_message(CompletionDismissed())
            self.hide()
            event.stop()
        elif event.key in ("up", "shift+tab"):
            self.navigate(-1)
            event.stop()
        elif event.key in ("down", "tab"):
            self.navigate(+1)
            event.stop()
        elif event.key == "enter":
            self.accept()
            event.stop()

    # --- Private helpers ---

    def _populate(self) -> None:
        try:
            option_list = self.query_one("#dd-list", OptionList)
            option_list.clear_options()
            header_text = "@mention" if self.mode == DropdownMode.MENTION else "/command"
            self.query_one("#dd-header", Static).update(f"─ {header_text} ─")
            current_group = ""
            for c in self._completions:
                if c.group and c.group != current_group:
                    option_list.add_option(Separator())
                    option_list.add_option(Option(f"── {c.group} ──", disabled=True))
                    current_group = c.group
                label = f"{c.label:<30} {c.description:<30} {c.kind}"
                option_list.add_option(Option(label, id=c.value))
        except Exception:
            pass

    def _highlight_selected(self) -> None:
        try:
            option_list = self.query_one("#dd-list", OptionList)
            option_list.highlighted = self._selected_index
        except Exception:
            pass
```

### Visual Mockup

```
╔═ @mention ════════════════════════════════════════╗
║ src/auth.py          file        12.3 KB           ║ ← highlighted
║ src/auth_utils.py    file         4.1 KB           ║
║ src/                 directory   (34 files)         ║
╚═══════════════════════════════════════════════════╝
  ↑↓ navigate · Tab accept · Esc dismiss

╔═ /command ════════════════════════════════════════╗
║ /help     Show available commands    Built-in      ║ ← highlighted
║ /model    Switch active model        Built-in      ║
║ ─── Skills ───────────────────────────────────── ║
║ /deep-research  Deep research harness  Skills     ║
╚═══════════════════════════════════════════════════╝
```

### State Model

```python
class DropdownMode(Enum):
    HIDDEN   # Not visible; display:none
    MENTION  # Showing file/dir completions
    COMMAND  # Showing command completions
    LOADING  # Async scan in progress
```

### Rendering Spec

- Width: 60 chars; max 8 items; scroll within for more
- Header: `─ @mention ─` or `─ /command ─`
- Each row: `{label:<30} {description:<30} {kind}`
- Group separators: `── {group_name} ──` as disabled Option
- Selected item: `OptionList.highlighted` index
- `role="listbox"`, items `role="option"`, `aria-activedescendant`

### Events Emitted

| Message | When |
|---|---|
| `CompletionAccepted(value, kind)` | Enter/Tab selects |
| `CompletionDismissed()` | Escape pressed |
| `CompletionHighlighted(value)` | Navigation changes selection |

### Unit Tests

```python
# test_TD_01: show_mention_makes_widget_visible
# test_TD_02: hide_removes_visible_class
# test_TD_03: navigate_down_increments_index
# test_TD_04: navigate_wraps_at_end
# test_TD_05: accept_posts_completion_accepted_and_hides
# test_TD_06: escape_posts_dismissed_and_hides
# test_TD_07: completions_capped_at_8
# test_TD_08: group_headers_rendered_as_disabled_options
```

---

## Component 12: ApprovalRequest

**Purpose:** Inline approval gate blocking agent until user confirms or denies; shows diff preview when applicable.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/approval_request.py
from __future__ import annotations

import asyncio
from enum import Enum, auto
from typing import Callable, Awaitable

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, Button
from textual.containers import Horizontal

from ..messages import ApprovalGranted, ApprovalDenied, ApprovalAllGranted


class ApprovalState(Enum):
    PENDING     = auto()
    CONFIRMED   = auto()
    DENIED      = auto()
    ALLOWED_ALL = auto()
    TIMEOUT     = auto()


class ApprovalRequest(Widget):
    """Inline approval gate. Captures focus; blocks InputBar."""

    COMPONENT_CLASSES = {"approval-request--header"}
    DEFAULT_CSS = """
    ApprovalRequest {
        border: round $status-approval;
        padding: 1;
        margin: 0 0 1 0;
    }
    ApprovalRequest.--high-risk { border: round $mode-safe; }
    ApprovalRequest.--low-risk  { border: round $mode-auto; }
    """

    CAN_FOCUS = True

    state: reactive[ApprovalState] = reactive(ApprovalState.PENDING)

    def __init__(
        self,
        request_id: str,
        tool_name: str,
        description: str,
        proposed_diff: str | None = None,
        command_text: str | None = None,
        risk_level: str = "medium",
        timeout_s: int | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.request_id = request_id
        self.tool_name = tool_name
        self.description = description
        self.proposed_diff = proposed_diff
        self.command_text = command_text
        self.risk_level = risk_level
        self.timeout_s = timeout_s
        self._timeout_task: asyncio.Task | None = None

    # --- Public API ---

    def confirm(self) -> None:
        if self.state != ApprovalState.PENDING:
            return
        self.state = ApprovalState.CONFIRMED
        self.post_message(ApprovalGranted(self.request_id))
        self._cancel_timeout()

    def deny(self, reason: str = "user denied") -> None:
        if self.state != ApprovalState.PENDING:
            return
        self.state = ApprovalState.DENIED
        self.post_message(ApprovalDenied(self.request_id, reason))
        self._cancel_timeout()

    def allow_all(self) -> None:
        if self.state != ApprovalState.PENDING:
            return
        self.state = ApprovalState.ALLOWED_ALL
        self.post_message(ApprovalAllGranted(self.tool_name))
        self._cancel_timeout()

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        yield Static(
            f"⚠  {self.tool_name} wants to {self.description}",
            classes="approval-request--header",
        )
        if self.proposed_diff:
            from .diff_viewer import DiffViewer
            yield DiffViewer(diff_text=self.proposed_diff, collapsed=True)
        if self.command_text:
            yield Static(f"    {self.command_text}", classes="command-preview")
        yield Horizontal(
            Button("[Y] Allow",   id="btn-allow",    variant="success"),
            Button("[N] Deny",    id="btn-deny",     variant="error"),
            Button("[A] Allow all", id="btn-allow-all", variant="warning"),
            classes="approval-buttons",
        )

    def on_mount(self) -> None:
        self.focus()
        if self.risk_level == "high":
            self.add_class("--high-risk")
        elif self.risk_level == "low":
            self.add_class("--low-risk")
        if self.timeout_s is not None:
            self._timeout_task = asyncio.create_task(self._auto_timeout())

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "y":
            self.confirm()
            event.stop()
        elif event.key in ("n", "escape"):
            self.deny()
            event.stop()
        elif event.key == "a":
            self.allow_all()
            event.stop()

    def on_button_pressed(self, event: "Button.Pressed") -> None:
        if event.button.id == "btn-allow":
            self.confirm()
        elif event.button.id == "btn-deny":
            self.deny()
        elif event.button.id == "btn-allow-all":
            self.allow_all()

    # --- Private helpers ---

    async def _auto_timeout(self) -> None:
        assert self.timeout_s is not None
        await asyncio.sleep(self.timeout_s)
        if self.state == ApprovalState.PENDING:
            self.state = ApprovalState.TIMEOUT
            self.post_message(ApprovalDenied(self.request_id, "timeout"))

    def _cancel_timeout(self) -> None:
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
```

### Visual Mockup

```
╔══ Approval Required ════════════════════════════════════╗
║  ⚠  write_file wants to modify src/auth.py             ║
║                                                         ║
║  ┌─ proposed change ───────────────────────────────┐   ║
║  │ @@ -85,7 +85,7 @@                              │   ║
║  │ -     return verify(token)                      │   ║
║  │ +     return await verify(token)                │   ║
║  └──────────────────────────────────────────────────┘  ║
║                                                         ║
║  [Y] Allow    [N] Deny    [A] Allow all (session)      ║
╚═════════════════════════════════════════════════════════╝
```

### State Model

```python
class ApprovalState(Enum):
    PENDING     # Waiting; InputBar blocked
    CONFIRMED   # Allowed; agent proceeds
    DENIED      # Denied; agent receives denial
    ALLOWED_ALL # Session-wide allow for this tool
    TIMEOUT     # Auto-denied after timeout_s
```

### Accessibility

- `role="alertdialog"`, `aria-modal="true"`
- Focus trapped inside widget until resolved
- Risk level conveyed as text AND border colour
- Countdown announced via `aria-live="assertive"`

### Unit Tests

```python
# test_AR_01: initial_state_pending_and_focused
# test_AR_02: y_key_calls_confirm_posts_granted
# test_AR_03: n_key_calls_deny_posts_denied
# test_AR_04: a_key_calls_allow_all_posts_all_granted
# test_AR_05: escape_key_denies_with_reason
# test_AR_06: high_risk_adds_high_risk_css_class
# test_AR_07: timeout_auto_denies_after_seconds
# test_AR_08: second_confirm_ignored_after_first
# test_AR_09: diff_viewer_mounted_when_proposed_diff_provided
# test_AR_10: command_text_shown_for_bash_tools
```

---

## Component 13: ProgressIndicator

**Purpose:** Compact spinner (indeterminate) or bounded bar (determinate) inside ToolCallBlock while tool executes.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/progress_indicator.py
from __future__ import annotations

from enum import Enum, auto

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import ProgressBar, Static


class ProgressMode(Enum):
    INDETERMINATE = auto()
    DETERMINATE   = auto()
    COMPLETE      = auto()
    ERROR         = auto()


_SPINNER_FRAMES = ["◐", "◑", "◒", "◓"]
_SPINNER_INTERVAL = 0.125  # 8 fps


class ProgressIndicator(Widget):
    """Animated progress indicator: spinner or bounded bar."""

    DEFAULT_CSS = """
    ProgressIndicator { height: 1; }
    ProgressIndicator.--complete { color: $mode-auto; }
    ProgressIndicator.--error    { color: $mode-safe; }
    """

    mode: reactive[ProgressMode] = reactive(ProgressMode.INDETERMINATE)
    value: reactive[float] = reactive(0.0)

    def __init__(
        self,
        mode: str = "indeterminate",
        value: float | None = None,
        label: str = "running…",
        width: int = 20,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.mode = (
            ProgressMode.DETERMINATE if mode == "determinate" else ProgressMode.INDETERMINATE
        )
        self.value = value or 0.0
        self.label = label
        self.bar_width = width
        self._frame_index: int = 0
        self._timer: Timer | None = None

    # --- Public API ---

    def start(self) -> None:
        if self.mode == ProgressMode.INDETERMINATE and self._timer is None:
            self._timer = self.set_interval(_SPINNER_INTERVAL, self._tick)

    def stop(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None

    def set_progress(self, value: float) -> None:
        self.value = max(0.0, min(1.0, value))
        if self.mode == ProgressMode.INDETERMINATE:
            self.mode = ProgressMode.DETERMINATE

    def complete(self) -> None:
        self.stop()
        self.mode = ProgressMode.COMPLETE
        self.add_class("--complete")
        self.set_timer(0.5, self.remove)

    def error(self) -> None:
        self.stop()
        self.mode = ProgressMode.ERROR
        self.add_class("--error")

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        if self.mode == ProgressMode.DETERMINATE:
            yield ProgressBar(id="prog-bar")
            yield Static(self.label, id="prog-label")
        else:
            yield Static(f"{_SPINNER_FRAMES[0]}  {self.label}", id="spinner-text")

    def on_mount(self) -> None:
        self.start()

    def on_unmount(self) -> None:
        self.stop()

    # --- Private helpers ---

    def _tick(self) -> None:
        self._frame_index = (self._frame_index + 1) % len(_SPINNER_FRAMES)
        try:
            self.query_one("#spinner-text", Static).update(
                f"{_SPINNER_FRAMES[self._frame_index]}  {self.label}"
            )
        except Exception:
            pass
```

### Visual Mockup

```
Indeterminate:  ◑  running…
Determinate:    [████████████░░░░░░░░]  64%  (writing 3 of 5 files)
Complete:       [████████████████████]  ← green for 500ms then removed
Error:          ✗  failed              ← red, stays
```

### State Model

```python
class ProgressMode(Enum):
    INDETERMINATE  # No %; spinner cycling at 8 fps
    DETERMINATE    # 0–100% known; progress bar
    COMPLETE       # Full bar; green 500ms then auto-unmounts
    ERROR          # Bar turns red; stops
```

### Unit Tests

```python
# test_PI_01: indeterminate_shows_spinner_frame
# test_PI_02: spinner_advances_frame_on_tick
# test_PI_03: set_progress_switches_to_determinate_mode
# test_PI_04: complete_schedules_remove_after_500ms
# test_PI_05: error_adds_error_css_class
# test_PI_06: timer_stopped_on_unmount
# test_PI_07: value_clamped_between_0_and_1
# test_PI_08: spinner_cycles_all_four_frames
```

---

## Component 14: NotificationToast

**Purpose:** Transient self-dismissing notification banner; appears at top; non-blocking.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/notification_toast.py
from __future__ import annotations

import asyncio
from enum import Enum, auto

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ..messages import ToastDismissed


class NotifLevel(Enum):
    INFO    = auto()
    SUCCESS = auto()
    WARNING = auto()
    ERROR   = auto()


_NOTIF_ICON = {
    NotifLevel.INFO:    "ℹ",
    NotifLevel.SUCCESS: "✓",
    NotifLevel.WARNING: "⚠",
    NotifLevel.ERROR:   "✗",
}

_NOTIF_CSS = {
    NotifLevel.INFO:    "--info",
    NotifLevel.SUCCESS: "--success",
    NotifLevel.WARNING: "--warning",
    NotifLevel.ERROR:   "--error",
}

_DEFAULT_DURATION_MS = 3_000


class NotificationToast(Widget):
    """Self-dismissing notification banner."""

    DEFAULT_CSS = """
    NotificationToast {
        dock: top;
        height: auto;
        border: round $color-tool-border;
        padding: 0 1;
    }
    NotificationToast.--info    { border: round $toast-info; }
    NotificationToast.--success { border: round $toast-success; }
    NotificationToast.--warning { border: round $toast-warning; }
    NotificationToast.--error   { border: round $toast-error; }
    """

    def __init__(
        self,
        toast_id: str,
        message: str,
        level: NotifLevel = NotifLevel.INFO,
        duration_ms: int = _DEFAULT_DURATION_MS,
        detail: str | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.toast_id = toast_id
        self.message = message
        self.level = level
        self.duration_ms = duration_ms
        self.detail = detail
        self._dismiss_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        icon = _NOTIF_ICON[self.level]
        yield Static(f"{icon}  {self.message}", id="toast-main")
        if self.detail:
            yield Static(f"   {self.detail}", id="toast-detail", classes="dim")

    def on_mount(self) -> None:
        self.add_class(_NOTIF_CSS[self.level])
        if self.duration_ms > 0:
            self._dismiss_task = asyncio.create_task(self._auto_dismiss())

    def on_unmount(self) -> None:
        if self._dismiss_task and not self._dismiss_task.done():
            self._dismiss_task.cancel()

    def dismiss(self) -> None:
        self.post_message(ToastDismissed(self.toast_id))
        try:
            self.remove()
        except Exception:
            pass

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "escape":
            self.dismiss()
            event.stop()

    async def _auto_dismiss(self) -> None:
        await asyncio.sleep(self.duration_ms / 1000.0)
        self.dismiss()
```

### Visual Mockup

```
╔═ ℹ  Mode changed to PLAN ═══════════════╗  ← INFO (blue border)
╚══════════════════════════════════════════╝

╔═ ⚠  Approaching token budget (82%) ═════╗  ← WARNING (yellow)
╚══════════════════════════════════════════╝

╔═ ✗  Plugin "custom-tools" failed ════════╗  ← ERROR (red)
║  ImportError: missing dependency 'httpx'  ║
╚══════════════════════════════════════════╝
```

### State Model

```python
class NotifLevel(Enum):
    INFO     # dim white border + ℹ
    SUCCESS  # green border + ✓
    WARNING  # yellow border + ⚠
    ERROR    # red border + ✗
```

### Accessibility

- `role="alert"` for WARNING/ERROR; `role="status"` for INFO/SUCCESS
- `aria-live="assertive"` for ERROR; `"polite"` for others
- Sticky (`duration_ms=0`) must be manually dismissible via Escape

### Unit Tests

```python
# test_NT_01: info_level_shows_info_icon_and_blue_border_class
# test_NT_02: auto_dismiss_fires_after_duration_ms
# test_NT_03: duration_zero_does_not_auto_dismiss
# test_NT_04: escape_key_dismisses_immediately
# test_NT_05: dismiss_posts_toast_dismissed_message
# test_NT_06: detail_line_rendered_when_provided
# test_NT_07: error_level_adds_error_css_class
# test_NT_08: max_three_toasts_stack_managed_by_app
```

---

## Component 15: SessionHeader

**Purpose:** Single-line header pinned at top showing cwd, session ID, time, and current intent.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/session_header.py
from __future__ import annotations

import datetime
from pathlib import Path

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class SessionHeader(Widget):
    """Single-line top chrome. Always 1 row."""

    DEFAULT_CSS = """
    SessionHeader {
        dock: top;
        height: 1;
        background: $color-user-bg;
        color: $status-idle;
        padding: 0 1;
    }
    """

    current_intent: reactive[str | None] = reactive(None)

    def __init__(
        self,
        cwd: Path,
        session_id: str,
        started_at: datetime.datetime,
        current_intent: str | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.cwd = cwd
        self.session_id = session_id
        self._short_session_id = session_id[:8]
        self.started_at = started_at
        self.current_intent = current_intent

    def compose(self):
        yield Static(self._render(), id="header-text")

    def watch_current_intent(self, intent: str | None) -> None:
        try:
            self.query_one("#header-text", Static).update(self._render())
        except Exception:
            pass

    def on_click(self) -> None:
        import pyperclip  # type: ignore[import]
        try:
            pyperclip.copy(self.session_id)
        except Exception:
            pass

    def _render(self) -> str:
        ts = self.started_at.strftime("%H:%M")
        parts = [
            "AgentHICC",
            str(self.cwd),
            f"Session {self._short_session_id}",
            ts,
        ]
        if self.current_intent:
            intent = self.current_intent[:40]
            parts.append(intent)
        return "  ·  ".join(parts)
```

### Visual Mockup

```
AgentHICC  /home/user/myproject  ·  Session abc12345  ·  11:38  ·  Fix auth bug

[narrow terminal]:
AgentHICC  /myproject  ·  abc12345
```

### Unit Tests

```python
# test_SH_01: renders_all_parts_separated_by_dots
# test_SH_02: intent_truncated_at_40_chars
# test_SH_03: session_id_shows_first_8_chars
# test_SH_04: watch_intent_updates_static
# test_SH_05: click_copies_full_session_id
# test_SH_06: no_intent_omits_intent_segment
```

---

## Component 16: ThinkingIndicator

**Purpose:** Animated "Thinking…" label shown in AgentMessage between turn start and first token.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/thinking_indicator.py
from __future__ import annotations

from enum import Enum, auto

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static


_DEFAULT_FRAMES = ["◐", "◑", "◒", "◓"]
_DEFAULT_LABEL = "Thinking…"
_DEFAULT_INTERVAL_MS = 200


class ThinkingState(Enum):
    ACTIVE = auto()
    HIDDEN = auto()


class ThinkingIndicator(Widget):
    """Animated thinking label. Unmounted (not hidden) on first token."""

    DEFAULT_CSS = """
    ThinkingIndicator {
        color: $mode-plan;
        height: 1;
    }
    """

    state: reactive[ThinkingState] = reactive(ThinkingState.ACTIVE)

    def __init__(
        self,
        spinner_frames: list[str] | None = None,
        label: str = _DEFAULT_LABEL,
        interval_ms: int = _DEFAULT_INTERVAL_MS,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.spinner_frames = spinner_frames or _DEFAULT_FRAMES
        self.label = label
        self.interval_ms = interval_ms
        self._frame_index = 0
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static(f"{self.spinner_frames[0]}  {self.label}", id="thinking-text")

    def on_mount(self) -> None:
        self._timer = self.set_interval(
            self.interval_ms / 1000.0, self._tick
        )

    def on_unmount(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._frame_index = (self._frame_index + 1) % len(self.spinner_frames)
        try:
            self.query_one("#thinking-text", Static).update(
                f"{self.spinner_frames[self._frame_index]}  {self.label}"
            )
        except Exception:
            pass
```

### Visual Mockup

```
AGENT  11:43
─────────────────────────────────────
  ◐ Thinking…    ← ACTIVE (animating)
  ◑ Thinking…    ← next frame 200ms later

[After first token: widget unmounted, StreamingCursor appears]
  I have found the issue…▌
```

### State Model

```python
class ThinkingState(Enum):
    ACTIVE  # Animated; waiting for first token
    HIDDEN  # Unmounted from DOM (not display:none)
```

Note: transition to HIDDEN is done by `AgentMessage._remove_thinking_indicator()` which calls `.remove()`.

### Accessibility

- `aria-live="polite"` announces "Thinking" once on mount
- Spinner is character sequence (not CSS animation)
- Label customisable for extended thinking: `"Thinking (extended)…"` with cyan colour

### Unit Tests

```python
# test_TI_01: initial_text_shows_first_frame_and_label
# test_TI_02: tick_advances_to_next_frame
# test_TI_03: tick_wraps_after_last_frame
# test_TI_04: timer_stopped_on_unmount
# test_TI_05: custom_frames_used_when_provided
# test_TI_06: custom_label_shown
# test_TI_07: interval_ms_respected_in_set_interval_call
# test_TI_08: all_four_default_frames_cycle_correctly
```

---

## Component 17: CommandPalette

**Purpose:** Full-screen modal overlay (Ctrl-/ or Ctrl-P) providing fuzzy-search access to all registered slash commands across all sources.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/command_palette.py
from __future__ import annotations

import difflib
from enum import Enum, auto
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option, Separator

from ..messages import CommandSelected, PaletteDismissed

if TYPE_CHECKING:
    pass


class PaletteState(Enum):
    HIDDEN    = auto()
    OPEN      = auto()
    EXECUTING = auto()


@dataclass_ish_placeholder = None  # Command is defined in commands.py


class CommandPalette(ModalScreen):
    """
    Full-screen modal command browser.
    Extend textual.screen.ModalScreen so it overlays the full app.
    """

    BINDINGS = [
        Binding("escape", "dismiss_palette", "Close"),
    ]

    DEFAULT_CSS = """
    CommandPalette {
        align: center middle;
        background: $color-user-bg 80%;
    }
    CommandPalette > .palette-container {
        width: 70;
        height: 20;
        border: round $color-tool-border;
        background: $color-user-bg;
        padding: 1;
    }
    """

    def __init__(
        self,
        registry: "UnifiedCommandRegistry",
        initial_query: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.registry = registry
        self.initial_query = initial_query
        self._all_commands: list["Command"] = []
        self._filtered: list["Command"] = []

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(classes="palette-container"):
            yield Static("── Command Palette ──", classes="palette-title")
            yield Input(
                value=self.initial_query,
                placeholder="Search commands…",
                id="palette-search",
            )
            yield OptionList(id="palette-list")
            yield Static(
                "↑↓ navigate · Enter execute · Tab insert · Esc close",
                classes="palette-footer dim",
            )

    def on_mount(self) -> None:
        self._all_commands = self.registry.all_commands()
        self._filtered = list(self._all_commands)
        self._populate_list()
        self.query_one("#palette-search", Input).focus()

    def on_input_changed(self, event: "Input.Changed") -> None:
        query = event.value
        if not query:
            self._filtered = list(self._all_commands)
        else:
            self._filtered = self._fuzzy_filter(query)
        self._populate_list()

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "enter":
            self._execute_selected()
            event.stop()
        elif event.key == "tab":
            self._insert_into_input_bar()
            event.stop()
        elif event.key == "down":
            try:
                self.query_one("#palette-list", OptionList).action_cursor_down()
            except Exception:
                pass
            event.stop()
        elif event.key == "up":
            try:
                self.query_one("#palette-list", OptionList).action_cursor_up()
            except Exception:
                pass
            event.stop()

    def action_dismiss_palette(self) -> None:
        self.post_message(PaletteDismissed())
        self.dismiss()

    # --- Private helpers ---

    def _fuzzy_filter(self, query: str) -> "list[Command]":
        results: list[tuple[float, "Command"]] = []
        q = query.lower().strip("/")
        for cmd in self._all_commands:
            name = cmd.name.lower().strip("/")
            desc = cmd.description.lower()
            if name.startswith(q):
                score = 1.0
            elif q in name:
                score = 0.8
            else:
                ratio = difflib.SequenceMatcher(None, q, name + " " + desc).ratio()
                score = ratio
            if score > 0.3:
                results.append((score, cmd))
        results.sort(key=lambda x: x[0], reverse=True)
        return [cmd for _, cmd in results]

    def _populate_list(self) -> None:
        try:
            option_list = self.query_one("#palette-list", OptionList)
            option_list.clear_options()
            current_source = ""
            for cmd in self._filtered[:20]:
                if cmd.source != current_source:
                    if current_source:
                        option_list.add_option(Separator())
                    option_list.add_option(Option(f"── {cmd.source} ──", disabled=True))
                    current_source = cmd.source
                label = f"{cmd.name:<20} {cmd.description:<40} {cmd.source}"
                option_list.add_option(Option(label, id=cmd.name))
        except Exception:
            pass

    def _execute_selected(self) -> None:
        try:
            option_list = self.query_one("#palette-list", OptionList)
            idx = option_list.highlighted
            if idx is not None and idx < len(self._filtered):
                cmd = self._filtered[idx]
                args = self.query_one("#palette-search", Input).value
                self.post_message(CommandSelected(cmd, args))
                self.dismiss()
        except Exception:
            pass

    def _insert_into_input_bar(self) -> None:
        """Insert command name into InputBar without executing."""
        try:
            option_list = self.query_one("#palette-list", OptionList)
            idx = option_list.highlighted
            if idx is not None and idx < len(self._filtered):
                cmd = self._filtered[idx]
                self.post_message(CommandSelected(cmd, "__insert_only__"))
                self.dismiss()
        except Exception:
            pass
```

### Visual Mockup

```
╔══ Command Palette ══════════════════════════════════════════════╗
║  > deep res_                                                     ║
║  ─────────────────────────────────────────────────────────────   ║
║  /deep-research   Deep research harness         Skills           ║
║  /review          Review a PR                   Skills           ║
║  /code-review     Review code for bugs          Skills           ║
║  ─────────────────────────────────────────────────────────────   ║
║  ── Built-in ─────────────────────────────────────────────────   ║
║  /help            Show available commands       Built-in         ║
║  ─────────────────────────────────────────────────────────────   ║
║  ── MCP ──────────────────────────────────────────────────────   ║
║  /mcp:github:pr   Open a GitHub PR              MCP              ║
╚═════════════════════════════════════════════════════════════════╝
  ↑↓ navigate · Enter execute · Tab insert · Esc close
```

### State Model

```python
class PaletteState(Enum):
    HIDDEN    # Not mounted / dismissed
    OPEN      # Search input focused; list populated
    EXECUTING # Selected command being dispatched
```

### Fuzzy Scoring

- Exact prefix match: 1.0
- Word-boundary / substring match: 0.8
- `difflib.SequenceMatcher` ratio over name+description: threshold 0.3
- Max 20 results shown; source-grouped with separators

### Events Emitted

| Message | When |
|---|---|
| `CommandSelected(command, args)` | Enter selects; Tab uses `"__insert_only__"` sentinel |
| `PaletteDismissed()` | Escape pressed |

### Unit Tests

```python
# test_CP_01: fuzzy_filter_returns_prefix_matches_first
# test_CP_02: fuzzy_filter_excludes_below_threshold
# test_CP_03: enter_key_executes_highlighted_command
# test_CP_04: escape_key_dismisses_and_posts_palette_dismissed
# test_CP_05: tab_posts_command_selected_with_insert_only_args
# test_CP_06: results_capped_at_20
# test_CP_07: source_groups_shown_as_disabled_separators
# test_CP_08: initial_query_pre_fills_search_input
```

---

## Component 18: MentionChip

**Purpose:** Styled inline token representing a resolved @mention (file/dir/URL/glob); appears in UserMessage and InputBar preview row.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/mention_chip.py
from __future__ import annotations

from enum import Enum, auto
from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button

from ..messages import ChipActivated, ChipRemoved

if TYPE_CHECKING:
    pass


class MentionKind(Enum):
    FILE       = "file"
    DIRECTORY  = "directory"
    GLOB       = "glob"
    URL        = "url"
    UNRESOLVED = "unresolved"


@dataclass
class Mention:
    raw_token: str        # e.g. "@src/auth.py"
    resolved_path: str    # e.g. "src/auth.py"
    kind: MentionKind
    start_pos: int = 0
    end_pos: int = 0
    size_bytes: int | None = None


_KIND_CSS = {
    MentionKind.FILE:       "--file",
    MentionKind.DIRECTORY:  "--directory",
    MentionKind.GLOB:       "--glob",
    MentionKind.URL:        "--url",
    MentionKind.UNRESOLVED: "--unresolved",
}


class MentionChip(Widget):
    """Inline styled chip representing a resolved @mention."""

    DEFAULT_CSS = """
    MentionChip {
        width: auto;
        padding: 0 1;
        margin: 0 1 0 0;
        height: 1;
    }
    MentionChip.--file       { border-left: solid $mode-review; }
    MentionChip.--directory  { border-left: solid $mode-ask; }
    MentionChip.--glob       { border-left: solid $mode-plan; }
    MentionChip.--url        { border-left: solid $mode-auto; }
    MentionChip.--unresolved { border-left: solid $mode-safe; }
    """

    def __init__(
        self,
        mention: Mention,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.mention = mention
        self.add_class(_KIND_CSS[mention.kind])

    def compose(self):
        label = self._build_label()
        from textual.widgets import Static
        yield Static(f"[{label}]", id="chip-label")

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key in ("enter", "space"):
            self.post_message(ChipActivated(self.mention))
            event.stop()
        elif event.key in ("delete", "backspace"):
            self.post_message(ChipRemoved(self.mention))
            self.remove()
            event.stop()

    def on_click(self) -> None:
        self.post_message(ChipActivated(self.mention))

    # --- Private helpers ---

    def _build_label(self) -> str:
        path = self.mention.resolved_path
        if self.mention.size_bytes and self.mention.size_bytes > 10_240:
            kb = self.mention.size_bytes / 1024
            return f"{path} · {kb:.1f} KB"
        if self.mention.kind == MentionKind.UNRESOLVED:
            return f"?{path}"
        return path
```

### Visual Mockup

```
[src/auth.py]          ← file chip (blue left border)
[src/]                 ← directory chip (cyan left border)
[https://example.com]  ← URL chip (green left border)
[src/**/*.py]          ← glob chip (yellow left border)
[?unknown.py]          ← unresolved chip (red left border)
[src/auth.py · 12 KB]  ← large file (size suffix)
```

### State Model

```python
class MentionKind(Enum):
    FILE DIRECTORY GLOB URL UNRESOLVED
```

Each kind maps to a left-border colour via CSS class.

### Unit Tests

```python
# test_MC_01: file_chip_has_file_css_class
# test_MC_02: unresolved_chip_shows_question_prefix
# test_MC_03: large_file_shows_size_suffix
# test_MC_04: enter_key_posts_chip_activated
# test_MC_05: delete_key_posts_chip_removed_and_removes_widget
# test_MC_06: click_posts_chip_activated
# test_MC_07: kind_maps_to_correct_css_class
# test_MC_08: label_shows_resolved_path_not_raw_token
```

---

## Component 19: ErrorBlock

**Purpose:** Permanent in-flow error widget rendered in transcript for tool failures, LLM errors, and unhandled exceptions.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/error_block.py
from __future__ import annotations

from enum import Enum, auto

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Static
from textual.containers import Horizontal

from ..messages import ErrorRetried, ErrorDismissed, ErrorCopied


class ErrorBlockState(Enum):
    VISIBLE   = auto()
    RETRYING  = auto()
    RESOLVED  = auto()
    DISMISSED = auto()


class ErrorBlock(Widget):
    """Permanent in-flow error display with retry/copy/dismiss actions."""

    DEFAULT_CSS = """
    ErrorBlock {
        border: round $mode-safe;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    ErrorBlock.--dismissed {
        height: 1;
        border: none;
        color: $status-idle;
    }
    """

    state: reactive[ErrorBlockState] = reactive(ErrorBlockState.VISIBLE)

    def __init__(
        self,
        error_id: str,
        title: str,
        message: str,
        suggestion: str | None = None,
        retryable: bool = False,
        source: str = "tool",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.error_id = error_id
        self.title = title
        self.message = message
        self.suggestion = suggestion
        self.retryable = retryable
        self.source = source

    def compose(self) -> ComposeResult:
        yield Static(f"✗  {self.title}", id="err-title")
        yield Static(f"   {self.message}", id="err-message")
        if self.suggestion:
            yield Static(f"   Try: {self.suggestion}", id="err-suggestion")
        buttons: list[Button] = []
        if self.retryable:
            buttons.append(Button("[Retry]", id="btn-retry", variant="warning"))
        buttons.append(Button("[Copy error]", id="btn-copy"))
        buttons.append(Button("[Dismiss]", id="btn-dismiss"))
        yield Horizontal(*buttons, id="err-actions")

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key == "r" and self.retryable:
            self._retry()
            event.stop()
        elif event.key == "c":
            self._copy()
            event.stop()
        elif event.key in ("d", "escape"):
            self._dismiss()
            event.stop()

    def on_button_pressed(self, event: "Button.Pressed") -> None:
        if event.button.id == "btn-retry":
            self._retry()
        elif event.button.id == "btn-copy":
            self._copy()
        elif event.button.id == "btn-dismiss":
            self._dismiss()

    def _retry(self) -> None:
        self.state = ErrorBlockState.RETRYING
        self.post_message(ErrorRetried(self.error_id))

    def _copy(self) -> None:
        import pyperclip  # type: ignore[import]
        try:
            pyperclip.copy(f"{self.title}\n{self.message}")
        except Exception:
            pass
        self.post_message(ErrorCopied(self.message))

    def _dismiss(self) -> None:
        self.state = ErrorBlockState.DISMISSED
        self.add_class("--dismissed")
        try:
            self.query_one("#err-message").remove()
            self.query_one("#err-suggestion").remove()
        except Exception:
            pass
        try:
            self.query_one("#err-actions").remove()
        except Exception:
            pass
        try:
            self.query_one("#err-title", Static).update(f"✗  {self.title}  (dismissed)")
        except Exception:
            pass
        self.post_message(ErrorDismissed(self.error_id))
```

### Visual Mockup

```
╔══ Error ════════════════════════════════════════════════════╗
║  ✗  Tool run_bash failed                                    ║
║     CommandNotFound: 'pytest' is not installed              ║
║     Try:  pip install pytest                                ║
║  [Retry]  [Copy error]  [Dismiss]                           ║
╚═════════════════════════════════════════════════════════════╝

[DISMISSED state — single dim line]:
✗  Tool run_bash failed  (dismissed)
```

### Unit Tests

```python
# test_EB_01: visible_state_shows_title_message_buttons
# test_EB_02: r_key_when_retryable_posts_error_retried
# test_EB_03: r_key_when_not_retryable_does_nothing
# test_EB_04: c_key_posts_error_copied
# test_EB_05: d_key_transitions_to_dismissed
# test_EB_06: dismissed_state_collapses_to_single_line
# test_EB_07: suggestion_rendered_when_provided
# test_EB_08: retry_button_absent_when_not_retryable
```

---

## Component 20: ContextSummary

**Purpose:** Collapsible panel between ChatTranscript and AgentStatusBar listing active context: files, skills, MCP servers, mode, context window usage.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/context_summary.py
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from ..messages import ContextExpanded, ContextCollapsed, FileRemoved

if TYPE_CHECKING:
    from .mention_chip import Mention


class ContextSummary(Widget):
    """Collapsible active context panel."""

    DEFAULT_CSS = """
    ContextSummary {
        background: $color-user-bg;
        border: round $color-tool-border;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    """

    collapsed: reactive[bool] = reactive(True)

    def __init__(
        self,
        mentioned_files: "list[Mention]",
        active_skills: list[str],
        mcp_servers: list[str],
        mode: str,
        context_fraction: float,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.mentioned_files = mentioned_files
        self.active_skills = active_skills
        self.mcp_servers = mcp_servers
        self.mode = mode
        self.context_fraction = max(0.0, min(1.0, context_fraction))
        self._should_show = bool(mentioned_files or active_skills or mcp_servers)

    # --- Public API ---

    def update_context(
        self,
        mentioned_files: "list[Mention] | None" = None,
        active_skills: list[str] | None = None,
        mcp_servers: list[str] | None = None,
        context_fraction: float | None = None,
    ) -> None:
        if mentioned_files is not None:
            self.mentioned_files = mentioned_files
        if active_skills is not None:
            self.active_skills = active_skills
        if mcp_servers is not None:
            self.mcp_servers = mcp_servers
        if context_fraction is not None:
            self.context_fraction = context_fraction
        self._should_show = bool(
            self.mentioned_files or self.active_skills or self.mcp_servers
        )
        self._refresh_summary()

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        yield Static(self._summary_line(), id="ctx-summary-line")
        with Collapsible(collapsed=True, id="ctx-detail"):
            if self.mentioned_files:
                yield Static("Files", classes="ctx-section-header")
                for f in self.mentioned_files:
                    yield Static(f"  {f.resolved_path}", classes="ctx-file-entry")
            if self.active_skills:
                yield Static("Skills", classes="ctx-section-header")
                for s in self.active_skills:
                    yield Static(f"  {s}", classes="ctx-skill-entry")
            if self.mcp_servers:
                yield Static("MCP Servers", classes="ctx-section-header")
                for srv in self.mcp_servers:
                    yield Static(f"  {srv}", classes="ctx-mcp-entry")
            yield Static(self._context_bar_line(), id="ctx-bar")

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key in ("space", "enter"):
            try:
                collapsible = self.query_one("#ctx-detail", Collapsible)
                collapsible.collapsed = not collapsible.collapsed
                if collapsible.collapsed:
                    self.post_message(ContextCollapsed())
                else:
                    self.post_message(ContextExpanded())
            except Exception:
                pass
            event.stop()

    # --- Private helpers ---

    def _summary_line(self) -> str:
        parts = []
        if self.mentioned_files:
            parts.append(f"{len(self.mentioned_files)} files")
        if self.active_skills:
            parts.append(f"{len(self.active_skills)} skills")
        if self.mcp_servers:
            parts.append(f"{len(self.mcp_servers)} MCP")
        parts.append(f"{self.mode} mode")
        pct = int(self.context_fraction * 100)
        parts.append(f"{pct}% context")
        return f"Context: {' · '.join(parts)}  [▶ expand]"

    def _context_bar_line(self) -> str:
        filled = int(self.context_fraction * 20)
        bar = "█" * filled + "░" * (20 - filled)
        pct = int(self.context_fraction * 100)
        return f"Context window: [{bar}]  {pct}%"

    def _refresh_summary(self) -> None:
        try:
            self.query_one("#ctx-summary-line", Static).update(self._summary_line())
            self.query_one("#ctx-bar", Static).update(self._context_bar_line())
        except Exception:
            pass
```

### Visual Mockup

```
[COLLAPSED]:
 Context: 3 files · 2 skills · AUTO mode · 42% context  [▶ expand]

[EXPANDED]:
╔══ Active Context ══════════════════════════════╗
║  Context: 3 files · 2 skills · AUTO mode · 42%  ║
║  Files                                           ║
║    src/auth.py              (mentioned)          ║
║  Skills                                          ║
║    deep-research  (auto-triggered)              ║
║  MCP Servers                                     ║
║    github   (connected)                         ║
║  Context window: [████████░░░░░░░░░░░░]  42%   ║
╚══════════════════════════════════════════════════╝
```

### Unit Tests

```python
# test_CS_01: summary_line_includes_all_parts
# test_CS_02: empty_context_not_shown
# test_CS_03: space_key_toggles_collapsible
# test_CS_04: context_bar_uses_20_char_width
# test_CS_05: update_context_refreshes_summary_line
# test_CS_06: expand_posts_context_expanded
# test_CS_07: collapse_posts_context_collapsed
# test_CS_08: context_fraction_clamped_0_to_1
```

---

## Component 21: ConversationDivider

**Purpose:** Thin horizontal separator between turns with turn counter; static display element.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/conversation_divider.py
from __future__ import annotations

import datetime
from enum import Enum
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Rule, Static


class DividerKind(Enum):
    TURN    = "turn"
    RESUME  = "resume"
    COMPACT = "compact"


class ConversationDivider(Widget):
    """Static turn separator rendered in ChatTranscript."""

    DEFAULT_CSS = """
    ConversationDivider {
        color: $status-idle;
        margin: 1 0;
        height: 1;
    }
    """

    def __init__(
        self,
        turn_number: int | None = None,
        timestamp: datetime.datetime | None = None,
        kind: DividerKind = DividerKind.TURN,
        session_id: str | None = None,
        elapsed: datetime.timedelta | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.turn_number = turn_number
        self.timestamp = timestamp
        self.kind = kind
        self.session_id = session_id
        self.elapsed = elapsed

    def compose(self) -> ComposeResult:
        yield Rule(line_style="ascii", classes="divider-rule")
        yield Static(self._label(), id="divider-label")

    def _label(self) -> str:
        if self.kind == DividerKind.COMPACT:
            return f"── {self.turn_number} ──" if self.turn_number else "────"
        if self.kind == DividerKind.RESUME:
            elapsed_str = self._format_elapsed()
            sid = (self.session_id or "")[:8]
            return f"━━ Session resumed  ·  {sid}  ·  {elapsed_str} ago ━━"
        ts_str = self.timestamp.strftime("%H:%M") if self.timestamp else ""
        turn_str = f"Turn {self.turn_number}" if self.turn_number is not None else ""
        parts = [p for p in [turn_str, ts_str] if p]
        return f" ── {' · '.join(parts)} ──"

    def _format_elapsed(self) -> str:
        if self.elapsed is None:
            return "?"
        total_s = int(self.elapsed.total_seconds())
        if total_s < 60:
            return f"{total_s}s"
        m = total_s // 60
        h = m // 60
        if h:
            return f"{h}h {m % 60}m"
        return f"{m}m"
```

### Visual Mockup

```
── Turn 4 · 11:47 ──────────────────────────────────────── (TURN)
━━ Session resumed · abc12345 · 2h 14m ago ━━━━━━━━━━━━━ (RESUME)
── 4 ──                                                    (COMPACT)
```

### Accessibility

- `role="separator"` with `aria-label="Turn 4"`
- Turn numbers 1-indexed (human-visible)

### Unit Tests

```python
# test_CD_01: turn_kind_shows_turn_number_and_timestamp
# test_CD_02: resume_kind_shows_session_id_and_elapsed
# test_CD_03: compact_kind_shows_number_only
# test_CD_04: elapsed_format_hours_minutes
# test_CD_05: elapsed_format_seconds
# test_CD_06: no_turn_number_omits_turn_prefix
# test_CD_07: no_timestamp_omits_timestamp
# test_CD_08: session_id_shown_as_first_8_chars
```

---

## Component 22: ExpandableOutput

**Purpose:** Collapsible wrapper for long tool output; shows configurable preview lines with "Show N more" affordance.

### Python Class Signature

```python
# src/agenthicc/tui/widgets/expandable_output.py
from __future__ import annotations

from enum import Enum, auto

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Collapsible, RichLog, Static, Button

from ..messages import OutputExpanded, OutputCollapsed


class OutputState(Enum):
    COLLAPSED = auto()
    EXPANDED  = auto()


class ExpandableOutput(Widget):
    """Collapsible long-output wrapper with line-count threshold."""

    DEFAULT_CSS = """
    ExpandableOutput { padding: 0 1; }
    ExpandableOutput .output-toggle { color: $mode-ask; }
    """

    state: reactive[OutputState] = reactive(OutputState.COLLAPSED)

    def __init__(
        self,
        content: str,
        preview_lines: int = 5,
        syntax: str | None = None,
        initially_collapsed: bool | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.content = content
        self.preview_lines = preview_lines
        self.syntax = syntax
        all_lines = content.splitlines()
        self._total_lines = len(all_lines)
        self._preview_lines_list = all_lines[:preview_lines]
        self._remaining = max(0, self._total_lines - preview_lines)
        self._needs_collapse = (
            self._total_lines > preview_lines or len(content) > 2000
        )
        if initially_collapsed is None:
            self.state = (
                OutputState.COLLAPSED if self._needs_collapse else OutputState.EXPANDED
            )
        else:
            self.state = (
                OutputState.COLLAPSED if initially_collapsed else OutputState.EXPANDED
            )

    # --- Public API ---

    def expand(self) -> None:
        self.state = OutputState.EXPANDED
        self._render_content()
        self.post_message(OutputExpanded(self.id or ""))

    def collapse(self) -> None:
        self.state = OutputState.COLLAPSED
        self._render_content()
        self.post_message(OutputCollapsed(self.id or ""))

    def toggle(self) -> None:
        if self.state == OutputState.COLLAPSED:
            self.expand()
        else:
            self.collapse()

    # --- Textual lifecycle ---

    def compose(self) -> ComposeResult:
        yield RichLog(highlight=bool(self.syntax), markup=False, id="output-log")
        yield Static("", id="output-toggle-label", classes="output-toggle")

    def on_mount(self) -> None:
        self._render_content()

    def on_key(self, event: "KeyEvent") -> None:  # type: ignore[name-defined]
        if event.key in ("space", "enter"):
            self.toggle()
            event.stop()
        elif event.key == "c":
            import pyperclip  # type: ignore[import]
            try:
                pyperclip.copy(self.content)
            except Exception:
                pass
            event.stop()

    # --- Private helpers ---

    def _render_content(self) -> None:
        try:
            log = self.query_one("#output-log", RichLog)
            log.clear()
            if self.state == OutputState.EXPANDED:
                lines_to_show = self.content.splitlines()
            else:
                lines_to_show = self._preview_lines_list

            for line in lines_to_show:
                if self.syntax:
                    from rich.syntax import Syntax
                    log.write(Syntax(line, self.syntax, theme="monokai"))
                else:
                    log.write(line)

            toggle = self.query_one("#output-toggle-label", Static)
            if self.state == OutputState.COLLAPSED and self._remaining > 0:
                toggle.update(f"… {self._remaining} more lines  [Show all]")
            elif self.state == OutputState.EXPANDED and self._needs_collapse:
                toggle.update("[Show less]")
            else:
                toggle.update("")
        except Exception:
            pass
```

### Visual Mockup

```
[COLLAPSED]:
│ Process exited with code 1                              │
│ FAILED tests/test_auth.py::test_verify_jwt              │
│ FAILED tests/test_auth.py::test_refresh_token           │
│ … 44 more lines  [Show all]

[EXPANDED]:
│ Process exited with code 1
│ FAILED tests/test_auth.py::test_verify_jwt
│ … (full 47 lines) …
│ [Show less]
```

### State Model

```python
class OutputState(Enum):
    COLLAPSED  # max preview_lines shown; expand affordance
    EXPANDED   # full content shown; collapse affordance
```

Threshold: collapse if `total_lines > preview_lines` OR `len(content) > 2000`.

### Unit Tests

```python
# test_EO_01: short_content_starts_expanded
# test_EO_02: long_content_starts_collapsed
# test_EO_03: expand_shows_full_content
# test_EO_04: collapse_shows_only_preview_lines
# test_EO_05: remaining_line_count_shown_in_toggle_label
# test_EO_06: space_key_toggles_state
# test_EO_07: expand_posts_output_expanded_message
# test_EO_08: syntax_applies_rich_syntax_highlighting
# test_EO_09: copy_key_copies_full_content
# test_EO_10: initially_collapsed_false_overrides_threshold
```

---

## Full Test Specification

### Unit Tests (114 named)

All tests use `pytest` with `asyncio_mode = "auto"`, marked `@pytest.mark.unit`.

**ChatTranscript (8):** test_CT_01 through test_CT_08 — see Component 1 spec.  
**AgentMessage (8):** test_AM_01 through test_AM_08 — see Component 2 spec.  
**UserMessage (8):** test_UM_01 through test_UM_08 — see Component 3 spec.  
**ToolCallBlock (8):** test_TCB_01 through test_TCB_08 — see Component 4 spec.  
**DiffViewer (8):** test_DV_01 through test_DV_08 — see Component 5 spec.  
**StreamingCursor (8):** test_SC_01 through test_SC_08 — see Component 6 spec.  
**AgentStatusBar (8):** test_ASB_01 through test_ASB_08 — see Component 7 spec.  
**TokenMeter (8):** test_TM_01 through test_TM_08 — see Component 8 spec.  
**ModeIndicator (8):** test_MI_01 through test_MI_08 — see Component 9 spec.  
**InputBar (10):** test_IB_01 through test_IB_10 — see Component 10 spec.  
**TriggerDropdown (8):** test_TD_01 through test_TD_08 — see Component 11 spec.  
**ApprovalRequest (10):** test_AR_01 through test_AR_10 — see Component 12 spec.  
**ProgressIndicator (8):** test_PI_01 through test_PI_08 — see Component 13 spec.  
**NotificationToast (8):** test_NT_01 through test_NT_08 — see Component 14 spec.  
**SessionHeader (6):** test_SH_01 through test_SH_06 — see Component 15 spec.  
**ThinkingIndicator (8):** test_TI_01 through test_TI_08 — see Component 16 spec.  
**CommandPalette (8):** test_CP_01 through test_CP_08 — see Component 17 spec.  
**MentionChip (8):** test_MC_01 through test_MC_08 — see Component 18 spec.  
**ErrorBlock (8):** test_EB_01 through test_EB_08 — see Component 19 spec.  
**ContextSummary (8):** test_CS_01 through test_CS_08 — see Component 20 spec.  
**ConversationDivider (8):** test_CD_01 through test_CD_08 — see Component 21 spec.  
**ExpandableOutput (10):** test_EO_01 through test_EO_10 — see Component 22 spec.  

**Additional shared tests in `tests/unit/test_tui_messages.py` (4):**

```python
# test_MSG_01: all_message_classes_are_textual_message_subclasses
#   Assert: every class in messages.py inherits from textual.message.Message

# test_MSG_02: message_payload_fields_accessible_after_init
#   Assert: TurnComplete("abc").turn_id == "abc"

# test_MSG_03: from_future_annotations_present_on_all_tui_modules
#   Assert: every .py in src/agenthicc/tui/ starts with from __future__ import annotations

# test_MSG_04: no_module_imports_at_top_level_that_would_cause_circular
#   Assert: importing agenthicc.tui.messages does not raise ImportError
```

---

### Integration Tests (32 named)

All marked `@pytest.mark.integration`.

```python
# tests/integration/test_tui_rendering.py

# test_INT_01_agent_turn_streaming_commits_to_transcript
#   Flow: emit streaming tokens to TranscriptModel, run RenderLoop 2 ticks
#   Assert: FakeTerminal.committed_lines contains agent header + partial text

# test_INT_02_tool_call_lifecycle_pending_to_success
#   Flow: mount ToolCallBlock, call set_running(), then set_success()
#   Assert: status transitions correctly; output widget mounted

# test_INT_03_approval_gate_blocks_input_bar
#   Flow: mount ApprovalRequest, check InputBar
#   Assert: InputBar.agent_ready == False while approval PENDING

# test_INT_04_approval_grant_unblocks_input_bar
#   Flow: ApprovalRequest.confirm()
#   Assert: ApprovalGranted posted; InputBar.agent_ready == True

# test_INT_05_mention_trigger_opens_dropdown
#   Flow: InputBar types "@src", TriggerDetected posted
#   Assert: TriggerDropdown.mode == MENTION, visible

# test_INT_06_completion_accepted_inserts_into_input_bar
#   Flow: CompletionAccepted("src/auth.py", "file") handled by App
#   Assert: InputBar TextArea text contains "src/auth.py"

# test_INT_07_slash_trigger_opens_command_dropdown
#   Flow: InputBar text set to "/help"
#   Assert: TriggerDropdown.mode == COMMAND

# test_INT_08_mode_cycle_updates_status_bar_and_indicator
#   Flow: ModeIndicator.cycle_forward()
#   Assert: AgentStatusBar ModeIndicator shows new mode

# test_INT_09_budget_warning_triggers_notification_toast
#   Flow: TokenMeter.update_stats exceeds 80% budget
#   Assert: BudgetWarning posted; NotificationToast mounted

# test_INT_10_streaming_cursor_appears_on_first_token
#   Flow: AgentMessage.append_token("Hello")
#   Assert: StreamingCursor in DOM; ThinkingIndicator removed

# test_INT_11_turn_complete_removes_cursor
#   Flow: AgentMessage.complete()
#   Assert: StreamingCursor not in DOM

# test_INT_12_diff_viewer_inside_tool_call_block
#   Flow: ToolCallBlock with is_diff=True, set_success(diff_text, 100)
#   Assert: DiffViewer mounted inside ToolCallBlock

# test_INT_13_expandable_output_toggle_via_keyboard
#   Flow: mount ExpandableOutput(long_text), press Space
#   Assert: state == EXPANDED, OutputExpanded posted

# test_INT_14_error_block_retry_emits_error_retried
#   Flow: ErrorBlock(retryable=True), press "r"
#   Assert: ErrorRetried message posted

# test_INT_15_approval_timeout_auto_denies
#   Flow: ApprovalRequest(timeout_s=1), wait 1.1s
#   Assert: ApprovalDenied posted with reason="timeout"

# test_INT_16_context_summary_shows_when_files_mentioned
#   Flow: ContextSummary(mentioned_files=[Mention(...)], ...)
#   Assert: widget visible; summary line contains file count

# test_INT_17_notification_toast_auto_dismisses
#   Flow: NotificationToast(duration_ms=100), wait 200ms
#   Assert: ToastDismissed posted; widget removed

# test_INT_18_agent_status_bar_state_change_chain
#   Flow: update_state(THINKING), then (RUNNING_TOOLS), then (IDLE)
#   Assert: labels update correctly each transition

# test_INT_19_input_history_recalled_on_up_arrow
#   Flow: submit "msg1", clear, press Up
#   Assert: TextArea contains "msg1"

# test_INT_20_command_palette_fuzzy_filters_results
#   Flow: open palette, type "deep"
#   Assert: only commands matching "deep" shown in list

# test_INT_21_mention_chip_in_user_message
#   Flow: UserMessage with mentions=[Mention(kind=FILE)]
#   Assert: MentionChip mounted; has --file CSS class

# test_INT_22_thinking_indicator_removed_on_token
#   Flow: AgentMessage in PENDING, append_token("")
#   Assert: ThinkingIndicator no longer in DOM

# test_INT_23_progress_indicator_complete_removes_after_500ms
#   Flow: ProgressIndicator.complete(), wait 600ms
#   Assert: widget no longer in DOM

# test_INT_24_trigger_dropdown_escape_posts_dismissed
#   Flow: TriggerDropdown visible, press Escape
#   Assert: CompletionDismissed posted, widget hidden

# test_INT_25_session_header_updates_on_intent_change
#   Flow: SessionHeader.current_intent = "New intent"
#   Assert: header Static text updated

# test_INT_26_conversation_divider_turn_kind_label
#   Flow: ConversationDivider(turn_number=3, timestamp=datetime(...))
#   Assert: label contains "Turn 3" and "HH:MM"

# test_INT_27_error_block_dismiss_collapses_to_single_line
#   Flow: ErrorBlock.dismiss()
#   Assert: has --dismissed class; message and buttons removed

# test_INT_28_mode_indicator_css_class_swaps_on_cycle
#   Flow: cycle AUTO→PLAN
#   Assert: --auto removed, --plan added

# test_INT_29_tool_call_block_approval_gates_input_bar
#   Flow: ToolCallBlock.set_approval_needed(), App handles ApprovalRequested
#   Assert: ApprovalRequest mounted; InputBar BLOCKED

# test_INT_30_render_loop_skips_redraw_on_unchanged_frame
#   Flow: RenderLoop, same TranscriptModel state across two ticks
#   Assert: FakeTerminal.write_call_count increments by 1 (not 2)

# test_INT_31_chat_transcript_virtualises_old_items
#   Flow: append max_items+1 widgets
#   Assert: oldest widget not in DOM; _items length == max_items

# test_INT_32_token_meter_throttle_at_200ms
#   Flow: two update_stats calls 50ms apart
#   Assert: display updated only once
```

---

### E2E Tests (20 named)

All marked `@pytest.mark.e2e`. Use `pyte` for terminal emulation where specified.

```python
# tests/e2e/test_tui_e2e.py

# test_E2E_01_standard_chat_workflow
#   Flow: type message, press Enter, stream tokens, turn completes
#   Assert: committed transcript has agent header + body; InputBar cleared

# test_E2E_02_tool_execution_committed_to_transcript
#   Flow: agent emits tool call, tool completes
#   Assert: committed line contains "⎿ {tool_name}  ✓ {duration}"

# test_E2E_03_approval_workflow_allow
#   Flow: tool requires approval, user presses "y"
#   Assert: tool executes; committed line shows "✓ approved"

# test_E2E_04_approval_workflow_deny
#   Flow: tool requires approval, user presses "n"
#   Assert: tool skipped; committed line shows "✗ denied by user"

# test_E2E_05_at_mention_completion_flow
#   Flow: type "@src", dropdown appears, Tab selects "src/auth.py"
#   Assert: InputBar text shows "src/auth.py"; dropdown hidden

# test_E2E_06_slash_command_execution
#   Flow: type "/help", Enter
#   Assert: help text committed to transcript

# test_E2E_07_mode_switch_via_ctrl_m
#   Flow: press Ctrl-M three times from AUTO
#   Assert: mode cycles AUTO→PLAN→ASK→REVIEW; toast shown each time

# test_E2E_08_multiline_input_shift_enter
#   Flow: type "line 1", Shift-Enter, "line 2", Enter
#   Assert: MessageSubmitted with text containing newline

# test_E2E_09_scroll_paused_and_resumed
#   Flow: streaming turn active, scroll up (pyte), scroll to bottom
#   Assert: ScrollPaused then ScrollResumed messages posted

# test_E2E_10_session_header_cwd_and_session_id
#   Flow: launch app with cwd=/tmp and session_id="abc12345678"
#   Assert: pyte screen row 0 contains "AgentHICC  /tmp  · Session abc12345"

# test_E2E_11_terminal_resize_redraws_bottom_block
#   Flow: send SIGWINCH, change terminal size to 60×20
#   Assert: status bar redrawn at width 60 within 50ms

# test_E2E_12_ctrl_c_cancels_streaming_turn
#   Flow: during STREAMING state, press Ctrl-C
#   Assert: turn committed with [cancelled]; InputBar unblocked

# test_E2E_13_budget_warning_toast_shown
#   Flow: accumulate tokens to 82% of budget
#   Assert: pyte screen shows toast with "⚠" at top

# test_E2E_14_doom_loop_detection
#   Flow: same tool called 3x with same args
#   Assert: doom loop banner committed to transcript

# test_E2E_15_session_resume_replays_transcript
#   Flow: save session, launch with --resume, check transcript
#   Assert: last 50 committed turns printed to scrollback

# test_E2E_16_no_color_mode_strips_ansi
#   Flow: launch with NO_COLOR=1 env var
#   Assert: pyte screen has no ANSI escape sequences; symbols retained

# test_E2E_17_ssh_degraded_mode_reduces_render_rate
#   Flow: set simulated RTT > 200ms
#   Assert: RenderLoop.MIN_TICK_INTERVAL becomes 0.150

# test_E2E_18_command_palette_open_and_execute
#   Flow: press Ctrl-/, type "model", Enter
#   Assert: CommandSelected posted with /model command

# test_E2E_19_expandable_output_expand_collapse
#   Flow: tool produces 50-line output; user presses Space on ToolCallBlock
#   Assert: full 50 lines visible; Space again collapses to 5

# test_E2E_20_parallel_agents_get_distinct_colors
#   Flow: spawn two agents simultaneously
#   Assert: committed headers use different ANSI colour codes
```

---

## Acceptance Criteria

### Per-Component Measurable Criteria

| Component | Criterion | Measurement |
|---|---|---|
| ChatTranscript | Auto-scroll re-engages within 1 frame (≤50ms) of End key | FakeTerminal timing |
| AgentMessage | First token transitions from PENDING in < 1ms | unit test timing |
| AgentMessage | `Markdown.update()` called once per token, not remount | call count assertion |
| UserMessage | MentionChips rendered in correct text position order | position test |
| ToolCallBlock | Header icon updates on each state transition | state machine test |
| DiffViewer | Parses 100-line diff in < 5ms | benchmark test |
| StreamingCursor | Blink interval within ±10ms of target | timer test |
| AgentStatusBar | Fits in 1 terminal row at 60 columns minimum | layout test |
| TokenMeter | Updates throttled to ≤ 1/200ms | timing test |
| ModeIndicator | CSS class swap < 1ms | reactive test |
| InputBar | @mention trigger fires only when not in email context | parser tests |
| TriggerDropdown | Shows within 1 frame of trigger | mount timing |
| ApprovalRequest | Focus trapped: Tab does not leave widget | focus test |
| ProgressIndicator | Spinner completes full cycle in `4 × interval_ms` | frame count |
| NotificationToast | Auto-dismisses within ±50ms of `duration_ms` | timing test |
| SessionHeader | Always 1 row, never wraps | layout assertion |
| ThinkingIndicator | Unmounted (not hidden) on first token | DOM query |
| CommandPalette | Fuzzy filter returns in < 10ms for 100 commands | benchmark |
| MentionChip | Kind-specific CSS class applied in `on_mount` | class test |
| ErrorBlock | Dismissed state reduces to 1 row | height test |
| ContextSummary | Hidden when no files/skills/MCP | visibility test |
| ConversationDivider | Turn number shown as 1-indexed | label test |
| ExpandableOutput | Collapse threshold: > `preview_lines` OR > 2000 chars | threshold test |

### Global Acceptance Criteria

1. **No alternate screen:** `App.run(inline=True)` only; `\x1b[?1049h` never written to stdout.
2. **mypy clean:** `uv run mypy src/agenthicc/tui/` exits 0 with `--strict`.
3. **ruff clean:** `uv run ruff check src/agenthicc/tui/` exits 0.
4. **`from __future__ import annotations`** present on every `.py` file in `src/agenthicc/tui/`.
5. **wcwidth:** every display-width calculation uses `wcwidth.wcswidth()`, verified by grep.
6. **NO_COLOR:** all ANSI disabled when `NO_COLOR` set; symbols retained; verified by E2E test_E2E_16.
7. **Test coverage:** ≥ 90% line coverage on all non-`__init__.py` files in `src/agenthicc/tui/widgets/`.
8. **Single write per frame:** `FakeTerminal.write_call_count` increments by exactly 1 per `set_bottom()` call.
9. **Max bottom block height:** `min(12, terminal.rows // 3)` enforced in `FrameComposer.compose()`.
10. **Timer cleanup:** every `set_interval` timer stopped in `on_unmount`; verified by test.

---

*End of Component Implementation PRD v1.0*
