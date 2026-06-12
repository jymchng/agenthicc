---
title: "PRD-21: Configuration Management — Directory Structure, Precedence, and Application Packaging"
status: draft
version: 0.1.0
created: 2025-01-01
replaces: prd-07-configuration-and-security.md (partially)
---

# PRD-21: Configuration Management

## Executive Summary

Agenthicc is a **terminal application**, not a library. Its current packaging treats
rich TUI dependencies as optional extras (`pip install agenthicc[tui]`), which is the
wrong model — nobody installs a terminal emulator as an optional feature. This PRD
specifies three changes that fix both packaging and configuration:

1. **Application packaging**: `rich`, `prompt_toolkit`, and all TUI/API dependencies
   move into the core `dependencies` list. `pip install agenthicc` installs a
   fully-functional application. Optional extras (`cloud`, `dev`) remain for
   non-essential integrations.

2. **Directory structure**: `.agenthicc/` in the project root is the per-project
   storage and configuration directory; `~/.agenthicc/` is the user-global directory.
   Both follow the same layout. Configuration files are named `agenthicc.toml` or
   `.agenthicc.toml` (both accepted; `agenthicc.toml` takes precedence).

3. **Configuration precedence** (lowest to highest):
   ```
   built-in defaults
        ↓
   ~/.agenthicc/agenthicc.toml    (user global)
        ↓
   ./.agenthicc/agenthicc.toml   (project local)  ← or ./agenthicc.toml / ./.agenthicc.toml
        ↓
   environment variables          AGENTHICC_* prefix
        ↓
   command-line arguments         --config, --headless, etc.   (highest)
   ```

---

## Goals

| ID | Goal |
|----|------|
| G1 | `pip install agenthicc` installs a fully functional application — no extras needed |
| G2 | `.agenthicc/` in cwd is the per-project config and storage directory |
| G3 | `~/.agenthicc/` is the user-global config and storage directory |
| G4 | Both `agenthicc.toml` and `.agenthicc.toml` are accepted filename spellings |
| G5 | `agenthicc.toml` (no leading dot) takes precedence over `.agenthicc.toml` |
| G6 | Environment variables `AGENTHICC_*` override file config |
| G7 | CLI flags `--config PATH`, `--set section.key=value` override all file/env config |
| G8 | `agenthicc config show` prints the merged effective configuration |
| G9 | `agenthicc config init` creates a template `agenthicc.toml` in the current directory |
| G10 | Unknown config keys produce a warning, not an error (forward compatibility) |

## Non-Goals
- GUI config editor (the `/settings` TUI slash command handles that)
- Config encryption at rest
- Remote config sources (S3, Vault, etc.)
- JSON or YAML config formats (TOML only)

---

## Directory Structure

```
~/.agenthicc/                    ← user-global directory
  agenthicc.toml                 ← user-global config (canonical name)
  .agenthicc.toml                ← also accepted (legacy / dotfile style)
  history                        ← input bar command history
  sessions.json                  ← session index (cross-project)
  global.db                      ← global memory SQLite

./.agenthicc/                    ← per-project directory (cwd)
  agenthicc.toml                 ← project config (canonical name)
  .agenthicc.toml                ← also accepted
  events.jsonl                   ← current session event log
  snapshot.json                  ← AppState snapshot
  history                        ← project-scoped input history
  sessions/                      ← session event log archive
    <session-id>.jsonl
  sessions.json                  ← project session index
  memory/                        ← project memory SQLite + vector index
    project.db
    artifacts/
      <sha256>.bin
  ads_cache.json                 ← cached ad responses
  tokens.json                    ← auth token cache (fallback from keyring)

./agenthicc.toml                 ← alternate location (root of cwd, no subdir)
./.agenthicc.toml                ← alternate location (dotfile in cwd)
```

### Config file search order (first found wins for project config):

```python
PROJECT_CONFIG_CANDIDATES = [
    Path(".agenthicc") / "agenthicc.toml",    # preferred
    Path(".agenthicc") / ".agenthicc.toml",
    Path("agenthicc.toml"),                    # root of cwd
    Path(".agenthicc.toml"),                   # dotfile in cwd
]

USER_CONFIG_CANDIDATES = [
    Path.home() / ".agenthicc" / "agenthicc.toml",   # preferred
    Path.home() / ".agenthicc" / ".agenthicc.toml",
    Path.home() / ".agenthicc.toml",                  # legacy location
]
```

---

## Configuration Precedence

```
Priority 1 (lowest) — Built-in defaults (hardcoded in Python dataclasses)
Priority 2          — ./. .agenthicc/agenthicc.toml (project local)
Priority 3          — ./.agenthicc/agenthicc.toml (project local)
Priority 4          — AGENTHICC_* environment variables
Priority 5 (highest) — CLI flags (--config PATH overrides file; --set overrides key)
```

### Environment variable mapping

Each config key maps to `AGENTHICC_<SECTION>_<KEY>` (uppercased, dots → underscores):

| Env var | Config key | Type |
|---------|-----------|------|
| `AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS` | `execution.max_parallel_tasks` | int |
| `AGENTHICC_MEMORY_PROJECT_MEMORY_PATH` | `memory.project_memory_path` | str |
| `AGENTHICC_API_API_KEY` | `api.api_key` | str |
| `AGENTHICC_SECURITY_DEFAULT_ACTION` | `security.default_action` | str |
| `AGENTHICC_AUTH_TOKEN` | shorthand for MSGRAPH token / any bearer token | str |
| `AGENTHICC_HEADLESS` | `--headless` equivalent | bool ("1"/"true"/"yes") |

### `--config` and `--set` flags

```bash
# Use a custom config file (replaces project config, not user config)
agenthicc --config /path/to/myconfig.toml

# Override individual keys (can repeat)
agenthicc --set execution.max_parallel_tasks=10 --set api.port=9000

# Both together
agenthicc --config base.toml --set memory.project_memory_path=/fast-ssd/proj
```

---

## Application Packaging

### Current (wrong — library model):

```toml
[project]
dependencies = ["lauren-ai", "anyio>=4.0", "typing-extensions>=4.11"]

[project.optional-dependencies]
tui = ["prompt_toolkit>=3.0", "rich>=13.0"]
api = ["fastapi>=0.110", "uvicorn>=0.29", "websockets>=12.0"]
```

Installation: `pip install agenthicc[tui,api]` ← wrong for an application

### New (correct — application model):

```toml
[project]
dependencies = [
    "lauren-ai",
    "anyio>=4.0",
    "typing-extensions>=4.11",
    # TUI — always required (this is a terminal application)
    "prompt_toolkit>=3.0",
    "rich>=13.0",
    # API server — always included
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "websockets>=12.0",
    # HTTP client — used by tools, oauth, ads
    "httpx>=0.27",
]

[project.optional-dependencies]
# Cloud features — only needed for OAuth + ads + MCP remote servers
cloud = ["aiohttp>=3.9", "keyring>=25.0"]

# Developer extras
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-timeout>=2.3",
    "hypothesis>=6.100",
    "pyte>=0.8",
    "pytest-cov>=7.0",
]
```

Installation: `pip install agenthicc` ← correct for an application

### Guard removal in code

Remove all `RICH_AVAILABLE` / `PROMPT_TOOLKIT_AVAILABLE` conditional import guards.
Rich and prompt_toolkit are always present. Simplify the `tui/app.py` import block:

```python
# Before (library-style guards):
try:
    from rich.console import Console
    RICH_AVAILABLE = True
except Exception:
    RICH_AVAILABLE = False

# After (application-style direct import):
from rich.console import Console
from rich.live import Live
# etc.
RICH_AVAILABLE = True   # keep constant for backward compat in tests, but always True
```

---

## Implementation

### 5.1 Updated `config.py`

```python
# src/agenthicc/config.py  — key additions

PROJECT_CONFIG_CANDIDATES = [
    Path(".agenthicc") / "agenthicc.toml",
    Path(".agenthicc") / ".agenthicc.toml",
    Path("agenthicc.toml"),
    Path(".agenthicc.toml"),
]

USER_CONFIG_CANDIDATES = [
    Path.home() / ".agenthicc" / "agenthicc.toml",
    Path.home() / ".agenthicc" / ".agenthicc.toml",
    Path.home() / ".agenthicc.toml",
]


def _find_config_file(candidates: list[Path]) -> Path | None:
    """Return the first candidate that exists, or None."""
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _load_toml_safe(path: Path) -> dict[str, Any]:
    """Load a TOML file, returning {} on any error (warn unknown keys)."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, PermissionError):
        return {}
    except tomllib.TOMLDecodeError as exc:
        import warnings
        warnings.warn(f"Invalid TOML in {path}: {exc}", stacklevel=3)
        return {}


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Apply AGENTHICC_* environment variable overrides."""
    import os
    for key, value in os.environ.items():
        if not key.startswith("AGENTHICC_"):
            continue
        parts = key[len("AGENTHICC_"):].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, field = parts[0], parts[1]
        config.setdefault(section, {})[field] = _coerce_env(value)
    return config


def _coerce_env(value: str) -> Any:
    """Coerce env var string to int / bool / str."""
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _apply_cli_overrides(config: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    """Apply --set section.key=value overrides."""
    for override in overrides:
        if "=" not in override:
            continue
        key_path, _, value_str = override.partition("=")
        parts = key_path.split(".")
        if len(parts) < 2:
            continue
        section = parts[0]
        field = ".".join(parts[1:])
        config.setdefault(section, {})[field] = _coerce_env(value_str)
    return config


def load_config(
    project_path: str | None = None,
    user_path: str | None = None,
    env_overrides: bool = True,
    cli_overrides: list[str] | None = None,
) -> AgenthiccConfig:
    """Load and merge configuration from all sources.

    Search order (each overrides the previous):
    1. Hardcoded defaults
    2. User config: ~/.agenthicc/agenthicc.toml (or legacy paths)
    3. Project config: .agenthicc/agenthicc.toml (or agenthicc.toml, etc.)
    4. Environment variables AGENTHICC_*
    5. CLI --set overrides
    """
    # 1. Start with defaults
    merged: dict[str, Any] = {}

    # 2. User config
    user_file = (
        Path(user_path) if user_path
        else _find_config_file(USER_CONFIG_CANDIDATES)
    )
    if user_file:
        merged = deep_merge(merged, _load_toml_safe(user_file))

    # 3. Project config
    project_file = (
        Path(project_path) if project_path
        else _find_config_file(PROJECT_CONFIG_CANDIDATES)
    )
    if project_file:
        merged = deep_merge(merged, _load_toml_safe(project_file))

    # 4. Environment variable overrides
    if env_overrides:
        merged = _apply_env_overrides(merged)

    # 5. CLI overrides
    if cli_overrides:
        merged = _apply_cli_overrides(merged, cli_overrides)

    return _dict_to_config(merged)
```

### 5.2 New `__main__.py` subcommands

```python
# agenthicc config show  — print merged effective configuration
# agenthicc config init  — create template agenthicc.toml in cwd

def _do_config_show(args) -> None:
    import tomllib, sys
    config = load_config(cli_overrides=getattr(args, "set", []))
    # Print as TOML-like output
    for section, values in config.__dict__.items():
        if hasattr(values, "__dict__"):
            print(f"\n[{section}]")
            for k, v in values.__dict__.items():
                print(f"{k} = {v!r}")

TEMPLATE_CONFIG = """\
# agenthicc.toml — project configuration
# See: https://agenthicc.dev/docs/guides/configuration

[execution]
max_concurrent_intents = 10
max_parallel_tasks = 20
agent_pool_size = 30

[memory]
project_memory_path = ".agenthicc/memory"

[security]
default_action = "deny"

[api]
host = "127.0.0.1"
port = 8000
"""

def _do_config_init(args) -> None:
    target = Path(".agenthicc") / "agenthicc.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        print(f"{target} already exists. Use --force to overwrite.")
        return
    target.write_text(TEMPLATE_CONFIG)
    print(f"Created {target}")
```

### 5.3 Updated `SystemSettings` path defaults

```python
@dataclass(frozen=True)
class SystemSettings:
    max_concurrent_intents: int = 10
    max_parallel_tasks: int = 20
    agent_pool_size: int = 30
    snapshot_every_n_events: int = 100
    event_log_path: str = ".agenthicc/events.jsonl"      # ← was ".agenthicc/events.jsonl" (unchanged)
    snapshot_path: str = ".agenthicc/snapshot.json"       # ← was ".agenthicc/snapshot.json" (unchanged)
```

These already use `.agenthicc/` — confirm they match the new directory spec.

---

## Migration Guide

### For existing users with `agenthicc.toml` in the project root:

Config at `./agenthicc.toml` is still supported (Priority 3, candidate 3 in the search list). No changes needed. To move to the canonical location:

```bash
mkdir -p .agenthicc
mv agenthicc.toml .agenthicc/agenthicc.toml
```

### For CI/CD scripts using `pip install agenthicc[tui]`:

```bash
# Before:
pip install agenthicc[tui,api]

# After:
pip install agenthicc
```

### For uv-based installs:

```bash
# Before:
uv add agenthicc[tui]

# After:
uv add agenthicc
```

---

## Implementation Plan

### Phase 1 — pyproject.toml (30 min)
1. Move `prompt_toolkit`, `rich`, `fastapi`, `uvicorn`, `websockets`, `httpx` to core `dependencies`
2. Remove `tui` and `api` optional-extra sections
3. Keep `cloud` and `dev` as the only optional extras
4. Run `uv sync` and verify full test suite passes
5. Update README and docs install instructions

### Phase 2 — config.py (2 h)
1. Add `PROJECT_CONFIG_CANDIDATES` and `USER_CONFIG_CANDIDATES` lists
2. Implement `_find_config_file()`, `_load_toml_safe()`, `_apply_env_overrides()`, `_apply_cli_overrides()`
3. Update `load_config()` signature with `env_overrides=True` and `cli_overrides=None`
4. Add `_coerce_env()` for int/bool/float coercion
5. Update tests: `test_config.py` env override tests, CLI override tests

### Phase 3 — `__main__.py` (1 h)
1. Add `config` subcommand with `show` and `init` sub-subcommands
2. Add `--set key=value` flag (repeatable) to all non-subcommand invocations
3. Add `config init` creates `.agenthicc/agenthicc.toml` from template
4. Update `test_main.py` with config subcommand tests

### Phase 4 — Guard removal (30 min)
1. Remove `try/except` import guards for `rich` and `prompt_toolkit` from `tui/app.py`
2. Keep `RICH_AVAILABLE = True` and `PROMPT_TOOLKIT_AVAILABLE = True` constants (backward compat for existing tests that check them)
3. Remove `# pragma: no cover` on the `except` branches (no longer needed)
4. Run tests — no functional change expected

### Phase 5 — `.gitignore` update (15 min)
Add `.agenthicc/` to `.gitignore` except `agenthicc.toml`:
```gitignore
.agenthicc/
!.agenthicc/agenthicc.toml
!.agenthicc/.agenthicc.toml
```

---

## Tests

### Updated `tests/unit/test_config.py`

```python
# Add these test classes:

class TestConfigFileSearch:
    def test_project_config_in_subdir(self, tmp_path, monkeypatch):
        """Finds .agenthicc/agenthicc.toml first."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".agenthicc").mkdir()
        (tmp_path / ".agenthicc" / "agenthicc.toml").write_text("[execution]\nmax_parallel_tasks = 7\n")
        config = load_config()
        assert config.execution.max_parallel_tasks == 7

    def test_project_config_fallback_to_root(self, tmp_path, monkeypatch):
        """Falls back to ./agenthicc.toml if .agenthicc/ not present."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "agenthicc.toml").write_text("[execution]\nmax_parallel_tasks = 5\n")
        config = load_config()
        assert config.execution.max_parallel_tasks == 5

    def test_subdir_config_wins_over_root(self, tmp_path, monkeypatch):
        """When both exist, .agenthicc/agenthicc.toml takes precedence over ./agenthicc.toml."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".agenthicc").mkdir()
        (tmp_path / ".agenthicc" / "agenthicc.toml").write_text("[execution]\nmax_parallel_tasks = 9\n")
        (tmp_path / "agenthicc.toml").write_text("[execution]\nmax_parallel_tasks = 3\n")
        config = load_config()
        assert config.execution.max_parallel_tasks == 9

    def test_dotfile_spelling_accepted(self, tmp_path, monkeypatch):
        """Accepts .agenthicc.toml (dotfile spelling) in project root."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".agenthicc.toml").write_text("[execution]\nmax_parallel_tasks = 4\n")
        config = load_config()
        assert config.execution.max_parallel_tasks == 4


class TestEnvOverrides:
    def test_int_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "42")
        config = load_config(project_path=None, user_path=None)
        assert config.execution.max_parallel_tasks == 42

    def test_bool_true_coercion(self, monkeypatch):
        for val in ("true", "1", "yes", "True"):
            monkeypatch.setenv("AGENTHICC_HEADLESS", val)
            from agenthicc.config import _coerce_env
            assert _coerce_env(val) is True

    def test_bool_false_coercion(self, monkeypatch):
        from agenthicc.config import _coerce_env
        for val in ("false", "0", "no"):
            assert _coerce_env(val) is False

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        (tmp_path / "agenthicc.toml").write_text("[execution]\nmax_parallel_tasks = 5\n")
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "99")
        config = load_config(project_path=str(tmp_path / "agenthicc.toml"))
        assert config.execution.max_parallel_tasks == 99


class TestCliOverrides:
    def test_set_overrides_key(self, tmp_path):
        config = load_config(project_path=None, user_path=None,
                             cli_overrides=["execution.max_parallel_tasks=77"])
        assert config.execution.max_parallel_tasks == 77

    def test_set_multiple(self):
        config = load_config(project_path=None, user_path=None,
                             cli_overrides=["execution.max_parallel_tasks=10",
                                            "execution.agent_pool_size=5"])
        assert config.execution.max_parallel_tasks == 10
        assert config.execution.agent_pool_size == 5

    def test_cli_beats_env(self, monkeypatch):
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "20")
        config = load_config(project_path=None, user_path=None,
                             cli_overrides=["execution.max_parallel_tasks=99"])
        assert config.execution.max_parallel_tasks == 99
```

---

## Configuration Reference

```toml
# .agenthicc/agenthicc.toml — canonical project config location

[execution]
max_concurrent_intents = 10
max_parallel_tasks = 20
agent_pool_size = 30

[memory]
project_memory_path = ".agenthicc/memory"
vector_db = "sqlite"
session_ttl_seconds = 3600

[security]
default_action = "deny"
sandbox_mode = true
allowed_paths = ["."]
network_allow_list = []

[api]
host = "127.0.0.1"
port = 8000
api_key_env = "AGENTHICC_API_KEY"

[tools]
plugins = []

[hooks]
# intent.pre_validate = ["my_hooks.policy_check"]

[skills]
# enabled = ["web_search"]

# [skills.web_search]
# api_key = "${BRAVE_API_KEY}"
```

### Environment variable quick reference

```bash
# Override execution settings
export AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS=50
export AGENTHICC_EXECUTION_AGENT_POOL_SIZE=100

# Override memory path (useful in CI)
export AGENTHICC_MEMORY_PROJECT_MEMORY_PATH=/tmp/ci-memory

# API key for headless mode
export AGENTHICC_API_API_KEY=my-secret-key

# Skip ads (for Pro users who haven't run login)
export AGENTHICC_AUTH_TOKEN=my-graph-api-token

# Non-interactive mode
export AGENTHICC_HEADLESS=1
```

---

## Open Questions

1. **`.agenthicc/` in `.gitignore`**: should `agenthicc config init` also add `.agenthicc/` to `.gitignore` automatically? Risk: overwriting user customisation. Proposal: print a note recommending it, but never modify `.gitignore` without explicit `--update-gitignore` flag.

2. **`agenthicc.toml` at project root vs `.agenthicc/agenthicc.toml`**: teams who prefer a visible config at the project root should use `./agenthicc.toml`. Teams who prefer keeping the project root clean should use `.agenthicc/agenthicc.toml`. Both are supported. The docs should recommend `.agenthicc/agenthicc.toml` as the canonical form.

3. **User config merging**: should user config be ONLY for personal preferences (editor, history size) and NOT security or execution settings? Or should a user be allowed to globally set `max_parallel_tasks = 50` across all projects? Current proposal: all keys are valid at the user level, project config overrides user config.

4. **`AGENTHICC_HEADLESS=1` vs `--headless`**: should the env var also affect whether the API server starts? Currently `--headless` starts a JSON-lines stdin/stdout loop. This seems right — the API server is a separate concern.

5. **Windows `%USERPROFILE%`**: `Path.home()` works cross-platform. No special handling needed for Windows.
