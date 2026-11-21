---
title: "Claude Code's input panel cursor: model, navigation, wrapping and edge cases"
status: research
date: 2026-11-07
subject: "@anthropic-ai/claude-code 2.1.100 (cli.js bundle) and code.claude.com/docs/en v2.1.3xx"
tags: [tui, cursor, composer, claude-code, research]
---

# Claude Code's input panel cursor

**Sources.** Official docs: `code.claude.com/docs/en/interactive-mode` (plus
`fullscreen`, `terminal-config`, `keybindings`). The shipped implementation:
`@anthropic-ai/claude-code@2.1.100`'s `cli.js` (13 MB bundle). Current releases
(2.1.319) ship a native binary, so the internals below are from that JS build.
Where the 2.1.26x readline changes apply, it is flagged.

## The model in one sentence

The input is a **Unicode string plus one integer, `cursorOffset`**. Everything
else — the visual row/column, the drawn block, the real terminal caret, click
mapping, word motion, scrolling — is derived from that offset against a
measured, wrapped copy of the text.

You can see this directly in the React component (`e4`): it takes `value`,
`cursorOffset`, `onChangeCursorOffset`, `columns`, `multiline`, `showCursor`,
`focus`, `highlights`, `inlineGhostText`, and calls a headless hook that returns
`{onInput, renderedValue, cursorLine, cursorColumn}`.

## How the cursor is drawn

The cursor is **painted**, not delegated to the terminal:

1. The text is split into spans at highlight boundaries and at the cursor
   offset (`…, cursorOffset, cursorOffset+1`), and the span starting at the
   cursor is rendered with `inverse` (reverse video on the character under the
   cursor).
   - `function UVY(text, highlights, cursorIndex)` builds
     `Text({inverse: $ === cursorIndex}, H)`.
   - At end of input it substitutes `" "` so the cursor is still visible after
     the last character.
2. `cursorChar` is `" "` when `showCursor` is on and `""` when off.
3. The cursor only appears when all three are true: the widget has focus,
   `showCursor` is on, and the terminal window itself is focused
   (`terminalFocus`). Unfocus the terminal and the block disappears.
4. Screen-reader/accessibility mode replaces `chalk.inverse` with identity; a
   voice-recording session animates the block through `" ▁▂▃▄▅▆▇█"` with a hue
   rotation instead of a flat inverse cell.
5. With an empty input and a placeholder, the first placeholder character is
   the solid cursor and the rest is `dimColor` ghost text.

## It also moves the real terminal cursor

Separately from the painted block, the component registers `{line, column,
active}` with a layout context (`nK6`, a `useLayoutEffect` that reports the
Yoga node plus `relativeX`/`relativeY`). The renderer converts that to absolute
coordinates, tracks `displayCursor`, and emits relative/absolute
cursor-movement escapes each frame.

That matters for three things: IME composition (the native caret must be where
you are typing), screen readers (they follow the terminal caret, which is why
`inverse` is disabled there), and terminals that draw their own caret.

## Navigation is layered: grapheme → word → visual row → logical line → history

The buffer class derives everything from `Intl.Segmenter`:

- **Grapheme boundaries.** `getGraphemeBoundaries()` and
  `snapToGraphemeBoundary()` mean the offset can never land inside an emoji, a
  combining sequence, or a flag. `nextOffset`/`prevOffset` are cached binary
  searches over those boundaries.
- **Display width, not code units.** `getPositionFromOffset(offset)` returns
  `{line, column}` where `column` is a display column
  (`stringIndexToDisplayWidth`), so CJK/emoji count 2. On Bun it uses
  `Bun.stringWidth`; otherwise a `string-width` clone with
  `ambiguousIsNarrow: true`.
- **Text is NFC-normalized on construction** (`text.normalize("NFC")`), so the
  offset is stable against combining marks.
- **Word motion** uses the word segmenter's `isWordLike` runs. Current docs
  (v2.1.261+) pin the readline semantics: `Alt+B/F/D`, `Option+Delete`,
  `Ctrl+Backspace` treat a word as letters/digits (punctuation such as `_`,
  `.`, `/` separates them), while `Ctrl+W` deletes back to the previous
  whitespace — one press removes all of `src/utils/foo.ts`.

Vertical motion has four tiers (from the Up handler):

```js
if (disableCursorMovementForUpDownKeys) return historyUp();
let next = buffer.up();                        // 1. previous *wrapped* visual row
if (!next.equals(buffer)) return next;
if (multiline) {
  let logical = buffer.upLogicalLine();        // 2. same column, previous \n line
  if (!logical.equals(buffer)) return logical;
}
return historyUp();                            // 3. command history
```

`up()`/`down()` keep the display column and clamp it to the target row's width;
`upLogicalLine()` keeps the character column relative to the logical line.
`disableCursorMovementForUpDownKeys` is exactly what makes ↑/↓ drive the
`/`-command and `@`-file suggestion list when it is open.

`Ctrl+A`/`Home` and `Ctrl+E`/`End` are `startOfLine()`/`endOfLine()` on the
wrapped row, and pressing again walks to the next row's edge — i.e. they walk to
the start/end of the logical line (this is the documented "start of current
line, then the whole input" behaviour).

## Wrapping, viewport and click-to-position

- **Wrapping** re-measures the buffer whenever `columns` changes:
  `wrapText(text, columns, {hard: true, trim: false})` — word-wrap, but
  hard-break words longer than the line, and do not trim leading whitespace on
  continuation rows. Each row records `{text, startOffset,
  isPrecededByNewline, endsWithNewline}`, which is what lets
  `getOffsetFromPosition({line, column})` map a screen cell back to a string
  index.
- **The composer's width** is terminal width minus border/padding
  (`HZ = columns - padding`).
- **Classic renderer:** the composer grows without a cap. **Fullscreen
  renderer:** `maxVisibleLines ≈ max(8, floor(rows/2) - chrome)`, and the buffer
  keeps a cursor-centred viewport (`getViewportStartLine` scrolls by half a
  page, `getViewportCharOffset`/`End` slice what is drawn). So the input scrolls
  internally instead of eating the screen.
- **Click-to-position** (fullscreen, mouse enabled) maps a click to an offset:

  ```js
  offset = buffer.getOffsetFromPosition({
      line: click.localRow + buffer.getViewportStartLine(maxVisibleLines),
      column: click.localCol
  });
  setCursorOffset(offset);
  ```

  Disabling mouse capture (`CLAUDE_CODE_DISABLE_MOUSE`) removes
  click-to-position along with click-to-expand and wheel scrolling.

## Editing, undo, chips and paste

- The key handler is a dispatch table over the same buffer: `Ctrl+A/E` line
  ends, `Alt+B/F` word motion, `Ctrl+W`/`Alt+Backspace`/`Ctrl+H` delete-back,
  `Alt+D` delete-forward, `Ctrl+K`/`Ctrl+U` kill, `Ctrl+Y` yank,
  `Delete`/`Ctrl+D` delete-under.
- Undo restores text **and** cursor offset
  (`onUndo → setText(entry.text); setCursorOffset(entry.cursorOffset)`).
  `Ctrl+S` stash/restore likewise brings back text, cursor position, pasted
  content and input mode.
- Large pastes become **atomic chips** — `[Pasted text #N]`,
  `[Pasted text #N +M lines]`, `[Image #N]`, `[…Truncated text #N +M lines…]`.
  The buffer treats them as single units: `snapOutOfImageRef()` pulls the cursor
  to the chip's edge and `deleteTokenBefore()` removes the whole chip. On
  submit, a regex pass expands every chip back to its full stored content.
  `Ctrl+V`/`Cmd+V`/`Alt+V` inserts an `[Image #N]` chip **at the cursor**, so
  its position in the prompt is meaningful.
- `Ctrl+V`-pasted text triggers `highlightPastedText`, and the cursor/highlight
  spans are computed together so the inverse cell never splits a highlight
  incorrectly:

  ```js
  highlights.filter(h =>
      h.dimColor || cursorOffset < h.start || cursorOffset >= h.end)
  ```

## Multiline

Newlines enter through the same key path and simply insert `\n` at the offset:
`\` + Enter (the backslash is removed), `Shift+Enter`/`Option+Enter` (when the
terminal reports them), `Ctrl+J` (universal), or a multi-line paste.
`multiline: true` is what enables the logical-line tier of ↑/↓.

## Vim mode

`/config` → Editor mode swaps the **key layer**, not the buffer: `h/j/k/l`,
`w/e/b`, `0/^/$`, `gg/G`, `f/F/t/T/;/ ,` (via `findCharacter`), `%` bracket
matching, operators + text objects, visual mode, `u`/`.`.

- Vim word classes are **separate** from readline ones: `nextVimWord()` /
  `endOfVimWord()` use `[\p{L}\p{N}\p{M}_]` for word characters and a distinct
  "punctuation run" class — standard vim behaviour.
- The cursor is the same inverse cell; NORMAL/INSERT changes **where it is
  allowed to sit**.
- Mode and cursor position survive `Ctrl+O` (transcript viewer) and
  opening/closing panels like `/config`.
- `vimInsertModeRemaps` (e.g. `jj → <Esc>`) is implemented as a one-second
  pending-character window: the first key is inserted normally, the second
  removes it and switches to NORMAL.
- In NORMAL mode, at the start/end of input `j`/`k` and ↑/↓ fall through to
  command history.

## Edge cases worth knowing

| Behaviour | Detail |
| --- | --- |
| macOS Option keys | `Alt+B/F/D/Y` need "Option as Meta" configured in the terminal; otherwise the terminal sends a composed character. |
| Invisible characters | On Enter, bidi controls / zero-width / tag characters are stripped from the prompt and expanded paste chips. If anything was removed, that Enter is swallowed: the cleaned text goes back into the box with *Removed N invisible characters · review and press Enter to send*. |
| Cursor and history | Recalled prompts land in the input box; a recalled prompt containing pasted text is re-expanded on submit. Current releases keep the last ten `Ctrl+C`-cleared drafts per session, including pasted text and images. |
| Queued messages | While a turn runs, ↑ from the first row takes back the queue and puts it in the input box above your draft, instead of moving the cursor. |
| CJK/Thai/Lao etc. | Grapheme segmentation and double-width columns keep the block aligned; word motion still advances one word at a time. |
| Emoji shortcodes | `:heart:` replaces in place at the cursor; `:hea` opens a popup anchored to the cursor and Tab/Enter inserts. |
| Ghost text | `/`-command and `@`-file completions render as dim `inlineGhostText` after the cursor; Tab/→ accepts, typing dismisses. |
| Spell check | Underlines are highlights, not text changes; the word under the cursor is left alone until you move past it. |

## Caveats

- **Version-sensitive:** docs now describe v2.1.3xx; `Ctrl+W`'s whitespace
  semantics and the readline word classes arrived in v2.1.261, so a 2.1.100
  bundle (what was inspected) still had `Ctrl+W` bound to the segmenter's word
  boundary. The architecture (offset + measured wrapped lines + painted inverse
  cell + moved terminal caret) is unchanged across those versions.
- The keyboard-shortcut tables and the docs' word-boundary section are the
  authoritative UX contract; the minified implementation is best read for
  *how*, not for exact key lists.

## If you're copying this into agenthicc

The four load-bearing ideas, in order of payoff:

1. One integer cursor offset over an NFC-normalised string, never a
   row/column pair.
2. Measure with display width + `Intl.Segmenter` graphemes so wide characters
   and emoji are atomic.
3. Separate "visual row" from "logical line" and make ↑/↓ try them in that
   order before history.
4. Paint the cursor yourself and park the real terminal caret on it, so IME and
   screen readers work.

This is close to what TUX-04 already does for the composer; the notable gaps
versus Claude Code are the viewport (internal scrolling past `maxVisibleLines`),
atomic paste/image chips, and click-to-position mapping through the viewport
offset.

---

## Appendix: bundle anchors verified in 2.1.100

Byte offsets into `package/cli.js` (13,402,650 bytes, one line). Recover a
slice with
`python -c "print(open('cli.js').read()[A:B])"`.

| Offset | What lives there | Why it matters |
| --- | --- | --- |
| ≈ 10,040,451 | `class v5` — `measuredText`, `selection`, `offset`; `static fromText()`; `getViewportStartLine()` | the buffer/offset model |
| ≈ 10,041,143 | `v5.render(mask, invert, columns, ghost, maxVisibleLines)` | splits the caret row into `before + invert(cell) + ghost + after` |
| ≈ 10,057,260 | `zc8(...)` — key dispatch table, `cursorChar`, edit commands | where every motion lands |
| ≈ 10,067,000 | `Yc8` — computes `{line, column, active}` and calls `nK6(...)` | the caret declaration |
| ≈ 10,068,600 | `e4` — TextInput wrapper; picks `invert` (`A8.inverse`, identity, or the voice meter) | the single styling seam |
| ≈ 6,837,601 | `nK6({line, column, active})` → `declare({relativeX, relativeY, node})` | painted cursor vs. real caret |
| ≈ 4,093,553 | paint pass: `nodePositions.get(decl.node)` → absolute; `CSI r;c H` in alt screen, relative `CSI n C/D/A/B` inline | how the real caret is moved |
| ≈ 4,036,005 | renderer mount: `stdout.write(CSI ?25l)` unless `CLAUDE_CODE_ACCESSIBILITY`; `?25h` on unmount | terminal caret is hidden, not used |
| ≈ 12,339,275 | `showCursor: !footerSelection && !isResponding && !onImageChip` | suppression rules |
| ≈ 12,324,900 | highlight span builders (`{start, end, color, themeColor, dimColor, inverse, shimmerColor, priority}`) | inverse cell wins over syntax colour |
| ≈ 12,309,125 | `XH7`/`K75` — the `❯` (`a6.pointer`, U+276F; `>` in the ASCII fallback table) + NBSP marker | panel marker, dimmed while loading |
