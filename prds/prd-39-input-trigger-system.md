---
title: "PRD-39: Input Trigger System — Generalised Dropdown Architecture"
status: draft
version: 0.1.0
created: 2026-06-12
---

# PRD-39: Input Trigger System

## Executive Summary

The `@`-mention dropdown in `mention_input.py` works but is hardcoded for one
trigger character.  PRD-36/37/38 need `/` to open a slash-command dropdown,
and future features may need `#` (issue references, tags, etc.) or other
characters.  Rather than duplicating the state machine for each new trigger,
this PRD defines the **Input Trigger System** — an abstract, registry-driven
architecture that lets any character open a typed dropdown below the input bar.

The state machine in `mention_input.py` is refactored once.  Each new trigger
is a single class that implements four methods.

---

## Goals

| ID | Goal |
|----|------|
| G1 | Any single character can be registered as a trigger that opens a dropdown |
| G2 | Each trigger is an independent handler class — zero shared state between them |
| G3 | The state machine in `mention_input.py` is fully generic; trigger-specific logic lives in handlers |
| G4 | Adding a new trigger (`#`, `!`, …) requires only writing one handler class and registering it |
| G5 | Handlers can provide an optional **hint line** shown below the dropdown (e.g. argument syntax for `/model`) |
| G6 | The `@` file-mention handler is migrated to the new system with no behaviour change |
| G7 | `TriggerRegistry` is initialised once per session and passed into `read_line_with_mention` |

## Non-Goals
- Nested triggers (a trigger opening a sub-dropdown)
- Triggers that span multiple characters (e.g. `##`)
- Mouse input handling

---

## Data Structures

### `MatchItem`

```python
# src/agenthicc/tui/trigger.py

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class MatchItem:
    """One row in the dropdown for any trigger type."""
    display: str        # shown in the dropdown left column (e.g. "+ src/auth.py", "/deploy")
    value: str          # inserted into the buffer on selection (may differ from display)
    hint: str = ""      # optional right-column or below-dropdown annotation
```

### `TriggerContext`

```python
@dataclass
class TriggerContext:
    """Read-only runtime context passed to handlers on every call."""
    cwd: Path
    history: list[str] = field(default_factory=list)
```

### `TriggerHandler` protocol

```python
@runtime_checkable
class TriggerHandler(Protocol):
    """One handler per trigger character.  All methods are pure (no I/O)."""

    #: The single character that activates this handler (e.g. "@", "/", "#").
    char: str

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        """Return dropdown rows for the current fragment.

        Called on every keystroke after the trigger character.
        *fragment* is everything the user typed AFTER the trigger char.
        Return an empty list to show the "no matches" state.
        """

    def on_select(
        self,
        item: MatchItem | None,
        fragment: str,
        buf: list[str],
    ) -> list[str]:
        """Return the new buffer after the user confirms a selection.

        *item* is None only when matches is empty and the user pressed Enter.
        Implementations typically insert ``self.char + item.value`` into *buf*.
        """

    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
        """Return the new buffer when the user presses ESC.

        Typically restores the literal trigger char + fragment so no input is lost.
        """

    def get_hint(self, item: MatchItem | None) -> str | None:
        """Optional one-line hint shown below the dropdown for the highlighted item.

        Return ``None`` to show no hint (default behaviour for most handlers).
        Example: a slash-command handler returns ``"/model [provider] [model]"``
        when ``/model`` is highlighted.
        """
        return None
```

### `TriggerRegistry`

```python
class TriggerRegistry:
    """Maps trigger characters to their handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, TriggerHandler] = {}

    def register(self, handler: TriggerHandler) -> None:
        if len(handler.char) != 1:
            raise ValueError(f"Trigger char must be exactly one character, got {handler.char!r}")
        self._handlers[handler.char] = handler

    def get(self, char: str) -> TriggerHandler | None:
        return self._handlers.get(char)

    @property
    def chars(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def __repr__(self) -> str:
        return f"TriggerRegistry(chars={sorted(self.chars)})"
```

---

## Refactored `mention_input.py` State Machine

### New state variables (replacing `in_mention: bool`)

```python
active_handler: TriggerHandler | None = None  # None = normal editing mode
fragment: str = ""                             # text typed after trigger char
matches: list[MatchItem] = []
selected: int = 0
current_hint: str | None = None               # from handler.get_hint()
```

### Key dispatch changes

```python
# ── NOT in any trigger mode ────────────────────────────────────────────────
if active_handler is None:
    if key == Key.CHAR and ch in registry.chars:
        handler = registry.get(ch)
        active_handler = handler
        fragment = ""
        matches = handler.get_matches("", ctx)
        selected = 0
        current_hint = handler.get_hint(matches[selected] if matches else None)
    else:
        # existing: BACKSPACE, CTRL_U, UP/DOWN history, ENTER, etc.
        ...

# ── IN trigger mode ────────────────────────────────────────────────────────
else:
    if key in (Key.ENTER, Key.TAB):
        item = matches[selected] if matches else None
        buf = active_handler.on_select(item, fragment, buf)
        if key == Key.TAB and buf and buf[-1] != " ":
            buf.append(" ")
        active_handler = None
        fragment = ""
        matches = []
        current_hint = None
        # Immediate redraw to close dropdown:
        prev_dropdown_lines = _redraw(
            prompt_str, buf, "", [], 0, prev_dropdown_lines, False, None
        )

    elif key == Key.ESC:
        buf = active_handler.on_cancel(fragment, buf)
        active_handler = None
        fragment = ""
        matches = []
        current_hint = None
        prev_dropdown_lines = _redraw(
            prompt_str, buf, "", [], 0, prev_dropdown_lines, False, None
        )

    elif key == Key.BACKSPACE:
        if fragment:
            fragment = fragment[:-1]
            matches = active_handler.get_matches(fragment, ctx)
            selected = 0
            current_hint = active_handler.get_hint(matches[selected] if matches else None)
        else:
            # Backspace past trigger char — cancel
            buf = active_handler.on_cancel(fragment, buf)
            buf.pop()  # remove the trigger char that on_cancel restored
            active_handler = None
            fragment = ""
            matches = []
            current_hint = None

    elif key == Key.UP:
        if matches:
            selected = (selected - 1) % min(len(matches), _MAX_VISIBLE)
            current_hint = active_handler.get_hint(matches[selected])

    elif key == Key.DOWN:
        if matches:
            selected = (selected + 1) % min(len(matches), _MAX_VISIBLE)
            current_hint = active_handler.get_hint(matches[selected])

    elif key == Key.CHAR and ch:
        fragment += ch
        matches = active_handler.get_matches(fragment, ctx)
        selected = 0
        current_hint = active_handler.get_hint(matches[selected] if matches else None)
```

### Updated `_redraw` signature

```python
def _redraw(
    prompt_str: str,
    buf: list[str],
    fragment: str,
    matches: list[MatchItem],
    selected: int,
    prev_n_lines: int,
    in_trigger: bool,            # was in_mention
    hint: str | None = None,     # NEW: shown as last line of dropdown area
) -> int:
```

When `hint` is not None, an extra line is rendered below the dropdown entries:

```
  ▶ /model          Show or switch LLM provider/model
    /models         List all available LLM providers
  ─────────────────────────────────────────────────
  ↑ /model [provider] [model]
```

### Updated `read_line_with_mention` signature

```python
def read_line_with_mention(
    prompt_str: str,
    cwd: Path,
    history: list[str],
    registry: TriggerRegistry | None = None,   # NEW
) -> str | None:
```

When `registry` is None, a default registry is created with just the
`AtMentionTrigger` registered (backward-compatible).

---

## `AtMentionTrigger` — migration of existing `@` handler

```python
# src/agenthicc/tui/triggers/at_mention.py

from agenthicc.tui.trigger import TriggerHandler, TriggerContext, MatchItem
from pathlib import Path


class AtMentionTrigger:
    """File/directory mention trigger for the '@' character."""

    char = "@"

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        # Exact same logic as the current _get_matches() in mention_input.py.
        # Returns MatchItem(display="src/auth.py", value="src/auth.py", hint="")
        ...

    def on_select(self, item, fragment, buf):
        if item is None:
            return buf + ["@"] + list(fragment)
        return buf + list("@" + item.value)

    def on_cancel(self, fragment, buf):
        return buf + ["@"] + list(fragment)

    def get_hint(self, item):
        return None   # no hint for file mentions
```

---

## File Layout

```
src/agenthicc/tui/
  trigger.py                    ← NEW: MatchItem, TriggerContext, TriggerHandler, TriggerRegistry
  mention_input.py              ← REFACTORED: uses TriggerRegistry, generic state machine
  triggers/
    __init__.py
    at_mention.py               ← MIGRATED: AtMentionTrigger (was inline in mention_input.py)
    slash_command.py            ← NEW: SlashCommandTrigger (see PRD-40)
```

---

## Session Startup Integration

In `InlineRenderer.run()` (or wherever `read_line_with_mention` is called):

```python
from agenthicc.tui.trigger import TriggerRegistry
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger

registry = TriggerRegistry()
registry.register(AtMentionTrigger())
registry.register(SlashCommandTrigger(
    command_registry=getattr(renderer, "_command_registry", None)
))

text = await _asyncio.to_thread(
    read_line_with_mention, "❯ ", _cwd, _history, registry
)
```

---

## Tests

```python
# tests/unit/test_trigger_system.py

import pytest
from pathlib import Path
from agenthicc.tui.trigger import TriggerRegistry, TriggerContext, MatchItem, TriggerHandler

pytestmark = pytest.mark.unit


class EchoTrigger:
    """Test trigger: '!' — returns fragment as a single match."""
    char = "!"

    def get_matches(self, fragment, ctx):
        if fragment:
            return [MatchItem(display=fragment, value=fragment)]
        return []

    def on_select(self, item, fragment, buf):
        return buf + list("!" + (item.value if item else ""))

    def on_cancel(self, fragment, buf):
        return buf + ["!"] + list(fragment)

    def get_hint(self, item):
        return f"echo: {item.value}" if item else None


def test_registry_register_and_get():
    reg = TriggerRegistry()
    reg.register(EchoTrigger())
    assert reg.get("!") is not None
    assert reg.get("@") is None


def test_registry_chars():
    reg = TriggerRegistry()
    reg.register(EchoTrigger())
    assert "!" in reg.chars


def test_registry_rejects_multi_char_trigger():
    class Bad:
        char = "!!"
    with pytest.raises(ValueError):
        TriggerRegistry().register(Bad())


def test_echo_trigger_matches():
    t = EchoTrigger()
    ctx = TriggerContext(cwd=Path("."))
    result = t.get_matches("hello", ctx)
    assert result[0].value == "hello"


def test_echo_trigger_on_select():
    t = EchoTrigger()
    item = MatchItem(display="world", value="world")
    buf = t.on_select(item, "world", list("say "))
    assert "".join(buf) == "say !world"


def test_echo_trigger_on_cancel_restores_literal():
    t = EchoTrigger()
    buf = t.on_cancel("part", [])
    assert "".join(buf) == "!part"


def test_echo_trigger_get_hint():
    t = EchoTrigger()
    item = MatchItem(display="x", value="x")
    assert "echo" in (t.get_hint(item) or "")
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_trigger_system.py -v

uv run agenthicc
# @ still works as before (AtMentionTrigger)
# / opens slash-command dropdown (SlashCommandTrigger, PRD-40)
# Adding a new trigger for '#' requires only:
#   class HashTrigger:
#       char = "#"
#       ...
#   registry.register(HashTrigger())
```
