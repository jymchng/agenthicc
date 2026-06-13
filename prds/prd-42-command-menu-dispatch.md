---
title: "PRD-42: Command Menu Dispatch — Opening Menus in Response to Slash Commands"
status: draft
version: 0.1.0
created: 2026-06-13
depends-on: prd-41-menu-widget-system.md
---

# PRD-42: Command Menu Dispatch

## Executive Summary

PRD-41 defines the `MenuWidget` protocol and `MenuDriver`.  This PRD specifies
**how a slash command such as `/config` opens a rich interactive menu** instead
of printing a static table.  The mechanism is a `CommandMenuRegistry` that maps
command names to factory functions, wired into `SlashCommandHandler` and
`InlineRenderer.run()`.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `CommandMenuRegistry` maps `/command` names to `MenuWidget` factory callables |
| G2 | `SlashCommandHandler.handle()` checks the registry before falling through to table output |
| G3 | When a command opens a menu, `InlineRenderer.run()` enters **menu mode** for the next input cycle |
| G4 | In menu mode, `read_line_with_mention` is called with `initial_menu=` pre-populated |
| G5 | The input bar in menu mode repurposes its line buffer as an edit field |
| G6 | Pressing Esc or the save action in the menu returns to normal input mode |
| G7 | Any command that is not in `CommandMenuRegistry` falls through to the existing behaviour |

---

## `CommandMenuRegistry`

```python
# src/agenthicc/tui/menu.py  (addition)

from typing import Callable

MenuFactory = Callable[["RendererContext"], MenuWidget]


@dataclass
class RendererContext:
    """Minimal snapshot of renderer state passed to menu factories."""
    config: Any              # AgenthiccConfig live object
    console: Any             # Rich Console
    session_id: str          # for display


class CommandMenuRegistry:
    """Maps slash-command names to MenuWidget factory callables."""

    def __init__(self) -> None:
        self._factories: dict[str, MenuFactory] = {}

    def register(self, command: str, factory: MenuFactory) -> None:
        self._factories[command] = factory

    def get(self, command: str) -> MenuFactory | None:
        return self._factories.get(command)

    def commands(self) -> list[str]:
        return list(self._factories)
```

### Built-in registrations (in `app.py` startup)

```python
from agenthicc.tui.widgets.config_menu import ConfigurationMenu
from agenthicc.tui.menu import CommandMenuRegistry, RendererContext

_menu_registry = CommandMenuRegistry()
_menu_registry.register(
    "/config",
    lambda ctx: ConfigurationMenu(ctx.config, ctx.console),
)
renderer._menu_registry = _menu_registry
```

---

## `SlashCommandHandler.handle()` — menu-aware dispatch

```python
def handle(self, text: str, model: TranscriptModel, console: Any) -> bool:
    stripped = text.strip()
    first = stripped.split()[0] if stripped.split() else stripped

    # Check CommandMenuRegistry first (new: PRD-42)
    menu_registry = (
        getattr(self._renderer, "_menu_registry", None)
        if self._renderer else None
    )
    if menu_registry:
        factory = menu_registry.get(first)
        if factory:
            ctx = RendererContext(
                config=getattr(self._renderer, "_loaded_config", None),
                console=console,
                session_id=getattr(self._renderer._status, "session_id", ""),
            )
            widget = factory(ctx)
            if self._renderer is not None:
                self._renderer._pending_menu = widget
            return True   # handled — don't print table

    # ... existing dispatch (unchanged)
    if first == "/status": ...
```

---

## `InlineRenderer.run()` — menu mode

```python
async def run(self, on_input: Any) -> None:
    ...
    _pending_menu: MenuWidget | None = None

    try:
        while True:
            self._flush_new_lines()
            self._print_status()

            # Check if a command opened a menu in the previous iteration.
            _initial_menu = getattr(self, "_pending_menu", None)
            if _initial_menu is not None:
                self._pending_menu = None

            try:
                text = await _asyncio.to_thread(
                    read_line_with_mention,
                    "❯ ", _cwd, _history, _trigger_registry,
                    initial_menu=_initial_menu,   # ← NEW parameter
                )
            except KeyboardInterrupt:
                break

            if text is None:
                break
            ...
```

---

## `read_line_with_mention` — `initial_menu` parameter

```python
def read_line_with_mention(
    prompt_str: str,
    cwd: Path,
    history: list[str],
    registry: TriggerRegistry | None = None,
    initial_menu: MenuWidget | None = None,   # NEW
) -> str | None:
    ...
    driver = MenuDriver()
    if initial_menu is not None:
        driver.open(initial_menu)

    with _raw_mode(fd):
        while True:
            # Determine what the input bar shows
            edit_val = driver.widget.edit_field_value if driver.active else None
            display_buf = list(edit_val) if edit_val is not None else buf

            _redraw(prompt_str, display_buf, ...)

            if driver.active:
                # Render the menu widget below the input row
                driver.render(prompt_str, display_buf)
                key, ch = _read_key(fd)
                result = driver.handle_key(key, ch)
                if result.kind == MenuResultKind.DONE:
                    # Apply the result to buf if needed
                    _apply_menu_done(result, buf)
                elif result.kind == MenuResultKind.CANCEL:
                    pass  # menu closed, return to normal editing
                continue

            # Normal line editing (unchanged)
            key, ch = _read_key(fd)
            ...
```

---

## Visual Flow

```
Normal mode:
  ❯ _

User types /config and presses Enter:
  SlashCommandHandler detects /config → CommandMenuRegistry → ConfigurationMenu factory
  renderer._pending_menu = ConfigurationMenu(...)
  current input cycle returns text = "/config"
  on_input("/config") is NOT called — menu takes over

Next input cycle starts with initial_menu = ConfigurationMenu:
  ❯ [execution.provider]        ← input bar shows focused field value
  ──────────────────────────────────────────────────────────
   ► execution
       provider         anthropic
       model            claude-sonnet-4-6
       max_agent_turns  200
     memory
       project_memory_path  .agenthicc/memory
     security
       sandbox_mode  true
  ──────────────────────────────────────────────────────────
  ↑↓ navigate   Enter edit   Esc cancel

User presses ↓ to focus "model", presses Enter to edit:
  ❯ claude-sonnet-4-6_        ← editing the field value inline
  ── editing: execution.model ──────────────────────────────
  ...field list stays visible...

User types new value and presses Enter:
  Value updated in live config.
  Menu returns to navigation mode.

User presses Esc:
  Menu closes, input bar returns to empty normal mode.
```

---

## Renderer attribute: `_loaded_config`

`InlineRenderer` needs a reference to the live mutable config so menus can edit
it.  Add this in `_run_tui_session()`:

```python
renderer._loaded_config = cfg   # AgenthiccConfig instance
```

---

## Tests

```python
# tests/unit/test_command_menu_dispatch.py

def test_registry_register_and_get():
    reg = CommandMenuRegistry()
    factory = lambda ctx: EchoWidget()
    reg.register("/config", factory)
    assert reg.get("/config") is factory
    assert reg.get("/other") is None


def test_slash_handler_opens_menu(monkeypatch):
    from agenthicc.tui.app import SlashCommandHandler
    from agenthicc.tui.menu import CommandMenuRegistry, RendererContext
    from unittest.mock import MagicMock

    renderer = MagicMock()
    renderer._menu_registry = CommandMenuRegistry()
    renderer._menu_registry.register("/config", lambda ctx: EchoWidget())
    renderer._loaded_config = None
    renderer._status.session_id = "test"

    handler = SlashCommandHandler(renderer=renderer)
    model = MagicMock()
    console = MagicMock()

    handled = handler.handle("/config", model, console)
    assert handled is True
    assert renderer._pending_menu is not None


def test_slash_handler_falls_through_without_registry():
    from agenthicc.tui.app import SlashCommandHandler
    from unittest.mock import MagicMock

    handler = SlashCommandHandler(renderer=None)
    # /status has a built-in handler, should not raise
    handler.handle("/status", MagicMock(), MagicMock())
```
