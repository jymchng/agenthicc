---
title: "PRD-44: Unified Command System — Single Registry, Auto-Discovery, Type-Safe Dispatch"
status: draft
version: 0.1.0
created: 2026-06-13
---

# PRD-44: Unified Command System

## Context

The current command infrastructure is split across four independent systems that
do not share data:

| System | Location | Purpose |
|---|---|---|
| `BUILTIN_COMMANDS` list | `input_bar.py` | Populates the `/` dropdown |
| `CommandRegistry` | `input_bar.py` | Deduplication + groups for `/help` |
| `CommandMenuRegistry` | `menu.py` | Maps names → `MenuWidget` factories |
| `SlashCommandHandler.handle()` | `app.py` | Hardcoded `if first == "/x"` dispatch |

**Consequence**: registering `/config` in `CommandMenuRegistry` does NOT add it
to the `/` dropdown, because the dropdown reads `CommandRegistry` (which only
knows about `BUILTIN_COMMANDS`).  Every new command must be added to multiple
places manually.

This PRD replaces all four systems with a single **`Command`** dataclass and a
unified **`CommandRegistry`** that is the single source of truth for every slash
command — its label in the dropdown, its argument hint, its group, its handler
function, and its optional menu widget factory.

---

## Goals

| ID | Goal |
|----|------|
| G1 | A single `Command` object contains everything needed: name, description, group, hint, handler, menu_factory |
| G2 | `UnifiedCommandRegistry` is the only place commands are registered |
| G3 | The `/` trigger dropdown auto-discovers from `UnifiedCommandRegistry.all_commands()` |
| G4 | `CommandDispatcher.dispatch()` replaces all `if first == "/x"` branches |
| G5 | `CommandContext` provides a type-safe bag of runtime state to handler functions |
| G6 | Built-in commands, skills, plugins, and MCP servers all register via the same API |
| G7 | Adding a new command requires exactly ONE `registry.register(Command(...))` call |
| G8 | `/config` (and any future menu-commands) appear in the dropdown automatically |

## Non-Goals
- Multi-word commands (e.g. `/git commit`) — still single-token `/command`
- Command versioning or deprecation warnings
- Permission-based command visibility

---

## `Command` Dataclass

```python
# src/agenthicc/commands/command.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.menu import MenuWidget

__all__ = ["Command", "CommandContext"]


@dataclass
class CommandContext:
    """Runtime state available to command handler functions."""
    text: str               # full text the user submitted, e.g. "/model anthropic gpt-4o"
    args: str               # everything after the command name, e.g. "anthropic gpt-4o"
    model: Any              # TranscriptModel
    console: Any            # Rich Console
    renderer: Any           # InlineRenderer
    config: Any             # AgenthiccConfig (live, mutable)
    session_id: str = ""


# A handler takes a CommandContext and returns True if it handled the command.
CommandHandler = Callable[[CommandContext], bool]

# A menu factory takes a CommandContext and returns a MenuWidget.
MenuFactory = Callable[[CommandContext], "MenuWidget"]


@dataclass
class Command:
    """Complete specification for a single slash command."""

    name: str                        # canonical name, e.g. "/config"
    description: str                 # shown in dropdown right column
    group: str = "Built-in"          # "Built-in" | "Skills" | "Plugins" | "MCP"
    aliases: tuple[str, ...] = ()    # e.g. ("/cfg",)
    argument_hint: str = ""          # e.g. "[section.key=value]"

    # Exactly one of handler / menu_factory should be set (both is also fine:
    # the menu factory takes precedence when the command is typed standalone).
    handler: CommandHandler | None = None
    menu_factory: MenuFactory | None = None

    @property
    def opens_menu(self) -> bool:
        return self.menu_factory is not None

    def display_row(self) -> tuple[str, str, str]:
        """Return (name, argument_hint, description) for the /help table."""
        return self.name, self.argument_hint, self.description
```

---

## `UnifiedCommandRegistry`

```python
# src/agenthicc/commands/registry.py

from __future__ import annotations

from typing import Iterator
from .command import Command

__all__ = ["UnifiedCommandRegistry"]


class UnifiedCommandRegistry:
    """Single source of truth for all slash commands.

    Replaces:
    - ``BUILTIN_COMMANDS`` list  (input_bar.py)
    - ``CommandRegistry``        (input_bar.py)
    - ``CommandMenuRegistry``    (menu.py)
    - The ``if first == ...`` dispatch in ``SlashCommandHandler``
    """

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}   # canonical name → Command
        self._aliases:  dict[str, str] = {}        # alias → canonical name

    # ── write ────────────────────────────────────────────────────────────────

    def register(self, cmd: Command) -> None:
        """Register (or replace) a command and its aliases."""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._aliases[alias] = cmd.name

    def register_many(self, cmds: list[Command]) -> None:
        for cmd in cmds:
            self.register(cmd)

    def unregister(self, name: str) -> None:
        canonical = self._aliases.pop(name, name)
        cmd = self._commands.pop(canonical, None)
        if cmd:
            for alias in cmd.aliases:
                self._aliases.pop(alias, None)

    # ── read ─────────────────────────────────────────────────────────────────

    def get(self, name: str) -> Command | None:
        """Resolve a name or alias to a Command."""
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)

    def all_commands(self) -> list[Command]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def commands_for_group(self, group: str) -> list[Command]:
        return sorted(
            (c for c in self._commands.values() if c.group == group),
            key=lambda c: c.name,
        )

    def groups(self) -> list[str]:
        order = ["Built-in", "Skills", "Plugins", "MCP"]
        seen = {c.group for c in self._commands.values()}
        return [g for g in order if g in seen] + sorted(seen - set(order))

    def matches(self, partial: str) -> list[Command]:
        """Return commands whose name or alias starts with *partial*."""
        result: list[Command] = []
        for cmd in self._commands.values():
            for candidate in (cmd.name,) + cmd.aliases:
                if candidate.startswith(partial):
                    result.append(cmd)
                    break
        return sorted(result, key=lambda c: c.name)

    def __iter__(self) -> Iterator[Command]:
        return iter(self.all_commands())

    def __len__(self) -> int:
        return len(self._commands)
```

---

## `CommandDispatcher`

```python
# src/agenthicc/commands/dispatcher.py

from __future__ import annotations

from .command import Command, CommandContext

__all__ = ["CommandDispatcher"]


class CommandDispatcher:
    """Executes a command given its name and context.

    Usage::

        handled = dispatcher.dispatch("/config", ctx)
    """

    def __init__(self, registry: "UnifiedCommandRegistry") -> None:
        self._registry = registry

    def dispatch(self, text: str, ctx: CommandContext) -> bool:
        """Look up and execute the command for *text*.

        Returns True if the command was handled (either via handler or by
        setting ``ctx.renderer._pending_menu``), False if unknown.
        """
        parts = text.strip().split(None, 1)
        name = parts[0] if parts else text.strip()
        args = parts[1] if len(parts) > 1 else ""

        cmd = self._registry.get(name)
        if cmd is None:
            return False

        ctx_with_args = CommandContext(
            text=text, args=args,
            model=ctx.model, console=ctx.console,
            renderer=ctx.renderer, config=ctx.config,
            session_id=ctx.session_id,
        )

        # Menu factory takes precedence when no args are given.
        if cmd.menu_factory is not None and not args.strip():
            widget = cmd.menu_factory(ctx_with_args)
            if ctx.renderer is not None:
                ctx.renderer._pending_menu = widget
            return True

        # Handler (can also be triggered alongside a menu factory when args exist)
        if cmd.handler is not None:
            return cmd.handler(ctx_with_args)

        return False
```

---

## Package Layout

```
src/agenthicc/commands/
  __init__.py           ← re-exports Command, CommandContext, UnifiedCommandRegistry,
                           CommandDispatcher, build_builtin_registry
  command.py            ← Command, CommandContext, CommandHandler, MenuFactory
  registry.py           ← UnifiedCommandRegistry
  dispatcher.py         ← CommandDispatcher
  builtins.py           ← build_builtin_registry() — all built-in commands defined here
```

---

## `builtins.py` — All Built-in Commands

```python
# src/agenthicc/commands/builtins.py

from .command import Command, CommandContext
from .registry import UnifiedCommandRegistry


def _cmd_status(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._status(ctx.model, ctx.console)
    return True


def _cmd_history(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._history(ctx.model, ctx.console)
    return True


def _cmd_model(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._model(ctx.text, ctx.console)
    return True


def _cmd_expand(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._expand(ctx.text, ctx.model, ctx.console)
    return True


def _cmd_skills(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._list_skills(ctx.console)
    return True


def _cmd_help(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._help(ctx.console)
    return True


def _menu_config(ctx: CommandContext):
    from agenthicc.tui.widgets.config_menu import ConfigurationMenu  # noqa: PLC0415
    return ConfigurationMenu(ctx.config, ctx.console)


BUILTIN_COMMANDS: list[Command] = [
    Command("/cancel",   "Cancel the currently running intent",          handler=None),
    Command("/clear",    "Clear the transcript display",                  handler=None),
    Command("/config",   "Open configuration editor",  group="Built-in",
            menu_factory=_menu_config),                                        # ← in dropdown AND opens menu
    Command("/expand",   "Expand tool output or @mention",
            argument_hint="[tool-id-or-@path]",                               handler=_cmd_expand),
    Command("/help",     "List available commands",                        handler=_cmd_help),
    Command("/history",  "Browse the event log",                          handler=_cmd_history),
    Command("/mcp",      "Show MCP server status",  group="MCP",
            argument_hint="[connect <url> [transport]]",                  handler=None),
    Command("/model",    "Show or switch LLM provider/model",
            argument_hint="[provider] [model]",                           handler=_cmd_model),
    Command("/models",   "List all available LLM providers",              handler=_cmd_model),
    Command("/skills",   "List available skills",                         handler=_cmd_skills),
    Command("/status",   "Show running agents and their tasks",           handler=_cmd_status),
]


def build_builtin_registry() -> UnifiedCommandRegistry:
    """Return a UnifiedCommandRegistry pre-loaded with all built-in commands."""
    reg = UnifiedCommandRegistry()
    reg.register_many(BUILTIN_COMMANDS)
    return reg
```

---

## Auto-Discovery at Session Startup

```python
# In InlineRenderer.run() — replaces the current fragmented setup

from agenthicc.commands import build_builtin_registry, CommandDispatcher, CommandContext

_cmd_registry = build_builtin_registry()

# Skills auto-register
for slug, skill in getattr(self, "_skills", {}).items():
    from agenthicc.commands import Command  # noqa
    _cmd_registry.register(Command(
        name=f"/{slug}",
        description=skill.description or skill.name,
        group="Skills",
        argument_hint=getattr(skill, "argument_hint", ""),
        handler=lambda ctx, s=skill: _invoke_skill(ctx, s),
    ))

self._cmd_registry = _cmd_registry
self._dispatcher = CommandDispatcher(_cmd_registry)

# SlashCommandTrigger reads from the unified registry
_trigger_registry.register(SlashCommandTrigger(_cmd_registry))
```

`SlashCommandHandler` becomes a **thin adapter** that just calls
`self._dispatcher.dispatch(text, ctx)` instead of the `if first == ...` chain:

```python
def handle(self, text, model, console) -> bool:
    ctx = CommandContext(
        text=text,
        args=" ".join(text.split()[1:]),
        model=model,
        console=console,
        renderer=self._renderer,
        config=getattr(self._renderer, "_loaded_config", None),
        session_id=getattr(getattr(self._renderer, "_status", None), "session_id", ""),
    )
    dispatcher = getattr(self._renderer, "_dispatcher", None)
    if dispatcher is not None:
        return dispatcher.dispatch(text, ctx)
    return False   # fallback: unknown
```

---

## `SlashCommandTrigger` reads `UnifiedCommandRegistry`

```python
# src/agenthicc/tui/triggers/slash_command.py

class SlashCommandTrigger:
    char = "/"

    def __init__(self, registry: UnifiedCommandRegistry) -> None:
        self._registry = registry

    def get_matches(self, fragment, ctx) -> list[MatchItem]:
        partial = "/" + fragment
        cmds = self._registry.matches(partial)
        return [
            MatchItem(
                display=f"{cmd.name:<24} {cmd.description}",
                value=cmd.name,
                hint=self._format_hint(cmd),
            )
            for cmd in cmds
        ]
    ...
```

Because `SlashCommandTrigger` now reads `UnifiedCommandRegistry`, every command
registered there — including `/config` — automatically appears in the `/` dropdown.

---

## Backward Compatibility

`input_bar.py` keeps `CommandSpec`, `BUILTIN_COMMANDS`, `CommandRegistry`, and
`SlashCommandCompleter` for any external code still importing them, but they are
**deprecated** (marked with a docstring note).  `InputBarSession` is gone; the
new `SlashCommandTrigger` + `UnifiedCommandRegistry` replaces it.

---

## Tests

```python
# tests/unit/test_unified_commands.py

def test_command_register_and_get():
    from agenthicc.commands import UnifiedCommandRegistry, Command
    reg = UnifiedCommandRegistry()
    cmd = Command("/test", "A test command", handler=lambda ctx: True)
    reg.register(cmd)
    assert reg.get("/test") is cmd

def test_command_opens_menu_flag():
    from agenthicc.commands import Command
    cmd_no_menu = Command("/x", "no menu")
    cmd_with_menu = Command("/y", "with menu", menu_factory=lambda ctx: object())
    assert not cmd_no_menu.opens_menu
    assert cmd_with_menu.opens_menu

def test_builtin_registry_includes_config():
    from agenthicc.commands import build_builtin_registry
    reg = build_builtin_registry()
    cfg_cmd = reg.get("/config")
    assert cfg_cmd is not None
    assert cfg_cmd.opens_menu

def test_config_appears_in_slash_matches():
    from agenthicc.commands import build_builtin_registry
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext
    from pathlib import Path
    reg = build_builtin_registry()
    trigger = SlashCommandTrigger(reg)
    ctx = TriggerContext(cwd=Path("."))
    matches = trigger.get_matches("con", ctx)
    assert any("/config" in m.value for m in matches)

def test_dispatcher_calls_handler():
    from agenthicc.commands import UnifiedCommandRegistry, Command, CommandDispatcher
    from unittest.mock import MagicMock
    called = []
    reg = UnifiedCommandRegistry()
    reg.register(Command("/ping", "Ping", handler=lambda ctx: called.append(ctx) or True))
    disp = CommandDispatcher(reg)
    ctx = MagicMock()
    ctx.renderer = None
    result = disp.dispatch("/ping", ctx)
    assert result is True
    assert len(called) == 1

def test_dispatcher_opens_menu():
    from agenthicc.commands import UnifiedCommandRegistry, Command, CommandDispatcher
    from unittest.mock import MagicMock
    widget = object()
    reg = UnifiedCommandRegistry()
    reg.register(Command("/cfg", "Config", menu_factory=lambda ctx: widget))
    disp = CommandDispatcher(reg)
    renderer = MagicMock()
    ctx = MagicMock()
    ctx.renderer = renderer
    ctx.args = ""
    disp.dispatch("/cfg", ctx)
    assert renderer._pending_menu is widget

def test_dispatcher_returns_false_for_unknown():
    from agenthicc.commands import UnifiedCommandRegistry, CommandDispatcher
    from unittest.mock import MagicMock
    disp = CommandDispatcher(UnifiedCommandRegistry())
    assert disp.dispatch("/unknown", MagicMock()) is False

def test_skill_auto_registers_in_dropdown():
    from agenthicc.commands import UnifiedCommandRegistry, Command
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext
    from pathlib import Path
    reg = UnifiedCommandRegistry()
    reg.register(Command("/git-summary", "Summarise git", group="Skills"))
    trigger = SlashCommandTrigger(reg)
    ctx = TriggerContext(cwd=Path("."))
    matches = trigger.get_matches("git", ctx)
    assert any("/git-summary" in m.value for m in matches)
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_unified_commands.py -v

uv run agenthicc
# Type /   → dropdown shows ALL commands including /config
# Type /con → dropdown narrows to /config
# Press Enter → ConfigurationMenu opens
# Type /hel → dropdown narrows to /help
# Press Enter → /help renders grouped command table
```
