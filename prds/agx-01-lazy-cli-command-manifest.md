---
title: "AGX-01: Lazy CLI command manifest and import budgets"
status: Implemented
version: 1.0.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P0
series: AGX
related_prds: [agx-00, agx-02, agx-14, prd-206, prd-209]
tags: [cli, startup, performance, imports]
---

# AGX-01 — Lazy CLI command manifest and import budgets

## Implementation status (2026-10-23)

Implemented. Modules loaded per invocation, measured as `sys.modules` at exit
in a fresh interpreter (`-X importtime` undercounts modules imported through
`importlib.import_module`, so it is not used for budgets):

| Invocation | Before | After |
|---|---:|---:|
| `agenthicc --version` | 211 | 153 |
| `agenthicc --help` | 211 | 111 |
| `agenthicc config --help` | 457 | 131 |
| `agenthicc agents --json` / `jobs list --json` | 457 | 169 |
| `agenthicc runs --json` | 457 | 174 |
| `agenthicc sessions list` (non-TTY) | 462 | 158 |

Besides the manifest (§5.1–5.3) the change made the `agenthicc.kernel` and
`agenthicc.background` packages resolve their public names lazily, moved
`McpServerConfig` to `agenthicc.tools.mcp_server_config` (configuration
loading had imported the MCP client), made `--version` read metadata only when
used, and removed module-level `asyncio` imports from listing paths. Two
deviations from §5.5: `--version` needs `importlib.metadata` (115 modules on
its own), so its budget is ≤ 160 modules; and `jobs`, `agents`, `runs` and
`schedules` are commands as well as groups, so their help imports their own
module (pure groups such as `config` import none). Removing discovery also
exposed that importing `cli/commands/background.py` patched `TUISession`;
`/background`, `/bg` and `/detach` are now ordinary built-in commands.
Tests: `tests/unit/test_cli_manifest.py`, `tests/e2e/test_cli_import_budget.py`.

## 1. Summary

Every `agenthicc` subcommand imports every built-in command module before it
parses its arguments. `agenthicc agents --json` on an empty registry therefore
costs 1.25 s of CPU and imports 440 modules — including `aiohttp`, the TUI
session runner and the input stack — to print `[]`. Replace import-time
discovery of built-in commands with a declarative command manifest, import only
the selected command's module, and enforce per-command import budgets so the
cost cannot creep back.

## 2. Problem and evidence

### 2.1 Discovery imports everything

`parse_cli()` (`src/agenthicc/cli/parser.py:144`) loads configuration, then
calls `_discover()` (`src/agenthicc/cli/registry.py:370`), whose first step is
`_discover_package("agenthicc.cli.commands")` (`:250`):

```python
for _, name, _ in pkgutil.iter_modules(getattr(pkg, "__path__", [])):
    importlib.import_module(f"{package_name}.{name}")
```

All 14 modules in `agenthicc/cli/commands/` are imported so their `@command`
decorators can fill `_REGISTRY`; then every command is wired into one argparse
tree. Command modules import heavy dependencies at module level — for example
`session_service.py` pulls in `agenthicc.session_service.transport` and with it
`aiohttp`.

### 2.2 Measured cost

Empty `HOME`, best of 3, load ≈ 4 (AGX-00 §4.1):

| Invocation | CPU | Note |
|---|---:|---|
| `python -c pass` | 0.04 s | interpreter floor |
| `agenthicc --version` | 0.23 s | fast path skips discovery, still 204 modules |
| `agenthicc --help` | 0.39 s | existing fast path |
| `agenthicc agents --json` | 1.25 s | 440 modules |
| `agenthicc jobs list --json` | 1.25 s | |
| `agenthicc runs --json` | 1.43 s | |

`python -X importtime -m agenthicc agents --json` attributes 0.49 s
(cumulative) to `agenthicc.session_service.transport` → `aiohttp`, and lists
`agenthicc.runners.tui_session`, `agenthicc.tui.input.unified_session`,
`agenthicc.schedules` and `agenthicc.background` among the largest imports —
none needed to print the registry.

Users and scripts call listing commands often (`agents --json`, `jobs status`,
`runs show`); E2E tests run dozens of them. Each one pays the full price.

### 2.3 Why the existing fast paths do not generalise

`--version` and `--help` are special-cased before discovery. Every other
invocation, including `agenthicc agents --help`, still discovers everything.
Extending the special cases one by one would duplicate argument handling.

## 3. How others approach it

- Codex ships a native binary, so subcommand start-up has no interpreter or
  import cost.
- OpenCode documents `opencode run --attach <url>` to reuse a running
  `opencode serve` instance and avoid MCP cold boots on every run.

agenthicc cannot change language, but it can stop importing code a command does
not use. AGX-02 covers the warm-process option for the interactive TUI.

## 4. Goals and non-goals

**Goals**

1. A subcommand imports only its own module and that module's real
   dependencies.
2. `agenthicc <group> --help` and argument errors do not import command modules
   other than the selected one.
3. Per-command import and CPU budgets are enforced in CI (AGX-14).
4. Project and user command plugins keep their precedence and trust rules.

**Non-goals**

- Changing the public command surface or plugin API (`@command` stays the way
  plugins declare commands).
- Caching parsed configuration across processes.

## 5. Design

### 5.1 Declarative manifest for built-in commands

Add `src/agenthicc/cli/manifest.py` with one immutable table:

```python
@dataclass(frozen=True)
class CommandRef:
    path: tuple[str, ...]          # ("agents",), ("jobs", "list"), ...
    target: str                    # "agenthicc.cli.commands.background:agents"
    help: str

BUILTIN_COMMANDS: tuple[CommandRef, ...] = (...)
GROUP_HELP: Mapping[tuple[str, ...], str] = {...}
```

Built-in command modules drop the `@command` decorator; their handlers become
plain functions referenced by `target`. The table is the single source of truth
for built-in paths and help text, so there is no generated file to drift and no
second registry (the decorator path for built-ins is deleted, per the
repository's no-legacy rule).

### 5.2 Resolution and parsing

1. Parse global flags with the existing bootstrap parser
   (`parse_known_args`), which already knows which flags take values.
2. Match the remaining positional tokens against manifest paths, longest prefix
   first.
3. Discover project and user plugins (§5.3). A plugin path shadows a built-in
   path exactly as today, and shadow warnings keep their wording.
4. Import only the resolved target, then build an argparse parser containing the
   global flags and the single command chain, inferring arguments from the
   handler signature as `registry._add_arguments` does today.
5. Group help (`agenthicc jobs --help`) and "unknown command" errors are built
   from `GROUP_HELP` and `BUILTIN_COMMANDS` without importing any command module.

The `--version` and top-level `--help` fast paths become ordinary manifest
lookups, so the special cases in `parse_cli` are removed.

### 5.3 Plugins

Plugin directories are only scanned when they exist and contain files. A user
with no `~/.agenthicc/cli/` and no `.agenthicc/cli/` pays one `stat` per
directory. When plugins exist, behaviour is unchanged: trusted files are
executed and register through `@command`.

### 5.4 Import hygiene in command modules

Command modules import heavy dependencies inside the handlers that need them.
In particular:

| Module | Must not import at module level |
|---|---|
| `cli/commands/background.py` | `agenthicc.runners.tui_session`, `agenthicc.tui.*`, `rich.live` |
| `cli/commands/session_service.py` | `agenthicc.session_service.transport` (`aiohttp`) |
| `cli/commands/runs.py`, `schedules.py`, `worktrees.py` | TUI and provider (`lauren_ai`) modules |

`agenthicc.background` and `agenthicc.session_service` package `__init__`
modules must not eagerly import transports, notification delivery or
webhook clients; their public names stay available through module-level
`__getattr__` only where a name is re-exported today.

### 5.5 Budgets

Measured as described in AGX-14 (CPU time and module count, best of N):

| Command (empty registry) | CPU | Modules |
|---|---:|---:|
| `agenthicc --version` | ≤ 0.25 s | ≤ 160 |
| `agenthicc --help`, `agenthicc <group> --help` | ≤ 0.25 s | ≤ 140 |
| `agenthicc agents --json`, `jobs list --json`, `runs --json`, `sessions list` (non-TTY) | ≤ 0.30 s | ≤ 200 |

For scale: a bare interpreter imports 29 modules, `argparse` 49,
`importlib.metadata` (used for `--version`) 115 and `asyncio` 130.
`agenthicc/__main__.py` imports `asyncio` at module level, which is why even
`--version` reaches 204 today; it moves into the paths that run a coroutine.

A deny-list check fails the build if a listing command imports `aiohttp`,
`lauren_ai`, `httpx`, `agenthicc.runners.tui_session` or `rich.live`.

## 6. Acceptance criteria

1. `agenthicc agents --json` meets the budget above on the reference host and
   prints the same JSON as before for the same registry.
2. Every path in `BUILTIN_COMMANDS` resolves to an importable callable; a unit
   test imports each target and checks its signature can be wired.
3. `agenthicc <group> --help` and `agenthicc <unknown>` import no command
   module (asserted through `sys.modules` in a subprocess test).
4. Project and user plugin precedence, trust checks and shadow warnings behave
   as today (existing CLI plugin tests pass unchanged in intent).
5. No `_discover_package` call remains; `rg "_discover_package"` finds nothing.

## 7. Test plan

- Unit: manifest integrity (unique paths, importable targets, help text present,
  every module under `cli/commands/` referenced).
- Subprocess: module-count and deny-list assertions per listing command.
- E2E: the existing CLI E2E suite (it runs about 30 CLI processes) becomes
  faster; its `tests/cli_process.py` ceilings stay as hang bounds.
- AGX-14 budget job records CPU time per command.

## 8. Risks

- **Argument inference drift.** Wiring one command at a time must produce the
  same argparse behaviour as the full tree. Mitigation: a test wires every
  manifest entry and compares `--help` output with today's.
- **Hidden import side effects.** Some modules may rely on another module having
  registered something at import time. Mitigation: the manifest integrity test
  imports targets in isolation, one per subprocess.

## 9. Reproduction

```bash
HOME=$(mktemp -d) python -X importtime -m agenthicc agents --json 2> imports.txt
grep -c "import time:" imports.txt            # import records (449; 440 unique modules)
sort -t'|' -k2 -n imports.txt | tail -20      # largest cumulative imports
```

The module-level import that drags `aiohttp` into every command is
`from agenthicc.session_service.transport import LocalSessionServer` at
`src/agenthicc/cli/commands/session_service.py:17`.

## Sources

- OpenCode CLI (`run --attach`): <https://opencode.ai/docs/cli/>
- Codex CLI reference: <https://developers.openai.com/codex/cli/reference>
