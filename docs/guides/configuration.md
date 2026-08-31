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
uv run agenthicc config profiles
uv run agenthicc config validate
```

For one-off secret-backed settings, use `--set-secret PATH=ENV_VAR`. The right
side is an environment-variable name; its value is resolved only when the
provider client is built. This keeps the credential out of the command-line
arguments, configuration output, checkpoints, and cassettes:

```bash
export MODAL_KEY="..."
uv run agenthicc \
  --set-secret execution.default_headers.Modal-Key=MODAL_KEY
```

`--set-secret` supports nested paths such as
`execution.request_options.extra_headers.Modal-Key` and
`execution.api_key`. Missing variables and malformed paths fail validation
with an actionable error. Existing `--set` remains a plaintext value
override and is unchanged.

Review the source of `config.py` when adding a setting: a dataclass field is
not automatically loaded from TOML until `_dict_to_config()` handles it. This
is a known improvement item for tool and validation settings.

## Generated project template

`agenthicc init` creates `.agenthicc/.agenthicc.toml`. It is intentionally
fully commented, so the file parses as an empty TOML document and cannot
silently change runtime behavior. The template includes the typed scalar
settings and commented examples for dynamic tables such as:

- `[providers."profile-name"]` and its headers/request options;
- `[[tools.mcp_servers]]`;
- `[memory.context_windows]`;
- `[agents."agent-name"]` and `[workflows."workflow-name"]`;
- `[storage.s3.mounts."mount-name"]`; and
- hook tables.

Uncomment and edit only the settings needed by the project. Existing files are
preserved by `agenthicc init`; pass `--force` only when replacement is
intentional. The legacy `agenthicc config init` command remains available for
projects that use the older active `.agenthicc/agenthicc.toml` path.

## Provider settings

```toml
[execution]
provider = "anthropic" # anthropic | openai | ollama | litellm
model = ""             # empty uses the provider default
base_url = ""          # useful for Ollama or compatible endpoints
api_key = ""           # prefer environment variables
```

For repeatable deployments, use a named profile. Profiles are the portable
connection boundary for workflows: `code_plan`, `create_workflow`, custom
workflows, and spawned subagents inherit the active profile without workflow
code needing provider-specific branches.

```toml
[execution]
profile = "modal_kimi"
max_output_tokens = 32768
transport_max_retries = 3
llm_sdk_max_retries = 2

[providers.modal_kimi]
provider = "openai"                 # OpenAI-compatible, including Modal
model = "moonshotai/Kimi-K3"
base_url = "https://your-endpoint.modal.run/v1"
api_key_env = "MODAL_API_KEY"
timeout_s = 3600.0                   # one-hour provider request deadline
temperature = 0.3
top_p = 0.95
max_completion_tokens = 16384

[providers.modal_kimi.default_headers]
"Modal-Key" = { env = "MODAL_KEY" }

[providers.modal_kimi.request_options.provider]
reasoning_effort = "none"

[providers.modal_kimi.request_options.extra_body]
vendor_trace = true
```

`provider = "openai"` selects lauren-ai's OpenAI transport; no Modal SDK is
required. `base_url` can point at any compatible gateway, vLLM server, or
private endpoint. Secret values should use `{ env = "NAME" }` references (or
the provider's `api_key_env`), never TOML literals. `config show` redacts
literal secrets and displays only environment-variable names. `config validate`
checks the selected profile and resolves required environment variables without
printing their values.

`request_options` maps to lauren-ai 1.4's request-scoped options:
`provider` for vendor fields, `extra_body` for compatible JSON body extensions,
`extra_headers` and `extra_query` for per-request additions, plus optional
`timeout_s`, `max_retries`, and `include_raw_response`. `default_headers`,
`default_query`, and `client_options` configure the underlying provider client.
Profile secrets are resolved again when a resumed session starts; workflow
checkpoints store only the profile name and never store resolved credentials.
OpenAI-compatible endpoints may omit token-usage fields; spawned subagents
normalize those unavailable counts so the response remains usable instead of
failing with an `int + NoneType` error.

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
| `authoring_max_generation_attempts` | 20 | Maximum complete source-generation attempts for `create_*` authoring |
| `authoring_max_phase_turns` | 20 | Maximum agent sub-turns in one `create_*` phase; phase definitions may request less |
| `max_output_tokens` | 32768 | Completion-token ceiling for one LLM round-trip |
| `timeout_s` | 3600 | Provider request timeout in seconds; applies to the active profile unless overridden |
| `turn_timeout_s` | 0 | Per-turn watchdog in seconds; zero disables it |
| `auto_compact` | true | Enable proactive model-aware conversation compaction |
| `context_windows` | `{}` | Model id → context window under `[memory.context_windows]` |
| `prompt_cache` | true | Enable provider prompt-cache integration where supported |
| `file_cache` | true | Enable freshness-validated workspace file cache |
| `transport_max_retries` | 3 | Transient provider-stream retry count; preserves the latest committed/current conversation prefix |
| `transport_retry_base_delay_s` | 1.0 | Exponential transport-retry base delay; provider `retry_after` hints are honored |
| `transport_retry_max_total_s` | 0 | Optional provider-step retry wall-clock ceiling |
| `llm_sdk_max_retries` | 2 | Provider SDK retry count |

The live usable context budget is derived from the resolved model window and
reservations; it is not a second independent `session_memory_max_tokens`
setting in the current configuration model. `max_output_tokens` is one of those
reservations, so raising it shrinks the live window by the same amount.

`max_output_tokens` must be larger than the biggest single tool call a turn can
make. lauren-ai's own default is 4096, which is not enough for a `write_file`
carrying a whole source file: the provider truncates the completion mid-argument,
the partial tool call is discarded, the sub-turn produces nothing at all, and the
calling phase retries with no visible cause. agenthicc therefore defaults to
32768 and passes the value through explicitly. When a response is truncated the
session prints a friendly notice explaining how to split the file: use
`write_file` for the first chunk, `append_file` for subsequent chunks, and then
read the file to verify it.

```toml
[execution]
max_output_tokens = 32768   # raise for a model that supports a larger completion
```

For very large files, prefer a chunked write — `write_file` for the first chunk,
`append_file` for the rest — rather than relying on a high ceiling.

For project workflow definitions and per-phase model overrides, see the
[custom workflows and TOML configuration guide](custom-workflows-and-config.md).

## Tools and MCP

```toml
[tools]
allowed = ["read_file", "git_*"]
denied = ["delete_file"]
plugins = []
max_live_tool_calls = 5
group_exploratory_calls = true  # presentation-only grouping of marked reads

[[tools.mcp_servers]]
name = "local-tools"
transport = "stdio"
command = ["python", "-m", "my_mcp_server"]
cwd = "."
auto_connect = true
reconnect_attempts = 3
startup_timeout_s = 10
tool_timeout_s = 60
enabled_tools = ["read_file", "list_directory"]
disabled_tools = ["write_file"]

[[tools.mcp_servers]]
name = "remote-search"
transport = "streamable_http"
url = "https://mcp.example.test/mcp"
auto_connect = false

[tools.mcp_servers.env_headers]
Authorization = "MCP_TOKEN"
```

MCP tokens, command arguments, environment values, headers, and URLs support
approved `${ENV_VAR}` references. New stdio entries use an argv array and never
invoke a shell; legacy `url = "python -m my_mcp_server"` strings remain
compatible and are split without `shell=True`. Available transports are
validated by the MCP bridge; remote servers pass the configured network policy
before connecting. Discovered tools are cached in one session-scoped catalog,
filtered deterministically, and shared by normal chat, Plan mode, workflows,
subagents, and headless runs. A list-change notification or `/mcp refresh NAME`
replaces only the affected server's catalog. For the current user-defined
Python tool journey, including which settings are and are not connected to
direct TUI plugin execution, see the
[user-defined tools guide](tools.md).
The internal identity `mcp:<server>:<tool>` is retained for MCP routing;
the provider-facing schema uses a valid equivalent such as
`mcp_database-dev_create_row`.

MCP support is optional. Install it only for projects that configure MCP
servers. The extra includes the stdio client plus the optional HTTP/SSE and
WebSocket transport adapters; it does not install MCP dependencies for a base
agenthicc installation:

```bash
pip install 'agenthicc[mcp]'
# or, in a uv checkout:
uv sync --extra mcp
```

Without this extra, configured servers remain visible in diagnostics and fail
with an actionable optional-dependency status; ordinary agenthicc sessions do
not require the MCP client package.

## Optional CloakBrowser integration

CloakBrowser is not a base dependency. Install it only when browser tools are
needed:

```bash
pip install 'agenthicc[cloakbrowser]'
# or, in a uv checkout:
uv sync --extra cloakbrowser
```

The feature is enabled at the configuration layer by default and browser
destinations are liberal by default for local VPS and sandbox use: localhost,
private addresses, arbitrary HTTP(S) hosts, and all ports are allowed. Set
`allow_all_domains = false` and provide an allow-list when a narrower policy
is required. The package import is lazy, so a base installation still reports
a safe `dependency_missing` status instead of failing session startup.

```toml
[tools.cloakbrowser]
enabled = true
transport = "local"             # local or loopback-only cdp
allowed_domains = []
allow_all_domains = true      # localhost, private addresses, all HTTP(S) hosts/ports
headless = true
max_pages = 4
max_actions_per_turn = 20
max_snapshot_chars = 20000
max_screenshot_bytes = 10000000
```

`allowed_domains` accepts the existing bare-host form and HTTP(S) origin
forms, including an optional port:

```toml
allowed_domains = [
  "https://example.com",
  "https://*.example.org:8443",
]
```

A bare host such as `example.com` allows that host and its subdomains on the
configured HTTP(S) ports. A full origin matches its scheme and port exactly;
the leading `*.` form allows subdomains but not the root host. Paths,
credentials, queries, fragments, and an unrestricted `*` are rejected. The
DNS resolution and HTTP(S)-only validation still apply to every destination.
With the default `allow_all_domains = true`, loopback and private-address
checks are intentionally disabled for this local/sandbox deployment profile.

## Optional Playwright integration

Playwright is an alternative browser backend using Microsoft's official
[`playwright-python`](https://github.com/microsoft/playwright-python) package.
Install it only when this backend is selected:

```bash
pip install 'agenthicc[playwright]'
playwright install chromium
# or, in a uv checkout:
uv sync --extra playwright
uv run playwright install chromium
```

When running the checkout from another uv project (for example, a sibling
`python-password-generator` checkout), `uv sync --extra playwright` selects
that project's extras and therefore cannot see agenthicc's extra. Use an
editable requirement for agenthicc instead:

```bash
uv run --no-project --with-editable '../agenthicc[playwright]' playwright install chromium
OPENAI_API_KEY='...' OPENAI_MODEL='...' OPENAI_BASE_URL='...' \
  uv run --no-project --with-editable '../agenthicc[playwright]' agenthicc --continue
```

The same pattern works with the `cloakbrowser` extra by replacing
`playwright` in both commands. The path is resolved relative to the
consumer-project directory.

Select it explicitly; only one browser backend is exposed to the agent session:

```toml
[tools]
browser_backend = "playwright"  # cloakbrowser, playwright, or none

[tools.playwright]
enabled = true
browser_type = "chromium"        # chromium, firefox, or webkit
allowed_domains = []
allow_all_domains = true
headless = true
max_pages = 4
max_actions_per_turn = 20
max_snapshot_chars = 20000
max_screenshot_bytes = 10000000
allow_persistent_profiles = false
```

Playwright supports the same bare-host and exact HTTP(S) origin formats as
CloakBrowser, including wildcard subdomains and explicit ports. The default
`allow_all_domains = true` permits localhost, private addresses, and every
HTTP(S) host and port. Set it to `false` to enforce hostname matching and
private-address protection. The browser executable and profile path are
operator configuration; they are never supplied by the model.

The browser tools remain available without Playwright installed, but return a
structured `dependency_missing` result. Playwright browser binaries are not
downloaded automatically.

CDP uses the fixed loopback endpoint `http://127.0.0.1:9222`; it cannot be
selected by an agent. Persistent profiles are disabled by default. Do not put
license keys in TOML; the configured `license_key_env` names the environment
variable read by the browser adapter.

The wrapper and its browser binary are third-party software with their own
platform support, download, and licensing terms. Install the pinned
`cloakbrowser==0.5.3` package and any required browser runtime only from the
official distribution channels, and use the tools only against sites and
accounts the operator is authorized to access. Static HTTP tools and browser
tools are separate surfaces: enabling this extra does not broaden ordinary
HTTP or shell-tool permissions.

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

### Resume transcript size

Large sessions do not replay their entire visual transcript synchronously at
startup. By default, resume displays the newest 20 complete turns by reading
the tail of `conversation.jsonl`:

```toml
[behaviour]
resume_transcript_turns = 20
```

Use `0` to restore the legacy full-transcript replay. This setting only affects
the TUI transcript and input-history projection; the provider conversation,
durable journal, usage records, and workflow state remain complete.

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
