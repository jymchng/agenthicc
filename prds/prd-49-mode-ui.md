---
title: "PRD-49: Mode UI — Shift+Tab Cycling, Badge, Status Bar, Footer"
status: draft
version: 0.2.0
created: 2026-06-13
updated: 2026-06-13
depends-on: prd-47-mode-system-architecture.md
---

# PRD-49: Mode UI

## Executive Summary

The mode must be visible at all times and switchable with a single key gesture.
This PRD specifies: the SHIFT+TAB key binding in the input loop, the **permanent
mode footer line** that is always rendered below the `❯` prompt (modelled on
Claude Code's `⏵⏵ bypass permissions on (shift+tab to cycle)` footer), the mode
badge in the status bar, and a transient notification when the mode changes.

---

## Goals

| ID | Goal |
|----|------|
| G1 | Shift+Tab (`\x1b[Z`) cycles to the next mode in registration order |
| G2 | A **permanent footer line** is always rendered below the `❯` prompt showing the active mode and the Shift+Tab hint |
| G3 | The footer is re-rendered on every `_redraw()` call — it never disappears between turns |
| G4 | The status bar includes the active mode badge when not in Auto |
| G5 | Switching modes shows a transient notification inside the footer line; it clears on the next keypress |
| G6 | Mode name is included in `_print_status()` output |
| G7 | A `/mode [name]` command switches to a named mode directly |
| G8 | The `❯` prompt badge (`[PLAN] ❯`) is shown only in the prompt when not in Auto |

---

## 1. Shift+Tab Key Detection

Shift+Tab sends the terminal escape sequence `\x1b[Z` (3 bytes: ESC, `[`, `Z`).
This is parsed in `_read_key()` inside `mention_input.py`:

```python
# In _read_key(), in the escape-sequence branch, after b"D" (LEFT):

if b3 == b"Z":
    return (Key.SHIFT_TAB, "")
```

Add `SHIFT_TAB = "SHIFT_TAB"` to the `Key` enum.

---

## 2. Permanent Mode Footer Line

### Layout

The footer occupies one terminal row immediately below the `❯` prompt and is
**always** present — in every mode, including Auto, on every keypress redraw:

```
 openai/claude-sonnet-4-6  │  2 turns  │  $0.004
────────────────────────────────────────────────────────────────────────────────
❯ _
  ⏵⏵ Auto  (shift+tab to cycle)
```

When a non-Auto mode is active, the badge and a one-line description are shown:

```
❯ _
  ⏵⏵ [PLAN] Plan — read-only, agent plans only  (shift+tab to cycle)
```

After a mode switch the footer briefly shows the switch confirmation before
reverting to the standard line on the next keypress:

```
❯ _
  ✦ Switched to PLAN mode
```

### `_redraw()` changes

`_redraw` gains a `mode_line: str | None` parameter.  After writing the input
line (step 2), it unconditionally renders the mode footer as row +1 below the
cursor, then moves the cursor back up, then renders the dropdown (if open) below
the footer:

```
Input row (row 0):   ❯ some text
Footer row (row 1):    ⏵⏵ Auto  (shift+tab to cycle)
Dropdown row +2…:      ▶ + src/agenthicc/app.py
                         + src/agenthicc/__init__.py
                       … 3 more ↓
```

`_redraw` returns the total number of rows below the input line (footer + dropdown).
When there is no dropdown the return value is `1` (the footer alone).

```python
def _redraw(
    prompt_str: str,
    buf: list[str],
    fragment: str,
    matches: list[MatchItem],
    selected: int,
    prev_n_lines: int,
    in_trigger: bool,
    hint: str | None = None,
    trigger_char: str = "@",
    mode_line: str | None = None,   # ← NEW — always rendered when provided
) -> int:
    out = sys.stdout

    # Step 1 — erase old rows
    if prev_n_lines > 0:
        for _ in range(prev_n_lines):
            out.write("\n\r\x1b[2K")
        out.write(f"\x1b[{prev_n_lines}A")

    # Step 2 — redraw input line
    mention_suffix = (trigger_char + fragment) if in_trigger else ""
    out.write("\r\x1b[2K" + prompt_str + "".join(buf) + mention_suffix)

    # Step 2b — footer line (always, when mode_line is provided)
    n_base = 0
    if mode_line is not None:
        out.write(f"\n\r\x1b[2K  \x1b[2m{mode_line}\x1b[0m")
        n_base = 1

    # Step 3 — dropdown (rendered below the footer)
    if in_trigger and matches:
        ...render dropdown lines as before...
        new_n = n_base + len(dropdown_lines)
        out.write("\n" + "\n".join(dropdown_lines))
        out.write(f"\x1b[{new_n}A")
        out.write("\r" + prompt_str + "".join(buf) + mention_suffix)
        out.flush()
        return new_n

    # No dropdown — cursor back up over footer only
    if n_base:
        out.write(f"\x1b[{n_base}A")
        out.write("\r" + prompt_str + "".join(buf) + mention_suffix)
    out.flush()
    return n_base
```

### `_get_mode_line()` helper

Lives inside `read_line_with_mention` as a closure (reads `_mode_notification`):

```python
def _get_mode_line() -> str:
    # If a mode-switch just happened, show the confirmation once.
    notif = _mode_notification[0]
    if notif is not None:
        _mode_notification[0] = None
        return f"✦ Switched to {notif.name} mode"

    if mode_manager is None:
        return "⏵⏵ Auto  (shift+tab to cycle)"

    m = mode_manager.active
    if m.name == "Auto":
        return "⏵⏵ Auto  (shift+tab to cycle)"
    # Coloured badge + description
    return f"⏵⏵ {m.badge}\x1b[2m {m.name} — {m.description[:45]}  (shift+tab to cycle)"
```

`mode_manager` is the new optional parameter added to `read_line_with_mention`
(see §3 below).  The footer is passed to `_redraw` on every loop iteration:

```python
prev_dropdown_lines = _redraw(
    prompt_str, display_buf, fragment, matches, selected,
    prev_dropdown_lines, active_handler is not None, current_hint,
    active_handler.char if active_handler else "@",
    _get_mode_line(),   # ← always present
)
```

---

## 3. Mode Switch in the Input Loop

In `read_line_with_mention()`, in the normal-editing section:

```python
elif key == Key.SHIFT_TAB:
    mode_manager = getattr(_extra, "mode_manager", None)
    if mode_manager is not None:
        new_mode = mode_manager.cycle()
        _mode_notification[0] = new_mode   # set transient notification
    # Don't consume into buf; just redraw
    continue
```

`_extra` is a new optional parameter:

```python
def read_line_with_mention(
    prompt_str: str,
    cwd: Path,
    history: list[str],
    registry: TriggerRegistry | None = None,
    initial_menu: "MenuWidget | None" = None,
    resume_id: str = "",
    mode_manager: "ModeManager | None" = None,   # NEW
) -> str | None:
```

---

## 4. Mode Badge in the Prompt

`InlineRenderer.run()` constructs the `prompt_str` dynamically each iteration:

```python
def _make_prompt(mode_manager) -> str:
    if mode_manager and mode_manager.active_name != "Auto":
        badge = mode_manager.active.badge   # e.g. "\x1b[33m[PLAN]\x1b[0m"
        return f"{badge} \x1b[1;32m❯\x1b[0m "
    return "\x1b[1;32m❯\x1b[0m "   # default: just "❯ "
```

Pass `_make_prompt(_mode_manager)` instead of the fixed `"❯ "` string:

```python
# Each iteration:
prompt_str = _make_prompt(_mode_manager)
text = await _asyncio.to_thread(
    read_line_with_mention,
    prompt_str, _cwd, _history,
    _trigger_registry, _initial_menu,
    self._status.resume_id,
    _mode_manager,
)
```

Example prompts by mode:

```
[AUTO] ❯              (green [AUTO] badge)
[PLAN] ❯              (yellow [PLAN] badge)
[ASK]  ❯              (cyan [ASK] badge)
[REVIEW] ❯            (blue [REVIEW] badge)
[SAFE] ❯              (magenta [SAFE] badge)
[DEBUG] ❯             (red [DEBUG] badge)
```

In **Auto** mode the badge is omitted entirely (clean default).

---

## 5. Status Bar Update

In `InlineRenderer._print_status()`, add the mode badge when not in Auto:

```python
def _print_status(self) -> None:
    s = self._status
    mode_manager = getattr(self, "_mode_manager", None)

    # Mode badge (omitted in Auto to keep status line clean)
    mode_badge = ""
    if mode_manager and mode_manager.active_name != "Auto":
        mode_badge = mode_manager.active.badge + "  "

    sid = s.session_id or "session"
    turns = s.completed_agents
    cost = f"${s.session_cost_usd:.3f}"

    self.console.print(
        f" {mode_badge}[dim]{sid}  │  {turns} turn{'s' if turns != 1 else ''}  │  {cost}[/dim]"
    )
    ...
```

Status bar examples:

```
 openai/...  │  2 turns  │  $0.004           (Auto — no badge)
 [PLAN]  openai/...  │  2 turns  │  $0.004   (Plan mode)
 [SAFE]  openai/...  │  2 turns  │  $0.004   (Safe mode)
```

---

## 6. Transient Mode-Switch Notification (inside footer)

The notification reuses the **footer row** — it replaces the normal mode hint
text for exactly one redraw cycle.  No extra row is needed; the row count stays
constant at 1 (+ dropdown rows when open).

```
Before switch:   ⏵⏵ Auto  (shift+tab to cycle)
After Shift+Tab: ✦ Switched to PLAN mode            ← shows once
Next keypress:   ⏵⏵ [PLAN] Plan — read-only  (shift+tab to cycle)
```

Implementation: `_mode_notification` is a `list[Mode | None]` closure.
`_get_mode_line()` (§2) reads it, renders the confirmation, then clears it:

```python
_mode_notification: list[Any] = [None]

def _get_mode_line() -> str:
    notif = _mode_notification[0]
    if notif is not None:
        _mode_notification[0] = None          # clear — renders once only
        return f"✦ Switched to {notif.name} mode"
    ...  # normal footer text
```

---

## 7. `/mode` Command

Add to `BUILTIN_COMMANDS` in `commands/builtins.py`:

```python
def _cmd_mode(ctx: CommandContext) -> bool:
    mode_manager = getattr(ctx.renderer, "_mode_manager", None)
    mode_registry = getattr(ctx.renderer, "_mode_registry", None)
    if mode_manager is None:
        ctx.console.print("[dim]Mode system not available.[/dim]")
        return True

    args = ctx.args.strip()
    if not args:
        # Show current mode + all available modes
        table = Table(title="Modes", box=rich_box.SIMPLE)
        table.add_column("Mode")
        table.add_column("Label")
        table.add_column("Description")
        for m in (mode_registry or mode_manager._registry).all_modes():
            active_marker = "◀ active" if m.name == mode_manager.active_name else ""
            table.add_row(m.name, m.badge, m.description + (f"  {active_marker}" if active_marker else ""))
        ctx.console.print(table)
        ctx.console.print(f"  [dim]Use Shift+Tab to cycle, or /mode <name>[/dim]")
    else:
        new_mode = mode_manager.set(args)
        if new_mode:
            ctx.console.print(f"  {new_mode.badge} [dim]Switched to {new_mode.name} mode.[/dim]")
        else:
            ctx.console.print(f"  [red]Unknown mode: {args!r}[/red]")
            ctx.console.print(f"  [dim]Available: {', '.join(m.name for m in mode_manager._registry)}[/dim]")
    return True

Command(
    name="/mode",
    description="Show or switch operational mode  (/mode [Auto|Plan|Ask|Review|Safe|Debug])",
    argument_hint="[mode-name]",
    group="Built-in",
    handler=_cmd_mode,
)
```

---

## 8. Mode in Session Context Display

When a session is resumed (`--resume`), the mode is NOT persisted — it always
resets to Auto.  A future PRD may add mode persistence.

The mode name IS included in the session startup print:

```
 ~ agenthicc==0.1.0
 [PLAN] openai/claude-sonnet-4-6  │  0 turns  │  $0.000
```

---

## Tests

```python
# tests/unit/test_mode_ui.py  (pytestmark = pytest.mark.unit)

def test_footer_always_rendered(capsys):
    """_redraw returns 1 even with no dropdown when mode_line is provided."""
    from agenthicc.tui.mention_input import _redraw
    n = _redraw("❯ ", [], "", [], 0, 0, False, mode_line="⏵⏵ Auto  (shift+tab to cycle)")
    assert n == 1
    captured = capsys.readouterr()
    assert "Auto" in captured.out

def test_footer_counts_in_total_rows(capsys):
    """With dropdown open, return value is footer + dropdown rows."""
    from agenthicc.tui.mention_input import _redraw, MatchItem
    items = [MatchItem(display=f"file{i}.py", value=f"file{i}.py", meta="") for i in range(3)]
    n = _redraw("❯ ", [], "", items, 0, 0, True, mode_line="⏵⏵ Auto  (shift+tab to cycle)")
    assert n == 1 + 3   # 1 footer + 3 dropdown rows

def test_shift_tab_parsed_as_key():
    """\\x1b[Z is detected as Key.SHIFT_TAB."""
    import io, select
    from unittest.mock import patch
    # Feed the escape sequence byte-by-byte
    seq = [b"\x1b", b"[", b"Z"]
    it = iter(seq)
    def fake_read(fd, n): return next(it)
    def fake_select(*a, **kw): return ([1], [], [])
    from agenthicc.tui.mention_input import _read_key, Key
    with patch("agenthicc.tui.mention_input.os.read", fake_read), \
         patch("agenthicc.tui.mention_input.select.select", fake_select):
        key, ch = _read_key(42)
    assert key == Key.SHIFT_TAB

def test_mode_badge_auto_is_empty_prompt():
    from agenthicc.modes import build_default_registry, ModeManager
    from agenthicc.tui.app import _make_prompt  # will be created
    reg = build_default_registry()
    mgr = ModeManager(reg)
    assert "[AUTO]" not in _make_prompt(mgr)
    assert "❯" in _make_prompt(mgr)

def test_mode_badge_plan_in_prompt():
    from agenthicc.modes import build_default_registry, ModeManager
    reg = build_default_registry()
    mgr = ModeManager(reg)
    mgr.set("Plan")
    from agenthicc.tui.app import _make_prompt
    prompt = _make_prompt(mgr)
    assert "PLAN" in prompt

def test_cmd_mode_lists_all():
    from agenthicc.commands import Command, CommandContext
    from agenthicc.modes import build_default_registry, ModeManager
    from unittest.mock import MagicMock
    reg = build_default_registry()
    mgr = ModeManager(reg)
    renderer = MagicMock()
    renderer._mode_manager = mgr
    renderer._mode_registry = reg
    ctx = MagicMock()
    ctx.renderer = renderer
    ctx.args = ""
    ctx.console = MagicMock()
    # Should not raise
    from agenthicc.commands.builtins import _cmd_mode
    _cmd_mode(ctx)
    ctx.console.print.assert_called()

def test_cmd_mode_switches():
    from agenthicc.modes import build_default_registry, ModeManager
    from unittest.mock import MagicMock
    reg = build_default_registry()
    mgr = ModeManager(reg)
    renderer = MagicMock()
    renderer._mode_manager = mgr
    renderer._mode_registry = reg
    ctx = MagicMock()
    ctx.renderer = renderer
    ctx.args = "Plan"
    ctx.console = MagicMock()
    from agenthicc.commands.builtins import _cmd_mode
    _cmd_mode(ctx)
    assert mgr.active_name == "Plan"
```
