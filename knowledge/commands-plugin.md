# Commands Plugin System

End users can register custom slash commands by placing `.py` files in
`./.agenthicc/commands/` (project-local) or `~/.agenthicc/commands/`
(user-global).  The feature is fully implemented (PRD-46).

---

## File locations and precedence

| Directory | Scope | Precedence |
|---|---|---|
| `~/.agenthicc/commands/*.py` | User-global | Loaded first |
| `./.agenthicc/commands/*.py` | Project-local | Loaded second — **wins** on name collision |

Files starting with `_` are skipped (private helpers).

A project-local file is imported only when it is trusted: `agenthicc trust add`
records its SHA-256 in `~/.agenthicc/trust.json`, and an untrusted or edited
file is logged and skipped (PRD-250 S-02, `plugins/trust.py`). Files under
`~/.agenthicc/commands/` are trusted by location.

---

## File format

Each `.py` file exports `COMMAND` (single) or `COMMANDS` (list):

```python
# .agenthicc/commands/deploy.py
from agenthicc.commands import Command, CommandContext

def _handler(ctx: CommandContext) -> bool:
    env = ctx.args.strip() or "staging"
    ctx.console.print(f"Deploying to {env}…")
    return True   # True = handled; do NOT forward to agent

COMMAND = Command(
    name="/deploy",
    description="Deploy application to environment",
    argument_hint="[staging|prod]",
    group="Custom",       # optional — shown as section header in /help
    aliases=("/dep",),    # optional
    handler=_handler,     # Callable[[CommandContext], bool]
    # menu_factory=...    # optional: returns an overlay widget instead
    # source_id=...       # optional — auto-derived as "command-plugin:<stem>"
)

# Multiple commands in one file:
# COMMANDS = [Command(...), Command(...)]

# Optional: pre-checked before import; file skipped if any dep is missing
# DEPENDENCIES = ["boto3>=1.0", "rich>=13.0"]
```

### `CommandContext` fields available to handlers

Handlers receive a `CommandContext` with full runtime state including:
`console`, `config`, `model_label`, `command_registry`, `skills`,
`mode_manager`, `conv_store`, `app_state`, `args` (everything after the
command name), and more.  Source: `commands/command.py`.

---

## Session startup flow

```
_build_session_context()                          tui_session.py:193–243
  → _scan_directory("~/.agenthicc/commands")      user-global
  → _scan_directory(".agenthicc/commands")        project-local
  → cmd_registry.register(each Command)           last-write-wins
  → SlashCommandTrigger(cmd_registry)             wired into / dropdown
```

Source: `tui_session.py:193–239`, `commands/plugin_loader.py:206–232`

---

## Where custom commands appear automatically

| Surface | Behaviour |
|---|---|
| `/` dropdown | All commands listed with group headers and descriptions |
| `/help` overlay | Grouped by `group` field; detail view shows aliases, argument hint, source_id |
| `/commands` | Table of all commands with name, group, source, description |

No additional registration is required — any command in the registry
automatically appears in all three surfaces.

---

## Command execution flow

```
User types /deploy prod
  → TUISession.dispatch_slash("/deploy prod")      tui_session.py:394–413
  → CommandDispatcher(registry).dispatch(text, ctx)
  → menu_factory takes precedence over handler (if both set)
  → handler(ctx) called with ctx.args = "prod"
  → returns True → message NOT forwarded to agent
  → returns False → message forwarded to agent as normal text
```

Source: `commands/dispatcher.py:46–50`

---

## Dependency checking

A plugin can declare required packages:

```python
DEPENDENCIES = ["boto3>=1.0"]
```

`DEPENDENCIES` (a literal list) or a `<stem>.requirements.txt` sidecar is read
statically by `plugins/deps.declared_dependencies()` — the file is not executed
to learn its dependencies — and checked with `missing_requirements()`. If any
dependency is missing, the file is skipped with a warning and the command is not
registered; agenthicc never installs it. A trusted file is therefore imported
exactly once.

---

## Error handling

| Situation | Outcome |
|---|---|
| Missing `COMMAND` / `COMMANDS` export | File skipped, warning logged |
| `COMMAND` is not a `Command` instance | File skipped, warning logged |
| Syntax error in file | File skipped, error logged |
| Missing `DEPENDENCIES` package | File skipped, warning logged |
| Computed (non-literal) `DEPENDENCIES` | File skipped with `PluginDependencyError` |
| Untrusted or edited project file | File skipped, warning names `agenthicc trust add PATH` |
| Duplicate command name | Last-write-wins (project overrides user) |

Source: `commands/plugin_loader.py:79–196`

---

## Comparison with skills

| | Skills | Commands |
|---|---|---|
| Location | `.agenthicc/skills/<slug>/SKILL.md` | `.agenthicc/commands/<name>.py` |
| Format | Markdown + YAML frontmatter | Python file exporting `COMMAND`/`COMMANDS` |
| Activation | Auto (keyword match) **or** `/{slug}` | `/{name}` only |
| Effect | Injects text into agent system prompt | Runs arbitrary Python handler |
| Dependency check | None | `DEPENDENCIES = [...]` pre-checked |
| Override rule | Project overrides user-global | Project overrides user-global |
