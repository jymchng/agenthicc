# PRD-79 — CLI Revamp: Decorator-Based Subcommands, CLIContext, and Configuration Wiring

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

## File changes

| File | Change |
|---|---|
| `cli/registry.py` | **New** — `_Entry`, `_REGISTRY`, `_GROUPS`, `command()`, `group()`, `_add_params()`, `_as_tree()`, `_wire()`, `_call()`, `_discover()` |
| `cli/context.py` | **New** — `CLIFlags`, `CLIContext` dataclasses |
| `cli/commands/` | **New directory** — one file per command domain (`sessions.py`, `plugin.py`, `config.py`, `auth.py`) |
| `cli/parser.py` | Returns `(CLIContext, argparse.Namespace)`; iterates `_as_tree()` to build argparse; adds `--dangerously-skip-permissions` |
| `__main__.py` | `main()` — 6 lines; dispatch via `_entry` attribute set by `set_defaults` |
| `config.py` | Add `BehaviourSettings`; add `behaviour` field to `AgenthiccConfig`; parse in `_dict_to_config()` |
| `tui/conversation_store.py` | Add `cli_flags: CLIFlags` to `AppState` |
| `runners/tui_session.py` | Add `cli_flags` param to `_run_tui_session()`; accept `CLIContext` in `_run_tui()` |
| `runners/headless.py` | Accept `CLIContext`; respect `cli_flags` |
| `tools/approval.py` (PRD-78) | `ApprovalGate` checks `app_state.cli_flags.dangerously_skip_permissions` first |

---

## Acceptance criteria

- [ ] `agenthicc sessions show abc-123` works.
- [ ] `agenthicc plugin trust add my-plugin` works (three-level nesting).
- [ ] Adding a new subcommand at any depth requires only one `@command(...)` decorator — no changes to `main()`, `parse_cli()`, or any other handler.
- [ ] `CLIContext` is injected by annotation type; the parameter name is irrelevant (`ctx`, `app`, `c`, `session` all work).
- [ ] `agenthicc --dangerously-skip-permissions` disables all `ApprovalGate` prompts in all modes (Guard, Ask, Review, etc.).
- [ ] `--dangerously-skip-permissions` cannot be set in `agenthicc.toml` (no TOML path exists).
- [ ] `--set behaviour.verbose=true` sets `AgenthiccConfig.behaviour.verbose` via the normal TOML merge path.
- [ ] `AppState.cli_flags` is frozen after startup and never changes during the session.
- [ ] All existing tests pass.
- [ ] `agenthicc --help` and `agenthicc plugin --help` show correct help at every nesting level.
