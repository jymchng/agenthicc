# PRD-70 — /help Overlay and Dispatcher menu_factory with Args

## Problem

`/help` currently prints a grouped command table to the transcript scroll buffer
using `ctx.console.print()`. This has two deficiencies:

1. The output is static and not interactive — the user cannot navigate or drill
   into a specific command.
2. `/help /config` (deep-linking to a specific command) is impossible because
   the dispatcher only activates `menu_factory` when there are **no** args.

The dispatcher rule (line 45 of `dispatcher.py`):

```python
if cmd.menu_factory is not None and not args.strip():
```

forces a command with `menu_factory` to fall through to its `handler` when args
are present. A command that wants to open an overlay in both cases (with and
without args) cannot do so cleanly.

---

## Goals

- `/help` opens a scrollable, navigable overlay (LIST view → DETAIL view).
- `/help /config` opens the overlay pre-navigated to the `/config` detail view.
- `/help /con` opens the overlay with the cursor on the first matching command.
- `menu_factory` commands receive args via `ctx.args`; the factory decides what
  to render rather than the dispatcher deciding whether to fire.
- No changes to `CommandContext`, `Overlay`, `OverlayHost`, `tui_session`, or
  `unified_session`.

---

## Dispatcher change

Remove the `not args.strip()` guard. `menu_factory` always wins when set:

```python
# Before:
if cmd.menu_factory is not None and not args.strip():

# After:
if cmd.menu_factory is not None:
```

The factory receives `ctx_with_args` (which includes `ctx.args`). If a future
command wants "menu with no args, handler with args", the factory inspects
`ctx.args` and returns `None` to signal fall-through to the handler:

```python
# Possible future escape hatch (not required now):
if cmd.menu_factory is not None:
    widget = cmd.menu_factory(ctx_with_args)
    if widget is not None:
        ctx.set_pending_menu(widget)
        return True

if cmd.handler is not None:
    return cmd.handler(ctx_with_args)
```

For this PRD, `widget` is never `None` — the guard is simply removed and the
`None`-fallback pattern is reserved for future use if it becomes necessary.

---

## HelpOverlay

### Two-state internal machine

```
LIST_VIEW    — scrollable grouped command list
                 Up/Down  move cursor
                 Enter    → DETAIL_VIEW for highlighted command
                 Esc      → close overlay

DETAIL_VIEW  — full record for one command
                 Esc      → back to LIST_VIEW
```

### LIST_VIEW layout

```
  Built-in
  /cancel            Cancel the currently running intent
▶ /config            Open configuration editor
  /help              Show this help
  ...

  Skills
  /generate-password  Generate cryptographically secure passwords
  ...

  ↑↓ navigate   Enter detail   Esc close
```

Group headers are displayed but are not selectable. The cursor (`▶`) moves only
over `Command` rows. The visible window scrolls as the cursor moves.

### DETAIL_VIEW layout

```
  /config
  Open configuration editor

  Group:    Built-in
  Args:     (none)
  Aliases:  (none)
  Source:   builtin

  Esc  back to list
```

### `initial_query` routing

The overlay constructor accepts `initial_query: str`:

| `initial_query` | Behaviour |
|---|---|
| `""` (empty) | LIST_VIEW, cursor at first command |
| `"/config"` (exact name) | DETAIL_VIEW for `/config` immediately |
| `"/con"` (partial) | LIST_VIEW, cursor scrolled to first match |
| `"/xyz"` (no match) | LIST_VIEW, cursor at first command |

---

## File changes

| File | Change |
|---|---|
| `commands/dispatcher.py` | Remove `not args.strip()` guard (1 line) |
| `tui/workspace/overlays/help.py` | New — `HelpOverlay(Overlay)` |
| `commands/builtins.py` | `/help`: `handler=_cmd_help` → `menu_factory=_help_menu`; delete `_cmd_help` |

---

## Acceptance criteria

- [ ] `/help` (no args) opens overlay in LIST_VIEW.
- [ ] `/help /config` opens overlay in DETAIL_VIEW for `/config`.
- [ ] `/help /con` opens overlay in LIST_VIEW with cursor on first command
      whose name starts with `/con`.
- [ ] Up/Down navigates commands in LIST_VIEW, skipping group headers.
- [ ] Enter from LIST_VIEW opens DETAIL_VIEW for the highlighted command.
- [ ] Esc from DETAIL_VIEW returns to LIST_VIEW (cursor preserved).
- [ ] Esc from LIST_VIEW closes the overlay entirely.
- [ ] All existing tests pass.
- [ ] `menu_factory` for `/config` (no args, no `ctx.args` check) still works
      correctly after the dispatcher guard is removed.
