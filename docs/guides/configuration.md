# Configuration Reference

Agenthicc loads configuration from TOML files using a three-layer deep-merge:

1. **Hardcoded defaults** (lowest priority)
2. **`agenthicc.toml`** in the current working directory
3. **`~/.agenthicc.toml`** in the user's home directory (highest priority)

Scalars and lists in later files replace the corresponding values in earlier ones.
Nested tables are merged recursively.

---

## Loading config in code

```python
from agenthicc.config import load_config

cfg = load_config()                          # auto-discovers agenthicc.toml
cfg = load_config(project_path="myproj.toml")
cfg = load_config(user_path="/etc/agenthicc.toml")
```

`load_config` returns an `AgenthiccConfig` instance. Missing files are silently
skipped. Invalid TOML raises `tomllib.TOMLDecodeError`.

---

## Complete annotated agenthicc.toml

```toml
# ──────────────────────────────────────────────────────────
# [execution] — concurrency and pool sizing
# ──────────────────────────────────────────────────────────

[execution]

# Maximum number of intents in the running state simultaneously.
# Default: 8
max_concurrent_intents = 8

# asyncio.Semaphore value for DAGExecutor — max workflow nodes running in parallel.
# Default: 4
max_parallel_tasks = 4

# Maximum total agents in the AgentPool (idle + busy).
# Default: 16
agent_pool_size = 16

# ──────────────────────────────────────────────────────────
# [tools] — tool allow/deny lists and MCP servers
# ──────────────────────────────────────────────────────────

[tools]

# Tool names (fnmatch patterns) that are always allowed regardless of policy.
# Default: [] (empty — use security.permission_rules instead for fine-grained control)
allowed = ["application_log", "application_ui_update"]

# Tool names (fnmatch patterns) that are always denied.
# Default: [] (empty)
denied = ["shell_exec", "network_fetch"]

# Plugins to load (Python dotted paths to module or class).
# Default: []
plugins = ["myapp.plugins.custom_tools"]

# MCP server configurations (list of tables).
# Each table is passed to McpServerConfig.
# Default: []
mcp_servers = [
    { alias = "filesystem", command = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"] },
]

# ──────────────────────────────────────────────────────────
# [memory] — storage paths and session TTL
# ──────────────────────────────────────────────────────────

[memory]

# Directory for project-tier SQLite databases and artifacts.
# Default: ".agenthicc/memory"
project_memory_path = ".agenthicc/memory"

# Vector database backend. Currently only "sqlite-vec" is supported.
# Default: "sqlite-vec"
vector_db = "sqlite-vec"

# TTL in seconds for session-tier cache entries.
# Default: 86400 (24 hours)
session_ttl_seconds = 3600

# ──────────────────────────────────────────────────────────
# [security] — sandbox and network controls
# ──────────────────────────────────────────────────────────

[security]

# Enable WorkspaceView path-prefix sandbox.
# When true, all file tool operations are restricted to allowed_paths.
# Default: true
sandbox_mode = true

# List of path prefixes that file tools may access.
# Default: ["/workspace"]
allowed_paths = ["/workspace", "/tmp/agenthicc"]

# List of hostnames that network tools may connect to.
# Empty list blocks all outbound connections.
# Default: [] (block all)
network_allow_list = ["api.github.com", "registry.npmjs.org"]

# Maximum CPU seconds a single tool call may consume.
# Default: 30
max_tool_cpu_seconds = 30

# Maximum memory in MB a single tool call may allocate.
# Default: 512
max_tool_memory_mb = 512

# ──────────────────────────────────────────────────────────
# [api] — headless REST+WebSocket server
# ──────────────────────────────────────────────────────────

[api]

# Bind address for the FastAPI server.
# Default: "127.0.0.1"
host = "127.0.0.1"

# Listen port.
# Default: 8000
port = 8000

# Environment variable name that holds the API key.
# The server reads os.environ[api_key_env] at startup.
# Default: "AGENTHICC_API_KEY"
api_key_env = "AGENTHICC_API_KEY"

# ──────────────────────────────────────────────────────────
# [hooks] — static lifecycle hook registration
# ──────────────────────────────────────────────────────────
# Keys: <entity_type>.<stage>.handlers = [dotpaths]
# entity_type: tool name or "*" (all tools)
# stage: pre_execute | post_execute | on_error

[hooks.file_write.pre_execute]
handlers = ["myapp.hooks.FileWriteAuditHook"]

[hooks.file_write.post_execute]
handlers = ["myapp.hooks.FileWriteAuditHook"]

[hooks.file_write.on_error]
handlers = ["myapp.hooks.RetryOnTimeoutHook"]

[hooks."*".pre_execute]
handlers = ["myapp.hooks.RateLimitHook"]
```

---

## Section reference

### `[execution]`

| Key | Type | Default | Description |
|---|---|---|---|
| `max_concurrent_intents` | int | 8 | Max intents with status `running` simultaneously |
| `max_parallel_tasks` | int | 4 | `asyncio.Semaphore` value in `DAGExecutor` |
| `agent_pool_size` | int | 16 | Max agents in `AgentPool` |

### `[tools]`

| Key | Type | Default | Description |
|---|---|---|---|
| `allowed` | list[str] | `[]` | fnmatch patterns always allowed |
| `denied` | list[str] | `[]` | fnmatch patterns always denied |
| `plugins` | list[str] | `[]` | Python module dotpaths to load as plugins |
| `mcp_servers` | list[dict] | `[]` | MCP server configurations |

### `[memory]`

| Key | Type | Default | Description |
|---|---|---|---|
| `project_memory_path` | str | `".agenthicc/memory"` | Root dir for project SQLite + artifacts |
| `vector_db` | str | `"sqlite-vec"` | Vector backend identifier |
| `session_ttl_seconds` | int | 86400 | TTL for session-tier cache entries |

### `[security]`

| Key | Type | Default | Description |
|---|---|---|---|
| `sandbox_mode` | bool | `true` | Enforce `WorkspaceView` path restrictions |
| `allowed_paths` | list[str] | `["/workspace"]` | Path prefix whitelist for file tools |
| `network_allow_list` | list[str] | `[]` | Hostname whitelist for `NetworkGuard` |
| `max_tool_cpu_seconds` | int | 30 | Per-tool CPU timeout |
| `max_tool_memory_mb` | int | 512 | Per-tool memory limit in MB |

### `[api]`

| Key | Type | Default | Description |
|---|---|---|---|
| `host` | str | `"127.0.0.1"` | API server bind address |
| `port` | int | 8000 | API server port |
| `api_key_env` | str | `"AGENTHICC_API_KEY"` | Env var that holds the Bearer token |

### `[hooks]`

Nested TOML tables with the pattern `[hooks.<entity_type>.<stage>]`:

| Key | Type | Description |
|---|---|---|
| `handlers` | list[str] | Dotted import paths to `LifecycleHook` subclasses |

`entity_type` is either a specific tool name or `"*"` (all tools).
`stage` is one of `pre_execute`, `post_execute`, `on_error`.

---

## Using config in code

```python
from agenthicc.config import load_config
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.api.server import create_app
import os

cfg = load_config()

# Build kernel with typed settings
state = AppState.create(
    settings=cfg.to_system_settings(),
    policy=cfg.to_security_policy(),
)
proc = EventProcessor(initial_state=state, persist=True)

# Read API key from env var
api_key = os.environ.get(cfg.api.api_key_env)
app = create_app(proc, api_key=api_key)
```

---

## Deep-merge behaviour

Given a project `agenthicc.toml`:

```toml
[execution]
max_parallel_tasks = 4

[security]
allowed_paths = ["/workspace"]
```

And a user `~/.agenthicc.toml`:

```toml
[execution]
max_parallel_tasks = 8      # overrides project value

[security]
network_allow_list = ["api.github.com"]  # added (merge); allowed_paths unchanged
```

Result after merge:

```python
cfg.execution.max_parallel_tasks  # 8  (user wins)
cfg.security.allowed_paths        # ["/workspace"]  (project value preserved)
cfg.security.network_allow_list   # ["api.github.com"]  (user value added)
```

**Scalars and lists in user file replace project file values. Nested tables are
merged key-by-key.** `deep_merge` is exported from `agenthicc.config` for
programmatic use.

---

## LLM / Model Configuration

Agenthicc uses **[lauren-ai](https://github.com/lauren-framework/lauren-ai)** as its
LLM layer. You can use any supported provider — Anthropic, OpenAI, Ollama (local), or
LiteLLM — by setting `provider` and `model` in your config.

### Provider configuration

```toml
# .agenthicc/agenthicc.toml
[execution]
provider = "anthropic"             # anthropic | openai | ollama | litellm
model    = "claude-sonnet-4-6"     # empty → uses provider default
api_key  = ""                      # empty → reads from env var (see below)
base_url = ""                      # only needed for Ollama or self-hosted
```

### Provider reference

| Provider | `provider` value | Required env var | Default model |
|----------|-----------------|------------------|--------------|
| Anthropic | `"anthropic"` | `ANTHROPIC_API_KEY` | `claude-opus-4-8` |
| OpenAI | `"openai"` | `OPENAI_API_KEY` | `gpt-4o` |
| Ollama (local) | `"ollama"` | — (no key needed) | `llama3.2` |
| LiteLLM | `"litellm"` | `ANTHROPIC_API_KEY` (or provider's key) | `anthropic/claude-opus-4-8` |

### Quick setup by provider

=== "Anthropic (default)"

    ```bash
    export ANTHROPIC_API_KEY="sk-ant-api03-..."
    agenthicc
    ```

=== "OpenAI"

    ```bash
    export OPENAI_API_KEY="sk-..."
    agenthicc --set execution.provider=openai --set execution.model=gpt-4o
    ```

=== "Ollama (local)"

    ```bash
    # 1. Start Ollama
    ollama serve
    ollama pull llama3.2

    # 2. Run agenthicc
    agenthicc --set execution.provider=ollama --set execution.model=llama3.2
    ```

=== "LiteLLM"

    ```bash
    pip install litellm
    export ANTHROPIC_API_KEY="sk-ant-..."   # or whichever backend litellm routes to
    agenthicc --set execution.provider=litellm --set execution.model=anthropic/claude-sonnet-4-6
    ```

### Changing provider/model at runtime

Use the `/model` slash command in the TUI:

```
> /models                              # list all providers + API key status
> /model openai gpt-4o-mini            # switch to OpenAI gpt-4o-mini
> /model anthropic claude-sonnet-4-6   # switch to Anthropic Sonnet
> /model ollama llama3.2               # switch to local Ollama
```

Or via CLI:

```bash
agenthicc --set execution.provider=openai --set execution.model=gpt-4o-mini
```

### Persisting your provider choice

Add to `.agenthicc/agenthicc.toml`:

```toml
[execution]
provider = "openai"
model    = "gpt-4o-mini"
```

User config (`~/.agenthicc/agenthicc.toml`) overrides project config — set your
personal default provider there and it will apply across all projects.
