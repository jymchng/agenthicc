# Configuration

agenthicc reads TOML with environment-variable and CLI overrides.

## Precedence, lowest to highest

1. Hardcoded defaults
2. `~/.agenthicc/agenthicc.toml` — user-global defaults
3. `.agenthicc/agenthicc.toml` — per-project overrides
4. `AGENTHICC_*` environment variables
5. `--set` / `--set-secret` CLI overrides

Project config always wins over user-global config, mirroring the Git
`~/.gitconfig` / `.git/config` layering model. Scalars are overwritten, lists
are replaced, and tables are merged recursively.

`--config PATH` selects an explicit file instead of searching.

### Files searched, in order

The search stops at the first match in each scope (`src/agenthicc/config.py:79-89`):

| Scope | Candidates |
|---|---|
| Project | `.agenthicc/agenthicc.toml`, `.agenthicc/.agenthicc.toml`, `agenthicc.toml`, `.agenthicc.toml` |
| User | `~/.agenthicc/agenthicc.toml`, `~/.agenthicc/.agenthicc.toml`, `~/.agenthicc.toml` |

!!! warning "There is no XDG location"
    Configuration does **not** live under `~/.config/agenthicc/`. The user scope
    is `~/.agenthicc/`.

## Managing config from the CLI

```bash
agenthicc config show          # print the resolved configuration
agenthicc config validate      # check the current configuration
agenthicc config profiles      # list provider profiles
agenthicc config init          # write a commented template
```

`config set` is not one of them. `config init` accepts `--force` to overwrite.

## Overrides

```bash
agenthicc --set section.key=value           # override any config key
agenthicc --set-secret section.key=ENV_VAR  # point a secret at an env var
agenthicc --config path/to/agenthicc.toml   # explicit config file
```

- `--set` can be repeated and takes a dotted `section.key`.
- `--set-secret` never stores the value: it records which environment variable
  to read at runtime.

## Provider profiles

A named connection is a `ProviderProfile` (`src/agenthicc/config.py:723`):

| Field | Meaning |
|---|---|
| `name` | Profile name, used to select it |
| `provider` | `anthropic` / `openai` / `ollama` / `litellm` |
| `model` | Model id |
| `base_url` | API base URL |
| `api_key` / `api_key_env` | Inline key, or the name of an env var |
| `default_headers` / `default_query` | Extra HTTP headers / query params |
| `client_options` / `request_options` | Extra client and per-request options |
| `timeout_s` | Request timeout for this profile |
| `max_retries` | Retry budget for this profile |
| `temperature` / `top_p` / `max_completion_tokens` | Sampling parameters |
| `protocol` | Wire protocol override |
| `capabilities` | Feature flags detected or declared for the provider |
| `session_header` | Header populated with the stable session conversation ID |

!!! warning "Do not confuse profile fields with execution fields"
    `timeout_s` on a **profile** has no default of its own; the
    `[execution]` table carries `timeout_s = 3600.0` plus
    `provider_capabilities`, `transport_max_retries`, and
    `transport_retry_base_delay_s`. A profile's `capabilities` are copied into
    `execution.provider_capabilities` when the profile is resolved. Set a value
    in the table that actually owns it.

!!! danger "`timeout_s` is the LLM timeout, not the turn timeout"
    `timeout_s` governs the provider request; `turn_timeout_s` governs a turn
    (default `0.0`, meaning no turn deadline). Set both deliberately, or
    neither. `transport_max_retries` (default 10),
    `transport_retry_base_delay_s` (default 1.0), and `llm_sdk_max_retries`
    (default 2) control retries.

## Secrets

`SecretReference` resolves a value from an environment variable at runtime. Use
`api_key_env` in a profile, or `--set-secret key=ENV_VAR` at the command line,
so keys never appear in `agenthicc.toml`.

## Sections

The recognised top-level sections are `execution`, `providers`, `behaviour`,
`loops`, `hooks`, `tools`, `memory`, `security`, `api`, `plugins`, `skills`,
`agents`, `storage`, and `workflows.<name>`.

### Recurring `/loop` prompts

The interactive TUI accepts a local, session-scoped recurring prompt:

```text
/loop 5m Check the deployment and fix a failing health check.
/loop status
/loop pause
/loop resume
/loop stop
```

The first iteration is due immediately, but dispatch waits for the session to
be idle. Missed intervals are coalesced and never create concurrent agent
turns. A repeated `/loop` replaces the existing loop. State is stored under
`~/.agenthicc/sessions/<session-id>/loop.json`; it is rehydrated only when the
session is explicitly resumed. Defaults expire loops after 72 hours and stop
after repeated scheduler failures. The settings below apply only to the local
TUI scheduler; setting `enabled = false` blocks creation and resume while
allowing status/stop/pause controls to inspect existing state. Override finite
bounds with:

```toml
[loops]
enabled = true
default_interval_s = 600
min_interval_s = 60
max_interval_s = 86400
max_age_s = 259200
max_prompt_bytes = 16384
max_consecutive_failures = 3
busy_poll_s = 1.0
persist = true
allow_slash_commands = true
```

Scheduled prompts reuse the existing conversation, workflow checkpoints,
approvals, tools, and security policy. Arbitrary shell payloads are not
accepted; slash-command payloads must name a registered command. Headless mode
does not schedule loops and emits the structured
`loop_interactive_required` result instead. Use `/loops` in the TUI to inspect
all persisted jobs, run one immediately, or delete one after confirmation.

!!! danger "`[behaviour]` keeps its British `-our`"
    The section key is `behaviour` in code. It is a config key, not prose, so it
    must stay byte-accurate in every example and migration note.

## Next

- [Your first task →](03-first-task.md)
- Depth: [Configuration guide](../guides/configuration.md)
