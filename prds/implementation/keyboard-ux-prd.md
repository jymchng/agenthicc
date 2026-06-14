# AgentHICC Keyboard UX — Implementation PRD

**Document type**: Implementation-ready engineering specification  
**Scope**: Complete keyboard UX system for the AgentHICC TUI redesign  
**Status**: v1.0  
**Date**: 2026-06-13  
**Architecture constraint**: No alternate screen; committed-transcript + live-bottom-block pattern

---

## 1. Complete Keybinding Specification

### 1.1 Global Bindings (always active, regardless of input state)

| Key | Action | Implementation Notes |
|-----|--------|---------------------|
| `Ctrl+C` | Interrupt/exit (two-press) | First press: if agent running, cancel turn and set `ctrl_c_count=1`; second press within 2s: exit. Third press always exits immediately. |
| `Ctrl+D` | EOF/exit | Only fires when input buffer is empty (`text == ""`); otherwise is a no-op. Exits with code 0. |
| `Shift+Tab` | Cycle mode forward | Auto→Plan→Ask→Review→Safe→Debug→Auto. Emits `ModeChanged` event to kernel. Never intercepted by dropdown. |
| `Alt+Shift+Tab` | Cycle mode backward | Debug→Safe→Review→Ask→Plan→Auto→Debug. |
| `Ctrl+L` | Redraw bottom block | Clears and redraws bottom block without clearing scrollback. Sets `_needs_redraw=True` on `RenderLoop`. |
| `Ctrl+B` | Background current turn | Marks active agent turn as backgrounded; input returns to idle. Status bar shows `[1 bg]`. |
| `Ctrl+O` | Toggle transcript viewer | Opens `/history` overlay. Not yet implemented in v1 — reserved binding. |
| `Ctrl+T` | Toggle task list | Shows DAG progress overlay. Reserved binding for v1. |
| `Ctrl+X Ctrl+K` | Stop all sub-agents | Two-key chord. Emits `cancel_all_agents` kernel event. |
| `Alt+T` | Toggle thinking display | Shows/hides extended thinking content. Reserved binding. |
| `Alt+P` | Switch model | Opens model picker. Equivalent to `/model` command. |
| `?` | Show help | Only when input is empty. Equivalent to `/help`. |

**Ctrl+C two-press implementation:**

```python
# In InputState
_ctrl_c_count: int = 0
_ctrl_c_last_time: float = 0.0

def handle_ctrl_c(self) -> InputResult:
    now = time.monotonic()
    if now - self._ctrl_c_last_time > 2.0:
        self._ctrl_c_count = 0
    self._ctrl_c_count += 1
    self._ctrl_c_last_time = now
    if self._ctrl_c_count >= 2:
        return InputResult(kind=InputResultKind.EXIT)
    if self._agent_turn_active:
        return InputResult(kind=InputResultKind.CANCEL_TURN)
    # First press with no active turn: warn
    return InputResult(kind=InputResultKind.WARN_EXIT)
```

---

### 1.2 Input Mode Bindings (input bar focused, no dropdown open)

| Key | Action | Notes |
|-----|--------|-------|
| `Enter` | Submit input | Only if `text.strip()` is non-empty and not disabled. If text ends with `\` (literal backslash), strips it and inserts newline instead. |
| `Shift+Enter` | Insert newline | Multi-line input. Input bar grows up to `max_height_lines=8`. |
| `Alt+Enter` | Insert newline | Fallback for terminals that intercept `Shift+Enter`. |
| `Backspace` | Delete char left | Standard. If dropdown open, delegates to dropdown handler. |
| `Delete` / `Ctrl+D` (non-empty) | Delete char right | `Ctrl+D` only triggers delete-right when buffer is non-empty; see §1.1. |
| `Left` | Move cursor left one char | |
| `Right` | Move cursor right one char | |
| `Ctrl+Left` / `Alt+B` | Move word left | Jump to start of previous word (boundary: whitespace/punct). |
| `Ctrl+Right` / `Alt+F` | Move word right | Jump to start of next word. |
| `Home` / `Ctrl+A` | Move to line start | On multi-line input: moves to start of current visual line. |
| `End` / `Ctrl+E` | Move to line end | On multi-line input: moves to end of current visual line. |
| `Up` | History previous | When at first line of input or single-line; otherwise moves cursor up a visual line. |
| `Down` | History next / cursor down | When at last line; otherwise moves cursor down a visual line. |
| `Ctrl+P` | History previous | Always navigates history regardless of cursor position. |
| `Ctrl+N` | History next | Always navigates history. |
| `Ctrl+U` | Kill to start of line | Stores killed text in kill ring. |
| `Ctrl+K` | Kill to end of line | Stores killed text in kill ring. |
| `Ctrl+W` | Kill word backward | Stores in kill ring. |
| `Alt+D` | Kill word forward | Stores in kill ring. |
| `Ctrl+Y` | Yank from kill ring | Inserts last killed text at cursor. |
| `Tab` | Accept top dropdown item | If dropdown open. Otherwise: no-op (future: indent for multiline). |
| `Escape` | Dismiss dropdown or clear | If dropdown open: close dropdown without selecting. If no dropdown: clear input buffer (with confirm if non-empty). |
| `@` | Open @mention dropdown | Fires only when preceded by whitespace or at position 0. See §3.1. |
| `/` | Open command dropdown | Fires only when at position 0 (first non-whitespace char). See §4.1. |
| `Ctrl+G` / `Ctrl+X Ctrl+E` | Open in external editor | Opens `$EDITOR` with current buffer content. Pastes result back on editor exit. |
| `Ctrl+R` | Reverse history search | Opens history search mode (future v2). |
| `!` (at position 0) | Shell passthrough prefix | Text after `!` is run directly as shell command. |

**Word boundary rule for `Ctrl+Left`/`Ctrl+Right`:**

```python
def _word_boundary_left(text: str, pos: int) -> int:
    """Return position of start of word to the left of pos."""
    if pos == 0:
        return 0
    # Skip trailing whitespace
    i = pos - 1
    while i > 0 and not text[i].isalnum() and text[i] != '_':
        i -= 1
    # Skip word chars
    while i > 0 and (text[i-1].isalnum() or text[i-1] == '_'):
        i -= 1
    return i

def _word_boundary_right(text: str, pos: int) -> int:
    """Return position of end of word to the right of pos."""
    n = len(text)
    if pos >= n:
        return n
    i = pos
    # Skip leading whitespace
    while i < n and not text[i].isalnum() and text[i] != '_':
        i += 1
    # Skip word chars
    while i < n and (text[i].isalnum() or text[i] == '_'):
        i += 1
    return i
```

---

### 1.3 Trigger/Dropdown Bindings (dropdown is open)

These bindings take precedence over input mode bindings when `input_state.dropdown_open is True`.

| Key | Action | Notes |
|-----|--------|-------|
| `Up` / `Ctrl+P` | Move selection up | Wraps: top → bottom. |
| `Down` / `Ctrl+N` | Move selection down | Wraps: bottom → top. |
| `Enter` | Accept selected item and submit | Replaces trigger fragment with selection; closes dropdown; does NOT submit the full message — inserts completion only. |
| `Tab` | Accept selected item, keep typing | Replaces fragment; closes dropdown; cursor stays in input. |
| `Escape` | Cancel dropdown | Restores input to pre-trigger state; cursor stays at `@` or `/`. |
| `Backspace` | Delete last fragment char | If fragment becomes empty after delete: keep dropdown open showing all items. If trigger char itself is deleted: close dropdown. |
| Any printable char | Append to fragment, re-filter | e.g. typing `a` after `@src` filters to `@srca*`. |
| `Ctrl+C` | Cancel dropdown and turn | Dropdown closes; then global Ctrl+C logic runs. |
| `?` | Show detail for highlighted command | Only in `/command` mode. Shows argument hints. |
| `Page Up` / `Page Down` | Scroll dropdown list | When list has >8 items. |

**Dropdown does NOT intercept:**
- `Shift+Tab` (mode cycle — always global)
- `Ctrl+L` (redraw — always global)
- `Ctrl+B` (background — always global)

---

### 1.4 During Agent Turn (agent is processing, input is disabled)

| Key | Action | Notes |
|-----|--------|-------|
| `Ctrl+C` | Interrupt agent turn | First press cancels turn. Second press within 2s exits. |
| `Escape` | Stop streaming, keep work | Equivalent to Ctrl+C first press. Turn committed as `[cancelled]`. |
| `Ctrl+B` | Background the turn | Input returns to idle; agent continues. |
| Any printable char | Buffer for next message | Displayed in input bar immediately (visual-only). NOT sent until user presses Enter after turn completes. |
| `Enter` | No-op (or enqueue) | Typed text is buffered. After turn completes, user must explicitly press Enter again to submit. (v1: buffer but don't auto-submit.) |

**Input disabled visual state:** The input bar prompt changes from `>` to `⠸` (spinner) and background dims. Typed characters still appear so user can compose the next message.

---

### 1.5 Approval Dialog Bindings

The approval gate replaces the normal footer in the bottom block. These bindings are active only when `InputState._approval_pending is True`.

| Key | Action | Notes |
|-----|--------|-------|
| `y` / `Y` | Approve this tool call | Emits `ApprovalGranted` event. |
| `n` / `N` | Reject this tool call | Emits `ApprovalDenied` event. |
| `a` / `A` | Approve all (session-scoped) | Auto-approves this tool name for the rest of the session. Registers `LifecycleHook` via `CommunicationTools.hook_register`. |
| `Enter` | Approve (default is Yes) | Same as `y`. |
| `Escape` | Reject | Same as `n`. |
| `d` / `D` | Show full diff | Expands `DiffViewer` in the bottom block (does not submit approval decision). |
| `e` / `E` | Edit before approving | Only for `run_bash`-type tools. Opens an inline edit prompt for the command text. |
| `s` / `S` | Skip (queue only) | When batched approvals are queued: skip this item, go to next. Returns to this item at end of queue. |
| `←` / `→` | Scroll diff left/right | When diff is wider than terminal. |
| `Ctrl+C` | Reject and exit | Immediately exits the session. Tool auto-rejected. |

---

### 1.6 Doom-Loop Response Bindings

Active only when `DoomLoopDetector` has fired and the `_doom_loop_pending` flag is set.

| Key | Action |
|-----|--------|
| `c` / `C` | Cancel the current turn |
| `r` / `R` | Retry once more (resets doom-loop counter) |
| `i` / `I` | Inject message mid-turn (opens inline text input) |
| `Escape` | Equivalent to Cancel |

---

### 1.7 Error Banner Bindings

Active only when a critical error banner is displayed (API failure, rate limit, etc.).

| Key | Action |
|-----|--------|
| `r` / `R` | Retry now |
| `w` / `W` | Wait and auto-retry (countdown timer) |
| `c` / `C` | Cancel turn |
| `Escape` | Dismiss banner (does not cancel turn) |

---

## 2. InputState State Machine — Complete Specification

### 2.1 Type Definitions

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Awaitable, Callable


class InputMode(Enum):
    """Primary mode of the input state machine."""
    IDLE = auto()               # Empty buffer, no agent turn
    TYPING = auto()             # User entering text
    MENTION_TRIGGER = auto()    # @ was typed; dropdown shows files
    COMMAND_TRIGGER = auto()    # / at position 0; dropdown shows commands
    DISABLED = auto()           # Agent turn active; chars buffer only
    APPROVAL = auto()           # Approval gate active; y/n/a/Enter expected
    DOOM_LOOP = auto()          # Doom-loop banner; c/r/i expected
    ERROR_BANNER = auto()       # Critical error banner; r/w/c expected


class InputResultKind(Enum):
    CONTINUE = auto()       # Key was consumed; redraw needed
    SUBMIT = auto()         # User submitted text; text field contains the message
    EXIT = auto()           # Process should exit with code 0
    CANCEL_TURN = auto()    # Cancel the active agent turn
    BACKGROUND_TURN = auto() # Background the active agent turn
    WARN_EXIT = auto()       # Warn user that second Ctrl+C will exit
    NO_CHANGE = auto()       # Key was consumed but state is identical; no redraw


@dataclass
class InputResult:
    kind: InputResultKind
    text: str = ""          # Populated when kind == SUBMIT
    extra: dict = field(default_factory=dict)  # mode-specific payload


@dataclass
class MatchItem:
    """One item in a trigger dropdown."""
    value: str              # The string to insert when selected
    label: str              # Display label (may differ from value)
    kind: str               # "file" | "dir" | "glob" | "url" | "command" | "skill" | "mcp"
    hint: str = ""          # Argument hint or file size
    description: str = ""   # One-line description (commands only)
```

### 2.2 InputState Class — Complete Interface

```python
@dataclass
class InputState:
    """
    All mutable TUI input state. Lives outside AppState (TUI-only).
    Never stored in the event log. Not serialised.
    """

    # --- Buffer ---
    text: str = ""
    cursor: int = 0

    # --- History ---
    history: list[str] = field(default_factory=list)
    hist_idx: int = -1          # -1 = not navigating history
    saved_buf: str = ""         # buffer saved before history navigation

    # --- Kill ring (Ctrl+K / Ctrl+U / Ctrl+W yank) ---
    kill_ring: list[str] = field(default_factory=list)

    # --- Mode ---
    mode: InputMode = InputMode.IDLE

    # --- Ctrl+C double-press ---
    ctrl_c_count: int = 0
    ctrl_c_last_time: float = 0.0

    # --- Trigger/dropdown ---
    active_trigger_char: str | None = None   # "@" or "/"
    trigger_start: int = 0                   # cursor pos when trigger fired
    fragment: str = ""                        # text after trigger char
    matches: list[MatchItem] = field(default_factory=list)
    selected: int = 0                        # highlighted index in dropdown
    dropdown_rows: list[str] = field(default_factory=list)  # rendered rows

    # --- Agent turn state ---
    agent_turn_active: bool = False

    # --- Approval gate ---
    approval_pending: bool = False
    approval_tool_name: str = ""
    approval_description: str = ""
    approval_has_diff: bool = False

    # --- Doom-loop flag ---
    doom_loop_pending: bool = False

    # --- Error banner ---
    error_banner_active: bool = False
    error_banner_message: str = ""

    @property
    def dropdown_open(self) -> bool:
        return self.mode in (InputMode.MENTION_TRIGGER, InputMode.COMMAND_TRIGGER)

    def handle(self, key_name: str, char: str) -> InputResult:
        """
        Central dispatch. Call for every key event.
        key_name: normalized key name e.g. "ctrl+c", "shift+tab", "enter",
                  "backspace", "up", "down", "left", "right", or a single char.
        char: the actual character (empty string for control keys).
        Returns InputResult describing what the caller should do.
        """
        # --- Global bindings always intercepted first ---
        if key_name == "ctrl+c":
            return self._handle_ctrl_c()
        if key_name == "shift+tab":
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "cycle_mode", "direction": 1})
        if key_name == "alt+shift+tab":
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "cycle_mode", "direction": -1})
        if key_name == "ctrl+l":
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "redraw"})
        if key_name == "ctrl+b":
            return InputResult(kind=InputResultKind.BACKGROUND_TURN)

        # --- Route to mode-specific handler ---
        if self.mode == InputMode.APPROVAL:
            return self._handle_approval(key_name, char)
        if self.mode == InputMode.DOOM_LOOP:
            return self._handle_doom_loop(key_name, char)
        if self.mode == InputMode.ERROR_BANNER:
            return self._handle_error_banner(key_name, char)
        if self.mode == InputMode.DISABLED:
            return self._handle_disabled(key_name, char)
        if self.dropdown_open:
            return self._handle_dropdown(key_name, char)

        # --- Normal / typing input ---
        return self._handle_normal(key_name, char)

    # ------------------------------------------------------------------ #
    # Mode-specific handlers                                               #
    # ------------------------------------------------------------------ #

    def _handle_ctrl_c(self) -> InputResult:
        now = time.monotonic()
        if now - self.ctrl_c_last_time > 2.0:
            self.ctrl_c_count = 0
        self.ctrl_c_count += 1
        self.ctrl_c_last_time = now
        if self.ctrl_c_count >= 2:
            return InputResult(kind=InputResultKind.EXIT)
        if self.agent_turn_active:
            return InputResult(kind=InputResultKind.CANCEL_TURN)
        return InputResult(kind=InputResultKind.WARN_EXIT)

    def _handle_normal(self, key_name: str, char: str) -> InputResult:
        if key_name == "ctrl+d":
            if self.text == "":
                return InputResult(kind=InputResultKind.EXIT)
            return self._delete_right()
        if key_name == "enter":
            if self.text.endswith("\\"):
                self._replace_trailing_backslash_with_newline()
                return InputResult(kind=InputResultKind.CONTINUE)
            return self._submit()
        if key_name in ("shift+enter", "alt+enter"):
            self._insert_char("\n")
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "backspace":
            self._backspace()
            self._check_trigger_after_backspace()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "delete":
            return self._delete_right()
        if key_name == "left":
            self._move_left()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "right":
            self._move_right()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name in ("ctrl+left", "alt+b"):
            self.cursor = _word_boundary_left(self.text, self.cursor)
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name in ("ctrl+right", "alt+f"):
            self.cursor = _word_boundary_right(self.text, self.cursor)
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name in ("home", "ctrl+a"):
            self._move_to_line_start()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name in ("end", "ctrl+e"):
            self._move_to_line_end()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name in ("up", "ctrl+p"):
            self._history_up()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name in ("down", "ctrl+n"):
            self._history_down()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "ctrl+u":
            self._kill_to_line_start()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "ctrl+k":
            self._kill_to_line_end()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "ctrl+w":
            self._kill_word_backward()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "alt+d":
            self._kill_word_forward()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "ctrl+y":
            self._yank()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "escape":
            if self.text:
                self._clear()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "tab":
            return InputResult(kind=InputResultKind.CONTINUE)  # no-op in v1
        if char and char.isprintable():
            self._insert_char(char)
            self._check_triggers()
            return InputResult(kind=InputResultKind.CONTINUE)
        return InputResult(kind=InputResultKind.NO_CHANGE)

    def _handle_dropdown(self, key_name: str, char: str) -> InputResult:
        if key_name in ("up", "ctrl+p"):
            self._dropdown_up()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name in ("down", "ctrl+n"):
            self._dropdown_down()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "enter":
            return self._dropdown_accept(submit=False)
        if key_name == "tab":
            return self._dropdown_accept(submit=False)
        if key_name == "escape":
            self._dropdown_cancel()
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name == "backspace":
            if self.fragment:
                self.fragment = self.fragment[:-1]
                self.cursor -= 1
                self.text = (self.text[:self.trigger_start + 1]
                             + self.fragment
                             + self.text[self.trigger_start + 1 + len(self.fragment) + 1:])
                self._refilter_matches()
            else:
                # Backspace deleted the trigger char itself
                self._backspace()
                self._close_dropdown()
            return InputResult(kind=InputResultKind.CONTINUE)
        if char and char.isprintable():
            self.fragment += char
            self.cursor += 1
            self.text = (self.text[:self.trigger_start + 1]
                         + self.fragment
                         + self.text[self.trigger_start + 1 + len(self.fragment) - 1:])
            self._refilter_matches()
            return InputResult(kind=InputResultKind.CONTINUE)
        return InputResult(kind=InputResultKind.NO_CHANGE)

    def _handle_disabled(self, key_name: str, char: str) -> InputResult:
        """Agent turn active. Buffer printable input for next turn."""
        if key_name == "escape":
            return InputResult(kind=InputResultKind.CANCEL_TURN)
        if char and char.isprintable():
            self._insert_char(char)
            return InputResult(kind=InputResultKind.CONTINUE)
        if key_name in ("shift+enter", "alt+enter"):
            self._insert_char("\n")
            return InputResult(kind=InputResultKind.CONTINUE)
        # Backspace works in disabled mode (user fixing pre-typed text)
        if key_name == "backspace":
            self._backspace()
            return InputResult(kind=InputResultKind.CONTINUE)
        return InputResult(kind=InputResultKind.NO_CHANGE)

    def _handle_approval(self, key_name: str, char: str) -> InputResult:
        lc = char.lower() if char else ""
        if lc == "y" or key_name == "enter":
            self.approval_pending = False
            self.mode = InputMode.IDLE
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "approval_grant"})
        if lc == "n" or key_name == "escape":
            self.approval_pending = False
            self.mode = InputMode.IDLE
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "approval_deny"})
        if lc == "a":
            self.approval_pending = False
            self.mode = InputMode.IDLE
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "approval_allow_all",
                                      "tool": self.approval_tool_name})
        if lc == "d":
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "approval_show_diff"})
        if lc == "e":
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "approval_edit_command"})
        if lc == "s":
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "approval_skip"})
        return InputResult(kind=InputResultKind.NO_CHANGE)

    def _handle_doom_loop(self, key_name: str, char: str) -> InputResult:
        lc = char.lower() if char else ""
        if lc == "c" or key_name == "escape":
            self.doom_loop_pending = False
            self.mode = InputMode.IDLE
            return InputResult(kind=InputResultKind.CANCEL_TURN)
        if lc == "r":
            self.doom_loop_pending = False
            self.mode = InputMode.DISABLED
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "doom_loop_retry"})
        if lc == "i":
            self.doom_loop_pending = False
            self.mode = InputMode.TYPING
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "doom_loop_inject"})
        return InputResult(kind=InputResultKind.NO_CHANGE)

    def _handle_error_banner(self, key_name: str, char: str) -> InputResult:
        lc = char.lower() if char else ""
        if lc == "r":
            self.error_banner_active = False
            self.mode = InputMode.DISABLED
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "error_retry"})
        if lc == "w":
            self.error_banner_active = False
            self.mode = InputMode.DISABLED
            return InputResult(kind=InputResultKind.CONTINUE,
                               extra={"action": "error_wait_retry"})
        if lc == "c" or key_name == "escape":
            self.error_banner_active = False
            self.mode = InputMode.IDLE
            return InputResult(kind=InputResultKind.CANCEL_TURN)
        return InputResult(kind=InputResultKind.NO_CHANGE)

    # ------------------------------------------------------------------ #
    # Buffer primitives                                                    #
    # ------------------------------------------------------------------ #

    def _insert_char(self, ch: str) -> None:
        self.text = self.text[:self.cursor] + ch + self.text[self.cursor:]
        self.cursor += 1
        if self.hist_idx != -1:
            self.hist_idx = -1

    def _backspace(self) -> None:
        if self.cursor > 0:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1

    def _delete_right(self) -> InputResult:
        if self.cursor < len(self.text):
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        return InputResult(kind=InputResultKind.CONTINUE)

    def _move_left(self) -> None:
        if self.cursor > 0:
            self.cursor -= 1

    def _move_right(self) -> None:
        if self.cursor < len(self.text):
            self.cursor += 1

    def _move_to_line_start(self) -> None:
        """Jump to start of the current line (before last \n)."""
        before = self.text[:self.cursor]
        nl = before.rfind("\n")
        self.cursor = nl + 1 if nl != -1 else 0

    def _move_to_line_end(self) -> None:
        """Jump to end of the current line (before next \n)."""
        after = self.text[self.cursor:]
        nl = after.find("\n")
        self.cursor = self.cursor + nl if nl != -1 else len(self.text)

    def _kill_to_line_start(self) -> None:
        nl = self.text[:self.cursor].rfind("\n")
        line_start = nl + 1 if nl != -1 else 0
        killed = self.text[line_start:self.cursor]
        if killed:
            self.kill_ring.append(killed)
        self.text = self.text[:line_start] + self.text[self.cursor:]
        self.cursor = line_start

    def _kill_to_line_end(self) -> None:
        after = self.text[self.cursor:]
        nl = after.find("\n")
        end = self.cursor + nl if nl != -1 else len(self.text)
        killed = self.text[self.cursor:end]
        if killed:
            self.kill_ring.append(killed)
        self.text = self.text[:self.cursor] + self.text[end:]

    def _kill_word_backward(self) -> None:
        new_pos = _word_boundary_left(self.text, self.cursor)
        killed = self.text[new_pos:self.cursor]
        if killed:
            self.kill_ring.append(killed)
        self.text = self.text[:new_pos] + self.text[self.cursor:]
        self.cursor = new_pos

    def _kill_word_forward(self) -> None:
        new_pos = _word_boundary_right(self.text, self.cursor)
        killed = self.text[self.cursor:new_pos]
        if killed:
            self.kill_ring.append(killed)
        self.text = self.text[:self.cursor] + self.text[new_pos:]

    def _yank(self) -> None:
        if self.kill_ring:
            yanked = self.kill_ring[-1]
            self.text = self.text[:self.cursor] + yanked + self.text[self.cursor:]
            self.cursor += len(yanked)

    def _clear(self) -> None:
        if self.hist_idx != -1:
            self.hist_idx = -1
            self.saved_buf = ""
        self.text = ""
        self.cursor = 0

    def _replace_trailing_backslash_with_newline(self) -> None:
        assert self.text.endswith("\\")
        self.text = self.text[:-1] + "\n"
        # cursor stays at same position (replaces 1 char with 1 char)

    def _submit(self) -> InputResult:
        text = self.text
        if not text.strip():
            return InputResult(kind=InputResultKind.NO_CHANGE)
        self.history.append(text)
        if len(self.history) > 200:
            self.history = self.history[-200:]
        self.hist_idx = -1
        self.saved_buf = ""
        self.text = ""
        self.cursor = 0
        return InputResult(kind=InputResultKind.SUBMIT, text=text)

    # ------------------------------------------------------------------ #
    # History                                                              #
    # ------------------------------------------------------------------ #

    def _history_up(self) -> None:
        if not self.history:
            return
        if self.hist_idx == -1:
            self.saved_buf = self.text
        if self.hist_idx < len(self.history) - 1:
            self.hist_idx += 1
            self.text = self.history[-(self.hist_idx + 1)]
            self.cursor = len(self.text)

    def _history_down(self) -> None:
        if self.hist_idx == -1:
            return
        if self.hist_idx > 0:
            self.hist_idx -= 1
            self.text = self.history[-(self.hist_idx + 1)]
            self.cursor = len(self.text)
        else:
            self.hist_idx = -1
            self.text = self.saved_buf
            self.cursor = len(self.text)

    # ------------------------------------------------------------------ #
    # Trigger detection                                                    #
    # ------------------------------------------------------------------ #

    def _check_triggers(self) -> None:
        if self.mode not in (InputMode.IDLE, InputMode.TYPING):
            return
        # Slash command: / must be the first non-whitespace char
        stripped = self.text[:self.cursor]
        if stripped.lstrip() == self.text[:self.cursor] and self.text[:self.cursor].startswith("/"):
            fragment_start = 1
            self.active_trigger_char = "/"
            self.trigger_start = 0
            self.fragment = self.text[fragment_start:self.cursor]
            self.mode = InputMode.COMMAND_TRIGGER
            self._refilter_matches()
            return
        # At-mention: @ preceded by whitespace or at pos 0, not part of email
        c = self.cursor
        if c > 0 and self.text[c - 1] == "@":
            pre = self.text[:c - 1]
            if not pre or not pre[-1].isalnum():
                self.active_trigger_char = "@"
                self.trigger_start = c - 1
                self.fragment = ""
                self.mode = InputMode.MENTION_TRIGGER
                self._refilter_matches()
                return

    def _check_trigger_after_backspace(self) -> None:
        """After backspace, close triggers if trigger char was deleted."""
        if self.dropdown_open:
            if self.cursor <= self.trigger_start:
                self._close_dropdown()
            else:
                self.fragment = self.text[self.trigger_start + 1:self.cursor]
                self._refilter_matches()

    def _close_dropdown(self) -> None:
        self.mode = InputMode.TYPING if self.text else InputMode.IDLE
        self.active_trigger_char = None
        self.fragment = ""
        self.matches = []
        self.selected = 0
        self.dropdown_rows = []

    def _dropdown_cancel(self) -> None:
        self._close_dropdown()

    def _dropdown_up(self) -> None:
        if self.matches:
            self.selected = (self.selected - 1) % len(self.matches)

    def _dropdown_down(self) -> None:
        if self.matches:
            self.selected = (self.selected + 1) % len(self.matches)

    def _dropdown_accept(self, *, submit: bool) -> InputResult:
        if not self.matches:
            self._close_dropdown()
            return InputResult(kind=InputResultKind.CONTINUE)
        item = self.matches[self.selected]
        # Replace trigger+fragment with selected value
        before_trigger = self.text[:self.trigger_start]
        after_fragment = self.text[self.trigger_start + 1 + len(self.fragment):]
        if self.active_trigger_char == "@":
            replacement = "@" + item.value + " "
        else:
            replacement = "/" + item.value + " "
        self.text = before_trigger + replacement + after_fragment
        self.cursor = len(before_trigger) + len(replacement)
        self._close_dropdown()
        self.mode = InputMode.TYPING
        return InputResult(kind=InputResultKind.CONTINUE,
                           extra={"action": "completion_accepted",
                                  "value": item.value,
                                  "kind": self.active_trigger_char})

    def _refilter_matches(self) -> None:
        """Called by the app after updating self.matches based on fragment."""
        # self.matches is set externally by MentionCache / CommandRegistry
        # This method resets selected index when filter changes.
        self.selected = 0

    # ------------------------------------------------------------------ #
    # External state transitions (called by App)                          #
    # ------------------------------------------------------------------ #

    def set_matches(self, matches: list[MatchItem]) -> None:
        """App calls this after async match resolution."""
        self.matches = matches
        self.selected = 0
        self.dropdown_rows = [
            f"  {m.label:<40} {m.kind:<12} {m.hint}"
            for m in matches[:8]
        ]

    def set_agent_turn_active(self, active: bool) -> None:
        self.agent_turn_active = active
        if active:
            self.mode = InputMode.DISABLED
        else:
            self.mode = InputMode.TYPING if self.text else InputMode.IDLE

    def set_approval_pending(
        self, tool_name: str, description: str, has_diff: bool
    ) -> None:
        self.approval_pending = True
        self.approval_tool_name = tool_name
        self.approval_description = description
        self.approval_has_diff = has_diff
        self.mode = InputMode.APPROVAL

    def set_doom_loop(self) -> None:
        self.doom_loop_pending = True
        self.mode = InputMode.DOOM_LOOP

    def set_error_banner(self, message: str) -> None:
        self.error_banner_active = True
        self.error_banner_message = message
        self.mode = InputMode.ERROR_BANNER
```

### 2.3 State Transition Diagram

```
                    ┌─────────┐
              ┌────►│  IDLE   │◄────────────────────────┐
              │     └────┬────┘                          │
              │          │ char typed                    │
              │          ▼                               │
              │     ┌─────────┐   submit / Escape+empty  │
              │     │ TYPING  │──────────────────────────┤
              │     └────┬────┘                          │
              │          │ @ (not email)                 │
              │          ▼                               │
              │  ┌────────────────┐  Escape/backspace     │
              │  │ MENTION_TRIGGER│──────────────────────►│
              │  └────────────────┘                      │
              │          │ / at pos 0                    │
              │          ▼                               │
              │  ┌─────────────────┐  Escape             │
              │  │ COMMAND_TRIGGER │────────────────────►│
              │  └─────────────────┘                     │
              │                                          │
              │  ┌──────────────┐  turn ends             │
              │  │   DISABLED   │──────────────────────►─┤
              │  └──────────────┘                        │
              │  ┌──────────────┐  y/n/a + Escape        │
              │  │   APPROVAL   │──────────────────────►─┤
              │  └──────────────┘                        │
              │  ┌──────────────┐  c/r/i                 │
              │  │  DOOM_LOOP   │──────────────────────►─┤
              │  └──────────────┘                        │
              │  ┌──────────────────┐  r/w/c             │
              └──│   ERROR_BANNER   │──────────────────►─┘
                 └──────────────────┘
```

---

## 3. @mention System

### 3.1 TriggerRegistry

```python
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class AtMentionItem:
    """One completion item for @mention."""
    value: str          # Inserted text (relative path, URL, agent name)
    label: str          # Display label
    kind: str           # "file" | "dir" | "glob" | "url" | "agent"
    size_bytes: int = 0
    hint: str = ""      # e.g. "12.3 KB" or "(34 files)"


class MentionResolver(Protocol):
    async def resolve(self, prefix: str, cwd: Path) -> list[AtMentionItem]: ...


class FileMentionResolver:
    """
    Resolves @mention completions from the filesystem.
    Uses glob with a 200-item limit. Results cached for 2s TTL.
    """
    _CACHE_TTL: float = 2.0

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, list[AtMentionItem]]] = {}

    async def resolve(self, prefix: str, cwd: Path) -> list[AtMentionItem]:
        cache_key = f"{cwd}:{prefix}"
        now = time.monotonic()
        if cache_key in self._cache:
            ts, items = self._cache[cache_key]
            if now - ts < self._CACHE_TTL:
                return items
        items = await asyncio.to_thread(self._resolve_sync, prefix, cwd)
        self._cache[cache_key] = (now, items)
        return items

    def _resolve_sync(self, prefix: str, cwd: Path) -> list[AtMentionItem]:
        results: list[AtMentionItem] = []
        # Build glob pattern
        if not prefix:
            pattern = "*"
        elif "*" in prefix or "?" in prefix:
            pattern = prefix  # user typed a glob
        else:
            pattern = f"{prefix}*"

        try:
            for p in sorted(cwd.glob(pattern))[:200]:
                rel = p.relative_to(cwd)
                rel_str = str(rel)
                if p.is_dir():
                    results.append(AtMentionItem(
                        value=rel_str + "/",
                        label=rel_str + "/",
                        kind="dir",
                        hint=f"({sum(1 for _ in p.iterdir())} items)",
                    ))
                else:
                    size = p.stat().st_size
                    results.append(AtMentionItem(
                        value=rel_str,
                        label=rel_str,
                        kind="file",
                        size_bytes=size,
                        hint=_format_size(size),
                    ))
        except (PermissionError, OSError):
            pass
        return results[:200]


def should_trigger_at_mention(text: str, cursor_pos: int) -> bool:
    """
    Return True if the @ at cursor_pos-1 should open the mention dropdown.
    Rules:
    - @ must be the character just inserted (text[cursor_pos-1] == '@')
    - The character before @ (if any) must NOT be alphanumeric (email check)
    - cursor_pos must be > 0
    """
    if cursor_pos == 0:
        return False
    if text[cursor_pos - 1] != "@":
        return False
    pre_at = text[:cursor_pos - 1]
    if pre_at and pre_at[-1].isalnum():
        return False  # email address context
    return True


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n // 1024:.1f} KB"
    return f"{n // (1024*1024):.1f} MB"
```

### 3.2 @mention Match Algorithm

```python
def filter_mention_items(
    items: list[AtMentionItem],
    fragment: str,
    *,
    case_sensitive: bool = False,
) -> list[AtMentionItem]:
    """
    Filter and rank mention items by fragment.
    Ranking (descending priority):
      1. Exact prefix match (case-insensitive by default)
      2. Contains fragment as substring
      3. Fuzzy: all chars of fragment appear in order in value
    Returns at most 8 items.
    """
    if not fragment:
        return items[:8]

    frag = fragment if case_sensitive else fragment.lower()
    exact: list[AtMentionItem] = []
    contains: list[AtMentionItem] = []
    fuzzy: list[AtMentionItem] = []

    for item in items:
        val = item.value if case_sensitive else item.value.lower()
        if val.startswith(frag):
            exact.append(item)
        elif frag in val:
            contains.append(item)
        elif _fuzzy_match(frag, val):
            fuzzy.append(item)

    merged = exact + contains + fuzzy
    return merged[:8]


def _fuzzy_match(frag: str, val: str) -> bool:
    """True if all chars of frag appear in val in order."""
    fi = 0
    for ch in val:
        if fi < len(frag) and ch == frag[fi]:
            fi += 1
    return fi == len(frag)
```

### 3.3 @mention Rendering in Transcript (MentionChip)

After submission, `parse_mentions(text)` extracts all `@token` occurrences and resolves them asynchronously. Each resolved mention becomes a `MentionChip` in the `UserMessage`:

```python
import re
from dataclasses import dataclass
from enum import Enum, auto


class MentionKind(Enum):
    FILE = auto()
    DIRECTORY = auto()
    GLOB = auto()
    URL = auto()
    AGENT = auto()
    UNRESOLVED = auto()


@dataclass
class Mention:
    raw: str            # original @token text
    value: str          # resolved value (path, URL)
    kind: MentionKind
    resolved: bool
    content: str = ""   # injected file content (populated by context injection)
    char_count: int = 0

    def chip_label(self) -> str:
        """Display text inside the chip bracket."""
        if self.kind == MentionKind.FILE:
            label = self.value
            if self.char_count > 0:
                label += f"  {_format_size(self.char_count)}"
            return label
        if self.kind == MentionKind.DIRECTORY:
            return self.value + "/"
        if self.kind == MentionKind.GLOB:
            return self.value
        if self.kind == MentionKind.URL:
            return self.value[:40] + ("…" if len(self.value) > 40 else "")
        if self.kind == MentionKind.UNRESOLVED:
            return f"?{self.raw}"
        return self.raw

    def chip_ansi(self, color: bool = True) -> str:
        """Rendered chip for committed transcript."""
        label = self.chip_label()
        if not color:
            return f"[@{label}]"
        color_codes = {
            MentionKind.FILE: "\033[34m",        # blue
            MentionKind.DIRECTORY: "\033[36m",   # cyan
            MentionKind.GLOB: "\033[33m",        # yellow
            MentionKind.URL: "\033[32m",         # green
            MentionKind.UNRESOLVED: "\033[31m",  # red
            MentionKind.AGENT: "\033[35m",       # magenta
        }
        code = color_codes.get(self.kind, "")
        return f"{code}[@{label}]\033[0m"


_MENTION_RE = re.compile(r"(?<!\w)@(\S+)")

def parse_mentions(text: str) -> list[tuple[int, int, str]]:
    """
    Return list of (start, end, token) for all @mention occurrences
    that should trigger resolution. Skips email addresses.
    """
    results = []
    for m in _MENTION_RE.finditer(text):
        start = m.start()
        # Check if preceded by alnum (email guard)
        if start > 0 and text[start - 1].isalnum():
            continue
        results.append((start, m.end(), m.group(1)))
    return results
```

### 3.4 Context Injection Flow

When the user submits a message containing `@file` mentions:

1. `parse_mentions(text)` extracts all tokens.
2. For each token, `AtMentionResolver.resolve_to_content(token, cwd)` is called (async).
3. File content is prepended to the LLM system prompt as a context block:
   ```
   <context>
   <file path="src/auth.py">
   {file content}
   </file>
   </context>
   ```
4. A `MentionChip` per mention is added to the `UserMessage` in the transcript showing the resolved file and size.
5. Unresolved mentions render as `[@?missing-file.py]` in red and are NOT injected.

```python
async def build_context_prefix(
    mentions: list[Mention],
    cwd: Path,
    *,
    max_total_chars: int = 200_000,
) -> str:
    """
    Build the <context> block to prepend to the system prompt.
    Respects max_total_chars budget; truncates largest files first.
    """
    parts: list[str] = []
    total = 0
    for mention in sorted(mentions, key=lambda m: m.char_count):
        if not mention.resolved or not mention.content:
            continue
        content = mention.content
        if total + len(content) > max_total_chars:
            remaining = max_total_chars - total
            if remaining < 100:
                break
            content = content[:remaining] + "\n[... truncated ...]"
        parts.append(
            f'<file path="{mention.value}">\n{content}\n</file>'
        )
        total += len(content)
    if not parts:
        return ""
    return "<context>\n" + "\n".join(parts) + "\n</context>\n"
```

---

## 4. / Command System

### 4.1 All Built-in Commands

| Command | Arguments | Description | Source |
|---------|-----------|-------------|--------|
| `/help` | `[command]` | Show all commands, or detail for one command | Built-in |
| `/mode` | `[auto\|plan\|ask\|review\|safe\|debug]` | Show or switch permission mode | Built-in |
| `/model` | `[provider model]` | Show or switch LLM model | Built-in |
| `/clear` | | Clear transcript display (not history) | Built-in |
| `/history` | `[N]` | Show last N turns (default 10) | Built-in |
| `/status` | | Show active agents, tokens, cost | Built-in |
| `/skills` | | List loaded skills | Built-in |
| `/expand` | `<tool-id\|@path>` | Expand collapsed tool output or file | Built-in |
| `/workflow` | | Show current DAG progress | Built-in |
| `/config` | | Show current configuration (read-only) | Built-in |
| `/mcp` | | Show MCP server status and tools | Built-in |
| `/btw` | `<question>` | Ask ephemeral side question (not saved to history) | Built-in |
| `/resume` | `<session-id>` | Resume a previous session | Built-in |
| `/sessions` | | List all sessions | Built-in |
| `/exit` | | Graceful exit (saves session) | Built-in |
| `/cancel` | | Cancel the current agent turn | Built-in |
| `/context` | | Show active context window usage | Built-in |
| `/tool-output` | `<tool-id>` | Show full output of a tool call | Built-in |
| `/permissions` | | Show recently denied/approved actions | Built-in |
| `/compact` | | Summarise and compact context window | Built-in |
| `/vim` | | Toggle vim mode in input bar | Built-in |
| `/debug` | | Show debug information (kernel state, event log tail) | Built-in |

**Skill-injected commands** (registered by `SkillRegistry` at startup):

| Command | Description | Source |
|---------|-------------|--------|
| `/deep-research` | Deep research harness | Skills |
| `/review` | Review a pull request | Skills |
| `/code-review` | Code review with inline comments | Skills |
| `/verify` | Verify a code change works | Skills |
| `/simplify` | Simplify changed code | Skills |
| `/security-review` | Security review of pending changes | Skills |
| `/run` | Run the application | Skills |
| `/init` | Initialize CLAUDE.md | Skills |

**MCP-injected commands** are prefixed `mcp:<server>:<command>` and registered by `MCPBridge` at startup.

### 4.2 Command Dispatch Architecture

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class CommandDefinition:
    name: str                           # Without leading slash; e.g. "mode"
    description: str                    # One line
    arg_hint: str = ""                  # e.g. "[auto|plan|ask|review|safe|debug]"
    source: str = "builtin"             # "builtin" | "skill" | "plugin" | "mcp"
    handler: Callable[..., Awaitable[None]] | None = None
    aliases: list[str] = field(default_factory=list)
    hidden: bool = False                # Hidden from palette (e.g. /debug)


class UnifiedCommandRegistry:
    """
    Aggregates commands from all sources.
    Thread-safe: all mutations use asyncio.Lock.
    """

    def __init__(self) -> None:
        self._commands: dict[str, CommandDefinition] = {}
        self._lock = asyncio.Lock()

    async def register(self, cmd: CommandDefinition) -> None:
        async with self._lock:
            self._commands[cmd.name] = cmd
            for alias in cmd.aliases:
                self._commands[alias] = cmd

    async def unregister(self, name: str) -> None:
        async with self._lock:
            self._commands.pop(name, None)

    def get(self, name: str) -> CommandDefinition | None:
        return self._commands.get(name.lstrip("/"))

    def all_visible(self) -> list[CommandDefinition]:
        seen: set[str] = set()
        result: list[CommandDefinition] = []
        for cmd in self._commands.values():
            if cmd.name not in seen and not cmd.hidden:
                seen.add(cmd.name)
                result.append(cmd)
        return sorted(result, key=lambda c: (c.source, c.name))

    def filter(self, fragment: str) -> list[CommandDefinition]:
        """
        Fuzzy-filter commands by fragment.
        Ranking: exact prefix > description contains > fuzzy name match.
        Returns at most 8 items.
        """
        frag = fragment.lower()
        exact: list[CommandDefinition] = []
        desc_match: list[CommandDefinition] = []
        fuzzy: list[CommandDefinition] = []

        for cmd in self.all_visible():
            name = cmd.name.lower()
            if name.startswith(frag):
                exact.append(cmd)
            elif frag in cmd.description.lower():
                desc_match.append(cmd)
            elif _fuzzy_match(frag, name):
                fuzzy.append(cmd)

        return (exact + desc_match + fuzzy)[:8]


async def dispatch_command(
    registry: UnifiedCommandRegistry,
    text: str,
    *,
    commit_line: Callable[[str], None],
) -> None:
    """
    Parse and dispatch a /command from submitted text.
    text starts with '/'.
    Emits committed output lines via commit_line().
    Raises CommandNotFound if command not registered.
    """
    parts = text[1:].split(maxsplit=1)
    if not parts:
        return
    name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    cmd = registry.get(name)
    if cmd is None:
        commit_line(f"\033[31m✗ Unknown command: /{name}\033[0m  (type /help for list)")
        return
    if cmd.handler is None:
        commit_line(f"\033[2m/{name}: no handler registered\033[0m")
        return
    await cmd.handler(args, commit_line=commit_line)
```

### 4.3 Plugin Command Integration

Plugin commands are registered via `PluginRegistry` during plugin load. Each plugin's `register_command()` method calls `UnifiedCommandRegistry.register()` with `source="plugin"`. The command name must not conflict with built-in names (enforced at registration — raises `CommandNameConflict`).

```python
class CommandNameConflict(Exception):
    pass

# In UnifiedCommandRegistry.register():
async def register(self, cmd: CommandDefinition) -> None:
    async with self._lock:
        if cmd.name in self._commands:
            existing = self._commands[cmd.name]
            if existing.source == "builtin" and cmd.source != "builtin":
                raise CommandNameConflict(
                    f"/{cmd.name} is a built-in command and cannot be overridden"
                )
        self._commands[cmd.name] = cmd
```

### 4.4 Async Command Execution

Commands run as `asyncio.Task` objects, not blocking the render loop. The command output is committed to the transcript via `RenderLoop.force_commit()`:

```python
async def run_command_task(
    registry: UnifiedCommandRegistry,
    text: str,
    render_loop: RenderLoop,
) -> None:
    lines_buf: list[str] = []

    def commit_line(line: str) -> None:
        lines_buf.append(line)

    await dispatch_command(registry, text, commit_line=commit_line)
    if lines_buf:
        render_loop.force_commit(lines_buf)
```

---

## 5. Mode System Integration

### 5.1 Mode Definitions

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, auto


class PermissionMode(Enum):
    AUTO = auto()
    PLAN = auto()
    ASK = auto()
    REVIEW = auto()
    SAFE = auto()
    DEBUG = auto()


# Cycle order for Shift+Tab (forward) and Alt+Shift+Tab (backward)
MODE_CYCLE: list[PermissionMode] = [
    PermissionMode.AUTO,
    PermissionMode.PLAN,
    PermissionMode.ASK,
    PermissionMode.REVIEW,
    PermissionMode.SAFE,
    PermissionMode.DEBUG,
]


@dataclass(frozen=True)
class ModeSpec:
    mode: PermissionMode
    label: str          # Badge text, max 6 chars
    symbol: str         # Symbol used in badge
    ansi_code: str      # Color escape (bold)
    ascii_badge: str    # NO_COLOR fallback
    description: str    # One-sentence description
    allows_writes: bool
    allows_exec: bool
    requires_approval: bool  # For file writes


MODE_SPECS: dict[PermissionMode, ModeSpec] = {
    PermissionMode.AUTO: ModeSpec(
        mode=PermissionMode.AUTO,
        label="AUTO",
        symbol="●",
        ansi_code="\033[32;1m",
        ascii_badge="[AUTO]",
        description="Full autonomy: reads, writes, and exec without confirmation.",
        allows_writes=True,
        allows_exec=True,
        requires_approval=False,
    ),
    PermissionMode.PLAN: ModeSpec(
        mode=PermissionMode.PLAN,
        label="PLAN",
        symbol="◆",
        ansi_code="\033[33;1m",
        ascii_badge="[PLAN]",
        description="Read-only: proposes changes without writing or executing.",
        allows_writes=False,
        allows_exec=False,
        requires_approval=False,
    ),
    PermissionMode.ASK: ModeSpec(
        mode=PermissionMode.ASK,
        label="ASK",
        symbol="?",
        ansi_code="\033[36;1m",
        ascii_badge="[ASK]",
        description="Conversational: no tool calls unless explicitly requested.",
        allows_writes=False,
        allows_exec=False,
        requires_approval=True,
    ),
    PermissionMode.REVIEW: ModeSpec(
        mode=PermissionMode.REVIEW,
        label="REVIEW",
        symbol="⊕",
        ansi_code="\033[34;1m",
        ascii_badge="[REVIEW]",
        description="Reads freely; every write requires y/n approval.",
        allows_writes=True,
        allows_exec=True,
        requires_approval=True,
    ),
    PermissionMode.SAFE: ModeSpec(
        mode=PermissionMode.SAFE,
        label="SAFE",
        symbol="⛔",
        ansi_code="\033[31;1m",
        ascii_badge="[SAFE]",
        description="Maximum safety: every tool call requires approval.",
        allows_writes=True,
        allows_exec=True,
        requires_approval=True,
    ),
    PermissionMode.DEBUG: ModeSpec(
        mode=PermissionMode.DEBUG,
        label="DEBUG",
        symbol="⚙",
        ansi_code="\033[35;1m",
        ascii_badge="[DEBUG]",
        description="Full autonomy with verbose kernel event logging.",
        allows_writes=True,
        allows_exec=True,
        requires_approval=False,
    ),
}
```

### 5.2 Mode Manager

```python
class ModeManager:
    def __init__(self, initial: PermissionMode = PermissionMode.AUTO) -> None:
        self._current = initial
        self._listeners: list[Callable[[PermissionMode], None]] = []

    @property
    def current(self) -> PermissionMode:
        return self._current

    @property
    def spec(self) -> ModeSpec:
        return MODE_SPECS[self._current]

    def cycle_forward(self) -> PermissionMode:
        idx = MODE_CYCLE.index(self._current)
        self._current = MODE_CYCLE[(idx + 1) % len(MODE_CYCLE)]
        self._notify()
        return self._current

    def cycle_backward(self) -> PermissionMode:
        idx = MODE_CYCLE.index(self._current)
        self._current = MODE_CYCLE[(idx - 1) % len(MODE_CYCLE)]
        self._notify()
        return self._current

    def set_mode(self, mode: PermissionMode) -> None:
        self._current = mode
        self._notify()

    def add_listener(self, cb: Callable[[PermissionMode], None]) -> None:
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in self._listeners:
            cb(self._current)

    def render_badge(self, color: bool = True) -> str:
        spec = self.spec
        if not color:
            return spec.ascii_badge
        return f"{spec.ansi_code}{spec.ascii_badge}\033[0m"
```

### 5.3 Mode Badge in Bottom Block

The `FrameComposer._render_status_bar()` renders the mode badge as the leftmost element:

```python
def _render_status_bar(self, transcript: TranscriptModel, cols: int) -> str:
    spec = MODE_SPECS[transcript.current_mode]
    badge = spec.ansi_code + spec.ascii_badge + "\033[0m" if self._color else spec.ascii_badge

    model_str = transcript.model_id or "unknown"
    agent_count = f"{transcript.active_agent_count} agent{'s' if transcript.active_agent_count != 1 else ''}"
    cost = f"${transcript.session_cost_usd:.3f}"
    tokens = _format_tokens(transcript.total_tokens)
    session = f"[{transcript.session_id[:4]}]"

    parts = [badge, model_str, agent_count, cost, tokens, session]
    line = "  ".join(parts)

    # Truncate to cols with wcwidth
    import wcwidth
    while wcwidth.wcswidth(_strip_ansi(line)) > cols and len(parts) > 2:
        parts.pop(-2)  # Remove tokens or cost first
        line = "  ".join(parts)

    return line
```

### 5.4 Mode Color Mapping (complete)

| Mode | ANSI | 256-color | NO_COLOR badge |
|------|------|-----------|----------------|
| AUTO | `\033[32;1m` | 2+bold | `[AUTO]` |
| PLAN | `\033[33;1m` | 3+bold | `[PLAN]` |
| ASK | `\033[36;1m` | 6+bold | `[ASK]` |
| REVIEW | `\033[34;1m` | 4+bold | `[REVIEW]` |
| SAFE | `\033[31;1m` | 1+bold | `[SAFE]` |
| DEBUG | `\033[35;1m` | 5+bold | `[DEBUG]` |

---

## 6. Focus Management

### 6.1 Core Principle: Input Bar Always Holds Focus

In the inline (no-alternate-screen) architecture, there is no widget focus model — the input bar is the only interactive element at any given time. All keyboard input routes through `InputState.handle()`.

**No focus stealing**: Dropdowns, command palette, approval gates — none of these "steal focus" in the traditional widget sense. They are all rendered in the bottom block and their key handling is activated by changing `InputState.mode`, not by changing which widget has focus.

```
Textual keyboard event
        │
        ▼
  InputState.handle(key_name, char)
        │
        ├── mode == APPROVAL     → approval key handler
        ├── mode == DOOM_LOOP    → doom-loop handler
        ├── mode == ERROR_BANNER → error-banner handler
        ├── dropdown_open        → dropdown handler
        ├── mode == DISABLED     → buffer-for-next handler
        └── else                 → normal typing handler
```

### 6.2 Dropdown Appearance/Disappearance

Dropdowns appear and disappear as part of the bottom block redraw cycle — they are rows in the `Frame` produced by `FrameComposer`, not separate widgets:

1. User types `@` → `InputState.mode = MENTION_TRIGGER`
2. App starts async `MentionCache.resolve(fragment, cwd)` task
3. While resolving: `input_state.matches = []`, `dropdown_rows = ["  Loading…"]`
4. On resolve: `input_state.set_matches(items)` called → `dropdown_rows` populated
5. `RenderLoop.request_redraw()` called
6. Next tick: `FrameComposer._render_dropdown()` produces rows; Frame height increases
7. `Terminal.set_bottom(frame)` erases old bottom block, draws new taller one

Dropdown **never** produces scroll jitter because `Terminal.set_bottom()` performs a single atomic write that includes both the erase and the new content.

### 6.3 Minimum Bottom Block Heights

| State | Minimum rows | Maximum rows |
|-------|-------------|-------------|
| Idle | 4 (status + divider + input + footer) | 4 |
| Multi-line input (2 lines) | 5 | 12 |
| Dropdown open (8 items) | 12 | 12 |
| Approval gate | 6 | 8 |
| Doom-loop banner | 7 | 8 |
| Error banner | 5 | 6 |

All values clamped to `min(12, terminal.rows // 3)`.

---

## 7. Accessibility

### 7.1 Keyboard Completeness Matrix

Every action achievable without mouse (verified):

| Action | Keyboard path |
|--------|--------------|
| Submit message | `Enter` |
| New line | `Shift+Enter` or `Alt+Enter` |
| Cancel agent turn | `Ctrl+C` or `Escape` |
| Background turn | `Ctrl+B` |
| Cycle mode forward | `Shift+Tab` |
| Cycle mode backward | `Alt+Shift+Tab` |
| Jump to specific mode | `/mode <name>` then `Enter` |
| Open @mention | Type `@` |
| Navigate mention list | `Up` / `Down` |
| Accept mention | `Tab` or `Enter` |
| Dismiss mention | `Escape` |
| Open command palette | Type `/` at line start |
| Navigate commands | `Up` / `Down` |
| Execute command | `Enter` |
| Insert command text only | `Tab` |
| Approve tool call | `y` or `Enter` |
| Deny tool call | `n` or `Escape` |
| Approve all session | `a` |
| Show approval diff | `d` |
| Expand tool output | `/expand <id>` |
| Clear input | `Escape` (with non-empty buffer) |
| History previous | `Up` or `Ctrl+P` |
| History next | `Down` or `Ctrl+N` |
| Show help | `?` (empty input) or `/help` |
| Exit session | `Ctrl+D` (empty) or `/exit` |

### 7.2 Color-Blind Safe Design

Every semantic color is paired with a distinct symbol and/or text label. The system never uses color as the only differentiator:

| Semantic | Color | Symbol | Text label |
|---------|-------|--------|-----------|
| Success | Green | `✓` | "ok" in ASCII mode |
| Error | Red | `✗` | "[!!]" in ASCII mode |
| Warning | Yellow | `⚠` | "[!]" in ASCII mode |
| Mode AUTO | Green | `●` | `[AUTO]` |
| Mode PLAN | Yellow | `◆` | `[PLAN]` |
| Mode ASK | Cyan | `?` | `[ASK]` |
| Mode REVIEW | Blue | `⊕` | `[REVIEW]` |
| Mode SAFE | Red | `⛔` | `[SAFE]` |
| Mode DEBUG | Magenta | `⚙` | `[DEBUG]` |
| Running | Cyan | `⠸` (spinner) | "running" |
| Pending | Dim | `○` | "pending" |

Under deuteranopia (red-green color blind), AUTO (green) and SAFE (red) remain distinguishable via `●` vs `⛔` symbol shapes.

### 7.3 NO_COLOR Support

When `NO_COLOR` is set in the environment:

```python
class ColorMode(Enum):
    TRUECOLOR = auto()
    COLOR_256 = auto()
    COLOR_8 = auto()
    NO_COLOR = auto()

def get_ansi(code: str, *, color_mode: ColorMode) -> str:
    """Return ANSI code or empty string in NO_COLOR mode."""
    if color_mode == ColorMode.NO_COLOR:
        return ""
    return code
```

In NO_COLOR mode, the `FrameComposer` is initialized with `color=False`, which causes `_render_status_bar()` to use `spec.ascii_badge` instead of `spec.ansi_code + spec.ascii_badge`.

### 7.4 Non-Flashing Guarantee

The `RenderLoop` enforces `MIN_TICK_INTERVAL = 0.050` (50ms = 20fps maximum). The `_needs_redraw` dirty flag ensures the loop never redraws when nothing changed. Combined with frame equality checking (`frame == self._last_frame`), the effective redraw rate during idle is 0 fps.

### 7.5 Screen Reader Mode

Activated by `--accessibility` flag or `accessibility = true` in config:

- Bottom block is NOT erased and redrawn — new content is appended as plain lines
- ANSI codes stripped (equivalent to NO_COLOR)
- Spinner replaced with periodic text: `Agent is working… (15s)`
- Input prompt is a simple `> ` prefix without the elaborate footer

```python
@dataclass
class AccessibilityConfig:
    enabled: bool = False
    announce_interval_s: float = 15.0  # how often to print "Agent working" during turns
    strip_ansi: bool = True
    simple_prompt: bool = True
```

---

## 8. Full Test Specification

### 8.1 Unit Tests — InputState (50 enumerated)

**File:** `tests/unit/test_input_state.py`  
**Markers:** `@pytest.mark.unit`

```python
# ── Buffer Operations ──────────────────────────────────────────────────────

# test_001: insert single char appends to empty buffer
# Initial: text="", cursor=0
# Action: handle("a", "a")
# Expected: text="a", cursor=1, kind=CONTINUE

# test_002: insert at middle of buffer
# Initial: text="hello", cursor=2
# Action: handle("X", "X")
# Expected: text="heXllo", cursor=3

# test_003: backspace on empty buffer is no-op
# Initial: text="", cursor=0
# Action: handle("backspace", "")
# Expected: text="", cursor=0, kind=NO_CHANGE (or CONTINUE — acceptable)

# test_004: backspace deletes char left of cursor
# Initial: text="abc", cursor=2
# Action: handle("backspace", "")
# Expected: text="ac", cursor=1

# test_005: delete-right removes char at cursor
# Initial: text="abc", cursor=1
# Action: handle("delete", "")
# Expected: text="ac", cursor=1

# test_006: ctrl+d on empty buffer returns EXIT
# Initial: text="", cursor=0, mode=IDLE
# Action: handle("ctrl+d", "")
# Expected: kind=EXIT

# test_007: ctrl+d on non-empty buffer deletes right
# Initial: text="hello", cursor=2
# Action: handle("ctrl+d", "")
# Expected: text="helo", cursor=2, kind=CONTINUE

# test_008: ctrl+a moves cursor to line start
# Initial: text="hello world", cursor=7
# Action: handle("ctrl+a", "")
# Expected: cursor=0

# test_009: ctrl+e moves cursor to line end
# Initial: text="hello world", cursor=3
# Action: handle("ctrl+e", "")
# Expected: cursor=11

# test_010: home on multiline input goes to current line start
# Initial: text="line1\nline2", cursor=9  (inside "line2")
# Action: handle("home", "")
# Expected: cursor=6  (start of "line2")

# test_011: end on multiline input goes to current line end
# Initial: text="line1\nline2", cursor=6
# Action: handle("end", "")
# Expected: cursor=11

# test_012: left arrow at position 0 is no-op
# Initial: text="abc", cursor=0
# Action: handle("left", "")
# Expected: cursor=0

# test_013: right arrow at end of buffer is no-op
# Initial: text="abc", cursor=3
# Action: handle("right", "")
# Expected: cursor=3

# test_014: ctrl+left jumps to word start
# Initial: text="hello world test", cursor=11
# Action: handle("ctrl+left", "")
# Expected: cursor=6  (start of "world")

# test_015: ctrl+right jumps to word end
# Initial: text="hello world test", cursor=0
# Action: handle("ctrl+right", "")
# Expected: cursor=5  (end of "hello")

# ── Kill Ring ─────────────────────────────────────────────────────────────

# test_016: ctrl+k kills to end of line
# Initial: text="hello world", cursor=5
# Action: handle("ctrl+k", "")
# Expected: text="hello", cursor=5, kill_ring=[" world"]

# test_017: ctrl+u kills to line start
# Initial: text="hello world", cursor=5
# Action: handle("ctrl+u", "")
# Expected: text=" world", cursor=0, kill_ring=["hello"]

# test_018: ctrl+w kills word backward
# Initial: text="hello world", cursor=11
# Action: handle("ctrl+w", "")
# Expected: text="hello ", cursor=6, kill_ring=["world"]

# test_019: ctrl+y yanks last killed text
# Initial: text="hello", cursor=5, kill_ring=["world"]
# Action: handle("ctrl+y", "")
# Expected: text="helloworld", cursor=10

# test_020: ctrl+y on empty kill ring is no-op
# Initial: text="hello", cursor=5, kill_ring=[]
# Action: handle("ctrl+y", "")
# Expected: text="hello", cursor=5

# ── History Navigation ────────────────────────────────────────────────────

# test_021: up arrow with empty history is no-op
# Initial: text="", history=[], hist_idx=-1
# Action: handle("up", "")
# Expected: text="", hist_idx=-1

# test_022: up arrow navigates to most recent history item
# Initial: text="current", history=["old1", "old2"], hist_idx=-1
# Action: handle("up", "")
# Expected: text="old2", hist_idx=0, saved_buf="current"

# test_023: up again goes to older item
# Initial: text="old2", history=["old1", "old2"], hist_idx=0
# Action: handle("up", "")
# Expected: text="old1", hist_idx=1

# test_024: up at oldest item is no-op
# Initial: text="old1", history=["old1", "old2"], hist_idx=1
# Action: handle("up", "")
# Expected: hist_idx=1  (unchanged)

# test_025: down from history restores saved buffer
# Initial: text="old1", history=["old1", "old2"], hist_idx=1, saved_buf="current"
# Action: handle("down", "") twice
# Expected: text="current", hist_idx=-1

# ── Submit Behavior ───────────────────────────────────────────────────────

# test_026: enter on empty buffer is no-op
# Initial: text="", mode=IDLE
# Action: handle("enter", "")
# Expected: kind=NO_CHANGE

# test_027: enter on whitespace-only buffer is no-op
# Initial: text="   ", mode=TYPING
# Action: handle("enter", "")
# Expected: kind=NO_CHANGE

# test_028: enter submits text and clears buffer
# Initial: text="hello world", cursor=11, mode=TYPING
# Action: handle("enter", "")
# Expected: kind=SUBMIT, result.text="hello world", text="", cursor=0

# test_029: submit appends to history
# Initial: text="my message", history=[]
# Action: handle("enter", "")
# Expected: history=["my message"]

# test_030: trailing backslash+enter inserts newline instead of submitting
# Initial: text="hello\\", cursor=6
# Action: handle("enter", "")
# Expected: text="hello\n", cursor=6, kind=CONTINUE  (NOT SUBMIT)

# test_031: shift+enter always inserts newline
# Initial: text="line1", cursor=5
# Action: handle("shift+enter", "")
# Expected: text="line1\n", cursor=6, kind=CONTINUE

# ── Ctrl+C Behavior ───────────────────────────────────────────────────────

# test_032: ctrl+c with no active turn returns WARN_EXIT on first press
# Initial: agent_turn_active=False, ctrl_c_count=0
# Action: handle("ctrl+c", "")
# Expected: kind=WARN_EXIT, ctrl_c_count=1

# test_033: ctrl+c second press within 2s returns EXIT
# Initial: ctrl_c_count=1, ctrl_c_last_time=time.monotonic()-0.5
# Action: handle("ctrl+c", "")
# Expected: kind=EXIT

# test_034: ctrl+c resets after 2s
# Initial: ctrl_c_count=1, ctrl_c_last_time=time.monotonic()-3.0
# Action: handle("ctrl+c", "")
# Expected: ctrl_c_count=1  (reset then incremented), kind=WARN_EXIT

# test_035: ctrl+c with active agent turn returns CANCEL_TURN
# Initial: agent_turn_active=True, ctrl_c_count=0
# Action: handle("ctrl+c", "")
# Expected: kind=CANCEL_TURN

# ── Trigger Detection ─────────────────────────────────────────────────────

# test_036: @ at position 0 opens mention trigger
# Initial: text="", cursor=0
# Action: handle("@", "@")
# Expected: mode=MENTION_TRIGGER, active_trigger_char="@", trigger_start=0, fragment=""

# test_037: @ after whitespace opens mention trigger
# Initial: text="fix ", cursor=4
# Action: handle("@", "@")
# Expected: mode=MENTION_TRIGGER, trigger_start=4

# test_038: @ after alnum does NOT open mention trigger (email guard)
# Initial: text="user", cursor=4
# Action: handle("@", "@")
# Expected: mode=TYPING  (not MENTION_TRIGGER)

# test_039: / at position 0 opens command trigger
# Initial: text="", cursor=0
# Action: handle("/", "/")
# Expected: mode=COMMAND_TRIGGER, active_trigger_char="/"

# test_040: / after text does NOT open command trigger
# Initial: text="some text", cursor=9
# Action: handle("/", "/")
# Expected: mode=TYPING  (not COMMAND_TRIGGER)

# ── Dropdown Navigation ───────────────────────────────────────────────────

# test_041: up in dropdown wraps from top to bottom
# Initial: mode=MENTION_TRIGGER, matches=[item0,item1,item2], selected=0
# Action: handle("up", "")
# Expected: selected=2  (wrapped)

# test_042: down in dropdown advances selection
# Initial: mode=MENTION_TRIGGER, matches=[item0,item1], selected=0
# Action: handle("down", "")
# Expected: selected=1

# test_043: enter in dropdown accepts item and closes dropdown
# Initial: mode=MENTION_TRIGGER, trigger_start=3, fragment="sr",
#          matches=[MatchItem(value="src/auth.py", ...)], selected=0
#          text="fix @sr", cursor=7
# Action: handle("enter", "")
# Expected: mode=TYPING, text="fix @src/auth.py ", cursor=17, fragment=""

# test_044: tab in dropdown accepts item without submitting
# Same as test_043 but with "tab" key — same result (no submit).

# test_045: escape closes dropdown without accepting
# Initial: mode=COMMAND_TRIGGER, text="/mo", cursor=3, matches=[...]
# Action: handle("escape", "")
# Expected: mode=TYPING (or IDLE), matches=[], active_trigger_char=None

# test_046: backspace in dropdown narrows fragment
# Initial: mode=MENTION_TRIGGER, fragment="src", cursor=7,
#          text="fix @src", trigger_start=4
# Action: handle("backspace", "")
# Expected: fragment="sr", cursor=6, text="fix @sr"  (dropdown stays open)

# test_047: backspace deletes trigger char itself closes dropdown
# Initial: mode=MENTION_TRIGGER, fragment="", cursor=5,
#          text="fix @", trigger_start=4
# Action: handle("backspace", "")
# Expected: mode=TYPING, text="fix ", cursor=4, active_trigger_char=None

# test_048: printable char in dropdown appends to fragment and refilters
# Initial: mode=COMMAND_TRIGGER, fragment="mo", cursor=3, text="/mo"
# Action: handle("d", "d")
# Expected: fragment="mod", cursor=4, text="/mod"

# ── Approval Dialog ───────────────────────────────────────────────────────

# test_049: y in approval mode grants approval
# Initial: mode=APPROVAL, approval_pending=True, approval_tool_name="write_file"
# Action: handle("y", "y")
# Expected: approval_pending=False, mode=IDLE, extra={"action":"approval_grant"}

# test_050: n in approval mode denies approval
# Initial: mode=APPROVAL, approval_pending=True
# Action: handle("n", "n")
# Expected: approval_pending=False, mode=IDLE, extra={"action":"approval_deny"}
```

### 8.2 Unit Tests — FrameComposer (20 enumerated)

**File:** `tests/unit/test_frame_composer.py`

```python
# test_fc_001: compose returns Frame with at least 4 rows
# test_fc_002: compose is deterministic (same args → identical Frame object)
# test_fc_003: compose clamps frame height to min(12, size.rows//3)
# test_fc_004: status bar always present as first non-streaming row
# test_fc_005: divider row is exactly "─" × cols (stripped of ANSI)
# test_fc_006: input bar row starts with "> "
# test_fc_007: footer row contains "Enter:send" in idle mode
# test_fc_008: footer row contains "Ctrl+C:cancel" during agent turn
# test_fc_009: streaming buffer adds rows above status bar
# test_fc_010: dropdown rows appear between streaming zone and status bar
# test_fc_011: approval mode footer shows "Y:allow  N:deny  A:allow-all"
# test_fc_012: mode badge "AUTO" present in status bar row
# test_fc_013: mode badge "PLAN" present after mode change
# test_fc_014: no ANSI codes in any row when color=False
# test_fc_015: frame equality check avoids redundant redraws
# test_fc_016: multi-line input produces multiple input rows
# test_fc_017: input cursor_row and cursor_col are within frame bounds
# test_fc_018: status bar truncates gracefully at 60-col terminal
# test_fc_019: dropdown shows maximum 8 items
# test_fc_020: doom-loop banner appears when doom_loop_pending=True
```

### 8.3 Unit Tests — ModeManager (10 enumerated)

**File:** `tests/unit/test_mode_manager.py`

```python
# test_mm_001: cycle_forward from AUTO goes to PLAN
# test_mm_002: cycle_forward from DEBUG wraps to AUTO
# test_mm_003: cycle_backward from AUTO goes to DEBUG
# test_mm_004: cycle_backward from PLAN goes to AUTO
# test_mm_005: set_mode sets mode directly
# test_mm_006: listener is called on cycle_forward
# test_mm_007: listener is called on set_mode
# test_mm_008: render_badge returns correct ANSI code in color mode
# test_mm_009: render_badge returns ascii_badge in NO_COLOR mode
# test_mm_010: multiple listeners all called on mode change
```

### 8.4 Unit Tests — UnifiedCommandRegistry (10 enumerated)

**File:** `tests/unit/test_command_registry.py`

```python
# test_cr_001: register and get command by name
# test_cr_002: get returns None for unknown command
# test_cr_003: alias lookup works after register with aliases
# test_cr_004: builtin command cannot be overridden by plugin (CommandNameConflict)
# test_cr_005: filter returns empty list when no commands match
# test_cr_006: filter with exact prefix returns exact match first
# test_cr_007: filter with description substring match works
# test_cr_008: filter returns at most 8 items
# test_cr_009: unregister removes command from registry
# test_cr_010: all_visible excludes hidden commands
```

### 8.5 Integration Tests (20 enumerated)

**File:** `tests/integration/test_keyboard_ux.py`

```python
# ── @mention trigger flow ──────────────────────────────────────────────────

# test_it_001: @ trigger opens mention dropdown
# Scenario: User types "fix @src"; InputState mode becomes MENTION_TRIGGER;
#   MentionCache.resolve("src") returns 3 items; set_matches() called;
#   dropdown_rows has 3 entries; FrameComposer adds rows.

# test_it_002: selecting mention inserts path and closes dropdown
# Scenario: Dropdown open with ["src/auth.py", "src/utils.py"]; user presses
#   Down then Tab; text becomes "fix @src/utils.py "; dropdown closed;
#   mode back to TYPING.

# test_it_003: email address does NOT trigger @mention
# Scenario: User types "user@example.com"; no dropdown appears;
#   mode stays TYPING.

# test_it_004: backspace through trigger char closes dropdown
# Scenario: Dropdown open, user presses Backspace until @ is deleted;
#   dropdown closes, mode=TYPING.

# test_it_005: @mention in submitted text becomes MentionChip in transcript
# Scenario: User submits "fix @src/auth.py"; parse_mentions() finds one mention;
#   UserMessage has one MentionChip with kind=FILE; chip_ansi() returns
#   bracketed label in blue.

# ── /command trigger flow ──────────────────────────────────────────────────

# test_it_006: / at pos 0 opens command dropdown
# Scenario: text="", user types "/"; mode=COMMAND_TRIGGER;
#   CommandRegistry.filter("") returns all commands; dropdown open.

# test_it_007: typing after / narrows command list
# Scenario: Dropdown open; user types "mo"; filter("mo") returns /mode, /model;
#   dropdown shows 2 items.

# test_it_008: Enter on /mode command dispatches mode command
# Scenario: Dropdown shows /mode; user presses Enter; dispatch_command()
#   called with "/mode"; commit_line() receives mode output.

# test_it_009: Tab inserts /mode into input bar without dispatching
# Scenario: /mode highlighted; user presses Tab; text="/mode "; dropdown
#   closes; mode=TYPING; no dispatch called.

# test_it_010: unknown command shows error line
# Scenario: User types "/xyz" and presses Enter; dispatch_command() runs;
#   commit_line receives "✗ Unknown command: /xyz".

# ── Mode cycling ───────────────────────────────────────────────────────────

# test_it_011: Shift+Tab cycles mode from AUTO to PLAN
# Scenario: current_mode=AUTO; handle("shift+tab", ""); ModeManager.cycle_forward()
#   called; kernel event ModeChanged emitted; status bar shows [PLAN].

# test_it_012: Shift+Tab from DEBUG wraps to AUTO
# Scenario: current_mode=DEBUG; Shift+Tab × 1; result=AUTO.

# test_it_013: /mode plan command sets PLAN mode directly
# Scenario: User types "/mode plan" + Enter; dispatch_command; ModeManager.set_mode(PLAN).

# ── Approval gate ──────────────────────────────────────────────────────────

# test_it_014: approval gate blocks normal input
# Scenario: set_approval_pending("write_file", "...", False); InputState.mode=APPROVAL;
#   user types "hello"; no chars added to text buffer.

# test_it_015: 'y' in approval mode emits ApprovalGranted
# Scenario: Approval pending; handle("y","y"); extra["action"]=="approval_grant";
#   mode returns to IDLE; approval_pending=False.

# test_it_016: 'a' in approval mode sets allow-all
# Scenario: handle("a","a"); extra["action"]=="approval_allow_all";
#   extra["tool"]=="write_file".

# test_it_017: Escape in approval mode denies
# Scenario: handle("escape",""); extra["action"]=="approval_deny".

# ── Multi-line input ──────────────────────────────────────────────────────

# test_it_018: Shift+Enter inserts newline; FrameComposer produces 2 input rows
# Scenario: text="line1"; Shift+Enter; text="line1\n"; FrameComposer with
#   cols=80 produces Frame with input zone ≥ 2 rows.

# test_it_019: Enter submits multi-line text intact
# Scenario: text="line1\nline2"; Enter; kind=SUBMIT; result.text="line1\nline2".

# test_it_020: trailing backslash+Enter inserts newline on submit attempt
# Scenario: text="hello\\"; Enter; kind=CONTINUE (not SUBMIT); text="hello\n".
```

### 8.6 E2E Tests (15 enumerated)

**File:** `tests/e2e/test_keyboard_e2e.py`  
**Uses:** `FakeTerminal` + `InputState` + `FrameComposer` + `RenderLoop` (no real stdout)

```python
# test_e2e_001: Cold start — bottom block renders within first tick
# Setup: FakeTerminal(rows=24, cols=80); RenderLoop started; await asyncio.sleep(0.1)
# Assert: fake_terminal.bottom_history has at least 1 frame;
#   frame.rows[-2] starts with ">"; write_call_count >= 1.

# test_e2e_002: Typing chars causes bottom block redraw
# Setup: RenderLoop running; feed 5 chars via InputState.handle()
# Assert: Each char causes _needs_redraw=True; subsequent tick updates bottom_history.

# test_e2e_003: Ctrl+C first press triggers WARN_EXIT, no exit
# Setup: Running session, no agent turn
# Feed: handle("ctrl+c","")
# Assert: result.kind==WARN_EXIT; RenderLoop still running; process not exited.

# test_e2e_004: Ctrl+C second press within 2s triggers EXIT
# Setup: ctrl_c_count=1, ctrl_c_last_time=time.monotonic()-0.5
# Feed: handle("ctrl+c","")
# Assert: result.kind==EXIT.

# test_e2e_005: Submit message clears input bar in bottom block
# Setup: text="hello world" in InputState; handle("enter","")
# Assert: kind==SUBMIT; next Frame has input row = "> "; no "hello world" in frame.

# test_e2e_006: @mention dropdown appears in Frame rows
# Setup: InputState; handle("@","@"); set_matches([item1, item2, item3])
# Assert: Frame produced by FrameComposer has rows containing item1.label.

# test_e2e_007: Shift+Tab updates mode badge in bottom block Frame
# Setup: ModeManager(AUTO); handle("shift+tab",""); request_redraw(); compose()
# Assert: Frame rows[0] (status bar) contains "PLAN" (not "AUTO").

# test_e2e_008: Agent turn active disables normal submit
# Setup: input_state.set_agent_turn_active(True); text="hello"; handle("enter","")
# Assert: kind != SUBMIT; text still "hello".

# test_e2e_009: Ctrl+B returns BACKGROUND_TURN regardless of mode
# For each mode in InputMode: handle("ctrl+b","") → kind==BACKGROUND_TURN.

# test_e2e_010: Approval gate footer shown in Frame
# Setup: set_approval_pending("write_file","write src/auth.py",True)
# Assert: Frame footer row contains "Y:allow" and "N:deny" and "A:allow-all".

# test_e2e_011: Doom-loop banner footer shown in Frame
# Setup: input_state.set_doom_loop()
# Assert: Frame contains "C:cancel" and "R:retry" and "I:inject".

# test_e2e_012: SIGWINCH triggers resize and redraw
# Setup: FakeTerminal._size=Size(24,80); set _resize_pending=True
# Tick: RenderLoop detects resize_pending; calls terminal.update_size(); redraws.
# Assert: frame dimensions reflect new size.

# test_e2e_013: RenderLoop skips redraw when Frame identical
# Setup: Two consecutive ticks with no state changes between them.
# Assert: write_call_count increments by at most 1 (first tick only).

# test_e2e_014: force_commit clears bottom before printing committed lines
# Setup: RenderLoop; force_commit(["line A", "line B"])
# Assert: On next tick: clear_bottom called; then commit_lines(["line A","line B"]);
#   then set_bottom called for new frame. Order verified by call_log.

# test_e2e_015: Full keyboard scenario: type, @mention, select, submit
# Scenario:
#   1. handle("f","f"), handle("i","i"), handle("x","x"), handle(" "," ")
#   2. handle("@","@")  → mode=MENTION_TRIGGER
#   3. set_matches([MatchItem("src/auth.py","src/auth.py","file")])
#   4. handle("enter","")  → dropdown accept, text="fix @src/auth.py "
#   5. handle("enter","")  → kind=SUBMIT, result.text="fix @src/auth.py "
# Assert: All state transitions correct; final text correct.
```

---

## 9. Acceptance Criteria

Every criterion is measurable and binary (pass/fail).

### 9.1 Keyboard Completeness

- **AC-KB-01**: Every action in §1.1–1.7 can be triggered by keyboard alone. Verified by manual walkthrough against the completeness matrix in §7.1.
- **AC-KB-02**: No action requires a mouse. Verified by `grep -r "mouse" tests/` showing zero mouse-dependent assertions.
- **AC-KB-03**: `Shift+Tab` cycles mode in all input states, including when dropdown is open. Verified by `test_it_011`.
- **AC-KB-04**: `Ctrl+C` first press produces `WARN_EXIT` (not `EXIT`) when no agent turn active. Verified by `test_e2e_003`.
- **AC-KB-05**: `Ctrl+D` on empty buffer produces `EXIT`; on non-empty buffer deletes right. Verified by `test_006` and `test_007`.

### 9.2 State Machine Correctness

- **AC-SM-01**: `InputState.handle()` returns `InputResult` for every key in every mode with no exception. Verified by 50 unit tests + parametrized fuzz test over all key names.
- **AC-SM-02**: `mode` never gets into an invalid state (enum invariant). Verified by `assert isinstance(state.mode, InputMode)` after every transition.
- **AC-SM-03**: `cursor` is always in `[0, len(text)]`. Verified by property check after each operation.
- **AC-SM-04**: `hist_idx` is always in `[-1, len(history)-1]`. Verified by property check.
- **AC-SM-05**: `dropdown_open` implies `active_trigger_char in ("@", "/")`. Verified by invariant assertion.

### 9.3 @mention System

- **AC-AT-01**: `@` followed by alphanumeric does NOT trigger mention dropdown (email guard). Verified by `test_038`.
- **AC-AT-02**: `@` at position 0 or after whitespace DOES trigger mention dropdown. Verified by `test_036`, `test_037`.
- **AC-AT-03**: Mention dropdown shows at most 8 items. Verified by `test_fc_019`.
- **AC-AT-04**: Accepted mention is inserted with trailing space. Verified by `test_it_002`.
- **AC-AT-05**: `parse_mentions()` correctly identifies `@mentions` and excludes email addresses in the same text. Verified by unit test.
- **AC-AT-06**: Submitted `@mention` items appear as `MentionChip` objects in `UserMessage`. Verified by `test_it_005`.

### 9.4 / Command System

- **AC-CMD-01**: `/` triggers command dropdown only when at position 0 of the input. Verified by `test_039`, `test_040`.
- **AC-CMD-02**: All 21 built-in commands in §4.1 are registered at startup. Verified by `assert len(registry.all_visible()) >= 21`.
- **AC-CMD-03**: Plugin command cannot override built-in command (`CommandNameConflict` raised). Verified by `test_cr_004`.
- **AC-CMD-04**: `/help` output lists all visible commands. Verified by integration test.
- **AC-CMD-05**: Unknown command shows `✗ Unknown command:` error line (not exception). Verified by `test_it_010`.

### 9.5 Mode System

- **AC-MODE-01**: Cycling 6 times with `Shift+Tab` returns to the original mode. Verified by `test_mm_001` through `test_mm_004`.
- **AC-MODE-02**: Mode badge is always visible in every Frame produced by `FrameComposer`. Verified by `test_fc_012`.
- **AC-MODE-03**: Mode badge renders without ANSI codes when `color=False`. Verified by `test_fc_014`.
- **AC-MODE-04**: Mode change is immediate — the next `FrameComposer.compose()` call reflects the new mode. Verified by `test_e2e_007`.

### 9.6 Approval Gate

- **AC-APPR-01**: Approval gate footer shows `Y:allow  N:deny  A:allow-all` in bottom block. Verified by `test_e2e_010`.
- **AC-APPR-02**: Normal text input is disabled during approval (typed chars do not modify text buffer). Verified by `test_it_014`.
- **AC-APPR-03**: `y`/`Y`/`Enter` produces `approval_grant` action. Verified by `test_049`, `test_e2e_015`.
- **AC-APPR-04**: `n`/`N`/`Escape` produces `approval_deny` action. Verified by `test_050`, `test_it_017`.
- **AC-APPR-05**: `a`/`A` produces `approval_allow_all` action with correct tool name. Verified by `test_it_016`.

### 9.7 Rendering

- **AC-RENDER-01**: `Terminal.set_bottom()` issues exactly 1 `os.write()` call per frame. Verified by `FakeTerminal.write_call_count == 1` per `set_bottom()` call.
- **AC-RENDER-02**: `FrameComposer.compose()` completes in < 8ms on 80-column terminal. Verified by benchmark: `timeit.timeit(compose_call, number=1000) / 1000 < 0.008`.
- **AC-RENDER-03**: `RenderLoop` does not redraw when `Frame == last_frame`. Verified by `test_e2e_013`.
- **AC-RENDER-04**: `force_commit()` clears bottom block before committing lines. Verified by `test_e2e_014`.
- **AC-RENDER-05**: Bottom block height is clamped to `min(12, terminal.rows // 3)`. Verified by `test_fc_003`.

### 9.8 Accessibility

- **AC-ACC-01**: `NO_COLOR=1` produces zero ANSI escape codes in any Frame row. Verified by `assert "\033[" not in "".join(frame.rows)` when `color=False`.
- **AC-ACC-02**: Every mode badge has both a color code AND a text label in color mode. Verified by checking that `spec.ansi_code` and `spec.ascii_badge` are both present in rendered badge.
- **AC-ACC-03**: Symbol+text dual coding: every status (✓/✗/⚠/⠸/○/●) appears alongside a text description in the footer or status bar. Verified by visual inspection checklist.
- **AC-ACC-04**: Render rate never exceeds 20fps (50ms minimum tick). Verified by `MIN_TICK_INTERVAL == 0.050` constant assertion.

---

## Appendix A: Word Boundary Helper Functions

```python
def _word_boundary_left(text: str, pos: int) -> int:
    """Position of start of word to the left of pos."""
    if pos == 0:
        return 0
    i = pos - 1
    # Skip non-word chars (whitespace and punctuation)
    while i > 0 and not (text[i].isalnum() or text[i] == '_'):
        i -= 1
    # Skip word chars
    while i > 0 and (text[i - 1].isalnum() or text[i - 1] == '_'):
        i -= 1
    return i


def _word_boundary_right(text: str, pos: int) -> int:
    """Position after end of word to the right of pos."""
    n = len(text)
    if pos >= n:
        return n
    i = pos
    # Skip non-word chars
    while i < n and not (text[i].isalnum() or text[i] == '_'):
        i += 1
    # Skip word chars
    while i < n and (text[i].isalnum() or text[i] == '_'):
        i += 1
    return i
```

## Appendix B: Key Name Normalization

All key names passed to `InputState.handle()` must be lowercase and hyphen-separated:

| Raw key | Normalized `key_name` |
|---------|----------------------|
| `\x01` (Ctrl+A) | `ctrl+a` |
| `\x02` (Ctrl+B) | `ctrl+b` |
| `\x03` (Ctrl+C) | `ctrl+c` |
| `\x04` (Ctrl+D) | `ctrl+d` |
| `\x0b` (Ctrl+K) | `ctrl+k` |
| `\x0c` (Ctrl+L) | `ctrl+l` |
| `\x0e` (Ctrl+N) | `ctrl+n` |
| `\x10` (Ctrl+P) | `ctrl+p` |
| `\x15` (Ctrl+U) | `ctrl+u` |
| `\x17` (Ctrl+W) | `ctrl+w` |
| `\x19` (Ctrl+Y) | `ctrl+y` |
| `\r` or `\n` | `enter` |
| `\x7f` | `backspace` |
| `\x1b[A` | `up` |
| `\x1b[B` | `down` |
| `\x1b[C` | `right` |
| `\x1b[D` | `left` |
| `\x1b[1~` or `\x1b[H` | `home` |
| `\x1b[4~` or `\x1b[F` | `end` |
| `\x1b[3~` | `delete` |
| `\x1b[Z` (Shift+Tab) | `shift+tab` |
| `\x1bOA` (application mode up) | `up` |
| `\x1b[1;5D` (Ctrl+Left) | `ctrl+left` |
| `\x1b[1;5C` (Ctrl+Right) | `ctrl+right` |
| `\x1b\r` (Alt+Enter) | `alt+enter` |
| `\x1b[M…` (mouse — IGNORED) | n/a (discard) |

Key normalization is performed by a standalone `normalize_key(raw: bytes) -> tuple[str, str]` function that returns `(key_name, char)`:

```python
def normalize_key(raw: bytes) -> tuple[str, str]:
    """
    Normalize a raw key sequence to (key_name, char).
    char is the printable character or empty string for control/escape sequences.
    """
    _CONTROL_MAP: dict[bytes, str] = {
        b"\x01": "ctrl+a",   b"\x02": "ctrl+b",   b"\x03": "ctrl+c",
        b"\x04": "ctrl+d",   b"\x05": "ctrl+e",   b"\x06": "ctrl+f",
        b"\x0b": "ctrl+k",   b"\x0c": "ctrl+l",   b"\x0e": "ctrl+n",
        b"\x10": "ctrl+p",   b"\x15": "ctrl+u",   b"\x17": "ctrl+w",
        b"\x19": "ctrl+y",   b"\x1a": "ctrl+z",
        b"\r":   "enter",    b"\n":   "enter",
        b"\x7f": "backspace", b"\x08": "backspace",
        b"\x1b": "escape",
        b"\x1b[A": "up",     b"\x1b[B": "down",
        b"\x1b[C": "right",  b"\x1b[D": "left",
        b"\x1b[H": "home",   b"\x1b[F": "end",
        b"\x1b[1~": "home",  b"\x1b[4~": "end",
        b"\x1b[3~": "delete",
        b"\x1b[Z": "shift+tab",
        b"\x1bOA": "up",     b"\x1bOB": "down",
        b"\x1bOC": "right",  b"\x1bOD": "left",
        b"\x1b[1;5D": "ctrl+left",  b"\x1b[1;5C": "ctrl+right",
        b"\x1b[1;3A": "alt+up",     b"\x1b[1;3B": "alt+down",
        b"\x1b\r": "alt+enter",     b"\x1b\n": "alt+enter",
        b"\x1b[1;2Z": "alt+shift+tab",
    }
    if raw in _CONTROL_MAP:
        return _CONTROL_MAP[raw], ""
    try:
        ch = raw.decode("utf-8")
        if ch.isprintable():
            return ch, ch
    except UnicodeDecodeError:
        pass
    return f"unknown:{raw.hex()}", ""
```

## Appendix C: ANSI Strip Utility

Used by `FrameComposer` for `wcwidth` calculations on rendered rows:

```python
import re

_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

def _strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)
```

---

*End of keyboard-ux-prd.md — v1.0*
