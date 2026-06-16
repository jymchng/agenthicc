# PRD-84 — Multi-line Composer Rendering

## Background

`ComposerComponent.render()` passes the prompt string through `_fit(prompt, cols)`
before handing it to Rich.  `_fit` is designed for single-line strings: it
calls `Text.from_markup(markup).cell_len` which sums the visible character
widths across **all lines** of a multi-line string.  When that sum exceeds
`cols` (typically 80–220 columns), `fit()` truncates the string at `cols`
characters, cutting off everything from the first newline onwards.

For short typed multi-line content (a few short lines via Ctrl+J), the sum
rarely exceeds `cols` so the bug is invisible.  For an expanded paste (Ctrl+V
on a paste block with many lines or long lines), the sum easily exceeds `cols`
and only the first line is visible.

`ComposerComponent.height()` already computes the correct multi-line height
(including terminal soft-wrap), so the Live block is always tall enough —
the content just fails to fill it.

---

## Goals

- Expanded paste (Ctrl+V on a condensed block) renders all lines of the
  pasted content in the composer area.
- Long typed multi-line input (via Ctrl+J) renders correctly regardless
  of total character count across all lines.
- Condensed paste label and single-line input continue to behave exactly
  as today.
- No changes to `InputBuffer`, `PasteState`, `build_prompt`, signals,
  `ComposerComponent.height()`, or any other file.

## Non-Goals

- Scrollable composer (the input area is not capped; all lines are shown).
- Syntax highlighting of pasted content.
- Changes to paste condensation / expansion logic.

---

## Root cause

```
ComposerComponent.render()
    prompt = build_prompt(buf, cursor)   # → "❯ line1\n\r  line2\n\r  line3"
    return Text.from_markup(             # ← _fit sums cell widths across ALL
        _fit(prompt, cols)               #   lines; large paste exceeds cols
    )                                    #   → fit() truncates after line 1
```

`_fit` is the wrong tool for multi-line strings.  It must be bypassed when
the buffer contains `\n`.

---

## Design

### Branch in `ComposerComponent.render()`

```
inp.paste_condensed() == True
    → single condensed label line  (existing path, _fit safe)

"\n" not in inp.buf()
    → single-line normal typing    (existing path, _fit safe)

"\n" in inp.buf()
    → multi-line path              (new: _render_multiline, no _fit)
```

The expanded-paste case falls naturally into the multi-line path because
`_push()` sets `paste_condensed=False` and stores the full buf when Ctrl+V
is pressed.  No special detection of "expanded paste" is needed.

### `_render_multiline(buf, cursor)` — module-level helper

Builds one Rich `Text` object per logical line (split on `'\n'`).  Returns
`Group(*lines)` so Rich lays them out as separate terminal rows.

Steps:
1. Split `buf` on `'\n'` into logical lines (`list[list[str]]`).
2. Walk the lines to find which logical line the cursor falls on and at
   which column offset.
3. For each logical line, build a `Text`:
   - Prefix: `"❯ "` (bold green) for line 0; `"  "` for subsequent lines.
   - Body before cursor: plain characters.
   - Cursor character `"▌"` (bold) at the cursor position.
   - Body after cursor: plain characters.
4. Return `Group(*texts)`.

The cursor appears on exactly one line.  Lines without the cursor carry no
cursor character.

### Why no `_fit` is needed here

Rich's `Live` block renders each `Text` in the `Group` as a separate Rich
renderable.  Rich handles terminal-width soft-wrapping internally — each
`Text` is wrapped by Rich if it exceeds the terminal width, so long lines
are displayed correctly without overflow.  `ComposerComponent.height()` already
accounts for this soft-wrap when it computes how many rows the composer occupies.

### `PROMPT_CHAR` and `CURSOR_CHAR` reuse

`_render_multiline` imports `PROMPT_CHAR` and `CURSOR_CHAR` from
`agenthicc.tui.input.renderer` so the characters are defined in one place.

---

## File changes

| File | Change |
|---|---|
| `tui/workspace/components.py` | Add `_render_multiline(buf, cursor)` module-level function; update `ComposerComponent.render()` to branch on `"\n" in disp_buf` |
| `prds/prd-68-feature-expectations.md` | Update §3 to document multi-line rendering behaviour |

No other files change.

---

## PRD-68 §3 update

Add to the Input Bar table:

| 3.12 | Multi-line rendering | When the buffer contains newlines (via Ctrl+J or expanded paste), the composer renders one row per logical line. All lines are visible. The `❯` prefix appears only on the first line; continuation lines are indented with two spaces. The cursor character `▌` appears on the correct line at the correct column. |

---

## Acceptance criteria

- [ ] After Ctrl+V expands a multi-line paste, all lines of the paste are
      visible in the composer area.
- [ ] After typing across multiple lines with Ctrl+J, all lines remain
      visible even when their combined character count exceeds terminal width.
- [ ] The cursor `▌` appears at the correct position on the correct line
      for all cursor positions in a multi-line buffer.
- [ ] Single-line input and condensed paste label are visually unchanged.
- [ ] `ComposerComponent.height()` returns the correct row count for all
      cases (already correct — verified by existing layout tests).
- [ ] All existing unit, integration, and e2e tests pass.
