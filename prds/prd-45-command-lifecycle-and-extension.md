---
title: "PRD-45: Command Lifecycle and Extension — Skills, Plugins, MCP, and Third-Party Commands"
status: draft
version: 0.1.0
created: 2026-06-13
depends-on: prd-44-unified-command-system.md
---

# PRD-45: Command Lifecycle and Extension

## Executive Summary

PRD-44 defines the `Command` type and `UnifiedCommandRegistry`.  This PRD covers
the **lifecycle** of commands: how they are registered at session startup, how
external sources (skills, plugins, MCP servers) contribute their own commands,
how commands can declare **sub-commands** and **argument completion**, and how
commands are unregistered when their source is removed.

---

## Goals

| ID | Goal |
|----|------|
| G1 | Skills registered at session start automatically appear as `/skill-name` commands |
| G2 | Plugin tool files may export an optional `COMMANDS: list[Command]` list |
| G3 | MCP servers may contribute commands when they connect |
| G4 | Each command source has a **namespace** (`source_id`) so its commands can be unregistered together |
| G5 | Commands can declare **sub-command completions** (e.g. `/model anthropic`, `/model openai`) |
| G6 | `CommandRegistry.commands_for_source(source_id)` returns all commands from one source |
| G7 | A `/commands` debug command lists all registered commands with their source |

---

## Source Namespacing

Every command carries a `source_id` that identifies where it came from.
This allows bulk unregistration (e.g. removing all commands from a skill that
was just disabled).

```python
# src/agenthicc/commands/command.py  (addition)

@dataclass
class Command:
    ...
    source_id: str = "builtin"   # NEW: "builtin" | "skill:<slug>" | "plugin:<stem>" | "mcp:<alias>"
```

```python
# src/agenthicc/commands/registry.py  (addition)

def commands_for_source(self, source_id: str) -> list[Command]:
    return [c for c in self._commands.values() if c.source_id == source_id]

def unregister_source(self, source_id: str) -> int:
    """Remove all commands with the given source_id.  Returns the count removed."""
    names = [c.name for c in self.commands_for_source(source_id)]
    for name in names:
        self.unregister(name)
    return len(names)
```

---

## Skills → Commands

When skills are discovered at startup, each `SkillDef` is converted into a
`Command` with `source_id="skill:<slug>"`:

```python
# In InlineRenderer.run() — replaces the current for-loop

for slug, skill in getattr(self, "_skills", {}).items():
    _cmd_registry.register(Command(
        name=f"/{slug}",
        description=skill.description or skill.name,
        group="Skills",
        argument_hint=getattr(skill, "argument_hint", ""),
        source_id=f"skill:{slug}",
        handler=_make_skill_handler(slug, skill, self),
    ))
```

```python
def _make_skill_handler(slug: str, skill: Any, renderer: Any) -> CommandHandler:
    """Return a handler that invokes the skill body via the pending-skill mechanism."""
    def _handler(ctx: CommandContext) -> bool:
        from agenthicc.skills.runner import process_skill_body  # noqa: PLC0415
        import os  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415
        args = ctx.args.split() if ctx.args.strip() else []
        session_id = getattr(getattr(renderer, "_status", None), "resume_id", "") or ""
        body = process_skill_body(
            skill, args=args, cwd=Path(os.getcwd()), session_id=session_id,
        )
        renderer._pending_skill = body
        ctx.console.print(f"  [dim]Invoking skill [bold]/{slug}[/bold][/dim]")
        return True
    return _handler
```

---

## Plugin Tool Files → Commands

Plugin files may export an optional `COMMANDS` list alongside `TOOLS`:

```python
# .agenthicc/tools/deploy_tools.py

from lauren_ai._tools import tool
from agenthicc.commands import Command, CommandContext

@tool()
async def deploy(env: str = "staging") -> dict:
    """Deploy to the given environment."""
    ...

TOOLS = [deploy]

def _handle_deploy(ctx: CommandContext) -> bool:
    env = ctx.args.strip() or "staging"
    ctx.console.print(f"[dim]Deploying to {env}…[/dim]")
    ctx.renderer._pending_skill = f"Deploy to the {env!r} environment using the deploy tool."
    return True

# Optional: contribute commands alongside tools
COMMANDS = [
    Command(
        "/deploy",
        "Deploy to an environment",
        group="Plugins",
        argument_hint="[staging|production]",
        source_id="plugin:deploy_tools",
        handler=_handle_deploy,
    )
]
```

The plugin discovery loader (`src/agenthicc/plugins/discovery.py`) reads `COMMANDS`
after `TOOLS`:

```python
# In _load_plugin_file():

plugin_commands = getattr(module, "COMMANDS", [])
if plugin_commands:
    result.commands = list(plugin_commands)
```

```python
# In discover_project_tools() caller:

for result in plugin_set.results:
    if result.ok and result.commands:
        for cmd in result.commands:
            _cmd_registry.register(cmd)
```

---

## MCP Servers → Commands

When an MCP server connects, it may advertise commands via a `COMMANDS`
capability (optional).  `McpToolRegistry.discover_all()` checks for this:

```python
# In McpToolRegistry._register_tools_from_bridge():

# Future: if the MCP server declares slash commands:
if hasattr(bridge._client, "list_commands"):
    raw_cmds = await bridge._client.list_commands()
    for raw in raw_cmds:
        cmd = Command(
            name=f"/mcp-{bridge.server_name}-{raw['name']}",
            description=raw.get("description", ""),
            group="MCP",
            source_id=f"mcp:{bridge.server_name}",
            handler=_make_mcp_command_handler(bridge, raw["name"]),
        )
        if event_processor_ref:
            # Notify registry via the event bus
            pass
```

In v1, MCP command contributions are optional and only matter when the server
explicitly declares the `commands` capability.

---

## Sub-Command Completions

Some commands accept well-known argument values that can be completed in the
dropdown's hint line.  A `Command` may declare a `completions_factory`:

```python
# src/agenthicc/commands/command.py  (addition)

CompletionsFactory = Callable[[str], list[str]]
# Called with the text AFTER the command name; returns completions for the
# argument fragment.  Used by SlashCommandTrigger to show inline suggestions.

@dataclass
class Command:
    ...
    completions_factory: CompletionsFactory | None = None
```

Example for `/model`:

```python
def _model_completions(args_fragment: str) -> list[str]:
    from agenthicc.config import SUPPORTED_PROVIDERS  # noqa: PLC0415
    return [p for p in SUPPORTED_PROVIDERS if p.startswith(args_fragment)]

Command("/model", "Switch model", argument_hint="[provider] [model]",
        completions_factory=_model_completions, handler=_cmd_model)
```

`SlashCommandTrigger` uses this when the user types `/model ant` to show
`anthropic` as a completion option below the hint line.

---

## `/commands` Debug Command

```python
Command(
    "/commands",
    "List all registered commands with their source",
    group="Built-in",
    handler=lambda ctx: _cmd_list(ctx),
)

def _cmd_list(ctx: CommandContext) -> bool:
    if not RICH_AVAILABLE:
        return True
    table = Table(title="Registered Commands", box=rich_box.SIMPLE)
    table.add_column("Command", style="bold")
    table.add_column("Group")
    table.add_column("Source", style="dim")
    table.add_column("Description")
    for cmd in ctx.renderer._cmd_registry.all_commands():
        table.add_row(cmd.name, cmd.group, cmd.source_id, cmd.description)
    ctx.console.print(table)
    return True
```

---

## Migration Path

### Phase 1 — Add `src/agenthicc/commands/` package
Create `command.py`, `registry.py`, `dispatcher.py`, `builtins.py`.
`build_builtin_registry()` returns a `UnifiedCommandRegistry` with all
existing built-in commands pre-registered.

### Phase 2 — Wire `UnifiedCommandRegistry` into session startup
Replace the current fragmented setup:
- Remove `BUILTIN_COMMANDS` from `SlashCommandTrigger` constructor
- Remove `CommandMenuRegistry` from `menu.py` (or deprecate it)
- `SlashCommandHandler.handle()` delegates to `CommandDispatcher`

### Phase 3 — Wire skills, plugins, MCP
Auto-register from each source using `source_id` namespacing.

### Phase 4 — Deprecate old types
Mark `CommandSpec`, `CommandRegistry` in `input_bar.py` as deprecated.
They remain for one release cycle then are removed.

---

## Tests

```python
# tests/unit/test_command_lifecycle.py

def test_source_namespacing():
    from agenthicc.commands import UnifiedCommandRegistry, Command
    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "a", source_id="skill:foo"))
    reg.register(Command("/b", "b", source_id="skill:foo"))
    reg.register(Command("/c", "c", source_id="builtin"))
    foo_cmds = reg.commands_for_source("skill:foo")
    assert len(foo_cmds) == 2


def test_unregister_source():
    from agenthicc.commands import UnifiedCommandRegistry, Command
    reg = UnifiedCommandRegistry()
    reg.register(Command("/a", "a", source_id="plugin:x"))
    reg.register(Command("/b", "b", source_id="plugin:x"))
    removed = reg.unregister_source("plugin:x")
    assert removed == 2
    assert reg.get("/a") is None
    assert reg.get("/b") is None


def test_plugin_commands_export(tmp_path):
    """A plugin file with COMMANDS contributes them to the registry."""
    plugin = tmp_path / "my_plugin.py"
    plugin.write_text(
        "from agenthicc.commands import Command\n"
        "TOOLS = []\n"
        "COMMANDS = [Command('/my-cmd', 'My command', source_id='plugin:my_plugin')]\n"
    )
    from agenthicc.plugins.discovery import _load_plugin_file
    result = _load_plugin_file(plugin)
    assert result.ok
    cmds = getattr(result, "commands", [])
    assert any(c.name == "/my-cmd" for c in cmds)


def test_skill_handler_sets_pending_skill():
    from agenthicc.commands.builtins import _make_skill_handler
    from unittest.mock import MagicMock, patch
    renderer = MagicMock()
    skill = MagicMock()
    skill.path = MagicMock()
    handler = _make_skill_handler("test-skill", skill, renderer)
    ctx = MagicMock()
    ctx.args = ""
    ctx.console = MagicMock()
    with patch("agenthicc.commands.builtins.process_skill_body", return_value="body"):
        handler(ctx)
    assert renderer._pending_skill == "body"


def test_config_command_sets_pending_menu():
    from agenthicc.commands import build_builtin_registry, CommandDispatcher
    from unittest.mock import MagicMock
    reg = build_builtin_registry()
    disp = CommandDispatcher(reg)
    renderer = MagicMock()
    renderer._loaded_config = MagicMock()
    ctx = MagicMock()
    ctx.renderer = renderer
    ctx.args = ""
    ctx.config = renderer._loaded_config
    disp.dispatch("/config", ctx)
    assert renderer._pending_menu is not None
```
