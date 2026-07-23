# User-defined slash commands

User-defined slash commands are Python plugins for the interactive TUI. They
are different from CLI subcommands such as `agenthicc workflows list`, and
different from lauren-ai tools that the model can call.

The current TUI path is:

```text
.agenthicc/commands/       project commands
~/.agenthicc/commands/     user-global commands
        │
        ▼
UnifiedCommandRegistry
        │
        ├── slash trigger picker
        └── TUISession → CommandDispatcher → handler or menu
```

## Create a working command

Create `.agenthicc/commands/greet.py`:

```python
from agenthicc.commands import Command, CommandContext


def _greet(ctx: CommandContext) -> bool:
    name = ctx.args.strip() or "there"
    ctx.console.print(f"Hello, {name}!", markup=False)
    return True


COMMANDS = [
    Command(
        name="/greet",
        description="Print a greeting.",
        group="Plugins",
        aliases=("/hello",),
        argument_hint="[name]",
        handler=_greet,
        source_id="command-plugin:greet",
    ),
]
```

Restart `uv run agenthicc`, type `/greet`, and submit it. `/hello` resolves to
the same command. The command appears in the trigger picker after typing `/`,
and `/commands` shows its name, group, source, and description.

For the normal interactive TUI, export `COMMANDS` as a list or tuple when a
module contains multiple commands. A singular `COMMAND` export is also
supported by the same command-specific loader. Both forms are discovered at
startup and by `/commands reload`; do not add a second command registry in a
plugin.

## Command specification

`Command` is the canonical slash-command object. Its useful fields are:

| Field | Purpose |
|---|---|
| `name` | Canonical slash name, including `/`, such as `/greet`. |
| `description` | Full text shown in the picker and `/commands`. |
| `group` | Display grouping; use `Plugins` for project commands. |
| `aliases` | Tuple of alternate slash names, also including `/`. |
| `argument_hint` | Picker/help hint such as `[name]` or `<path>`. |
| `handler` | Synchronous `CommandContext → bool` function. |
| `menu_factory` | Optional `CommandContext → MenuWidget` factory. |
| `source_id` | Ownership label used by `/commands` and source cleanup. |
| `completions_factory` | Stored metadata for argument completions; not currently consumed by the TUI picker. |

A command should normally set exactly one of `handler` or `menu_factory`.
When both are set, the menu factory wins for every invocation, including when
arguments are present. A handler should return `True` after it handles the
request. Return `False` only when deliberately declining dispatch; the normal
TUI route may then report the command as having no usable handler.

## What a handler can access

`CommandContext` is the ownership boundary for user handlers. It provides:

- the complete submitted `text` and the parsed `args` string;
- the Rich `console`, live config, session id, and active-agent label;
- the current command registry and mode manager;
- callbacks for pending skills, menus, replay, overlay close, and skill reload.

Handlers should use these fields rather than reaching into renderer or
workspace internals. The current TUI constructs `active_agent` as `"default"`
when dispatching slash commands, so a custom command must not assume that
field identifies a dynamically selected agent.

Commands needing workflow state, conversation memory, processor access, or
other session-owned fields cannot obtain those from `CommandContext` today.
They belong in a built-in command plus an explicit `TUISession` interception,
or require extending the context contract and its tests.

## Discovery and startup behavior

At TUI session construction, agenthicc:

1. scans `~/.agenthicc/commands/` first and `.agenthicc/commands/` second;
2. skips files whose names start with `_` and imports the remaining Python
   files;
3. extracts `COMMAND` and/or `COMMANDS`, logs import/missing-dependency
   failures, and collects valid command objects;
4. registers built-ins first, then user-global commands, then project-local
   commands; and
5. creates the dispatcher and slash trigger using that one registry.

The scanner walks command files recursively. A command module is executable
Python and is imported during startup. Keep import time side-effect free: do
not modify files, make network requests, install packages, or print secrets at
module scope. Put work in the handler or menu factory.

Project-local commands are loaded after user-global commands. Registry
registration is last-write-wins by canonical `name`, so a project command can
replace a global command or a built-in with the same name. Use `/commands` to
inspect the effective registry. Choose unique canonical names and aliases:
alias conflicts are not diagnosed, and aliases can affect name resolution.

Project-wide commands are loaded when the session is constructed.
`/commands reload` rescans both command directories and updates the existing
registry in place. Added, updated, and removed command names are reported.
`/skills reload` refreshes skill-owned dollar triggers only, not Python command
plugins. Explicit skills use `$skill-name`; slash-prefixed skill names are not
accepted.

## Picker and submission journey

The slash trigger activates when `/` is typed at the beginning of the input or
after a newline. The picker asks the registry for commands whose canonical
name or alias starts with the current fragment. It displays the command's
description and argument hint. Selecting an alias inserts the canonical name.

Typing `$` at the same line boundary opens the skill-only picker. It matches
discovered skill names and aliases and inserts the canonical `$skill-name`.
Skills never appear in the slash picker.

When the user submits text beginning with `/`, `TUISession.route()` handles it
locally. `/workflow` and `/compact` are special built-ins intercepted before
the generic dispatcher because they need session-local state. All other slash
commands go through `CommandDispatcher`:

1. split the first token into the command name and the remaining `args` string;
2. resolve the canonical name or alias in `UnifiedCommandRegistry`;
3. copy the base context with the parsed arguments;
4. invoke `menu_factory`, if present, or `handler`; and
5. return without forwarding unknown slash text to the model.

An unknown slash command is therefore a local command miss, not an agent
prompt. A registered command with no handler is also consumed locally and
reports that it has no handler.

## Dependencies and failures

The normal TUI scanner accepts a module-level `DEPENDENCIES` list or a matching
`<plugin-stem>.requirements.txt` sidecar and checks whether those packages are
installed. Missing dependencies cause the command file to be skipped with a
startup warning; the normal session path does not auto-install them. Keep
dependencies in the existing project environment.

The scanner probes a module for dependency metadata and then imports it again
to extract commands. Avoid import-time side effects for this reason. A syntax
or runtime import failure skips that file rather than preventing the entire
TUI from starting. A malformed `COMMAND` or `COMMANDS` export makes reload
fail atomically and leaves the previously active command set unchanged.

## Trust and security

`.agenthicc/commands/` and `~/.agenthicc/commands/` contain executable Python.
Review command files before launching a session. The normal slash-command
scanner does not invoke the trust helper or show a trust prompt. The separate
`agenthicc trust cli` flow protects project-local `.agenthicc/cli/` plugins;
that manifest does not automatically protect `.agenthicc/commands/`.

A command handler can still initiate writes, subprocesses, network calls, or
tool-like side effects. Keep command handlers small and route those operations
through the existing bounded services. Do not log credentials, accept
arbitrary shell text, or treat a picker entry as an authorization decision.
Validate arguments and return a handled error through `ctx.console` rather
than raising for expected malformed input.

## Testing checklist

Test both halves of the user journey:

- discovery from `.agenthicc/commands/`, `COMMAND`, and `COMMANDS`;
- user-global then project-local precedence;
- canonical names and aliases in the picker;
- submitted execution through `CommandDispatcher`;
- argument parsing and malformed-input messages;
- handler return value and menu-factory precedence;
- source id and `/commands` visibility;
- private files, syntax errors, missing dependencies, and import failures;
- side-effect permissions, path/network boundaries, and secret redaction;
- `/commands reload` add/update/remove behavior and rollback on failure.

The focused repository coverage is in
`tests/unit/test_command_plugins.py`,
`tests/unit/test_unified_commands.py`,
`tests/unit/test_slash_trigger.py`,
`tests/unit/test_command_lifecycle.py`, and
`tests/integration/test_commands_integration.py`.

Use `/create-commands <instructions>` to have the built-in authoring skill
prepare a command, then review the generated Python, run the focused tests,
and submit `/commands reload` in the active TUI session.
