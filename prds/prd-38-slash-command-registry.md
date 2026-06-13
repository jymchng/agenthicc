---
title: "PRD-38: Slash-Command Registry — Dynamic Registration, Groups, and Aliases"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-36-slash-command-dropdown.md, prd-37-slash-command-argument-hints.md
---

# PRD-38: Slash-Command Registry

## Context

Commands are currently hard-coded in `BUILTIN_COMMANDS` and registered one by
one via `session.register_command()`.  As skills, plugins, and MCP servers
add more commands, we need a **centralised registry** that:

- Deduplicates commands by name (last registration wins)
- Groups commands by category for the `/help` display
- Supports aliases (`/ls` → `/history`)
- Allows commands to be registered and unregistered at runtime
- Provides a single source of truth for both the dropdown and `/help`

---

## Goals

| ID | Goal |
|----|------|
| G1 | `CommandRegistry` is the single source of truth for all slash commands |
| G2 | Commands are grouped by `group` field for `/help` display (`Built-in`, `Skills`, `Plugins`, `MCP`) |
| G3 | Aliases register as thin wrappers that point to the canonical command |
| G4 | `registry.get(name)` resolves aliases transparently |
| G5 | `registry.unregister(name)` removes a command and its aliases |
| G6 | `registry.commands_for_group(group)` returns commands sorted by name |
| G7 | `InputBarSession` and `SlashCommandHandler` both read from the same registry instance |
| G8 | The registry is passed through `renderer._command_registry` so all subsystems share it |

---

## Data Structures

```python
# src/agenthicc/tui/input_bar.py  (additions)

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterator

@dataclass(frozen=True)
class CommandSpec:
    name: str                       # canonical name, e.g. "/model"
    description: str
    aliases: tuple[str, ...] = ()
    argument_hint: str = ""         # e.g. "[provider] [model]"
    group: str = "Built-in"         # "Built-in" | "Skills" | "Plugins" | "MCP" | custom


class CommandRegistry:
    """Centralised mutable registry of all slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}   # canonical name → spec
        self._aliases: dict[str, str] = {}            # alias → canonical name

    # ── write ────────────────────────────────────────────────────────────────

    def register(self, spec: CommandSpec) -> None:
        """Add or replace a command. Registers all its aliases."""
        self._commands[spec.name] = spec
        for alias in spec.aliases:
            self._aliases[alias] = spec.name

    def register_many(self, specs: list[CommandSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def unregister(self, name: str) -> None:
        """Remove a command and any aliases pointing to it."""
        canonical = self._aliases.pop(name, name)
        spec = self._commands.pop(canonical, None)
        if spec:
            for alias in spec.aliases:
                self._aliases.pop(alias, None)

    # ── read ─────────────────────────────────────────────────────────────────

    def get(self, name: str) -> CommandSpec | None:
        """Resolve name or alias to a CommandSpec."""
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)

    def all_commands(self) -> list[CommandSpec]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def commands_for_group(self, group: str) -> list[CommandSpec]:
        return sorted(
            (c for c in self._commands.values() if c.group == group),
            key=lambda c: c.name,
        )

    def groups(self) -> list[str]:
        """Ordered list of groups that have at least one command."""
        order = ["Built-in", "Skills", "Plugins", "MCP"]
        seen = {c.group for c in self._commands.values()}
        ordered = [g for g in order if g in seen]
        ordered += sorted(g for g in seen if g not in order)
        return ordered

    def names(self) -> list[str]:
        return list(self._commands) + list(self._aliases)

    def __iter__(self) -> Iterator[CommandSpec]:
        return iter(self.all_commands())

    def __len__(self) -> int:
        return len(self._commands)
```

---

## Built-in Commands with Groups

```python
BUILTIN_COMMANDS: list[CommandSpec] = [
    CommandSpec("/status",   "Show running agents and their tasks",  group="Built-in"),
    CommandSpec("/model",    "Show or switch LLM provider/model",
                argument_hint="[provider] [model]",                  group="Built-in"),
    CommandSpec("/models",   "List all available LLM providers",     group="Built-in"),
    CommandSpec("/skills",   "List available skills",                group="Built-in"),
    CommandSpec("/expand",   "Expand tool output or @mention",
                argument_hint="[tool-id-or-@path]",                  group="Built-in"),
    CommandSpec("/history",  "Browse the event log (last 20 entries)", group="Built-in"),
    CommandSpec("/help",     "List available commands",              group="Built-in"),
    CommandSpec("/cancel",   "Cancel the currently running intent",  group="Built-in"),
    CommandSpec("/clear",    "Clear the transcript display",         group="Built-in"),
    CommandSpec("/mcp",      "Show MCP server status",
                argument_hint="[connect <url> [transport]]",          group="MCP"),
]

def build_default_registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register_many(BUILTIN_COMMANDS)
    return reg
```

---

## Updated `SlashCommandCompleter`

`SlashCommandCompleter` now accepts a `CommandRegistry` instead of a plain list:

```python
class SlashCommandCompleter(Completer):
    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        m = _SLASH_RE.search(text)
        if m is None:
            return
        partial = m.group(1)
        for cmd in self._registry:
            candidates = (cmd.name,) + cmd.aliases
            for candidate in candidates:
                if candidate.startswith(partial):
                    yield Completion(
                        text=candidate[len(partial):],
                        start_position=0,
                        display=candidate,
                        display_meta=cmd.description,
                    )

    def add(self, spec: CommandSpec) -> None:
        """Convenience method — delegates to the underlying registry."""
        self._registry.register(spec)
```

---

## Updated `/help` to Show Groups

`SlashCommandHandler._help()` in `app.py` now renders grouped output:

```python
def _help(self, console: Any) -> None:
    if not RICH_AVAILABLE:
        return
    registry = getattr(self._renderer, "_command_registry", None)
    if registry is None:
        # Fallback: show flat SLASH_HELP dict
        ...
        return

    for group in registry.groups():
        table = Table(title=group, box=rich_box.SIMPLE)
        table.add_column("Command", style="bold")
        table.add_column("Arguments", style="dim")
        table.add_column("Description")
        for cmd in registry.commands_for_group(group):
            table.add_row(cmd.name, cmd.argument_hint or "", cmd.description)
        console.print(table)
```

Result:
```
 Built-in
──────────────────────────────────────────────────
 /cancel                    Cancel the current intent
 /clear                     Clear the transcript display
 /expand   [tool-id-or-…]   Expand tool output or @mention
 /help                      List available commands
 …

 Skills
──────────────────────────────────────────────────
 /deploy   [environment]    Deploy the application to production
 /git-summary  [format]     Summarise recent git activity
```

---

## Wiring in `_run_tui_session()`

```python
# After renderer creation:
from agenthicc.tui.input_bar import build_default_registry  # noqa: PLC0415
_cmd_registry = build_default_registry()
renderer._command_registry = _cmd_registry

# After skills discovery:
for slug, skill in _skills.items():
    _cmd_registry.register(CommandSpec(
        name=f"/{slug}",
        description=skill.description or skill.name,
        argument_hint=getattr(skill, "argument_hint", ""),
        group="Skills",
    ))

# InputBarSession now reads from the registry:
# (InputBarSession.__init__ accepts CommandRegistry instead of list)
session = InputBarSession(
    registry=_cmd_registry,
    base_path=self._base_path,
    history_file=self._history_file,
)
```

---

## Tests

```python
# tests/unit/test_command_registry.py

import pytest
from agenthicc.tui.input_bar import CommandSpec, CommandRegistry

pytestmark = pytest.mark.unit


def test_register_and_get():
    reg = CommandRegistry()
    spec = CommandSpec("/test", "Test command")
    reg.register(spec)
    assert reg.get("/test") is spec


def test_alias_resolves_to_canonical():
    reg = CommandRegistry()
    spec = CommandSpec("/history", "View history", aliases=("/hist", "/h"))
    reg.register(spec)
    assert reg.get("/hist") is spec
    assert reg.get("/h") is spec


def test_last_registration_wins():
    reg = CommandRegistry()
    reg.register(CommandSpec("/cmd", "v1"))
    reg.register(CommandSpec("/cmd", "v2"))
    assert reg.get("/cmd").description == "v2"


def test_unregister_removes_command_and_aliases():
    reg = CommandRegistry()
    reg.register(CommandSpec("/foo", "Foo", aliases=("/f",)))
    reg.unregister("/foo")
    assert reg.get("/foo") is None
    assert reg.get("/f") is None


def test_commands_for_group():
    reg = CommandRegistry()
    reg.register(CommandSpec("/a", "A", group="Skills"))
    reg.register(CommandSpec("/b", "B", group="Built-in"))
    skills = reg.commands_for_group("Skills")
    assert len(skills) == 1
    assert skills[0].name == "/a"


def test_groups_ordering():
    reg = CommandRegistry()
    reg.register(CommandSpec("/s", "s", group="Skills"))
    reg.register(CommandSpec("/b", "b", group="Built-in"))
    reg.register(CommandSpec("/m", "m", group="MCP"))
    groups = reg.groups()
    assert groups.index("Built-in") < groups.index("Skills")
    assert groups.index("Skills") < groups.index("MCP")


def test_len_counts_canonical_only():
    reg = CommandRegistry()
    reg.register(CommandSpec("/x", "X", aliases=("/xx",)))
    assert len(reg) == 1   # one canonical command, not two
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_command_registry.py -v

uv run agenthicc
# /help → shows grouped table: Built-in, Skills, MCP
# /  → dropdown shows all commands grouped
# /git-summary [TAB] → completes and shows argument hint in toolbar
```
