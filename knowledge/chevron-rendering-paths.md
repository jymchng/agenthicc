# Chevron `❯` Rendering Paths

The `❯` prompt character appears in four distinct contexts, each with its own
rendering path and colour mechanism.  All four must be updated together when
the colour changes.

| # | File | Context | Colour mechanism |
|---|---|---|---|
| 1 | `tui/input/renderer.py` | **Normal input bar** — raw CBREAK loop, Live block not involved. `render_input_line()` writes directly to stdout. | Raw ANSI: `\x1b[1;33m` (bold yellow) |
| 2 | `tui/workspace/components.py` | **Live block composer** — the `❯` shown in `ComposerComponent` when no overlay is active. Rendered inside the Rich Live Group. | Rich style: `"bold yellow"` |
| 3 | *(removed in TUX-07)* | **Overlay prompt input** — `PromptOverlay` was deleted. Every dialog's text field (approval "tell the agent", plan feedback, question "Other") is an `EmbeddedComposer`, which renders path 2's `ComposerComponent`. | — |
| 4 | `tui/workspace/appender.py` | **Scroll buffer echo** — the submitted user message printed to the scroll buffer via `ScrollBufferAppender` when a `user_message` event arrives. | Rich markup: `[bold yellow]` |

## Why four paths?

- **Path 1** must use raw ANSI because the Rich Live block is not running during
  the CBREAK input loop; `console.print()` is unsafe there.
- **Path 2** is inside the Live Group rendered by Rich, so Rich styles work.
- **Path 3** no longer exists: dialogs embed the composer of path 2.
- **Path 4** uses `console.print()` to write to the scroll buffer *above* the
  Live block; Rich markup is safe here.

## Invariant

All four paths must stay in sync.  When changing the chevron colour, grep for:

```
bold green.*❯
1;32m
PROMPT_CHAR
❯
```

across `appender.py`, `renderer.py`, `components.py`, and `prompt.py`.
