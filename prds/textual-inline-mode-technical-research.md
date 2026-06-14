# Textual Inline Mode, Rich Live, log-update & Ink — Deep Technical Research

**Document type**: Technical deep-dive  
**Scope**: Exact ANSI mechanics of inline (non-alternate-screen) rendering across four major libraries  
**Status**: Complete

---

## 1. Textual Inline Mode (`App.run(inline=True)`)

Introduced in **Textual v0.55.0** (April 2024).

### Activation

```python
app.run(inline=True)           # no alternate screen
app.run(inline=True, inline_no_clear=True)  # don't clear on exit
```

CSS pseudo-class for inline-only styles: `:inline`

### Internal Mechanism

Each frame:
1. Renders to a string with all lines ending in `\n` except the last
2. After the last line, emits a cursor-repositioning sequence to overwrite next frame
3. On shrink: emits "clear downward" to erase orphaned lines

Mouse support: queries cursor position via `\x1b[6n` (DSR), terminal responds `\x1b[row;colR`. App subtracts cursor row from absolute mouse coordinates to get app-relative events.

**Synchronized output**: `\x1b[?2026h`/`\x1b[?2026l` was used v0.55.0–v0.56.3, then **disabled in v0.56.4** due to terminal incompatibilities.

### Height Management

```css
/* Limit inline app height */
Screen {
    &:inline { height: 20; }
}
```

Remove top padding: `App.INLINE_PADDING = 0` (v0.80.0+)

### Known Bugs

| Version | Issue |
|---|---|
| v0.56.2 | **Interactive widgets severely laggy** — checkbox doesn't toggle until blur (#4403) |
| v0.56.1 | Flickering on non-current screen updates |
| v0.56.3 | App not updating properly |

**Recommendation**: Use Textual v0.58.0+ for inline mode stability.

---

## 2. Rich `Live` — Exact Source Code Analysis

### Core Data Flow

```python
# rich/live.py
self._live_render = LiveRender(
    self.get_renderable(),
    vertical_overflow=vertical_overflow
)
```

### `position_cursor()` — The Erase/Rewrite Sequence

```python
# rich/live_render.py
def position_cursor(self) -> Control:
    if self._shape is not None:
        _, height = self._shape
        return Control(
            ControlType.CARRIAGE_RETURN,       # \r
            (ControlType.ERASE_IN_LINE, 2),    # \x1b[2K
            *(
                (
                    (ControlType.CURSOR_UP, 1),        # \x1b[1A
                    (ControlType.ERASE_IN_LINE, 2),    # \x1b[2K
                )
                * (height - 1)
            )
        )
    return Control()
```

For a 4-line live region, `position_cursor()` emits:
```
\r\x1b[2K\x1b[1A\x1b[2K\x1b[1A\x1b[2K\x1b[1A\x1b[2K
```

### `restore_cursor()` — Full Clear

```python
def restore_cursor(self) -> Control:
    if self._shape is not None:
        _, height = self._shape
        return Control(
            ControlType.CARRIAGE_RETURN,
            *((ControlType.CURSOR_UP, 1), (ControlType.ERASE_IN_LINE, 2)) * height
        )
    return Control()
```

`restore_cursor()` does `height` up+erase pairs (one MORE than `position_cursor`), fully clearing the area.

### ControlType → ANSI Mapping

| ControlType | Sequence |
|---|---|
| `CARRIAGE_RETURN` | `\r` |
| `ERASE_IN_LINE, 2` | `\x1b[2K` (erase entire line) |
| `CURSOR_UP, N` | `\x1b[NA` |
| `HOME` | `\x1b[H` (only in alt-screen mode) |

### Vertical Overflow Modes

- `"crop"` — truncate to terminal height
- `"ellipsis"` — keep height-1 lines + `"…"`
- `"visible"` — allow overflow (scrolls)

---

## 3. `log-update` (npm) — Source Analysis

### Core: `eraseLines(count)` from `ansi-escapes`

```javascript
// ansi-escapes/base.js
export const eraseLines = count => {
    let clear = '';
    for (let i = 0; i < count; i++) {
        clear += eraseLine + (i < count - 1 ? cursorUp() : '');
    }
    if (count) {
        clear += cursorLeft;
    }
    return clear;
};
```

Where:
- `eraseLine` = `\x1b[2K`
- `cursorUp(n)` = `\x1b[{n}A` (default n=1)
- `cursorLeft` = `\x1b[G` (column 1)

**For 3 lines**: `\x1b[2K\x1b[1A\x1b[2K\x1b[1A\x1b[2K\x1b[G`

### Incremental Diff Mode

Ink's `log-update.ts` adds incremental updates:

```typescript
// Line-by-line diff
if (nextLines[i] === previousLines[i]) {
    // skip — advance cursor
} else {
    cursorTo(0) + eraseEndLine + write(newLine)
}
```

`diffFrames()` finds first changed line (`start`) and last changed line (`endPrevious`, `endNext`). `buildPatch()` constructs minimal escape sequence rewriting only the changed region.

### Synchronized Output

```javascript
const BSU = '[?2026h'  // Begin Synchronized Update
const ESU = '[?2026l'  // End Synchronized Update
```

Brackets each write when `isTTY && isInteractive`.

---

## 4. Ink (React for CLI) — Source Analysis

### Architecture

- Custom React reconciler → virtual tree of terminal elements
- **Yoga** (Facebook's Flexbox engine) computes positions
- `renderNodeToOutput` → 2D character buffer → ANSI string
- `log-update.ts` writes to stdout

### Core Render Loop

```typescript
// ink/src/ink.tsx
const lastOutputHeight = previousOutputHeight;
eraseLines(lastOutputHeight);
write(newOutput);
lastOutputHeight = newOutput.split('\n').length;
```

### Full-Screen Detection

```typescript
const wasFullscreen = previousOutputHeight >= viewportRows;
const isFullscreen  = nextOutputHeight >= viewportRows;
```

Full-screen frames (height >= terminal rows) trigger a complete `\x1b[2J` clear on Windows (console scrolls on bottom-right cell write). On non-Windows, selective erasure is used.

### `<Static>` Component — The "Committed" Pattern

`<Static>` renders content **permanently above the managed block**:
- Written once, never erased
- Subsequent live updates happen below it
- This is the `commit_lines()` equivalent in our architecture

### Complete ANSI Sequence Map (Ink)

| Operation | Sequence |
|---|---|
| Erase N lines | `\x1b[2K\x1b[1A` × N + `\x1b[G` |
| Cursor to col 0 | `\x1b[G` |
| Erase to end of line | `\x1b[K` |
| Cursor next line | `\x1b[E` |
| Enter alt screen | `\x1b[?1049h` (optional) |
| Exit alt screen | `\x1b[?1049l` (optional) |
| Hide cursor | `\x1b[?25l` |
| Show cursor | `\x1b[?25h` |
| Begin Sync Update | `\x1b[?2026h` |
| End Sync Update | `\x1b[?2026l` |

---

## 5. Canonical Pattern Comparison

| Library | Strategy | Erase method | Line tracking |
|---|---|---|---|
| **Textual inline** | Cursor-up per line, clear downward on shrink | `\r\x1b[2K\x1b[1A` repeated | `_previous_inline_height` |
| **Rich Live** | Same cursor-up-and-erase loop via `LiveRender` | `\r\x1b[2K\x1b[1A\x1b[2K...` | `_shape: (width, height)` |
| **log-update** | `eraseLines(n)` then full rewrite, or incremental patch | `\x1b[2K\x1b[1A` loop + `\x1b[G` | `previousLineCount` |
| **Ink** | `eraseLines(lastOutputHeight)` then rewrite | Same as log-update | `wrappedOutput.split('\n').length` |

**All four converge on the same core ANSI pattern**: erase N lines by looping `\x1b[2K\x1b[1A`, then write new content. Sophistication differences are in partial-diff updates and mouse coordinate translation.

---

## 6. Python Reference Implementation

```python
import sys

# The canonical inline-update sequence
CURSOR_UP   = "\x1b[1A"
ERASE_LINE  = "\x1b[2K"
CURSOR_COL1 = "\x1b[G"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

def erase_lines(n: int) -> str:
    """Erase n lines above cursor, position cursor at start of first."""
    if n == 0:
        return ""
    # Erase current line, then for each additional: go up + erase
    return ERASE_LINE + (CURSOR_UP + ERASE_LINE) * (n - 1) + CURSOR_COL1

def update_live_region(stdout, old_height: int, new_content: str) -> int:
    """Erase old live region and write new content. Returns new line count."""
    stdout.write(HIDE_CURSOR)
    if old_height > 0:
        stdout.write(erase_lines(old_height))
    stdout.write(new_content)
    stdout.write(SHOW_CURSOR)
    stdout.flush()
    return new_content.count("\n") + (1 if new_content and not new_content.endswith("\n") else 0)
```

---

**Sources:**
- [Textual — Behind the Curtain of Inline Terminal Applications](https://textual.textualize.io/blog/2024/04/20/behind-the-curtain-of-inline-terminal-applications/)
- [Textual — Style Inline Apps](https://textual.textualize.io/how-to/style-inline-apps/)
- [Interactive widgets lag in inline mode #4403](https://github.com/Textualize/textual/issues/4403)
- [rich/live.py](https://github.com/Textualize/rich/blob/master/rich/live.py)
- [log-update npm](https://www.npmjs.com/package/log-update)
- [ansi-escapes](https://github.com/sindresorhus/ansi-escapes)
- [Ink source — ink.tsx](https://github.com/vadimdemedes/ink/blob/master/src/ink.tsx)
- [Ink incremental rendering PR #781](https://github.com/vadimdemedes/ink/pull/781)
- [Terminal resize artifacts #907](https://github.com/vadimdemedes/ink/issues/907)
