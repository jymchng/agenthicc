---
title: "PRD-40: Slash Command Trigger — Implementing PRD-36, PRD-37, PRD-38 via the Trigger System"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-39-input-trigger-system.md, prd-36-slash-command-dropdown.md, prd-37-slash-command-argument-hints.md, prd-38-slash-command-registry.md
---

# PRD-40: Slash Command Trigger

## Executive Summary

PRD-39 defines the generic trigger system.  This PRD defines `SlashCommandTrigger`
— the concrete handler for the `/` character — and the `CommandRegistry` it reads
from.  Together they deliver:

- **PRD-36**: `/` opens a live-filtered dropdown of all commands + skills
- **PRD-37**: the highlighted command's argument syntax shown as a hint line
- **PRD-38**: a centralised `CommandRegistry` shared by the dropdown, `/help`, and `SlashCommandHandler`

---

## Goals

| ID | Goal | From PRD |
|----|------|----------|
| G1 | Typing `/` immediately opens dropdown of all commands + skills | 36 |
| G2 | Typing `/dep` filters to `/deploy` in real time | 36 |
| G3 | ↑/↓ navigate; Enter/Tab selects and inserts `/command ` with trailing space | 36 |
| G4 | Esc closes dropdown, restores `/fragment` in the buffer | 36 |
| G5 | Each row: `  ▶ /command   Description text` | 36 |
| G6 | While a command is highlighted, its argument hint appears below the list | 37 |
| G7 | `CommandRegistry` is the single source of truth for all slash commands | 38 |
| G8 | Commands are grouped (`Built-in`, `Skills`, `Plugins`, `MCP`) in `/help` output | 38 |
| G9 | Skills and plugins register via `registry.register()` at session startup | 38 |
| G10 | `/help` and `SlashCommandHandler` both read the same `CommandRegistry` instance | 38 |

---

## `CommandSpec` update

Add `argument_hint` and `group` fields to `CommandSpec` in `input_bar.py`:

```python
@dataclass(frozen=True)
class CommandSpec:
    name: str                   # e.g. "/model"
    description: str
    aliases: tuple[str, ...] = ()
    argument_hint: str = ""     # e.g. "[provider] [model]"  (PRD-37)
    group: str = "Built-in"     # "Built-in" | "Skills" | "Plugins" | "MCP"  (PRD-38)
```

Updated `BUILTIN_COMMANDS`:

```python
BUILTIN_COMMANDS: list[CommandSpec] = [
    CommandSpec("/status",   "Show running agents and their tasks"),
    CommandSpec("/model",    "Show or switch LLM provider/model",
                argument_hint="[provider] [model]"),
    CommandSpec("/models",   "List all available LLM providers"),
    CommandSpec("/skills",   "List available skills"),
    CommandSpec("/expand",   "Expand tool output or @mention",
                argument_hint="[tool-id-or-@path]"),
    CommandSpec("/history",  "Browse the event log"),
    CommandSpec("/help",     "List available commands"),
    CommandSpec("/cancel",   "Cancel the currently running intent"),
    CommandSpec("/clear",    "Clear the transcript display"),
    CommandSpec("/mcp",      "Show MCP server status",
                argument_hint="[connect <url> [transport]]", group="MCP"),
]
```

---

## `CommandRegistry` (PRD-38)

Replaces the simple `list[CommandSpec]` currently passed to `SlashCommandCompleter`.

```python
# src/agenthicc/tui/input_bar.py  (addition)

class CommandRegistry:
    """Centralised, deduplicated registry of all slash commands.

    Last-writer-wins on duplicate names.  Aliases are stored separately and
    resolve to the canonical command via ``get()``.
    """

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}   # canonical name → spec
        self._aliases:  dict[str, str] = {}            # alias → canonical name

    # ── write ────────────────────────────────────────────────────────────────

    def register(self, spec: CommandSpec) -> None:
        self._commands[spec.name] = spec
        for alias in spec.aliases:
            self._aliases[alias] = spec.name

    def register_many(self, specs: list[CommandSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def unregister(self, name: str) -> None:
        canonical = self._aliases.pop(name, name)
        spec = self._commands.pop(canonical, None)
        if spec:
            for alias in spec.aliases:
                self._aliases.pop(alias, None)

    # ── read ─────────────────────────────────────────────────────────────────

    def get(self, name: str) -> CommandSpec | None:
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
        order = ["Built-in", "Skills", "Plugins", "MCP"]
        seen = {c.group for c in self._commands.values()}
        return [g for g in order if g in seen] + sorted(seen - set(order))

    def matches(self, partial: str) -> list[CommandSpec]:
        """Return all commands whose name or alias starts with *partial*."""
        result = []
        for cmd in self._commands.values():
            for candidate in (cmd.name,) + cmd.aliases:
                if candidate.startswith(partial):
                    result.append(cmd)
                    break
        return sorted(result, key=lambda c: c.name)

    def __len__(self) -> int:
        return len(self._commands)


def build_default_registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register_many(BUILTIN_COMMANDS)
    return reg
```

---

## `SlashCommandTrigger`

```python
# src/agenthicc/tui/triggers/slash_command.py

from agenthicc.tui.trigger import TriggerHandler, TriggerContext, MatchItem
from agenthicc.tui.input_bar import CommandRegistry, CommandSpec


class SlashCommandTrigger:
    """Slash-command dropdown trigger for the '/' character.

    Reads from a shared CommandRegistry so skills, plugins, and MCP servers
    can all contribute commands that appear in the dropdown.
    """

    char = "/"

    def __init__(self, registry: CommandRegistry | None = None) -> None:
        self._registry = registry or CommandRegistry()

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        partial = "/" + fragment
        cmds = self._registry.matches(partial)
        return [
            MatchItem(
                display=f"{cmd.name:<22} {cmd.description}",
                value=cmd.name,
                hint=self._format_hint(cmd),
            )
            for cmd in cmds
        ]

    def _format_hint(self, cmd: CommandSpec) -> str:
        if cmd.argument_hint:
            return f"  ↑ {cmd.name} {cmd.argument_hint}  —  {cmd.description}"
        return f"  ↑ {cmd.name}  —  {cmd.description}"

    def on_select(self, item: MatchItem | None, fragment: str, buf: list[str]) -> list[str]:
        if item is None:
            # No match — restore "/" + fragment as typed
            return buf + ["/"] + list(fragment)
        # Insert "/command" (TAB caller adds the trailing space)
        return buf + list(item.value)

    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
        return buf + ["/"] + list(fragment)

    def get_hint(self, item: MatchItem | None) -> str | None:
        return item.hint if item else None
```

---

## Dropdown Visual Design

```
❯ /mod
  ▶ /model          Show or switch LLM provider/model
    /models         List all available LLM providers
  ─────────────────────────────────────────────────────────────
  ↑ /model [provider] [model]  —  Show or switch LLM provider/model
```

The separator + hint line is rendered by `_redraw` when `hint is not None`.

Hint line rendering in `_redraw`:

```python
if hint:
    cols = shutil.get_terminal_size((80, 24)).columns
    separator = "─" * min(cols, 60)
    lines.append(f"\r\x1b[2K  \x1b[2m{separator}\x1b[0m")
    lines.append(f"\r\x1b[2K  \x1b[2m{hint[:cols - 4]}\x1b[0m")
```

---

## Session Startup Wiring

```python
# src/agenthicc/tui/app.py — InlineRenderer.run()

from agenthicc.tui.trigger import TriggerRegistry
from agenthicc.tui.triggers.at_mention import AtMentionTrigger
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.tui.input_bar import build_default_registry

_cmd_registry = build_default_registry()
renderer._command_registry = _cmd_registry   # used by SlashCommandHandler + /help

# Register skills
for slug, skill in getattr(renderer, "_skills", {}).items():
    from agenthicc.tui.input_bar import CommandSpec  # noqa
    _cmd_registry.register(CommandSpec(
        name=f"/{slug}",
        description=skill.description or skill.name,
        argument_hint=getattr(skill, "argument_hint", ""),
        group="Skills",
    ))

_trigger_registry = TriggerRegistry()
_trigger_registry.register(AtMentionTrigger())
_trigger_registry.register(SlashCommandTrigger(_cmd_registry))

text = await _asyncio.to_thread(
    read_line_with_mention, "❯ ", _cwd, _history, _trigger_registry
)
```

### `SlashCommandHandler` reads `CommandRegistry`

`SlashCommandHandler._help()` in `app.py` switches from the static `SLASH_HELP`
dict to `renderer._command_registry.groups()` + `commands_for_group()` so the
full grouped output appears:

```
 Built-in
 /cancel   Cancel the currently running intent
 /clear    Clear the transcript display
 ...

 Skills
 /deploy   [environment]   Deploy the application to production
 /git-summary   [format]   Summarise recent git activity
```

---

## Tests

```python
# tests/unit/test_slash_trigger.py

import pytest
from pathlib import Path
from agenthicc.tui.trigger import TriggerContext, MatchItem
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.tui.input_bar import CommandRegistry, CommandSpec

pytestmark = pytest.mark.unit

CTX = TriggerContext(cwd=Path("."))


def _reg(*specs):
    r = CommandRegistry()
    r.register_many(list(specs))
    return r


def test_get_matches_empty_fragment_returns_all():
    reg = _reg(CommandSpec("/status", "Show status"), CommandSpec("/model", "Switch model"))
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("", CTX)
    values = [m.value for m in matches]
    assert "/model" in values
    assert "/status" in values


def test_get_matches_filters_by_prefix():
    reg = _reg(CommandSpec("/deploy", "Deploy"), CommandSpec("/debug", "Debug"))
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("dep", CTX)
    assert all("/deploy" in m.value for m in matches)
    assert all("/debug" not in m.value for m in matches)


def test_on_select_inserts_command():
    t = SlashCommandTrigger(_reg(CommandSpec("/model", "Switch")))
    item = MatchItem(display="/model  Switch", value="/model")
    buf = t.on_select(item, "mod", [])
    assert "".join(buf) == "/model"


def test_on_select_none_restores_literal():
    t = SlashCommandTrigger(CommandRegistry())
    buf = t.on_select(None, "dep", [])
    assert "".join(buf) == "/dep"


def test_on_cancel_restores_slash_fragment():
    t = SlashCommandTrigger(CommandRegistry())
    buf = t.on_cancel("mod", [])
    assert "".join(buf) == "/mod"


def test_get_hint_returns_argument_hint():
    reg = _reg(CommandSpec("/model", "Switch model", argument_hint="[provider] [model]"))
    t = SlashCommandTrigger(reg)
    matches = t.get_matches("mod", CTX)
    assert matches
    hint = t.get_hint(matches[0])
    assert hint is not None
    assert "[provider]" in hint


def test_get_hint_none_when_no_item():
    t = SlashCommandTrigger(CommandRegistry())
    assert t.get_hint(None) is None


def test_command_registry_dedup_last_wins():
    reg = CommandRegistry()
    reg.register(CommandSpec("/cmd", "v1"))
    reg.register(CommandSpec("/cmd", "v2"))
    assert reg.get("/cmd").description == "v2"


def test_command_registry_groups():
    reg = CommandRegistry()
    reg.register(CommandSpec("/a", "A", group="Built-in"))
    reg.register(CommandSpec("/b", "B", group="Skills"))
    groups = reg.groups()
    assert "Built-in" in groups
    assert "Skills" in groups
    assert groups.index("Built-in") < groups.index("Skills")


def test_command_registry_unregister():
    reg = CommandRegistry()
    reg.register(CommandSpec("/foo", "Foo"))
    reg.unregister("/foo")
    assert reg.get("/foo") is None
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_slash_trigger.py -v

uv run agenthicc
# Type /           → dropdown shows all Built-in + Skills commands
# Type /mod        → narrows to /model, /models
# ↓ key            → moves selection down
# ↑ key            → hint line below dropdown shows "[provider] [model] — ..."
# Enter            → inserts "/model " into input bar
# Type /help       → grouped table: Built-in, Skills, MCP
```
