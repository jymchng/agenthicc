# PRD-73 — Workspace Layout: Blank Separator and Dynamic Status Height

## Problem

The current Live block layout has two deficiencies:

1. **No visual gap between the scroll buffer and the status bar.**
   The transcript and the status bar are separated only by a `─` border line.
   A blank line above the border would create a cleaner visual boundary and
   make it immediately obvious where the scroll buffer ends.

2. **`StatusComponent.height()` is hardcoded, not dynamic.**
   It returns `3 if model_name else 1` regardless of what `render()` actually
   produces. If a future line is added (error detail, recovery sub-label,
   active-skill indicator), the `height()` value drifts from the real rendered
   row count. The `ScrollBufferAppender` and the workspace use `height()` to
   reason about the Live block's footprint; a wrong value causes mis-alignment.

---

## Goals

- A blank line appears between the transcript scroll buffer and the status bar.
- `StatusComponent.height()` always matches the true number of terminal rows
  that `render()` produces, including future additions.
- The input bar is always pinned to the bottom of the Live block regardless of
  status height changes.
- TUI overlays (trigger picker, config menu, help) are unaffected — they
  replace the composer in the same Group slot.
- The footer (mode string + hints) is always the last 2 rows of the Live block.
- No change to `transient=True`, `auto_refresh=False`, or the overlay/footer
  rendering paths.

---

## How Rich's Live block anchors to the terminal

The Live block always occupies the **bottom N rows** of the terminal. When it
is redrawn at a taller height (N grows), it grows **upward** — consuming rows
from the scroll buffer region, not pushing the input bar further down. The
input bar is the last item in the `Group`, so it sits at the absolute terminal
bottom at all times. Overlays live in the composer slot — their position in the
Group order does not change.

This means dynamic status height is structurally free: the scroll buffer loses
one row of visible content when status grows, and gains it back when status
shrinks. The input bar and footer stay at the terminal bottom without any
special handling.

---

## Layout (before and after)

### Before

```
[scroll buffer]
──────────────────────── (border)
✾ Thinking │ Runtime: 00:04        ← StatusComponent (3 lines)
openai/poolside/laguna-xs.2
abc123 │ 2 turns │ $0.000 ↑ 0 ↓ 0
──────────────────────── (border)
❯ ▌                                 ← ComposerComponent
──────────────────────── (border)
⏵⏵ Auto  (shift+tab to cycle)      ← FooterComponent (2 lines)
Enter Submit  │  /cmd  │  @Mention
```

### After

```
[scroll buffer]
                                     ← blank line (new)
──────────────────────── (border)
✾ Thinking │ Runtime: 00:04        ← StatusComponent (dynamic rows)
openai/poolside/laguna-xs.2
abc123 │ 2 turns │ $0.000 ↑ 0 ↓ 0
──────────────────────── (border)
❯ ▌                                 ← ComposerComponent (unchanged)
──────────────────────── (border)
⏵⏵ Auto  (shift+tab to cycle)      ← FooterComponent (unchanged)
Enter Submit  │  /cmd  │  @Mention
```

The blank line is the first element of the Live block's `Group`. It is inside
the Live block (not printed via `console.print()`), so it moves with the Live
block and never appears in the scroll buffer.

---

## Changes

### 1. `tui/workspace/workspace.py` — `_build()`

Insert `Text("")` as the first element of the `Group` before the status bar
render, and account for it in any height calculation that uses
`status.height()`.

```python
def _build(self) -> Any:
    from rich.text import Text
    from rich.console import Group
    cols = _get_cols()
    parts = [
        Text(""),                        # blank separator between scroll buffer and status
        self.status.render(),
        _border(cols),
        self.overlays.render() if self.overlays.active else self.composer.render(),
        _border(cols),
        self.footer.render(),
    ]
    return Group(*parts)
```

### 2. `tui/workspace/components.py` — `StatusComponent.height()`

Replace the hardcoded return value with a computation that matches what
`render()` actually produces. `render()` returns a `Group` of `Text` objects;
counting them gives the true row count. To avoid rendering twice, derive the
count from the same data `render()` uses:

```python
def height(self, cols: int) -> int:  # noqa: ARG002
    conv = self._state.conversation
    # 1 = blank separator, 1 = line-1 (state), N = optional lines
    blank       = 1
    line1       = 1                                  # always present
    line2       = 1 if conv.model_name() else 0      # model name
    line3       = 1 if conv.model_name() else 0      # session info / tokens
    return blank + line1 + line2 + line3
```

The `blank` term is included because the blank separator line is the first
element of the Live block and must be counted in the total Live block height.

Any future line added to `render()` must also be counted here. To enforce this,
a test asserts that `height()` equals the actual rendered line count.

---

## What does NOT change

| Component | Change |
|---|---|
| `ComposerComponent` | None |
| `FooterComponent` | None |
| `OverlayHost` / overlays | None |
| `ScrollBufferAppender` | None |
| `transient=True`, `auto_refresh=False` | None |
| Signal wiring / `_redraw()` | None |

---

## Acceptance criteria

- [ ] A blank line is visible between the last scroll-buffer line and the top
      border of the status bar.
- [ ] The blank line moves with the Live block on redraw — it never appears in
      the scroll buffer.
- [ ] `StatusComponent.height()` returns the same value as the number of
      terminal rows `render()` occupies (asserted by a unit test).
- [ ] With the blank line included, `height()` returns `4` when all three
      status lines are present, `2` when only line 1 is present.
- [ ] The input bar remains at the terminal bottom when the status bar height
      changes (verified visually — no unit test needed).
- [ ] All existing tests pass.
