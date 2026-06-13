---
title: "PRD-41: Menu Widget System — Abstract Architecture for Interactive Panels Below the Input Bar"
status: draft
version: 0.1.0
created: 2026-06-13
---

# PRD-41: Menu Widget System

## Executive Summary

The input bar currently hosts two kinds of interactive panels below the `❯`
prompt: the `@` file-picker and the `/` slash-command dropdown.  Both are
rendered by `_redraw()` in `mention_input.py` using the same visual pattern.

Going forward, more powerful **command-invoked menus** are needed — panels that
open in response to a completed command rather than a trigger character, that
may have multiple panes, and that put the input bar into an **edit-field mode**
where keystrokes go to the menu rather than to the normal line buffer.

Examples:
- `/config` → `ConfigurationMenu` for viewing and live-editing config values
- `/agents` → `AgentSwitcherMenu`
- `/env` → environment variable viewer

This PRD defines the **Menu Widget System**: an abstract protocol that any
interactive panel must satisfy, a `MenuDriver` that hosts the active menu and
routes input, and the split between **inline trigger menus** (opened during
typing by `@`, `/`, etc.) and **command menus** (opened after Enter by `/config`,
etc.).

---

## Goals

| ID | Goal |
|----|------|
| G1 | `MenuWidget` is a protocol any interactive panel implements |
| G2 | `MenuResult` communicates what happened after a keypress |
| G3 | `DropdownWidget` refactors the existing dropdown rendering into a `MenuWidget` |
| G4 | `MenuDriver` in `mention_input.py` delegates rendering and key-handling to the active widget |
| G5 | `InlineRenderer.run()` can open a command menu after receiving a `/config` etc. command |
| G6 | While a command menu is open, the input bar acts as an **edit field** for the focused row |
| G7 | All menus close on Escape and return to normal input mode with no side-effects |
| G8 | Menus are fully testable without a real TTY (same mock pattern as existing tests) |

## Non-Goals
- Mouse support
- Overlapping or stacked menus
- Persistent menus across multiple input cycles

---

## Concepts

### Inline Trigger Menu vs. Command Menu

| | Inline Trigger Menu | Command Menu |
|---|---|---|
| **Opens when** | Typing `@` or `/` | Submitting `/config`, `/agents` etc. |
| **Input bar during menu** | Shows `trigger + fragment` (filter) | Shows editable value of focused field |
| **Closes when** | Enter / Tab / Esc | Esc, explicit save, or timeout |
| **Returns** | Selected item → inserted into buf | Nothing / side-effects applied |
| **Example** | `@` file picker, `/` command list | `ConfigurationMenu` |

Both types share the same rendering position (below the `❯` line) and the same
key-routing mechanism.

---

## `MenuWidget` Protocol

```python
# src/agenthicc/tui/menu.py

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class MenuResultKind(str, Enum):
    CONTINUE = "CONTINUE"   # keep the menu open, no return value
    DONE     = "DONE"       # menu completed normally, value in .data
    CANCEL   = "CANCEL"     # user pressed Esc; no value


@dataclass
class MenuResult:
    kind: MenuResultKind
    data: Any = None          # set when kind == DONE

    @classmethod
    def continue_(cls) -> "MenuResult":
        return cls(kind=MenuResultKind.CONTINUE)

    @classmethod
    def done(cls, value: Any = None) -> "MenuResult":
        return cls(kind=MenuResultKind.DONE, data=value)

    @classmethod
    def cancel(cls) -> "MenuResult":
        return cls(kind=MenuResultKind.CANCEL)


@runtime_checkable
class MenuWidget(Protocol):
    """Abstract interactive panel rendered below the input bar."""

    def render(
        self,
        prompt_str: str,
        buf: list[str],
        prev_n_lines: int,
    ) -> int:
        """Erase *prev_n_lines* old rows, redraw the prompt + buf + widget.

        Returns the number of lines now visible below the input row (caller
        stores this as *prev_n_lines* for the next call).
        """

    def handle_key(self, key: Any, ch: str) -> MenuResult:
        """Process one keystroke.

        Returns a ``MenuResult`` indicating whether the menu should stay open,
        return a value, or be dismissed.
        """

    @property
    def edit_field_value(self) -> str | None:
        """Current value to show in the input bar while this menu is active.

        Return ``None`` to leave the input bar unchanged (normal filter mode).
        Override in command menus that repurpose the input bar for field editing.
        """
        return None
```

---

## `MenuDriver`

`MenuDriver` lives inside `read_line_with_mention` and replaces the current
`active_handler` + inline rendering pattern.  It owns one active `MenuWidget`
at a time.

```python
# src/agenthicc/tui/menu.py  (continued)

class MenuDriver:
    """Routes rendering and key events to the active MenuWidget."""

    def __init__(self) -> None:
        self._widget: MenuWidget | None = None
        self._prev_lines: int = 0

    @property
    def active(self) -> bool:
        return self._widget is not None

    @property
    def widget(self) -> MenuWidget | None:
        return self._widget

    def open(self, widget: MenuWidget) -> None:
        self._widget = widget
        self._prev_lines = 0

    def close(self) -> None:
        self._widget = None
        self._prev_lines = 0

    def render(self, prompt_str: str, buf: list[str]) -> None:
        if self._widget is not None:
            self._prev_lines = self._widget.render(
                prompt_str, buf, self._prev_lines
            )
        # else: caller handles normal _redraw

    def handle_key(self, key: Any, ch: str) -> MenuResult:
        if self._widget is None:
            return MenuResult.continue_()
        result = self._widget.handle_key(key, ch)
        if result.kind != MenuResultKind.CONTINUE:
            self.close()
        return result
```

---

## `DropdownWidget` — Inline Trigger Menu as a `MenuWidget`

Refactors the existing `_redraw` + trigger-handler rendering into a proper
`MenuWidget`:

```python
# src/agenthicc/tui/widgets/dropdown.py

from agenthicc.tui.menu import MenuWidget, MenuResult, MenuResultKind
from agenthicc.tui.trigger import TriggerHandler, TriggerContext, MatchItem

class DropdownWidget:
    """Inline filter-as-you-type dropdown.  Implements MenuWidget."""

    def __init__(
        self,
        handler: TriggerHandler,
        ctx: TriggerContext,
        initial_fragment: str = "",
    ) -> None:
        self._handler = handler
        self._ctx = ctx
        self._fragment = initial_fragment
        self._matches: list[MatchItem] = handler.get_matches(initial_fragment, ctx)
        self._selected = 0

    @property
    def edit_field_value(self) -> None:
        return None   # input bar shows trigger_char + fragment (existing behaviour)

    def render(self, prompt_str, buf, prev_n_lines) -> int:
        # Calls the existing _redraw logic (or re-implements it)
        ...

    def handle_key(self, key, ch) -> MenuResult:
        if key == Key.ESC:
            buf_suffix = self._handler.on_cancel(self._fragment, [])
            return MenuResult.done({"action": "cancel", "buf_suffix": buf_suffix})
        if key in (Key.ENTER, Key.TAB):
            item = self._matches[self._selected] if self._matches else None
            buf_suffix = self._handler.on_select(item, self._fragment, [])
            return MenuResult.done({
                "action": "select",
                "buf_suffix": buf_suffix,
                "add_space": key == Key.TAB,
            })
        if key == Key.UP:
            self._selected = (self._selected - 1) % max(len(self._matches), 1)
        elif key == Key.DOWN:
            self._selected = (self._selected + 1) % max(len(self._matches), 1)
        elif key == Key.BACKSPACE:
            if self._fragment:
                self._fragment = self._fragment[:-1]
                self._matches = self._handler.get_matches(self._fragment, self._ctx)
                self._selected = 0
            else:
                return MenuResult.done({"action": "backspace_past_trigger"})
        elif key == Key.CHAR and ch:
            self._fragment += ch
            self._matches = self._handler.get_matches(self._fragment, self._ctx)
            self._selected = 0
        return MenuResult.continue_()
```

This migration is **backward-compatible**: the existing `_redraw` logic continues
to work; `DropdownWidget` is an opt-in refactor path.

---

## Integration: `read_line_with_mention` with `MenuDriver`

Replace `active_handler: TriggerHandler | None` with `driver = MenuDriver()`:

```python
driver = MenuDriver()

while True:
    if driver.active and driver.widget.edit_field_value is not None:
        # Command menu: input bar shows the focused field value
        display_buf = list(driver.widget.edit_field_value)
    else:
        display_buf = buf

    if driver.active:
        driver.render(prompt_str, display_buf)
        # _redraw handles prompt + buf
        _redraw(prompt_str, display_buf, ...)
    else:
        _redraw(prompt_str, buf, ...)

    key, ch = _read_key(fd)

    if driver.active:
        result = driver.handle_key(key, ch)
        if result.kind == MenuResultKind.DONE:
            _apply_menu_result(result, buf, ...)
        continue

    # ... normal line editing
```

## Integration: `InlineRenderer.run()` — opening command menus

After the text is submitted and BEFORE passing to `on_input()`, check for
commands that open menus:

```python
# In InlineRenderer.run():
from agenthicc.tui.widgets.config_menu import ConfigurationMenu

MENU_COMMANDS = {"/config": lambda renderer: ConfigurationMenu(renderer._loaded_config)}

if text.strip() in MENU_COMMANDS:
    menu_widget = MENU_COMMANDS[text.strip()](self)
    # signal mention_input to open this menu instead of submitting
    self._pending_menu = menu_widget
    continue
```

`read_line_with_mention` gains an optional `initial_menu: MenuWidget | None`
parameter so `InlineRenderer` can pre-open a command menu before the next input
cycle begins.

---

## File Layout

```
src/agenthicc/tui/
  menu.py                       ← NEW: MenuWidget, MenuResult, MenuDriver
  widgets/
    __init__.py
    dropdown.py                 ← NEW: DropdownWidget (refactored from _redraw)
    config_menu.py              ← NEW: ConfigurationMenu (PRD-43)
```

---

## Tests

```python
# tests/unit/test_menu_system.py

class EchoWidget:
    """Minimal MenuWidget for testing."""
    def render(self, prompt_str, buf, prev): return 1
    def handle_key(self, key, ch):
        if key == Key.ENTER: return MenuResult.done("entered")
        if key == Key.ESC:   return MenuResult.cancel()
        return MenuResult.continue_()
    edit_field_value = None

def test_menu_driver_open_and_close():
    d = MenuDriver()
    assert not d.active
    d.open(EchoWidget())
    assert d.active
    d.close()
    assert not d.active

def test_menu_driver_routes_key_to_widget():
    d = MenuDriver()
    d.open(EchoWidget())
    result = d.handle_key(Key.ENTER, "")
    assert result.kind == MenuResultKind.DONE
    assert result.data == "entered"
    assert not d.active   # auto-closed after DONE

def test_menu_driver_cancel_closes():
    d = MenuDriver()
    d.open(EchoWidget())
    result = d.handle_key(Key.ESC, "")
    assert result.kind == MenuResultKind.CANCEL
    assert not d.active

def test_menu_result_factories():
    assert MenuResult.continue_().kind == MenuResultKind.CONTINUE
    assert MenuResult.done(42).data == 42
    assert MenuResult.cancel().kind == MenuResultKind.CANCEL
```
