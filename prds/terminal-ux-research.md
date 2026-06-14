# Terminal UX Research: Modern Design Patterns for AI Coding Agent TUIs

**Date:** 2026-06-13
**Scope:** Modern terminal application design patterns with specific focus on AI coding agent workflows, scrollback preservation, and the agenthicc TUI.

---

## 1. Executive Summary

Modern terminal applications have converged on a coherent set of design principles drawn from two decades of tools like htop, vim, tmux, and more recently lazygit, k9s, fzf, bat, delta, and ripgrep. These principles — keyboard-first navigation, semantic color, progressive disclosure, spatial consistency, and asynchronous operation — form a common lingua franca that experienced terminal users carry across tools.

For AI coding agent TUIs specifically, one architectural decision dominates all others: **alternate screen versus scrollback-preserving output**. The alternate screen (`\e[?1049h`) gives full-viewport control but destroys native scrollback, which is catastrophic for long coding sessions. Claude Code's approach of re-rendering the entire conversation on every streaming update causes quadratic scrollback growth (measured at 4,000–6,700 ANSI writes per second vs. 100–500/sec for a large `cat`), with a hard ~2,000 line limit that permanently loses earlier conversation. This is the canonical anti-pattern to avoid.

The recommended architecture for agenthicc is a **hybrid linear/differential render** approach: normal screen buffer for conversation content with a pinned prompt_toolkit chrome (status bar and input bar) that does not displace the conversation region. Conversation lines, once finalized, become permanent scrollback. In-progress streaming updates use cursor-up and synchronized output (`\e[?2026h`) to rewrite only changed lines without flicker. This eliminates quadratic growth, preserves native scrollback, and retains rich TUI affordances.

Key recommendations in priority order:

1. Never re-render completed conversation turns; append only.
2. Batch streaming redraws at ~60 FPS (16ms interval) via a flush loop.
3. Wrap all frame writes in synchronized output brackets (`CSI ?2026h/l`).
4. Represent tool calls as collapsible one-line entries with spinner/check/cross.
5. Show 3–5 keybinding hints in the footer, updated per focus context; `?` for full help.
6. Respect `NO_COLOR` / `FORCE_COLOR` / `COLORTERM` on startup; store level in `AppState`.
7. Use `wcwidth` for all display-width calculations; never `len()` on displayed strings.
8. Handle `Ctrl+C` during LLM streaming as a stream interrupt, not a process exit.

---

## 2. Terminal Design Principles

The most studied modern TUIs converge on seven non-negotiable principles.

### 2.1 Spatial Consistency

Panels are fixed in place. Users build location memory — "branches are always top-left, diff is always right." Never rearrange layout without explicit user action (e.g., a resize command or config toggle). Lazygit's five-panel layout (status, files, branches, commits, stash) never shuffles; k9s's header/body/footer division never changes between resource types. Violating spatial consistency breaks muscle memory instantly and forces the user back into conscious navigation rather than automatic flow.

### 2.2 Keyboard-First, Mouse-Optional

Every feature must be reachable without a mouse. Mouse support should be present for scrolling and selection but never required. fzf implements this perfectly: full keyboard navigation with mouse scroll as a supplement. The six-keystroke vim lingua franca (`j` down, `k` up, `h` back, `l` enter, `/` search, `q` quit) covers 80% of navigation for experienced users. Adopt it rather than reinvent it.

### 2.3 Progressive Disclosure (Three Tiers)

- **Tier 0 — Always visible:** Footer bar shows 3–5 critical bindings. Never more; never zero.
- **Tier 1 — On demand:** `?` opens a full keybinding help overlay.
- **Tier 2 — Documentation:** Advanced features in `--help` or docs.

htop shows `F1–F10` in the footer always; pressing `F1` opens the full help modal. Dump all options on the user simultaneously and they see none of them.

### 2.4 Semantic Color

Color must encode meaning, not aesthetics. The test: strip all color — if the app is still usable, color is enhancing; if it breaks, the design is broken. Delta uses green/red for added/removed lines but also `+`/`-` prefix characters so colorblind users lose nothing.

### 2.5 Async Everything

Zero blocking of the event loop. Every file operation, network call, and subprocess must run in the background with visible progress. A frozen cursor for even 200ms is noticeable and erodes trust. A frozen cursor for 2 seconds is unacceptable.

### 2.6 Contextual Intelligence

Keybindings and help text adapt to current focus and state. In lazygit, pressing `m` in the files panel means "merge"; in the branches panel it means "merge branch." The footer updates to reflect the current context. This requires dynamic footer rendering based on application state, not static text.

### 2.7 Design in Layers

Start with monochrome usability. Layer 16-color ANSI for readability. Layer truecolor for aesthetics. A TUI that only works with 24-bit color is a broken TUI. Degrade gracefully through the color tier stack.

---

## 3. Information Density Patterns

### 3.1 Density Lessons from Reference Apps

**htop:** Full-width color coding for process names with path truncation; numeric columns right-aligned; CPU/memory meter bars use fractional block characters (`▏▎▍▌▋▊▉█`) for sub-character precision. Default refresh: 1.5 seconds (configurable 0.1–10s). CPU overhead at default refresh: less than 1%. The lesson: don't redraw faster than your data changes.

**k9s:** Fixed header rows (namespace, context, resource count). Sortable column table with right-aligned numerics. Color by resource health (green/yellow/red). Bottom status bar always shows key hints. Contextual command mode via `:`. The lesson: a persistent header eliminates the need to scroll up to remember context.

**lazygit:** Three-column file status (`M`, `A`, `D`, `?`, `!` for staged/unstaged/untracked). Commit log uses colored symbols for merge/diverge topology. Status bar shows branch tracking in compact notation (`↑3 ↓1`). The lesson: symbolic compact notation is more scannable than verbose English.

### 3.2 Information Hierarchy Rules

1. Most-changed information (streaming output, spinner state) gets the most visually prominent location.
2. Least-changed information (model name, config) goes in the header/footer where it is visible but not demanding.
3. Actionable information (what can I do now?) always in the footer.
4. Historical information (what happened?) always in the scrollable transcript region.
5. Counts beat lists: show `23 files modified` not a list of 23 files; offer expand on demand.

### 3.3 The One-Line Principle for Tool Calls

Every tool call entry should occupy exactly one line when collapsed:

```
[read_file: "src/auth.py"]  ⠸ running...
[read_file: "src/auth.py"]  ✓ 142 lines (0.3s)
[run_bash: "pytest tests/"] ✗ exit code 1 (2.1s)
```

`Tab` or `→` expands to full output. Never print raw tool output inline by default. This single discipline transforms a cluttered transcript into a scannable log.

---

## 4. Keyboard-First UX

### 4.1 The Four-Layer Binding Model

| Layer | Bindings | Visibility |
|---|---|---|
| L0 Universal | `↑↓←→`, `Enter`, `Esc`, `q` | Always in footer |
| L1 Vim Motions | `j`/`k`, `/` search, `?` help | Footer (assumed known) |
| L2 Domain Actions | Single mnemonics: `d`=diff, `s`=stage, `r`=refresh | `?` overlay |
| L3 Power | Composed keys, config, macros | Documentation only |

### 4.2 Recommended Bindings for AI Coding Agent TUI

| Action | Binding | Rationale |
|---|---|---|
| Submit input | `Enter` | Universal |
| Newline in input | `Ctrl+J` or `Shift+Enter` | Disambiguates from submit |
| Abort/cancel | `Ctrl+C` | Unix standard; must interrupt stream, not kill process |
| Approve tool call | `y` or `Enter` | Minimal friction |
| Reject tool call | `n` or `Esc` | Escape always means "no" |
| View diff | `d` | Mnemonic |
| Expand/collapse block | `Tab` or `Space` | Common convention |
| Scroll up | `PgUp` / `Ctrl+U` | Vim feel |
| Jump to bottom | `G` / `Ctrl+End` | Vim standard |
| Interrupt LLM | `Ctrl+C` | Must cancel coroutine, not exit app |
| Toggle plan mode | `p` | Mnemonic |
| Open help | `?` | Universally understood in TUIs |

### 4.3 Interrupt Handling for Streaming

`Ctrl+C` during streaming must **interrupt the LLM call, not kill the application**. The interrupt handler must:
1. Cancel the streaming coroutine.
2. Print a `^C` inline marker in the transcript.
3. Return control to the input bar immediately.
4. Mark the turn as interrupted (not add a partial entry that looks complete).

In prompt_toolkit, this requires binding `Ctrl+C` in the global key bindings to a handler that sets a cancellation event, rather than the default behavior of raising `KeyboardInterrupt`.

### 4.4 Approval Gate Pattern

For tool calls requiring confirmation:
1. Display inline: `[run_bash: "rm -rf ./dist"] — approve? [y/N]`
2. Temporarily activate approval-mode bindings (`y`, `n`, `Enter`, `Esc` only).
3. On approval: immediately show `✓ approved` and proceed.
4. On rejection: show `✗ rejected — skipped` and continue.

In prompt_toolkit, implement via `modal=True` on a `FloatContainer` that intercepts all input until resolved.

---

## 5. Color Systems and Accessibility

### 5.1 Color Level Detection

Check in this exact order at startup:

```python
import os

def detect_color_level() -> int:
    """Returns 0=none, 1=16-color, 2=256-color, 3=truecolor."""
    if os.environ.get('NO_COLOR'):
        return 0
    if os.environ.get('FORCE_COLOR'):
        return 3
    colorterm = os.environ.get('COLORTERM', '').lower()
    if colorterm in ('24bit', 'truecolor'):
        return 3
    term = os.environ.get('TERM', '')
    if '256color' in term:
        return 2
    if os.environ.get('CI'):
        return 3
    if os.environ.get('TERM_PROGRAM') == 'iTerm.app':
        return 3
    return 1
```

**Critical:** macOS `Terminal.app` only supports 256-color despite being ubiquitous. Do not assume truecolor on macOS without `$COLORTERM` confirmation.

### 5.2 The NO_COLOR / FORCE_COLOR Standard

- `NO_COLOR` set (any non-empty value): strip ALL ANSI color unconditionally — this is a hard spec requirement.
- `FORCE_COLOR` set: override any TTY detection and emit color.
- Also handle `CLICOLOR` (1=enable, 0=disable) and `CLICOLOR_FORCE`.
- Per-app `--color=always` CLI flags may override `NO_COLOR` for that invocation.

Store the computed color level in `AppState.settings` (or `SystemSettings`) at startup so all rendering code queries a single source of truth.

### 5.3 Semantic Token Architecture

Never hardcode ANSI codes in layout code. Use a resolution pipeline:

```
Palette (raw hex or ANSI index)
    → Tokens (semantic names)
        → Styles (bold + token composition)
            → Rendered output
```

Recommended semantic token set for coding-agent TUIs:

| Token | Purpose |
|---|---|
| `text.primary` | Main body text |
| `text.muted` | Timestamps, metadata, secondary info |
| `text.emphasis` | Headers, current focus (bright + bold) |
| `bg.base / bg.surface / bg.overlay` | Depth layers |
| `accent.primary` | Interactive borders, selection highlight |
| `status.success / .warning / .error / .info` | State indicators |
| `diff.added / .removed / .context` | Diff content |
| `tool.running / .complete / .failed` | Tool call states |
| `git.staged / .modified / .untracked` | Git status |

### 5.4 Accessibility Rules

- **Never rely on color alone.** Always pair with a symbol or shape (`+`/`-`, `✓`/`✗`, `!`, text label).
- ANSI blue on black has poor contrast in many terminals — use bright blue (`\e[94m`) or an explicit truecolor value instead.
- Bright yellow on light backgrounds is nearly unreadable — never combine them.
- For diff views, do not use red/green as the only differentiator. Add `+`/`-` prefix characters.
- Modern terminals (iTerm2, Kitty, Ghostty, Windows Terminal) enforce minimum contrast by automatically adjusting foreground colors. Design for terminals that do not have this.

---

## 6. Progressive Disclosure

Progressive disclosure is the primary tool for managing complexity without hiding functionality.

### 6.1 Three-Tier Structure (Recap)

- **Tier 0:** Footer shows the 3–5 most relevant actions for the current focus state. Updating this dynamically based on what is focused is essential.
- **Tier 1:** `?` overlay shows all available bindings in the current context, organized by category.
- **Tier 2:** Documentation, `--help`, and advanced configuration.

### 6.2 Collapsible Sections

Long tool outputs, diffs, and plan details should be collapsible. Collapsed state shows one summary line. Expanded state shows full content. Default state for completed tool calls: collapsed. Default state for the current in-progress operation: expanded.

### 6.3 Plan Mode Disclosure

Show plan header always. Collapse completed steps to checkmarks. Keep the current step expanded. Never show all steps' full output simultaneously.

```
Plan: refactor auth module (5 steps)
  1. ✓ Read current auth.py
  2. ✓ Analyze dependencies
  3. ⠸ Writing new auth.py...
  4. ○ Run tests
  5. ○ Update imports
```

### 6.4 Diff Preview

Diffs should default to collapsed with a one-line summary:

```
[patch_file: "src/auth.py"]  ✓ +47 -23 lines  [Tab to expand]
```

Expanded view shows the full diff with syntax highlighting. Closing collapses back. Do not auto-expand diffs for files larger than a configurable line threshold (default: 200 lines).

---

## 7. Scrollback Preservation (Mandatory Deep-Dive)

Scrollback preservation is the most important architectural constraint for an AI coding agent TUI. Sessions routinely span hours. Users need to scroll back to review earlier LLM output, copy code snippets, and audit tool execution history.

### 7.1 The Two Architectures

**Alternate Screen Buffer (`\e[?1049h` / `\e[?1049l`)**

- Switches to a separate screen buffer with no scrollback.
- Application owns the full terminal viewport.
- Used by: htop, k9s, lazygit, vim, less, ncurses apps.
- On exit: restores previous shell content cleanly.

Critical disadvantages for coding agents:
- No native scrollback. The terminal's scroll wheel only scrolls within the app's internal scroll implementation.
- You must reimplement search, selection, and copy-paste from scratch.
- Claude Code uses alternate screen with a ~2,000 line hard limit. Long sessions permanently lose earlier content.
- Claude Code re-renders the entire conversation on every streaming update, causing quadratic scrollback growth (N exchanges = N full copies in the buffer; measured at 4,000–6,700 ANSI writes/second).

**Normal Screen / Linear Output**

- Output flows to the normal screen buffer.
- Terminal's native scrollback, search, and text selection work as expected.
- Used by: Aider (mostly), traditional CLIs, ripgrep, bat.
- Advantages: unlimited native scrollback; no need to reimplement scroll/search/copy.
- Disadvantages: cannot do fixed-position panels without cursor positioning tricks.

### 7.2 The Recommended Hybrid Architecture

Use the normal screen buffer for conversation content with differential rendering for streaming updates, plus a small prompt_toolkit chrome (status bar + input bar) anchored to the bottom.

**Core invariant:** Once an assistant turn or tool call is finalized (streaming complete, tool returned), it is written as permanent newline-terminated output and never touched again. Only the current in-progress region is rewritten.

**Differential render algorithm for in-progress streaming:**

```python
cached_lines: list[str] = []

def render_streaming_update(new_lines: list[str]) -> None:
    # Find first changed line
    first_diff = 0
    for i, (old, new) in enumerate(zip(cached_lines, new_lines)):
        if old != new:
            first_diff = i
            break
    else:
        first_diff = len(cached_lines)

    lines_to_rewrite = len(cached_lines) - first_diff
    if lines_to_rewrite > 0:
        # Cursor up to first changed line
        sys.stdout.write(f'\x1b[{lines_to_rewrite}A')

    # Write synchronized output frame to prevent flicker
    sys.stdout.write('\x1b[?2026h')
    for line in new_lines[first_diff:]:
        sys.stdout.write(f'\x1b[2K{line}\r\n')
    sys.stdout.write('\x1b[?2026l')
    sys.stdout.flush()

    cached_lines[:] = new_lines
```

When streaming completes, call `finalize_turn()` which writes the final newline and clears the cache — that content becomes permanent scrollback.

### 7.3 Memory Cost

The line cache for differential rendering costs "a few hundred kilobytes even for very large sessions" (hundreds of screen-widths of cached lines). This is negligible. The memory cost of quadratic re-rendering (Claude Code's anti-pattern) is unbounded and measured in megabytes for long sessions.

### 7.4 Synchronized Output (`CSI ?2026`)

Synchronized output (`\e[?2026h` ... `\e[?2026l`) tells the terminal to batch all rendering until the closing escape. This eliminates flicker during multi-line updates.

```
Query support:  \e[?2026$p
Response:       \e[?2026;N$y  where N=0 (no), 1 (yes), 2 (permanent)
```

Support: WezTerm, Kitty, iTerm2, Ghostty, most modern terminals. Not supported in older xterm, macOS Terminal.app (basic), some remote/embedded terminals.

Graceful degradation: query on startup; if unsupported, omit the brackets. The render result is correct either way — just flicker-visible on unsupported terminals.

### 7.5 The Quadratic Growth Anti-Pattern (What Not To Do)

Claude Code's architecture:
1. Enters alternate screen on startup.
2. On every streaming token: re-renders the entire conversation from the beginning.
3. Result: N exchanges × M average lines per exchange = N×M lines written per update.
4. Measured: 4,000–6,700 `write()` syscalls per second during active streaming.
5. Measured: ~189 KB/second of pure ANSI escape overhead from truecolor codes.
6. Consequence: the alternate screen buffer fills in minutes on long sessions; earlier content permanently inaccessible.

Avoid this by treating the finalized transcript as append-only. The only valid mutation is appending new content. Never touch completed turns.

### 7.6 Scrollback-Friendly Text Selection

In alternate screen mode, selecting text with the mouse often captures box-drawing characters, ANSI escape codes (in some terminals), and artifact whitespace from column-padded layouts. This makes copy-pasting code painful.

In normal screen mode, text selection works exactly as expected because there is no TUI chrome in the scrollback region — it is just text output. Code blocks, diffs, and command output are directly selectable and copyable.

If alternate screen is used, mitigate by: ensuring box-drawing characters are not interleaved with selectable content; using OSC 52 for clipboard integration; providing a copy shortcut (`y` in lazygit style) that copies the current selection without UI chrome.

---

## 8. Status and Progress Indicators

### 8.1 Timing Thresholds

| Operation Duration | Appropriate Indicator |
|---|---|
| < 100ms | Nothing — too fast to display meaningfully |
| 100ms–4s | Spinner (animated, no percentage) |
| 4s–30s | Spinner + elapsed time or step count |
| > 30s (measurable) | Progress bar with percentage + elapsed |
| > 30s (unknown duration) | Spinner + elapsed time; never a stalled bar at 0% |

Stalled progress bars at 0% or 99% are actively harmful — they communicate false certainty and trigger user anxiety. If duration is unknown, always use a spinner + elapsed time.

### 8.2 Spinner Frames

```python
# Braille dots (6-frame, 8–12 FPS, smooth and subtle)
SPINNER_BRAILLE = ['⠋', '⠙', '⠸', '⠴', '⠦', '⠇']

# Box rotation (4-frame, classic)
SPINNER_BOX = ['◐', '◓', '◑', '◒']

# ASCII fallback (4-frame, universal)
SPINNER_ASCII = ['|', '/', '-', '\\']

# Recommended rate: advance frame every 80–120ms
```

Always provide the ASCII fallback behind a config flag or Nerd Font detection.

### 8.3 Progress Bars

```
[████████████░░░░░░░░] 60% — step 3 of 5 (4.2s)
```

Use `█` (U+2588) for fill and `░` (U+2591) for empty. Standard width: 20–30 characters. Always clear with `\e[2K` or trailing spaces when done to prevent ghost characters.

### 8.4 Multi-Step Plan Status

```
Plan: refactor auth module  [3/5 complete]  ⠸ step 3 running (12s)
  1. ✓ Read auth.py (0.2s)
  2. ✓ Analyze deps (0.8s)
  3. ⠸ Write new auth.py... (12s elapsed)
  4. ○ Run tests
  5. ○ Update imports
```

The plan header shows aggregate progress without requiring the user to count checkmarks.

### 8.5 Status Bar Components

The pinned status bar (one line, always visible) should show:

```
 model: claude-sonnet-4-6 │ tokens: 12,847 │ tools: 7 calls │ ⠸ running  [?] help
```

Right-align the help hint so it does not shift as the left-side metrics change.

---

## 9. Notification Patterns Without Alternate Screen

### 9.1 Inline Status Updates (No Scrollback Pollution)

```python
# Spinner update — rewrites same line, no newline
sys.stdout.write(f'\r  {next_frame} Processing {item}...\x1b[K')
sys.stdout.flush()

# Completion — advance with newline to become permanent scrollback
sys.stdout.write(f'\r  ✓ Done processing {total} items\x1b[K\n')
sys.stdout.flush()
```

`\e[K` (erase to end of line) is essential after `\r` overwrites to clear any ghost characters from a previous longer message.

### 9.2 Ephemeral Bottom-Row Status

Reserve the last row for persistent status, using save/restore cursor:

```python
def update_status_line(status_text: str) -> None:
    rows, cols = os.get_terminal_size()
    clipped = status_text[:cols - 1]
    sys.stdout.write(
        f'\x1b[s'            # Save cursor position
        f'\x1b[{rows};0H'   # Move to last row, column 0
        f'\x1b[2K'           # Erase entire line
        f'{clipped}'
        f'\x1b[u'            # Restore cursor position
    )
    sys.stdout.flush()
```

In prompt_toolkit, this is handled by the `FormattedTextControl` in the status window — call `app.invalidate()` after updating its content.

### 9.3 Modal Confirmation Without Full-Screen

For approval gates that must not pollute scrollback:

```
  [run_bash: "rm -rf ./dist"]  approve? [y/N]: ▌
```

Display inline, replacing the input bar content temporarily. On response, clear and restore normal input mode. This preserves scrollback while blocking input for the decision.

### 9.4 The `app.invalidate()` Requirement

In prompt_toolkit, **you must call `app.invalidate()`** after mutating any `FormattedTextControl`, `BufferControl`, or any data structure that drives the UI. prompt_toolkit does not poll for changes — it redraws only when explicitly invalidated or on input events. Failing to call `invalidate()` after a data mutation renders the change invisible until the next keystroke.

```python
from prompt_toolkit.application import get_app

async def on_state_change(new_state):
    update_transcript_model(new_state)
    get_app().invalidate()  # Required — not optional
```

---

## 10. Layout Ergonomics for Long Sessions

### 10.1 The Coding Agent Layout Pattern

```
┌─────────────────────────────────────────────────┐
│  [Transcript — scrollable, native scrollback]   │
│                                                 │
│  User: implement auth module                    │
│                                                 │
│  Assistant: I'll start by reading the current   │
│  structure...                                   │
│                                                 │
│  [read_file: "src/auth.py"]  ✓ 142 lines (0.3s)│
│  [read_file: "src/config.py"] ✓ 89 lines (0.2s)│
│                                                 │
│  Here's my plan:                                │
│    1. ✓ Read existing auth                      │
│    2. ⠸ Writing new implementation...           │
│    3. ○ Run tests                               │
│                                                 │
├─────────────────────────────────────────────────┤
│  claude-sonnet-4-6 │ 12,847 tok │ 7 calls │ ⠸  │  ← 1-line status, pinned
├─────────────────────────────────────────────────┤
│  > ░                                            │  ← input bar, always last row
└─────────────────────────────────────────────────┘
```

This maps directly to agenthicc's `HSplit([transcript_window, status_window, input_window])` architecture in `app.py`.

### 10.2 Eye Comfort for Long Sessions

- Use `text.muted` (dim) for timestamps, metadata, and secondary information. High-contrast metadata competes visually with content.
- Separate agent turns with a blank line or a thin horizontal rule (`─` × width), not heavy box-drawing.
- Never use blinking (`\e[5m`) for anything other than a transient alert. Blinking text during a 2-hour session is torture.
- Dim completed tool calls relative to in-progress ones. Visual weight should track operational relevance.

### 10.3 Session Context Preservation

The status bar should show enough context that a user returning after a pause can orient themselves without scrolling:

- Current model
- Rough token count (or cost if enabled)
- Number of tool calls this session
- Current operation state (idle / streaming / running tool / awaiting approval)

---

## 11. Unicode and Visual Language

### 11.1 Safe Box-Drawing Characters

```
Single line (safe, universally supported):
  ─ │ ┌ ┐ └ ┘ ├ ┤ ┬ ┴ ┼

Double line (safe):
  ═ ║ ╔ ╗ ╚ ╝ ╠ ╣ ╦ ╩ ╬

Rounded corners (widely supported, not universal):
  ╭ ╮ ╰ ╯
```

Modern terminals (Kitty, WezTerm, Ghostty) render box-drawing characters programmatically at the pixel level, ensuring seamless line connections regardless of font. Older terminals rely on font glyphs — test on target environments if rounded corners are used.

### 11.2 Semantic Symbol Set

| Symbol | Unicode | Meaning |
|---|---|---|
| ✓ | U+2713 | Success / complete |
| ✗ | U+2717 | Failure / rejected |
| ○ | U+25CB | Pending / not started |
| ⠸ | Braille | Running (spinner frame) |
| → | U+2192 | Navigate / expand |
| ← | U+2190 | Back |
| ↑↓ | U+2191/2193 | Scroll / sort |
| ↑3 ↓1 | | Branch ahead/behind (lazygit style) |
| … | U+2026 | Truncated content |
| ▶ | U+25B6 | Collapsed section |
| ▼ | U+25BC | Expanded section |

Always provide ASCII fallbacks for environments where Unicode rendering is uncertain.

### 11.3 Nerd Fonts — Optional Enhancement

Nerd Fonts patch ~3,600 glyphs into Private Use Area Unicode codepoints. These only render correctly if the user has a Nerd Font installed. Make them optional behind a config flag:

```python
USE_NERD_ICONS = config.tui.nerd_fonts  # Default: auto-detect or False

ICON_FILE   = '' if USE_NERD_ICONS else '📄'  # fallback: plain text or ASCII
ICON_FOLDER = '' if USE_NERD_ICONS else '📁'
ICON_GIT    = '' if USE_NERD_ICONS else '[git]'
```

Use the mono variant (single-cell width) for column-aligned layouts.

### 11.4 CJK and East Asian Width

CJK characters occupy 2 columns in the terminal grid. Use `wcwidth` for all display-width calculations:

```python
from wcwidth import wcswidth

def truncate_to_display_width(text: str, max_cols: int, ellipsis: str = '…') -> str:
    """Truncate text to fit within max_cols terminal columns."""
    ellipsis_width = wcswidth(ellipsis)
    budget = max_cols - ellipsis_width
    result = []
    current = 0
    for char in text:
        char_width = wcswidth(char)
        if current + char_width > budget:
            result.append(ellipsis)
            break
        result.append(char)
        current += char_width
    return ''.join(result)
```

**Never use `len()` on strings intended for terminal display.** This is incorrect for any string containing CJK characters, emoji, or combining characters.

---

## 12. Responsive Design for Various Terminal Widths

### 12.1 Width Breakpoints

```python
def get_layout_mode(cols: int) -> str:
    if cols < 60:
        return 'minimal'    # Input bar + essential status only; collapse everything
    if cols < 80:
        return 'compact'    # Single column, compressed header
    if cols < 120:
        return 'standard'   # Normal two-region layout (transcript + status + input)
    return 'wide'           # Side-by-side diff or split-panel optional
```

All functionality must work at 80 columns. 80 columns is the baseline minimum; anything that requires more should be an optional enhancement.

### 12.2 Content Truncation Strategy

- Truncate at display-column boundaries using `wcwidth`, not byte or codepoint boundaries.
- Use `…` (U+2026) as the truncation indicator; takes one column.
- Truncate from the right for file paths (show filename, lose directory prefix).
- Truncate from the middle for long identifiers where both ends are meaningful: `src/.../auth.py`.

### 12.3 SIGWINCH Handling

In prompt_toolkit, resize is handled automatically — the app receives `SIGWINCH` and reflows the layout. Custom layout logic should query `app.output.get_size()` inside layout functions (called on each render), not cache terminal dimensions at startup.

For non-prompt_toolkit code (e.g., a custom ANSI renderer):

```python
import signal, os

def _on_resize(signum, frame):
    global _terminal_size
    _terminal_size = os.get_terminal_size()
    # Re-render with new dimensions

signal.signal(signal.SIGWINCH, _on_resize)
```

### 12.4 Narrow Terminal Degradations

At `< 60` columns:
- Collapse status bar to single character mode indicators: `⠸` for running, `●` for idle, `✗` for error.
- Hide timestamps entirely.
- Show only the input bar and a minimal one-line status.
- Transcript continues to scroll normally.

At `< 80` columns:
- Show a single-line plan summary instead of the full plan tree.
- Collapse all tool call entries unconditionally.
- Truncate model name to a short alias.

---

## 13. Performance and Perceived Speed

### 13.1 Refresh Rate Guidelines

| Scenario | Recommended Rate | Rationale |
|---|---|---|
| LLM streaming output | 60 FPS (16ms batches) | Feels instant; not faster than perception |
| Spinner animation | 8–12 FPS (80–120ms frame) | Smooth but not distracting |
| System resource monitors | 0.5–2s (1.5s default) | Data doesn't change faster |
| Tool call status | Event-driven | Update when state changes, not on timer |
| Progress bar | 4–10 FPS (100–250ms) | Smooth without burning CPU |

### 13.2 Streaming Batch Pattern (Critical)

Do not call `app.invalidate()` per token. Batch at ~60 FPS:

```python
async def stream_llm_with_batching(text_model, token_stream):
    buffer: list[str] = []
    last_flush = asyncio.get_event_loop().time()

    async for token in token_stream:
        buffer.append(token)
        now = asyncio.get_event_loop().time()
        if now - last_flush >= 0.016:       # 16ms = ~60 FPS
            text_model.append(''.join(buffer))
            buffer.clear()
            last_flush = now
            get_app().invalidate()
            await asyncio.sleep(0)          # Yield to event loop

    if buffer:
        text_model.append(''.join(buffer))
        get_app().invalidate()
```

This reduces ANSI write volume by 60–100× versus per-token invalidation.

### 13.3 ANSI Code Overhead

Truecolor codes add ~50 bytes per colored segment (`\e[38;2;255;255;255m` = 19 bytes, `\e[48;2;55;55;55m` = 17 bytes, `\e[0m` = 4 bytes). At 3,782 lines/second this accumulates to ~189 KB/second of pure ANSI overhead. Mitigation:

- Use `\e[0m` (reset all) rather than selectively restoring individual attributes.
- Cache rendered lines for completed turns — do not re-colorize on every frame.
- For repeated same-color spans, use 16-color or 256-color where semantic token precision allows.

### 13.4 Perceived Speed Techniques

- **Optimistic UI:** Show `[tool_name] ⠸ running...` before awaiting the result — do not wait for the coroutine to dispatch before updating the UI.
- **Streaming beats batch:** Show the first LLM token within 100ms of request dispatch; never wait for the complete response.
- **Lazy rendering:** Do not render collapsed sections; compute only what is in the viewport.
- **Component caching:** Cache rendered markdown/syntax-highlighted output of completed messages. Re-parse only on viewport resize or theme change.
- **Async tool dispatch:** Emit the `ToolCallStarted` event and update the spinner before the tool coroutine begins executing. The user sees motion immediately.

---

## 14. Anti-Patterns

### 14.1 Scrollback Anti-Patterns

| Anti-Pattern | Consequence | Fix |
|---|---|---|
| Re-rendering entire conversation on every token | Quadratic ANSI growth; alternate screen buffer exhausted in minutes | Append-only transcript; rewrite only in-progress region |
| Alternate screen with hard scrollback limit | Earlier conversation permanently inaccessible | Normal screen buffer with differential rendering |
| Per-token `app.invalidate()` calls | 4,000–6,700 writes/second; terminal saturation | 16ms batch flush loop |
| Truecolor codes on every line, every render | ~189 KB/s ANSI overhead | Cache rendered lines; reset with `\e[0m` |

### 14.2 UX Anti-Patterns

| Anti-Pattern | Consequence | Fix |
|---|---|---|
| Static footer with all bindings listed | Cognitive overload; wrong bindings for current context | Dynamic footer showing 3–5 context-relevant bindings |
| Printing raw tool output inline by default | Transcript becomes unreadable | One-line collapsed entries; expand on demand |
| Blocking event loop during tool calls | Frozen UI; no spinner updates | `asyncio.create_task()` for all I/O |
| `Ctrl+C` kills the application during streaming | Session lost; user must restart | Cancel stream coroutine, return to input bar |
| Progress bar at 0% for unknown-duration operations | User anxiety; perceived failure | Spinner + elapsed time for unknown-duration tasks |
| Color as the only differentiator in diffs | Colorblind users cannot read diffs | Always add `+`/`-` prefix characters |
| Blinking text (`\e[5m`) in persistent UI elements | Eye strain during long sessions | Never blink; use spinner frames for motion |
| `len()` on terminal-displayed strings | Wrong column widths with CJK/emoji; layout corruption | `wcwidth.wcswidth()` for all display width calculations |
| Caching `os.get_terminal_size()` at startup | Wrong layout on terminal resize | Query inside layout functions; handle SIGWINCH |
| No `app.invalidate()` after data mutations | Changes invisible until next input event | Always call `invalidate()` after mutating UI-backing data |

### 14.3 Terminal Compatibility Anti-Patterns

| Anti-Pattern | Consequence | Fix |
|---|---|---|
| Assuming truecolor on macOS | Broken colors in Terminal.app | Check `$COLORTERM`; degrade to 256-color |
| Nerd Font glyphs without detection | Garbled characters on stock font terminals | Make Nerd Fonts opt-in; provide ASCII fallbacks |
| Ignoring `NO_COLOR` | Breaks accessibility and CI pipelines | Always check `NO_COLOR` on startup |
| Rounded box corners (`╭╮╰╯`) without testing | Gaps or artifacts on older terminals | Test on xterm and macOS Terminal.app; provide fallback |

---

## 15. Specific Recommendations for AI Coding Agent TUI (agenthicc)

This section synthesizes all prior research into concrete, actionable recommendations for agenthicc specifically, mapped to its existing architecture in `src/agenthicc/tui/`.

### 15.1 Architecture: Normal Screen with Pinned Chrome (Priority: Critical)

The current `build_app()` in `app.py` uses `HSplit([transcript_window, status_window, input_window])`. The transcript window must render to the normal screen buffer (not alternate screen) so completed turns become permanent scrollback. In-progress streaming should use the differential render algorithm from §7.2.

Concretely:
- `TranscriptModel` in `transcript.py` should track a "finalized" boundary — lines below this boundary are immutable and must not be re-rendered.
- `render()` in `TranscriptModel` should return only the lines from the finalized boundary onward (the in-progress tail).
- `diff_lines()` already exists and is the correct primitive — it should drive the differential update.
- When the `TUIEventAdapter` in `events.py` detects a turn-complete event, it must call a `finalize_turn()` method that advances the boundary and writes the finalized lines to stdout as permanent output.

### 15.2 Streaming Batch Flush (Priority: Critical)

In `app.py` or `events.py`, implement a 16ms flush loop:

```python
async def _flush_loop(self) -> None:
    """Batch transcript updates at ~60 FPS to avoid per-token invalidation."""
    while True:
        await asyncio.sleep(0.016)
        if self._dirty:
            self._dirty = False
            get_app().invalidate()
```

Set `self._dirty = True` whenever `TranscriptModel` is mutated. This replaces any existing per-token `app.invalidate()` calls.

### 15.3 Tool Call Display (Priority: High)

`ToolCallEntry` in `transcript.py` should render as a single collapsed line:

```
  [tool_name: arg_summary]  ⠸ running...          ← ToolCallState.RUNNING
  [tool_name: arg_summary]  ✓ result_summary (0.3s) ← ToolCallState.DONE
  [tool_name: arg_summary]  ✗ error_summary (1.2s)  ← ToolCallState.FAILED
```

The `SPINNER_FRAMES` constant already exists in `transcript.py` — use it for the running state. `Tab` binding should toggle expanded state, revealing full tool input and output.

### 15.4 Color System (Priority: High)

Add a `Theme` dataclass to `tui/theme.py` (new file) with semantic token names. Detect color level in `__main__.py` at startup and store in `AgenthiccConfig` (already defined in `config.py`). All rendering code queries `theme.token_name` rather than raw ANSI codes. Respect `NO_COLOR` by returning empty strings for all color tokens.

### 15.5 Dynamic Footer (Priority: High)

The footer keybinding hints must update based on the current focus state. Define a `get_context_bindings(focus_state: FocusState) -> list[tuple[str, str]]` function that maps focus states to the 3–5 most relevant `(key, description)` pairs. The status window renders this list, truncated to terminal width.

### 15.6 `wcwidth` Integration (Priority: Medium)

Replace all `len()` calls on terminal-displayed strings with `wcswidth()` from the `wcwidth` package. Specifically audit: `render()` in `transcript.py`; any truncation or padding logic in `app.py`; the status bar formatting in `events.py`.

Add `wcwidth` to `pyproject.toml` dependencies.

### 15.7 Synchronized Output (Priority: Medium)

In `render_frame_ansi()` in `app.py`, wrap the render in synchronized output brackets. Query terminal support at startup:

```python
def query_synchronized_output_support() -> bool:
    sys.stdout.write('\x1b[?2026$p')
    sys.stdout.flush()
    # Read response with timeout; parse \x1b[?2026;N$y
    ...
```

Store the result in config. Emit `\x1b[?2026h` ... `\x1b[?2026l` around each frame when supported.

### 15.8 Interrupt Handling (Priority: Medium)

In `app.py`, bind `Ctrl+C` to a handler that:
1. Sets a `CancellationToken` on the active LLM stream (if any).
2. Emits a `stream_interrupted` kernel event.
3. Appends a `[interrupted]` marker to the current turn.
4. Returns focus to the input bar.

Do not propagate `KeyboardInterrupt` to terminate the process.

### 15.9 Progressive Disclosure for Plans (Priority: Medium)

When `AppState` contains a `Workflow` with multiple nodes, `TUIEventAdapter` should render a collapsed plan header:

```
Plan: {workflow.name}  [{done}/{total} complete]  {current_status}
```

Each `WorkflowNode` renders as one line. The current in-progress node is expanded; completed nodes are collapsed to a checkmark. Use the `DAG.ready_nodes()` output from `workflow/dag.py` to determine which nodes are pending.

### 15.10 Responsive Width (Priority: Low)

In `build_app()`, pass a `get_width` callable to layout components rather than a fixed width. In `render()` methods, query `app.output.get_size().columns` and apply the breakpoint logic from §12.1. At `< 80` columns, collapse all tool entries unconditionally and shorten the status bar to essential indicators only.

### 15.11 Reference Architecture Comparison

| Feature | Claude Code | Aider | Recommended for agenthicc |
|---|---|---|---|
| Screen mode | Alternate screen | Normal screen | Normal screen + pinned chrome |
| Streaming render | Full re-render per token | Linear append | Differential render, 16ms batch |
| Scrollback | ~2,000 line limit, lost | Unlimited native | Unlimited native |
| Tool call display | Inline verbose | Inline verbose | Collapsed one-line + expand |
| Approval UI | Full-screen modal | Inline `y/n` | Inline modal, no alternate screen |
| Color system | Hardcoded truecolor | Configurable | Semantic tokens, level-aware |
| Interrupt handling | `Ctrl+C` = exit | `Ctrl+C` = interrupt | `Ctrl+C` = interrupt stream |

---

## Sources

- [The Terminal Renaissance: Designing Beautiful TUIs in the Age of AI — DEV Community](https://dev.to/hyperb1iss/the-terminal-renaissance-designing-beautiful-tuis-in-the-age-of-ai-24do)
- [So you want to render colors in your terminal — marvinh.dev](https://marvinh.dev/blog/terminal-colors/)
- [Terminal colours are tricky — Julia Evans](https://jvns.ca/blog/2024/10/01/terminal-colours/)
- [What I learned building an opinionated and minimal coding agent — mariozechner.at](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/)
- [Claude Code issue #9935: Excessive scroll events causing UI jitter](https://github.com/anthropics/claude-code/issues/9935)
- [Claude Code issue #51199: TUI re-renders full conversation history on each response](https://github.com/anthropics/claude-code/issues/51199)
- [Claude Code issue #38283: Configurable scrollback buffer size for TUI alternate screen](https://github.com/anthropics/claude-code/issues/38283)
- [Announcing Toad — Will McGugan](https://willmcgugan.github.io/announcing-toad/)
- [CLI UX best practices: 3 patterns for improving progress displays — Evil Martians](https://evilmartians.com/chronicles/cli-ux-best-practices-3-patterns-for-improving-progress-displays)
- [How do terminal progress bars actually work? — code.mendhak.com](https://code.mendhak.com/how-do-terminal-progress-bars-actually-work/)
- [Terminal Spec: Synchronized Output CSI ?2026](https://gist.github.com/christianparpart/d8a62cc1ab659194337d73e399004036)
- [Escape Sequences — WezTerm docs](https://wezterm.org/escape-sequences.html)
- [NO_COLOR standard](https://no-color.org/)
- [Building full screen applications — prompt_toolkit 3.0 docs](https://python-prompt-toolkit.readthedocs.io/en/master/pages/full_screen_apps.html)
- [Running on top of asyncio — prompt_toolkit 3.0 docs](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/advanced_topics/asyncio.html)
- [prompt_toolkit asyncio invalidation — Issue #1847](https://github.com/prompt-toolkit/python-prompt-toolkit/issues/1847)
- [How less works: the terminal's alternative buffer — jameshfisher.com](https://jameshfisher.com/2017/12/04/how-less-works/)
- [Beyond the GUI: The Ultimate Guide to Modern TUI Applications — BrightCoding](https://www.blog.brightcoding.dev/2025/09/07/beyond-the-gui-the-ultimate-guide-to-modern-terminal-user-interface-applications-and-development-libraries/)
- [lazygit — GitHub](https://github.com/jesseduffield/lazygit)
- [k9s — GitHub](https://github.com/derailed/k9s)
- [fzf — GitHub](https://github.com/junegunn/fzf)
- [delta — GitHub](https://github.com/dandavison/delta)
- [Nerd Fonts — GitHub](https://github.com/ryanoasis/nerd-fonts)
- [wcwidth — PyPI](https://pypi.org/project/wcwidth/)
