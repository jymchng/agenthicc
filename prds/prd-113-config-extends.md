# PRD-113 — Configuration Inheritance via `extends`

## Summary

Any agenthicc TOML configuration file may declare one or more parent files
using an `extends` key.  The parent files are loaded and merged first; the
declaring file's values are applied on top.  This lets users maintain a single
base config and layer environment-specific or personal overrides without
duplicating shared settings.

A `--config <file>` CLI flag (already wired in the CLI parser but previously
ignored) and an `AGENTHICC_CONFIG` environment variable allow selecting which
project-level config file to use at invocation time.

---

## Problem

Users managing multiple environments (dev, staging, prod) or multiple servers
must either:
- Duplicate all common settings into every config file (fragile, high maintenance), or
- Use external tooling to concatenate/template files (out-of-band, non-obvious)

Neither is acceptable.  The solution must be native to agenthicc's config
system, require no external tools, and compose with all existing override
mechanisms.

---

## Design

### `extends` key in any TOML file

```toml
# agenthicc-dev.toml
extends = "agenthicc.toml"      # relative to this file

[execution]
model = "claude-haiku-4-5"      # override only what differs
```

Or a list of parents (merged left-to-right; each overrides the previous):

```toml
extends = ["../../shared/team-base.toml", "secrets.toml"]
```

**Resolution algorithm** (`_resolve_extends`):
1. Read the TOML file.
2. Extract and remove the `extends` key (so it never reaches `_dict_to_config`).
3. Normalize to a list of path strings.
4. For each parent: resolve relative to the current file's directory,
   expand `~`, then call `_resolve_extends` recursively.
5. `deep_merge` the parent results in order.
6. `deep_merge` the current file's remaining data on top.
7. Return the fully-merged dict.

**Cycle detection:** a `frozenset[Path]` of resolved absolute paths is
threaded through the call stack.  A cycle raises `ConfigExtendsCycleError`.

**Missing parent:** a non-existent file named in `extends` raises
`FileNotFoundError` immediately — this is an explicit user mistake, not a
graceful skip.

### `--config <file>` flag (wired)

The flag already exists in the CLI parser and `CLIContext.config_path`.
This PRD wires it through to `load_config()`.  When specified, it replaces
the auto-discovered `.agenthicc/agenthicc.toml` project file; the file's
`extends` chain is followed automatically.

### `AGENTHICC_CONFIG` environment variable

```bash
AGENTHICC_CONFIG=agenthicc-prod.toml agenthicc
```

Priority: `--config` > `AGENTHICC_CONFIG` > auto-discovery.

### Full merge order (unchanged semantics, new resolution step)

```
hardcoded defaults
    ↓
user-global file + its extends chain
    ↓
project-level file (--config / AGENTHICC_CONFIG / auto-discovered) + its extends chain
    ↓
AGENTHICC_* env vars
    ↓
--set key=value overrides
```

---

## Examples

### Dev/prod separation

```toml
# agenthicc.toml   (base — committed)
[execution]
provider = "anthropic"
model    = "claude-opus-4-8"

[tools]
http_timeout_s = 30
```

```toml
# agenthicc-dev.toml   (dev overlay — .gitignored)
extends = "agenthicc.toml"

[execution]
model = "claude-haiku-4-5"

[workflows.code_plan]
execute_model = "claude-haiku-4-5"
```

```bash
agenthicc --config agenthicc-dev.toml
```

### Shared team base in a monorepo

```toml
# services/auth/.agenthicc/agenthicc.toml
extends = ["../../../shared/team-agenthicc.toml"]

[execution]
model = "claude-opus-4-8"
```

### Secrets file out of version control

```toml
# agenthicc.toml
extends = ["agenthicc-secrets.toml"]   # .gitignored

[execution]
provider = "anthropic"
```

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `extends = "parent.toml"` in a project config loads the parent first and merges the current file on top. |
| 2 | `extends = ["a.toml", "b.toml"]` loads both parents left-to-right; the current file wins. |
| 3 | Parent paths are resolved relative to the file containing `extends`. |
| 4 | `~` in paths is expanded to the user home directory. |
| 5 | Chained `extends` (parent also has `extends`) is resolved recursively. |
| 6 | A cycle in the extends chain raises `ConfigExtendsCycleError`. |
| 7 | A non-existent file named in `extends` raises `FileNotFoundError`. |
| 8 | The `extends` key is stripped before the dict reaches `_dict_to_config` — it never appears in `AgenthiccConfig`. |
| 9 | `--config <file>` selects the project-level config file (wired through to `load_config`). |
| 10 | `AGENTHICC_CONFIG=<file>` env var selects the project-level config file. |
| 11 | `--config` takes priority over `AGENTHICC_CONFIG`. |
| 12 | User-global `~/.agenthicc/agenthicc.toml` is unaffected and still applied as the base. |
| 13 | `extends` in the user-global config file is also resolved. |
| 14 | All existing config loading tests continue to pass. |

---

## Files Changed

| File | Change |
|---|---|
| `config.py` | `ConfigExtendsCycleError`; `_resolve_extends(path, seen)`; `_load_toml_with_extends(path)`; `load_config()` gains `config_path` param and reads `AGENTHICC_CONFIG` |
| `runners/tui_session.py` | Thread `config_path` from `CLIContext` through `_run_tui_session()` → `_build_session_context()` → `load_config()` |
