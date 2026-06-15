# PRD-79 — CLI Revamp: Decorator-Based Subcommands, CLIContext, Configuration Wiring, and User-Defined Commands

## Background

The current CLI has three structural problems:

1. **Flat `if/elif` dispatch.** `main()` dispatches on `args.command` with a
   chain of `if/elif`. Adding a subcommand requires touching `parser.py`,
   `__main__.py`, and a handler file with no single registry.

2. **`cli_overrides` is the only CLI→config bridge — stringly-typed.**
   `--set execution.model=gpt-4o` works, but boolean flags like
   `--dangerously-skip-permissions` have no typed home and cannot flow through
   `_apply_cli_overrides()` which only handles `"section.key=value"` strings.

3. **CLI flags don't reach `AppState`.** `cli_overrides` is consumed by
   `load_config()` and disappears. The reactive TUI state (`AppState`) knows
   nothing about CLI flags. Flags that affect session runtime behaviour (e.g.
   disabling the approval gate) have no path to the components that need them.

---

## Goals

- A single decorator `@command(*path)` registers a handler for any depth of
  subcommand nesting (e.g. `plugin trust add` = three levels).
- Handler parameters are inferred from type annotations — positional args,
  `--flags`, and `--options` are auto-generated from the function signature.
- `CLIContext` is injected by annotation type (`ann is CLIContext`), not by
  parameter name — the developer can name it anything.
- `CLIFlags` carries typed ephemeral behaviour flags (security-bypassing flags
  that must NOT be settable in TOML).
- `AgenthiccConfig` gains a `[behaviour]` section for TOML-settable convenience
  preferences (non-security).
- `AppState` gains `cli_flags: CLIFlags` — frozen, set once at startup, read by
  runtime components (`ApprovalGate`, etc.).
- The complete precedence chain is explicit and unambiguous.

---

## Fix 1 — CLIContext injection by type annotation only

The signature inspector must detect `CLIContext` parameters by annotation type,
not by parameter name. Any name is valid.

```python
# _add_params() — skip parameters annotated CLIContext regardless of name
hints = typing.get_type_hints(fn)
for name, param in inspect.signature(fn).parameters.items():
    ann = hints.get(name, inspect.Parameter.empty)
    if ann is CLIContext:
        continue                    # injected at call time; never an argparse arg

# _call() — inject CLIContext regardless of parameter name
for name, _ in inspect.signature(entry.handler).parameters.items():
    ann = hints.get(name, inspect.Parameter.empty)
    if ann is CLIContext:
        kwargs[name] = ctx          # name is irrelevant; type is the contract
    else:
        attr = name.replace("-", "_")
        if hasattr(ns, attr):
            kwargs[name] = getattr(ns, attr)
```

This means `session`, `c`, `app`, `my_context: CLIContext` all work identically.

---

## Fix 2 — Decorator-based command registry

### The three decorators

```python
# cli/registry.py

@dataclass
class _Entry:
    path:     tuple[str, ...]
    help:     str
    handler:  Callable
    is_async: bool

_REGISTRY: dict[tuple[str, ...], _Entry] = {}
_GROUPS:   dict[tuple[str, ...], str]    = {}   # branch nodes with no handler


def command(*path: str, help: str = ""):
    """Register a leaf command handler.

    Signature rules (inferred at build time):
      param: str            (no default)  → positional argument
      flag:  bool = False                 → --flag  (store_true)
      opt:   str  = "value"               → --opt VALUE
      any:   CLIContext                   → injected, never an argparse arg
    """
    def decorator(fn: Callable) -> Callable:
        doc = help or (inspect.getdoc(fn) or "").splitlines()[0]
        _REGISTRY[path] = _Entry(
            path=path, help=doc,
            handler=fn, is_async=asyncio.iscoroutinefunction(fn),
        )
        return fn
    return decorator


def group(*path: str, help: str = ""):
    """Declare a command group that has no handler of its own."""
    _GROUPS[path] = help
    def decorator(fn: Callable | None = None) -> Callable | None:
        return fn
    return decorator
```

### Signature-driven argparse generation

```python
def _add_params(parser: argparse.ArgumentParser, fn: Callable) -> None:
    from agenthicc.cli.context import CLIContext                # noqa: PLC0415
    hints = typing.get_type_hints(fn)
    sig   = inspect.signature(fn)
    for name, param in sig.parameters.items():
        ann     = hints.get(name, inspect.Parameter.empty)
        default = param.default
        empty   = inspect.Parameter.empty
        if ann is CLIContext:
            continue                                             # injected — skip
        if ann is bool:
            parser.add_argument(
                f"--{name.replace('_', '-')}",
                action="store_true",
                default=default if default is not empty else False,
            )
        elif default is empty:
            parser.add_argument(name, metavar=name.upper())     # positional
        else:
            parser.add_argument(
                f"--{name.replace('_', '-')}",
                default=default,
                type=ann if ann in (int, float) else str,
            )
```

### Recursive tree builder (unlimited depth)

```python
def _as_tree() -> dict:
    """Build a nested dict from the flat registry."""
    tree: dict = {}
    for path, help_text in _GROUPS.items():
        node = tree
        for part in path:
            node = node.setdefault(part, {"help": "", "entry": None, "children": {}})
            if part == path[-1]:
                node["help"] = help_text
            node = node["children"]
    for path, entry in _REGISTRY.items():
        node = tree
        for part in path[:-1]:
            node = node.setdefault(part, {"help": "", "entry": None, "children": {}})
            node = node["children"]
        slot = node.setdefault(path[-1], {"help": entry.help, "entry": None, "children": {}})
        slot["entry"] = entry
        if not slot["help"]:
            slot["help"] = entry.help
    return tree


def _wire(parser: argparse.ArgumentParser, tree: dict) -> None:
    """Recursively wire the tree into argparse subparsers."""
    if not tree:
        return
    subs = parser.add_subparsers(metavar="<subcommand>")
    for name, node in tree.items():
        p = subs.add_parser(name, help=node["help"])
        if (entry := node["entry"]) is not None:
            _add_params(p, entry.handler)
            p.set_defaults(_entry=entry)
        _wire(p, node["children"])              # ← recurse, unlimited depth
```

### Handler invocation with typed kwargs

```python
def _call(entry: _Entry, ctx: "CLIContext", ns: argparse.Namespace) -> Any:
    from agenthicc.cli.context import CLIContext                # noqa: PLC0415
    hints  = typing.get_type_hints(entry.handler)
    kwargs = {}
    for name, _ in inspect.signature(entry.handler).parameters.items():
        ann = hints.get(name, inspect.Parameter.empty)
        if ann is CLIContext:
            kwargs[name] = ctx
        else:
            attr = name.replace("-", "_")
            if hasattr(ns, attr):
                kwargs[name] = getattr(ns, attr)
    return asyncio.run(entry.handler(**kwargs)) if entry.is_async else entry.handler(**kwargs)
```

### Auto-discovery of command modules

```python
def _discover() -> None:
    """Import every cli/commands/*.py to trigger @command registrations."""
    import importlib, pkgutil
    from agenthicc.cli import commands as pkg                   # noqa: PLC0415
    for _, name, _ in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"agenthicc.cli.commands.{name}")
```

### Example command file — any depth, any file

```python
# cli/commands/sessions.py

@group("sessions", help="Manage saved sessions")
def _(): ...

@command("sessions", "list")
def sessions_list(session: CLIContext) -> None:
    """List all sessions for the current directory."""
    ...

@command("sessions", "show")
def sessions_show(app: CLIContext, session_id: str) -> None:
    """Show detail for one session."""
    ...

@command("sessions", "delete")
async def sessions_delete(c: CLIContext, session_id: str, force: bool = False) -> None:
    """Delete a session."""
    ...
```

```python
# cli/commands/plugin.py

@group("plugin",         help="Manage plugins")
@group("plugin", "trust", help="Manage the plugin trust list")
def _(): ...

@command("plugin", "trust", "add")
def plugin_trust_add(ctx: CLIContext, name: str) -> None:
    """Add a plugin to the trust list."""
    ...

@command("plugin", "trust", "remove")
def plugin_trust_remove(ctx: CLIContext, name: str) -> None:
    """Remove a plugin from the trust list."""
    ...
```

`agenthicc plugin trust add my-plugin` works at three levels deep.
Adding a new subcommand at any depth is one `@command(...)` decorator entry.

### `parse_cli()` and `main()` — depth-independent

```python
def parse_cli() -> tuple["CLIContext", argparse.Namespace]:
    _discover()
    parser = argparse.ArgumentParser(prog="agenthicc")
    _add_global_flags(parser)
    subs = parser.add_subparsers(metavar="<command>")
    for name, node in _as_tree().items():
        p = subs.add_parser(name, help=node["help"])
        if node["entry"]:
            _add_params(p, node["entry"].handler)
            p.set_defaults(_entry=node["entry"])
        _wire(p, node["children"])
    ns  = parser.parse_args()
    ctx = _build_ctx(ns)
    return ctx, ns


def main() -> None:
    ctx, ns = parse_cli()
    if entry := getattr(ns, "_entry", None):
        _call(entry, ctx, ns)
        return
    asyncio.run(_run_headless(ctx)) if ctx.headless else _run_tui(ctx)
```

---

## Fix 3 — Configuration wiring

### Two separate things: `AgenthiccConfig` vs `CLIFlags`

```
CLI flag                        Destination
────────────────────────────────────────────────────────────────
--set execution.model=gpt-4o   → AgenthiccConfig (via TOML merge)
--dangerously-skip-permissions → CLIFlags (typed, frozen, AppState)
```

`--set` flags are configuration values that follow the TOML precedence chain.
`--dangerously-skip-permissions` and similar security-bypassing flags are
**intentionally NOT storable in TOML** — the user must type them explicitly on
every invocation. No silent persistence.

### `BehaviourSettings` — TOML-settable developer convenience

```python
# config.py

@dataclass
class BehaviourSettings:
    """[behaviour] section — non-security developer convenience defaults.

    These MAY live in TOML.  Security-bypassing flags must NOT live here —
    they belong in CLIFlags so they can never be silently persisted.
    """
    verbose:       bool = False   # extra diagnostic output
    confirm_exits: bool = True    # confirm before Ctrl+C exit

@dataclass
class AgenthiccConfig:
    execution: ExecutionSettings  = field(default_factory=ExecutionSettings)
    behaviour: BehaviourSettings  = field(default_factory=BehaviourSettings)  # NEW
    hooks:     dict[str, list[str]] = field(default_factory=dict)
    tools:     ToolSettings       = field(default_factory=ToolSettings)
    memory:    MemorySettings     = field(default_factory=MemorySettings)
    security:  SecuritySettings   = field(default_factory=SecuritySettings)
    api:       ApiSettings        = field(default_factory=ApiSettings)
    plugins:   PluginSettings     = field(default_factory=PluginSettings)
    agents:    AgentsSettings     = field(default_factory=AgentsSettings)
    storage:   StorageSettings    = field(default_factory=StorageSettings)
```

### `CLIFlags` — ephemeral, typed, AppState-scoped

```python
# cli/context.py

@dataclass(frozen=True)
class CLIFlags:
    """Typed boolean flags injected at session startup.

    Intentionally NOT settable in TOML:
    - Security-bypassing flags must be typed explicitly every invocation.
    - They are set once (frozen) and never change during the session.
    - Runtime components read them from AppState.cli_flags.
    """
    dangerously_skip_permissions: bool = False
    # future: dry_run, offline, no_telemetry …

@dataclass(frozen=True)
class CLIContext:
    resume_id:     str | None       = None
    headless:      bool             = False
    config_path:   str | None       = None
    set_overrides: tuple[str, ...]  = ()
    flags:         CLIFlags         = field(default_factory=CLIFlags)
    subcommand:    str | None       = None
    subcommand_args: dict           = field(default_factory=dict)
```

### Complete precedence chain

```
─────────────────────────────────────────────────────────────────────────
Layer                     Destination              Settable in TOML?
─────────────────────────────────────────────────────────────────────────
1. Hardcoded defaults     AgenthiccConfig          N/A
2. ~/.agenthicc/*.toml    AgenthiccConfig          Yes
3. .agenthicc/*.toml      AgenthiccConfig          Yes
4. AGENTHICC_* env vars   AgenthiccConfig          No (env only)
5. --set k=v              AgenthiccConfig          No (CLI only)
─────────────────────────────────────────────────────────────────────────
6. CLIFlags               AppState.cli_flags       NO — intentional
─────────────────────────────────────────────────────────────────────────
```

Layer 6 is structurally separate from layers 1-5. It never merges into
`AgenthiccConfig` and is never overridable from TOML.

### `AppState` gains `cli_flags: CLIFlags`

```python
# tui/conversation_store.py

class AppState:
    conversation:     ConversationStore
    input:            InputState
    active_mode:      Signal[RuntimeMode]
    overlay:          Signal[str]
    modal_open:       Signal[bool]
    pending_approval: Signal[ApprovalRequest | None]
    cli_flags:        CLIFlags          # NEW — frozen, set once at startup
```

### Wiring in `tui_session.py`

```python
async def _run_tui_session(
    resume_id:     str | None        = None,
    cli_overrides: list[str] | None  = None,
    cli_flags:     CLIFlags | None   = None,   # NEW
) -> None:
    cfg = load_config(cli_overrides=cli_overrides or [])
    app_state = AppState.create()
    app_state.cli_flags = cli_flags or CLIFlags()
    ...


def _run_tui(ctx: CLIContext) -> None:
    asyncio.run(_run_tui_session(
        resume_id=ctx.resume_id,
        cli_overrides=list(ctx.set_overrides),
        cli_flags=ctx.flags,
    ))
```

---

## Use case: `--dangerously-skip-permissions`

### Argparse flag

```python
# cli/parser.py  (_add_global_flags)

parser.add_argument(
    "--dangerously-skip-permissions",
    dest="dangerously_skip_permissions",
    action="store_true",
    default=False,
    help=(
        "Disable ALL tool approval prompts for this session. "
        "Overrides Guard mode and all per-mode approval requirements. "
        "Intentionally not settable in agenthicc.toml."
    ),
)
```

### `parse_cli()` builds `CLIFlags`

```python
flags = CLIFlags(
    dangerously_skip_permissions=ns.dangerously_skip_permissions,
)
ctx = CLIContext(..., flags=flags)
```

### `ApprovalGate` reads `app_state.cli_flags`

```python
# tools/approval.py

class ApprovalGate(ToolHook):
    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        if self._app_state.cli_flags.dangerously_skip_permissions:
            return BeforeToolHookDecision.proceed()      # skip ALL approval prompts
        mode      = self._app_state.active_mode()
        tool_caps = ctx.get_metadata(CAPABILITIES_KEY) or frozenset()
        if not (tool_caps & mode.approval_required):
            return BeforeToolHookDecision.proceed()
        ...
```

### End-to-end trace

```
agenthicc --dangerously-skip-permissions

parse_cli()
  → CLIContext(flags=CLIFlags(dangerously_skip_permissions=True))

_run_tui(ctx)
  → _run_tui_session(cli_flags=CLIFlags(dangerously_skip_permissions=True))

app_state.cli_flags = CLIFlags(dangerously_skip_permissions=True)

ApprovalGate.before_tool_call():
  app_state.cli_flags.dangerously_skip_permissions is True
  → BeforeToolHookDecision.proceed()   # Guard mode, Ask mode — all bypassed
```

---

---

## Fix 4 — User-defined commands from `~/.agenthicc/` and `.agenthicc/`

### Three-layer discovery (mirrors config precedence)

Commands are discovered in priority order.  Later layers overwrite earlier
layers for the same command path.  A project command `("deploy",)` silently
shadows a user-global `("deploy",)` which silently shadows a built-in
`("deploy",)`.

```
1. Built-in      agenthicc/cli/commands/*.py     source="builtin"   (lowest)
2. User-global   ~/.agenthicc/cli/*.py           source="user"
3. Project-local ./.agenthicc/cli/*.py           source="project"   (highest)
```

This is the same layering used by `load_config()` for TOML files.

### What the user writes

#### Python plugin (full power)

```python
# .agenthicc/cli/deploy.py

from agenthicc.cli.registry import command, group
from agenthicc.cli.context import CLIContext

@group("deploy", help="Deployment commands for this project")
def _(): ...

@command("deploy", "staging")
async def deploy_staging(ctx: CLIContext, dry_run: bool = False) -> None:
    """Deploy to the staging environment."""
    import subprocess
    cmd = ["./scripts/deploy.sh", "staging"]
    if dry_run: cmd.append("--dry-run")
    subprocess.run(cmd, check=True)

@command("deploy", "production")
async def deploy_production(ctx: CLIContext, tag: str) -> None:
    """Deploy a specific tag to production."""
    import subprocess
    subprocess.run(["./scripts/deploy.sh", "production", f"--tag={tag}"], check=True)
```

```
agenthicc deploy staging --dry-run
agenthicc deploy production --tag v2.1.0
```

#### TOML shorthand (no Python needed — shell-wrapping only)

```toml
# .agenthicc/cli.toml

[[command]]
path = ["deploy", "staging"]
help = "Deploy to the staging environment"
run  = "scripts/deploy.sh staging"
args = [
  { name = "dry_run", type = "bool", default = false },
]

[[command]]
path = ["deploy", "production"]
help = "Deploy a tagged release to production"
run  = "scripts/deploy.sh production --tag={tag}"
args = [
  { name = "tag", type = "str", help = "Git tag to deploy" },
]
```

The discovery layer synthesises TOML entries into Python handlers with
`subprocess.run` and `{arg}` interpolation.  The user never needs to touch
Python for simple shell-wrapping cases.

### `_Entry` gains `source`

```python
@dataclass
class _Entry:
    path:     tuple[str, ...]
    help:     str
    handler:  Callable
    is_async: bool
    source:   str = "builtin"   # "builtin" | "user" | "project"
```

`agenthicc --commands` (and `/commands` in the TUI) shows the badge next to
each command so provenance is always visible and auditable.

### `@command()` reads a `ContextVar` for source tagging

Registration happens at import time, before the call site knows the source.
A `ContextVar` threads the tag through the dynamic import:

```python
from contextvars import ContextVar

_LOADING_SOURCE: ContextVar[str] = ContextVar("_LOADING_SOURCE", default="builtin")

def command(*path: str, help: str = ""):
    def decorator(fn: Callable) -> Callable:
        doc = help or (inspect.getdoc(fn) or "").splitlines()[0]
        _REGISTRY[path] = _Entry(
            path=path, help=doc,
            handler=fn, is_async=asyncio.iscoroutinefunction(fn),
            source=_LOADING_SOURCE.get(),   # reads context at decoration time
        )
        return fn
    return decorator
```

### `_discover_directory()` — dynamic file loader

```python
import importlib.util, sys, warnings

def _discover_directory(directory: Path, source: str) -> None:
    if not directory.is_dir():
        return

    token = _LOADING_SOURCE.set(source)
    try:
        for py in sorted(directory.glob("*.py")):
            if py.name.startswith("_"):
                continue
            mod_name = f"agenthicc._cli_plugin.{source}.{py.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, py)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception as exc:          # noqa: BLE001
                warnings.warn(
                    f"[agenthicc] Failed to load CLI plugin {py}: {exc}",
                    stacklevel=2,
                )

        # Optional TOML shorthand (cli.toml sits beside the cli/ directory)
        toml_file = directory.parent / "cli.toml"
        if toml_file.exists():
            _load_toml_commands(toml_file, source=source)
    finally:
        _LOADING_SOURCE.reset(token)
```

### `_load_toml_commands()` — synthesises handlers for the TOML shorthand

```python
def _load_toml_commands(toml_file: Path, source: str) -> None:
    import tomllib, shlex, subprocess                    # noqa: PLC0415
    try:
        with open(toml_file, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:                             # noqa: BLE001
        import warnings
        warnings.warn(f"[agenthicc] Bad cli.toml at {toml_file}: {exc}", stacklevel=2)
        return

    token = _LOADING_SOURCE.set(source)
    try:
        for spec in data.get("command", []):
            path      = tuple(spec["path"])
            help_str  = spec.get("help", "")
            run_tmpl  = spec.get("run", "")
            arg_specs = spec.get("args", [])

            def _make_handler(tmpl: str, args: list[dict]):
                async def handler(**kwargs):
                    cmd_str = tmpl.format(**kwargs)
                    subprocess.run(shlex.split(cmd_str), check=True)
                handler.__name__        = "_".join(path)
                handler.__doc__         = help_str
                handler.__annotations__ = {
                    a["name"]: bool if a.get("type") == "bool" else str
                    for a in args
                }
                handler.__kwdefaults__ = {
                    a["name"]: a["default"]
                    for a in args if "default" in a
                }
                return handler

            _REGISTRY[path] = _Entry(
                path=path, help=help_str,
                handler=_make_handler(run_tmpl, arg_specs),
                is_async=True,
                source=source,
            )
    finally:
        _LOADING_SOURCE.reset(token)
```

### `_discover()` extended — all three layers in precedence order

```python
def _discover(
    project_dir: Path | None = None,
    user_dir:    Path | None = None,
) -> None:
    # 1. Built-in (lowest priority)
    _discover_package("agenthicc.cli.commands")

    # 2. User-global — can extend or shadow built-ins
    user_cli = (user_dir or Path.home() / ".agenthicc") / "cli"
    _discover_directory(user_cli, source="user")

    # 3. Project-local — highest priority, can shadow both
    project_cli = (project_dir or Path(".agenthicc")) / "cli"
    _discover_directory(project_cli, source="project")
```

`parse_cli()` calls `_discover()` once before building the argparse tree.

### Security model

**User-global (`~/.agenthicc/cli/`) — always trusted.**
The user's own home directory is implicitly trusted, the same as `~/.bashrc`
or `~/.gitconfig`.

**Project-local (`.agenthicc/cli/`) — explicit trust required.**
Loading arbitrary Python from a checked-out repository is a supply-chain risk.
The user must run `agenthicc trust cli` which hashes the current files and
writes `.agenthicc/trusted_cli.json`:

```json
{
  "signed_at": "2026-06-15T10:30:00Z",
  "files": {
    "cli/deploy.py": "sha256:abc123..."
  }
}
```

`_discover_directory()` verifies hashes before loading.  If any file has
changed since the trust was recorded, loading is skipped and a warning is
printed:

```
⚠  .agenthicc/cli/ has untrusted or modified files.
   Run `agenthicc trust cli` to allow loading.
```

`PluginSettings.auto_trust = true` (existing flag in `config.py`) bypasses the
check for CI/CD environments.

### Provenance display

`agenthicc --help` (and `agenthicc <group> --help`) shows a `[source]` badge
next to each command:

```
  deploy    Deployment commands for this project   [project]
  sessions  Manage saved sessions                  [builtin]
  config    Manage configuration                   [builtin]
```

This makes it immediately clear which commands originate from which layer,
which is especially useful when a project command shadows a built-in.

### Precedence table

| Source | Location | Trust | Shadows |
|---|---|---|---|
| `builtin` | `agenthicc/cli/commands/*.py` | Implicit | — |
| `user` | `~/.agenthicc/cli/*.py` | Implicit (home dir) | builtins |
| `project` | `.agenthicc/cli/*.py` | Explicit (`trust cli`) | builtins + user |

### Shadow conflict resolution

Not all shadows are equal.  The answer differs by which layer is being shadowed:

| Shadow direction | Behaviour | Rationale |
|---|---|---|
| user shadows builtin | **Silent** | This is the entire point of user plugins — same as aliasing `ls` in `.bashrc`. |
| project shadows builtin | **Silent** | Same. Project plugins exist to extend or replace built-ins for that codebase. |
| project shadows user-global | **Warn by default; error in strict mode** | The user wrote their global command intentionally. Silent replacement is surprising and a potential security concern (a malicious repo overriding a trusted personal command). |

**Default behaviour** — print a dim warning at startup and continue:

```
⚠  .agenthicc/cli/deploy.py (project) shadows ~/.agenthicc/cli/deploy.py (user)
   Run `agenthicc --help` to see which commands are active.
```

**Strict mode** — treat any user↔project conflict as a hard error.
Configured in `agenthicc.toml` or locked via the Profile (PRD-80):

```toml
# .agenthicc/agenthicc.toml
[plugins]
strict_cli_shadow = true
```

```
agenthicc: error: .agenthicc/cli/deploy.py conflicts with ~/.agenthicc/cli/deploy.py
           Both define command path ["deploy"]. Rename one or set
           [plugins] strict_cli_shadow = false to allow silent shadowing.
```

The rule in one sentence: **shadowing a built-in is intentional extensibility —
always silent.  Shadowing another user-controlled layer is a potential surprise
— warn by default, error in strict mode.**

---

## File changes

| File | Change |
|---|---|
| `cli/registry.py` | **New** — `_Entry` (with `source`), `_REGISTRY`, `_GROUPS`, `_LOADING_SOURCE` ContextVar, `command()`, `group()`, `_add_params()`, `_as_tree()`, `_wire()`, `_call()`, `_discover()`, `_discover_directory()`, `_load_toml_commands()` |
| `cli/context.py` | **New** — `CLIFlags`, `CLIContext` dataclasses |
| `cli/commands/` | **New directory** — one file per built-in command domain (`sessions.py`, `plugin.py`, `config.py`, `auth.py`) |
| `cli/commands/trust.py` | **New built-in** — `@command("trust", "cli")` writes `.agenthicc/trusted_cli.json` |
| `cli/parser.py` | Returns `(CLIContext, argparse.Namespace)`; iterates `_as_tree()` to build argparse; adds `--dangerously-skip-permissions` |
| `__main__.py` | `main()` — 6 lines; dispatch via `_entry` attribute set by `set_defaults` |
| `config.py` | Add `BehaviourSettings`; add `behaviour` field to `AgenthiccConfig`; parse in `_dict_to_config()` |
| `tui/conversation_store.py` | Add `cli_flags: CLIFlags` to `AppState` |
| `runners/tui_session.py` | Add `cli_flags` param to `_run_tui_session()`; accept `CLIContext` in `_run_tui()` |
| `runners/headless.py` | Accept `CLIContext`; respect `cli_flags` |
| `tools/approval.py` (PRD-78) | `ApprovalGate` checks `app_state.cli_flags.dangerously_skip_permissions` first |
| `~/.agenthicc/cli/*.py` | User-global Python command plugins (discovered at startup) |
| `.agenthicc/cli/*.py` | Project-local Python command plugins (trusted, then discovered) |
| `.agenthicc/cli.toml` | Optional TOML shorthand for shell-wrapping commands |
| `.agenthicc/trusted_cli.json` | Trust manifest written by `agenthicc trust cli` |
| `config.py` — `PluginSettings` | Add `strict_cli_shadow: bool = False` field |
| `cli/registry.py` | `_discover()` detects user↔project conflicts; warns or errors based on `strict_cli_shadow` |

---

## Acceptance criteria

- [ ] `agenthicc sessions show abc-123` works.
- [ ] `agenthicc plugin trust add my-plugin` works (three-level nesting).
- [ ] Adding a new built-in subcommand at any depth requires only one `@command(...)` decorator — no changes to `main()`, `parse_cli()`, or any other handler.
- [ ] `CLIContext` is injected by annotation type; the parameter name is irrelevant (`ctx`, `app`, `c`, `session` all work).
- [ ] `agenthicc --dangerously-skip-permissions` disables all `ApprovalGate` prompts in all modes (Guard, Ask, Review, etc.).
- [ ] `--dangerously-skip-permissions` cannot be set in `agenthicc.toml` (no TOML path exists).
- [ ] `--set behaviour.verbose=true` sets `AgenthiccConfig.behaviour.verbose` via the normal TOML merge path.
- [ ] `AppState.cli_flags` is frozen after startup and never changes during the session.
- [ ] All existing tests pass.
- [ ] `agenthicc --help` and `agenthicc plugin --help` show correct help at every nesting level.
- [ ] A Python file in `.agenthicc/cli/deploy.py` using `@command("deploy", "staging")` produces a working `agenthicc deploy staging` command.
- [ ] A `.agenthicc/cli.toml` entry with `run = "scripts/deploy.sh {env}"` produces a working command without any Python code.
- [ ] Project-local commands in `.agenthicc/cli/` are NOT loaded until `agenthicc trust cli` has been run (or `PluginSettings.auto_trust = true`).
- [ ] User-global commands in `~/.agenthicc/cli/` are always loaded without a trust step.
- [ ] `agenthicc --help` shows `[project]`, `[user]`, or `[builtin]` badges next to each command.
- [ ] A project command with the same path as a built-in silently shadows the built-in (no warning).
- [ ] A project command with the same path as a user-global command prints a startup warning naming both files and the shadowed path.
- [ ] Setting `[plugins] strict_cli_shadow = true` turns the user↔project shadow warning into a hard error that exits before the session starts.
- [ ] A user-global command shadowing a built-in produces no warning or error.
