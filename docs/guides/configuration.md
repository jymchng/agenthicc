# Configuration

Configuration is loaded by `agenthicc.config.load_config()` and converted into
typed settings dataclasses.

## File discovery and precedence

The effective configuration is merged in this order, from lowest to highest
priority:

1. built-in defaults;
2. the first existing user candidate: `~/.agenthicc/agenthicc.toml`,
   `~/.agenthicc/.agenthicc.toml`, or `~/.agenthicc.toml`;
3. the first existing project candidate: `.agenthicc/agenthicc.toml`,
   `.agenthicc/.agenthicc.toml`, `agenthicc.toml`, or `.agenthicc.toml`;
4. `AGENTHICC_<SECTION>_<FIELD>` environment variables and provider shortcut
   variables such as `OPENAI_MODEL`;
5. repeated CLI `--set section.field=value` overrides.

Tables merge recursively. Scalars and lists in a higher-priority layer replace
the lower value. Config files may use `extends` for explicit parent files;
cycles are rejected.

Inspect the result with:

```bash
uv run agenthicc config show
uv run agenthicc --set execution.provider=ollama config show
```

Review the source of `config.py` when adding a setting: a dataclass field is
not automatically loaded from TOML until `_dict_to_config()` handles it. This
is a known improvement item for tool and validation settings.

## Provider settings

```toml
[execution]
provider = "anthropic" # anthropic | openai | ollama | litellm
model = ""             # empty uses the provider default
base_url = ""          # useful for Ollama or compatible endpoints
api_key = ""           # prefer environment variables
```

Environment variables are safer for credentials:

| Provider | Key | Optional shortcuts |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` |
| OpenAI | `OPENAI_API_KEY` | `OPENAI_MODEL`, `OPENAI_BASE_URL` |
| Ollama | none | `OLLAMA_MODEL`, `OLLAMA_HOST` |
| LiteLLM | provider-specific | `LITELLM_MODEL` |

Current provider defaults are defined by `PROVIDER_DEFAULT_MODELS`; consult
the code rather than hard-coding them in support material.

## Execution

| Key | Default | Meaning |
|---|---:|---|
| `max_concurrent_intents` | 8 | Concurrent intent limit exposed to kernel settings |
| `max_parallel_tasks` | 4 | Workflow parallelism setting |
| `agent_pool_size` | 16 | Legacy/domain capacity setting still present in kernel settings |
| `max_agent_turns` | 200 | Agent-loop iteration cap |
| `auto_compact` | true | Enable proactive model-aware conversation compaction |
| `context_windows` | `{}` | Model id → context window under `[memory.context_windows]` |
| `prompt_cache` | true | Enable provider prompt-cache integration where supported |
| `file_cache` | true | Enable freshness-validated workspace file cache |
| `transport_max_retries` | 3 | Turn-level transport retry count |
| `transport_retry_base_delay_s` | 1.0 | Exponential retry base delay |
| `transport_retry_max_total_s` | 0 | Optional retry wall-clock ceiling |
| `llm_sdk_max_retries` | 2 | Provider SDK retry count |

The live usable context budget is derived from the resolved model window and
reservations; it is not a second independent `session_memory_max_tokens`
setting in the current configuration model.

## Tools and MCP

```toml
[tools]
allowed = ["read_file", "git_*"]
denied = ["delete_file"]
plugins = []
max_live_tool_calls = 5

[[tools.mcp_servers]]
name = "local-tools"
url = "python -m my_mcp_server"
transport = "stdio"
auto_connect = true
reconnect_attempts = 3
```

MCP tokens and URLs support `${ENV_VAR}` expansion. Available transports are
validated by the MCP bridge; remote servers must also pass network and trust
policy. For the current user-defined Python tool journey, including which
settings are and are not connected to direct TUI plugin execution, see the
[user-defined tools guide](tools.md).

## Memory and storage

```toml
[memory]
project_memory_path = ".agenthicc/memory"
vector_db = "sqlite-vec"
session_ttl_seconds = 86400

[memory.context_windows]
default = 128000
```

The current runtime also creates durable session journals and a project file
cache. Paths and retention are described in the [storage reference](../reference/storage.md).

## Security

```toml
[security]
sandbox_mode = true
allowed_paths = ["/absolute/path/to/project"]
network_allow_list = ["api.example.com"]
max_tool_cpu_seconds = 30
max_tool_memory_mb = 512
```

`WorkspaceView` resolves real paths, so `..` traversal, absolute escapes, and
symlink escapes are rejected. `NetworkGuard` permits exact hosts and
subdomains of an allow-listed domain. An empty network list blocks outbound
hosts when an explicit guard is used. The ordinary project-tool path does not
currently inject these boundaries into user callables automatically.

Security-bypassing flags such as `--dangerously-skip-permissions` are CLI-only
and are intentionally not persisted in TOML.

## API configuration status

`ApiSettings` remains in the configuration dataclasses for compatibility, but
there is no `src/agenthicc/api/` implementation in this checkout. Do not treat
`[api]` as a working server configuration until the API decision in PRD-138 is
implemented and tested.

## Adding a setting

1. Add the typed field and default to the relevant settings dataclass.
2. Parse it in `_dict_to_config()` and include env/CLI coercion if appropriate.
3. Define its security and precedence semantics.
4. Add merge, validation, and effective-value tests.
5. Update this table, README examples, `llms-full.txt`, and the changelog.
