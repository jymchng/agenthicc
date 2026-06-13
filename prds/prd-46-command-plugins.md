---
title: "PRD-46: Command Plugins — User-Defined Slash Commands via .agenthicc/commands/"
status: draft
version: 0.1.0
created: 2026-06-13
depends-on: prd-44-unified-command-system.md, prd-45-command-lifecycle-and-extension.md
---

# PRD-46: Command Plugins

## Executive Summary

Users need to create project-specific slash commands without writing Python
library code.  This PRD specifies a convention-over-configuration approach:
any `.py` file placed under `.agenthicc/commands/` is loaded at session startup
and its exported `COMMAND` (or `COMMANDS`) is registered in the
`UnifiedCommandRegistry`.  The command then appears in the `/` dropdown and is
fully dispatchable like any built-in command.

Mirrors the tool-plugin discovery pattern from PRD-24 (`.agenthicc/tools/`), so
the mental model is consistent.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `.agenthicc/commands/<name>.py` defines a project-scoped user command |
| G2 | `~/.agenthicc/commands/<name>.py` defines a user-global command |
| G3 | Each file must export `COMMAND: Command` or `COMMANDS: list[Command]` |
| G4 | Commands are loaded at session startup; failures are logged and skipped |
| G5 | Loaded commands appear in the `/` dropdown automatically |
| G6 | A command plugin may open a `MenuWidget` (interactive panel) or run a handler function |
| G7 | Project commands shadow user-global commands with the same name |
| G8 | `DEPENDENCIES: list[str]` in the file declares required packages (same pattern as tool plugins) |
| G9 | The trust / security model from PRD-27 applies — first-time load prompts for trust |

## Non-Goals
- Hot-reloading mid-session
- Commands that span multiple files (single-file per command for v1)
- GUI editor for command plugins

---

## Filesystem Layout

```
~/.agenthicc/
└── commands/
    ├── greet.py          # user-global "hello" command available in every project
    └── daily_standup.py  # generates a standup from git log

.agenthicc/
└── commands/
    ├── deploy.py         # project-specific deploy command
    ├── review.py         # AI-assisted code review command
    └── db_check.py       # custom database health-check command
```

---

## Command Plugin File Contract

Every command plugin file **must** export either:

- `COMMAND: Command` — a single command
- `COMMANDS: list[Command]` — multiple commands from one file

It **may** also export:

- `DEPENDENCIES: list[str]` — PEP-508 requirements checked before import (PRD-24 pattern)

### Minimal example

```python
# .agenthicc/commands/greet.py

from agenthicc.commands import Command, CommandContext

def _handle(ctx: CommandContext) -> bool:
    name = ctx.args.strip() or "World"
    ctx.console.print(f"[bold green]Hello, {name}![/bold green]")
    return True

COMMAND = Command(
    name="/greet",
    description="Say hello",
    argument_hint="[name]",
    group="Custom",
    source_id="command-plugin:greet",
    handler=_handle,
)
```

### Command that opens a menu

```python
# .agenthicc/commands/standup.py

from agenthicc.commands import Command, CommandContext

DEPENDENCIES = []   # stdlib only

def _open_standup(ctx: CommandContext):
    from agenthicc.tui.widgets.config_menu import ConfigurationMenu  # or a custom widget
    # Return a MenuWidget instance
    from my_standup_widget import StandupMenu
    return StandupMenu(ctx.config, ctx.console)

COMMAND = Command(
    name="/standup",
    description="Generate daily standup from git log",
    group="Custom",
    source_id="command-plugin:standup",
    menu_factory=_open_standup,
)
```

### Multiple commands from one file

```python
# .agenthicc/commands/deploy.py

from agenthicc.commands import Command, CommandContext

def _deploy_staging(ctx: CommandContext) -> bool:
    ctx.renderer._pending_skill = "Deploy the application to the staging environment."
    return True

def _deploy_prod(ctx: CommandContext) -> bool:
    ctx.renderer._pending_skill = "Deploy the application to production after confirming tests pass."
    return True

COMMANDS = [
    Command("/deploy-staging", "Deploy to staging", group="Custom",
            source_id="command-plugin:deploy", handler=_deploy_staging),
    Command("/deploy-prod",    "Deploy to production", group="Custom",
            source_id="command-plugin:deploy", handler=_deploy_prod),
]
```

---

## Loader Implementation

```python
# src/agenthicc/commands/plugin_loader.py

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .command import Command

log = logging.getLogger(__name__)

__all__ = ["CommandLoadResult", "CommandPluginSet", "discover_command_plugins"]


@dataclass
class CommandLoadResult:
    path: Path
    commands: list[Command] = field(default_factory=list)
    error: str | None = None
    missing_deps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing_deps


@dataclass
class CommandPluginSet:
    results: list[CommandLoadResult] = field(default_factory=list)

    @property
    def all_commands(self) -> list[Command]:
        return [cmd for r in self.results for cmd in r.commands if r.ok]

    @property
    def failed(self) -> list[CommandLoadResult]:
        return [r for r in self.results if not r.ok]


def _load_command_file(path: Path) -> CommandLoadResult:
    """Import a single command plugin file and extract COMMAND / COMMANDS."""
    module_name = f"_agenthicc_cmd_{path.stem}_{abs(hash(str(path)))}"

    # ── dependency check (same pattern as tool plugins PRD-24) ─────────────
    declared_deps: list[str] = []
    try:
        probe_spec = importlib.util.spec_from_file_location(f"{module_name}_probe", path)
        if probe_spec and probe_spec.loader:
            probe = importlib.util.module_from_spec(probe_spec)
            probe_spec.loader.exec_module(probe)  # type: ignore[union-attr]
            declared_deps = list(getattr(probe, "DEPENDENCIES", []))
    except Exception:
        pass   # syntax errors surface in the real import below

    if declared_deps:
        missing = _check_missing_deps(declared_deps)
        if missing:
            return CommandLoadResult(path=path, missing_deps=missing)

    # ── full import ─────────────────────────────────────────────────────────
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return CommandLoadResult(path=path, error="could not create module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        return CommandLoadResult(path=path, error=f"{type(exc).__name__}: {exc}")

    # ── extract exported commands ────────────────────────────────────────────
    single = getattr(module, "COMMAND", None)
    multi  = getattr(module, "COMMANDS", None)

    if single is None and multi is None:
        return CommandLoadResult(path=path, commands=[])   # no export — skip silently

    commands: list[Command] = []
    if single is not None:
        if isinstance(single, Command):
            commands.append(single)
        else:
            return CommandLoadResult(path=path, error="COMMAND must be a Command instance")
    if multi is not None:
        if not isinstance(multi, (list, tuple)):
            return CommandLoadResult(path=path, error="COMMANDS must be a list")
        for item in multi:
            if isinstance(item, Command):
                commands.append(item)
            else:
                log.warning("Command plugin %s: non-Command item in COMMANDS skipped: %r", path, item)

    # Ensure source_id is set to identify the plugin
    for cmd in commands:
        if cmd.source_id == "builtin":
            # User didn't set source_id — derive it from the file stem
            object.__setattr__(cmd, "source_id", f"command-plugin:{path.stem}")

    return CommandLoadResult(path=path, commands=commands)


def _check_missing_deps(requirements: list[str]) -> list[str]:
    import importlib.metadata  # noqa: PLC0415
    import re  # noqa: PLC0415
    missing = []
    for req in requirements:
        pkg = re.split(r"[>=<!~\[]", req)[0].strip()
        try:
            importlib.metadata.version(pkg)
        except Exception:
            missing.append(req)
    return missing


def _scan_commands_dir(root: Path) -> list[CommandLoadResult]:
    if not root.is_dir():
        return []
    results: list[CommandLoadResult] = []
    for py_file in sorted(root.glob("*.py")):   # flat scan, no recursion for commands
        if py_file.name.startswith("_"):
            continue
        result = _load_command_file(py_file)
        if result.missing_deps:
            log.warning(
                "Command plugin %s skipped — missing: %s\n"
                "  Fix: pip install %s",
                py_file, result.missing_deps, " ".join(result.missing_deps),
            )
        elif result.error:
            log.error("Command plugin %s failed to load: %s", py_file, result.error)
        elif result.commands:
            log.debug(
                "Loaded command(s) from %s: %s",
                py_file, [c.name for c in result.commands],
            )
        results.append(result)
    return results


def discover_command_plugins(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> CommandPluginSet:
    """Discover all command plugins; project-local commands shadow user-global ones."""
    user_root    = (user_dir    or Path.home() / ".agenthicc") / "commands"
    project_root = (project_dir or Path(".agenthicc"))         / "commands"

    results: list[CommandLoadResult] = []
    results.extend(_scan_commands_dir(user_root))
    results.extend(_scan_commands_dir(project_root))
    return CommandPluginSet(results=results)
```

---

## Conflict Resolution

When a project-local command has the same `name` as a user-global command,
the project-local version wins (same rule as tool plugins and skills).

The loader scans user-global first, then project-local.
`UnifiedCommandRegistry.register()` is last-write-wins, so project-local
naturally shadows user-global.

---

## Session Startup Integration

```python
# In InlineRenderer.run() — after skill registration, before _trigger_registry setup

from agenthicc.commands.plugin_loader import discover_command_plugins  # noqa: PLC0415

_cmd_plugins = discover_command_plugins(
    project_dir=Path(".agenthicc"),
    user_dir=Path.home() / ".agenthicc",
)
for cmd in _cmd_plugins.all_commands:
    _cmd_registry.register(cmd)

if _cmd_plugins.all_commands:
    from rich.console import Console as _C  # noqa: PLC0415
    names = ", ".join(c.name for c in _cmd_plugins.all_commands)
    _C().print(f"[dim]Loaded {len(_cmd_plugins.all_commands)} command plugin(s): {names}[/dim]")
```

---

## Trust / Security

Command plugin files execute arbitrary Python.  The same trust model from
PRD-27 applies: on first load (or after hash change) the user is prompted:

```
⚠  New command plugin detected:
   .agenthicc/commands/deploy.py  (512 bytes, sha256=ab12…)

   This file contains Python code that will run with your permissions.

   [T]rust once  [A]lways trust  [S]kip  [Q]uit  > _
```

The trust check is performed inside `_load_command_file()` by calling
`check_trust()` from `src/agenthicc/plugins/trust.py` before `exec_module`.
Set `[plugins] auto_trust = true` in `agenthicc.toml` to skip prompts (CI).

---

## `DEPENDENCIES` support

Exactly mirrors PRD-24 tool plugins:

```python
# .agenthicc/commands/slack_notify.py

DEPENDENCIES = ["requests>=2.31"]

import requests
from agenthicc.commands import Command, CommandContext

def _handle(ctx: CommandContext) -> bool:
    requests.post("https://hooks.slack.com/...", json={"text": ctx.args})
    ctx.console.print("[dim]Slack notification sent.[/dim]")
    return True

COMMAND = Command(
    "/slack-notify",
    "Send a Slack notification",
    group="Custom",
    argument_hint="<message>",
    handler=_handle,
)
```

If `requests` is missing, the user sees:
```
Command plugin .agenthicc/commands/slack_notify.py skipped — missing: requests>=2.31
  Fix: pip install requests>=2.31
  Or set [plugins] auto_install = true in agenthicc.toml
```

---

## `/commands` output with plugin commands

```
 Built-in
 /cancel       Cancel the currently running intent
 /config       Open configuration editor
 /help         List available commands
 ...

 Custom
 /deploy-prod      Deploy to production
 /deploy-staging   Deploy to staging
 /greet        [name]    Say hello
 /slack-notify <message>  Send a Slack notification
```

---

## Tests

```python
# tests/unit/test_command_plugins.py

import pytest
from pathlib import Path
from agenthicc.commands.plugin_loader import (
    _load_command_file, discover_command_plugins, CommandPluginSet,
)
from agenthicc.commands import Command

pytestmark = pytest.mark.unit


def test_load_single_command(tmp_path):
    f = tmp_path / "greet.py"
    f.write_text(
        "from agenthicc.commands import Command, CommandContext\n"
        "def _h(ctx): return True\n"
        "COMMAND = Command('/greet', 'Say hello', handler=_h)\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert len(result.commands) == 1
    assert result.commands[0].name == "/greet"


def test_load_multiple_commands(tmp_path):
    f = tmp_path / "deploy.py"
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMANDS = [\n"
        "    Command('/deploy-staging', 'Staging'),\n"
        "    Command('/deploy-prod', 'Production'),\n"
        "]\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert len(result.commands) == 2
    assert {c.name for c in result.commands} == {"/deploy-staging", "/deploy-prod"}


def test_load_no_export_skips_silently(tmp_path):
    f = tmp_path / "helper.py"
    f.write_text("x = 42\n")
    result = _load_command_file(f)
    assert result.ok
    assert result.commands == []


def test_load_syntax_error_captured(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def bad syntax !!!\n")
    result = _load_command_file(f)
    assert not result.ok
    assert "SyntaxError" in result.error


def test_load_invalid_command_type_captured(tmp_path):
    f = tmp_path / "bad_type.py"
    f.write_text("COMMAND = 'not a Command instance'\n")
    result = _load_command_file(f)
    assert not result.ok
    assert "must be a Command" in result.error


def test_source_id_derived_from_stem(tmp_path):
    f = tmp_path / "my_cmd.py"
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/x', 'X')\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert result.commands[0].source_id == "command-plugin:my_cmd"


def test_source_id_preserved_when_set(tmp_path):
    f = tmp_path / "explicit.py"
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/x', 'X', source_id='custom:my-source')\n"
    )
    result = _load_command_file(f)
    assert result.ok
    assert result.commands[0].source_id == "custom:my-source"


def test_discover_project_overrides_user(tmp_path):
    user_cmds = tmp_path / "user" / "commands"
    proj_cmds = tmp_path / "proj" / "commands"
    user_cmds.mkdir(parents=True)
    proj_cmds.mkdir(parents=True)

    (user_cmds / "greet.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/greet', 'User version')\n"
    )
    (proj_cmds / "greet.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/greet', 'Project version')\n"
    )

    plugin_set = discover_command_plugins(
        project_dir=tmp_path / "proj",
        user_dir=tmp_path / "user",
    )
    # all_commands deduplication is handled by UnifiedCommandRegistry (last-write-wins)
    all_names = [c.name for c in plugin_set.all_commands]
    assert all_names.count("/greet") == 2   # both loaded; registry deduplicates

    from agenthicc.commands import UnifiedCommandRegistry
    reg = UnifiedCommandRegistry()
    for cmd in plugin_set.all_commands:
        reg.register(cmd)
    assert reg.get("/greet").description == "Project version"


def test_missing_dep_reported(tmp_path):
    f = tmp_path / "needs_dep.py"
    f.write_text(
        "DEPENDENCIES = ['this-package-does-not-exist-xyz>=1.0']\n"
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/x', 'X')\n"
    )
    result = _load_command_file(f)
    assert not result.ok
    assert result.missing_deps


def test_private_files_skipped(tmp_path):
    cmds_dir = tmp_path / "commands"
    cmds_dir.mkdir()
    (cmds_dir / "_helper.py").write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/helper', 'Helper')\n"
    )
    plugin_set = discover_command_plugins(project_dir=tmp_path, user_dir=tmp_path / "user")
    assert plugin_set.all_commands == []


def test_command_appears_in_slash_dropdown(tmp_path):
    """End-to-end: a discovered command shows up in the / trigger dropdown."""
    from agenthicc.commands import build_builtin_registry
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext

    f = tmp_path / "commands" / "my_cmd.py"
    f.parent.mkdir()
    f.write_text(
        "from agenthicc.commands import Command\n"
        "COMMAND = Command('/my-custom', 'My custom command', group='Custom')\n"
    )
    plugin_set = discover_command_plugins(project_dir=tmp_path)
    reg = build_builtin_registry()
    for cmd in plugin_set.all_commands:
        reg.register(cmd)

    trigger = SlashCommandTrigger(reg)
    ctx = TriggerContext(cwd=tmp_path)
    matches = trigger.get_matches("my", ctx)
    assert any("/my-custom" in m.value for m in matches)
```

---

## Quick-Start for End Users

```bash
# Create the commands directory
mkdir -p .agenthicc/commands

# Write a command
cat > .agenthicc/commands/greet.py << 'EOF'
from agenthicc.commands import Command, CommandContext

def _handle(ctx: CommandContext) -> bool:
    name = ctx.args.strip() or "World"
    ctx.console.print(f"[bold green]Hello, {name}![/bold green]")
    return True

COMMAND = Command(
    name="/greet",
    description="Say hello to someone",
    argument_hint="[name]",
    group="Custom",
    handler=_handle,
)
EOF

# Start agenthicc — the command is auto-loaded
uv run agenthicc
# Startup: "Loaded 1 command plugin(s): /greet"
# Type /    → /greet appears in the dropdown under "Custom"
# Type /greet Alice  → "Hello, Alice!"
```

For a user-global command available in all projects:

```bash
mkdir -p ~/.agenthicc/commands
# Put the same file in ~/.agenthicc/commands/greet.py
```
