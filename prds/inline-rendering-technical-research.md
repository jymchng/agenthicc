# Inline Terminal Rendering — Deep Technical Research

**Document type**: Technical research  
**Scope**: ANSI escape sequences, inline update patterns, tmux/screen compatibility, prompt_toolkit inline mode  
**Status**: Complete  
**Constraint addressed**: NO alternate screen — scrollback must be preserved

---

## 1. How Top AI Coding Tools Handle Inline Rendering

### Claude Code

Claude Code's rendering history illustrates the evolution of inline TUI rendering:

**Phase 1 (pre-Oct 2025): Ink-based renderer**  
Used React for the terminal via [Ink](https://github.com/vadimdemedes/ink). Ink maintains a React component tree and re-renders to the terminal via ANSI escape sequences without alternate screen.

**Phase 2 (v2.0.10, Oct 2025): Custom differential renderer**  
Anthropic rewrote the renderer with a 5-stage pipeline:
1. React scene graph (UI components)
2. Layout calculation (proprietary engine)
3. Rasterization (2D terminal cell grid)
4. Diffing (per-cell change tracking vs previous frame)
5. ANSI sequence generation (minimal escape codes only for changed cells)

Target: ~16ms frame budget. **Still inline — no alternate screen.** The flicker problem came from terminals processing escape sequences non-atomically (cursor repositioning, streaming text, spinners, and the input box all appearing as intermediate states).

**Phase 3 (v2.1.88+, Mar 2026): Alternate screen opt-in**  
`CLAUDE_CODE_NO_FLICKER=1` or `/tui fullscreen` switches to `\x1b[?1049h`. This is opt-in only.

**Key insight**: Claude Code's default mode is inline (no alternate screen). The alternate screen is an escape hatch for users who prefer it.

### Aider

Aider uses Rich's `Live` context manager for its spinner — entirely inline, no alternate screen. The `uilive`/cursor-up/erase-line approach. Flicker on Windows Terminal with WSL2 was mitigated by slowing the spinner animation.

### OpenCode (Go)

Uses Bubble Tea with `tea.WithAltScreen()` — the only major tool that defaults to alternate screen.

---

## 2. The "Committed Transcript + Live Bottom" Pattern

This is the canonical pattern for inline TUI with scrollback preservation:

- **Committed region**: Permanent lines appended to stdout. Once written, never erased. Goes into scrollback.
- **Live region**: N-line dynamic area at the bottom of the visible terminal. Erased and redrawn on each update.

### Implementation: Cursor-Up / Erase-Line Loop (recommended)

```python
CURSOR_UP_ONE   = "\x1b[1A"
ERASE_LINE      = "\x1b[2K"
CARRIAGE_RETURN = "\r"

def erase_n_lines(n: int) -> str:
    """Return ANSI sequence to erase n lines and position cursor at start of first."""
    if n == 0:
        return ""
    return ERASE_LINE + (CURSOR_UP_ONE + ERASE_LINE) * (n - 1) + CARRIAGE_RETURN
```

**Full live-update sequence:**
```python
import sys

def live_update(old_line_count: int, new_content: str) -> None:
    sys.stdout.write("\x1b[?25l")  # hide cursor to prevent flicker
    if old_line_count:
        sys.stdout.write(erase_n_lines(old_line_count))
    sys.stdout.write(new_content)
    sys.stdout.write("\x1b[?25h")  # show cursor
    sys.stdout.flush()
```

This is how **Rich's `Live` class** works internally via `LiveRender.position_cursor()`.

### Implementation: DECSTBM Scroll Region (elegant but less portable)

```
\x1b[{top};{bottom}r   # Set scroll region to rows top–bottom (1-indexed)
\x1b[r                  # Reset to full screen
```

For a 40-row terminal with a 3-row input area:
```
\x1b[1;37r    # Scroll region = rows 1–37; rows 38–40 are "sticky"
```

**Advantage**: The terminal emulator handles scrolling natively. No app-level line counting needed.  
**Risk**: tmux may interfere with its own scroll region management. Test carefully.

---

## 3. Complete ANSI/CSI Sequence Reference

All sequences: `ESC [` = `\x1b[` (bytes `0x1B 0x5B`)

### Cursor Movement

| Sequence | Function |
|---|---|
| `\x1b[{n}A` | Cursor up n lines (CUU) |
| `\x1b[{n}B` | Cursor down n lines (CUD) |
| `\x1b[{n}C` | Cursor right n columns (CUF) |
| `\x1b[{n}D` | Cursor left n columns (CUB) |
| `\x1b[{n}E` | Next line: col 1, n lines down |
| `\x1b[{n}F` | Previous line: col 1, n lines up |
| `\x1b[{n}G` | Move to column n |
| `\x1b[{r};{c}H` | Absolute position (row, col), 1-indexed |
| `\r` | Carriage return — col 0, same row |

### Erase Functions

| Sequence | Function |
|---|---|
| `\x1b[K` or `\x1b[0K` | Erase from cursor to end of line |
| `\x1b[1K` | Erase from start of line to cursor |
| `\x1b[2K` | Erase entire line (cursor stays) |
| `\x1b[J` or `\x1b[0J` | Erase from cursor to end of screen |
| `\x1b[1J` | Erase from screen start to cursor |
| `\x1b[2J` | Erase entire screen |

### Cursor Visibility

| Sequence | Function |
|---|---|
| `\x1b[?25l` | Hide cursor |
| `\x1b[?25h` | Show cursor |

### Cursor Save/Restore

| Sequence | Notes |
|---|---|
| `\x1b7` / `\x1b8` | DEC save/restore — more portable |
| `\x1b[s` / `\x1b[u` | SCO/ANSI save/restore |

### Scroll Region (DECSTBM)

| Sequence | Function |
|---|---|
| `\x1b[{top};{bottom}r` | Set scroll region |
| `\x1b[r` | Reset scroll region to full screen |

### Alternate Screen (AVOID)

| Sequence | Function |
|---|---|
| `\x1b[?1049h` | Enter alternate screen ← **FORBIDDEN** |
| `\x1b[?1049l` | Exit alternate screen ← **FORBIDDEN** |

---

## 4. tmux/screen Compatibility

### Detection

```python
import os

def in_tmux() -> bool:
    return "TMUX" in os.environ

def in_screen() -> bool:
    return (os.environ.get("TERM", "").startswith("screen") or
            "STY" in os.environ)

def in_multiplexer() -> bool:
    return in_tmux() or in_screen()
```

### Terminal Size in tmux

Inside tmux, the pseudo-terminal reports the pane size (correct). Query via:

```python
import os, struct, fcntl, termios

def get_terminal_size() -> tuple[int, int]:
    try:
        data = fcntl.ioctl(1, termios.TIOCGWINSZ, b'\x00' * 8)
        rows, cols = struct.unpack('HHHH', data)[:2]
        return rows, cols
    except Exception:
        return os.get_terminal_size()
```

### Color Support in tmux

- `screen` terminfo: 256 colors, **no truecolor by default**
- `tmux-256color`: 256 colors; truecolor requires:

```
# ~/.tmux.conf
set -g default-terminal "tmux-256color"
set -as terminal-features ",xterm-256color:RGB"
```

```python
colorterm = os.environ.get("COLORTERM", "")
supports_truecolor = colorterm in ("truecolor", "24bit")
```

### tmux Pitfalls for Inline TUI

| Pitfall | Mitigation |
|---|---|
| DECSTBM may conflict with tmux's own scroll region | Fall back to cursor-up/erase-line loop; detect with `in_tmux()` |
| Line drawing characters misrendered | `set -as terminal-overrides ",*:U8=0"` in `.tmux.conf` |
| Italics disabled with `screen` terminfo | Use `tmux-256color` |
| Color depth limited | Detect with `COLORTERM` env var |
| Mouse events require opt-in | `set -g mouse on` in `.tmux.conf` |

---

## 5. Textual Inline Mode

Textual supports inline mode via `App.run(inline=True)`. This does NOT enter the alternate screen.

```python
from textual.app import App, ComposeResult
from textual.widgets import Label

class MyApp(App):
    def compose(self) -> ComposeResult:
        yield Label("Hello from inline Textual!")

app = MyApp()
app.run(inline=True)   # ← no alternate screen
```

### Inline Mode Constraints

- App renders in-place below the current prompt
- Height is constrained to available terminal rows
- Scrollback is preserved — completed output stays in scrollback
- Mouse support works
- All widgets available

### Height Management

Set a fixed height via CSS:

```css
/* app.tcss */
Screen {
    height: 20;  /* rows */
}
```

Or dynamically constrain via `App.INLINE_PADDING`.

---

## 6. Recommended Architecture for AgentHICC

Based on all research:

**Use the cursor-up/erase-line loop** (not DECSTBM) for compatibility with tmux/screen:

```
Scrollback (permanent, append-only):
  ┌─────────────────────────────────────────┐
  │ [12:01] User: implement auth module     │
  │ ● assistant  12:01:03                   │
  │   ⎿ read_file(path='app.py')  ✓  23ms  │
  │   Let me analyze the code...            │
  └─────────────────────────────────────────┘

Live bottom region (erased/redrawn each frame):
  ┌─────────────────────────────────────────┐
  │ Thinking...  3.2s │ ↑ 12,450  ↓ 891   │  ← status
  │ ────────────────────────────────────── │  ← divider  
  │ ❯ _                                    │  ← input
  │   ⏵⏵ Auto  (shift+tab to cycle)       │  ← mode footer
  └─────────────────────────────────────────┘
```

**Python pattern**:

```python
def update_live_region(terminal, old_height: int, new_rows: list[str]) -> int:
    """Erase old live region and write new one. Returns new height."""
    out = terminal.stdout
    out.write("\x1b[?25l")  # hide cursor
    if old_height > 0:
        out.write("\x1b[2K" + "\x1b[1A\x1b[2K" * (old_height - 1) + "\r")
    content = "\n".join(new_rows)
    out.write(content)
    out.write("\x1b[?25h")  # show cursor
    out.flush()
    return len(new_rows)
```

**Sources:**
- slyapustin.com/blog/claude-code-no-flicker
- steipete.me/posts/2025/signature-flicker
- github.com/anthropics/claude-code issues #769, #42670
- github.com/Aider-AI/aider PR #3911
- github.com/gosuri/uilive (writer_posix.go)
- github.com/Textualize/rich live.py
- gist.github.com/fnky/458719343aabd01cfb17a3a4f7296797
- ghostty.org/docs/vt/csi/decstbm
- python-prompt-toolkit docs (full_screen_apps, rendering_pipeline)
