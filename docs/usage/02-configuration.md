# Configuration

agenthicc reads a TOML config file with environment-variable and `--set`
overrides. This guide documents the surface verified against
`src/agenthicc/config.py`.

## Config file locations (precedence, highest first)

1. A config file passed with `--config <path>`
2. `.agenthicc/agenthicc.toml` in the project directory
3. `agenthicc.toml` in the project directory
4. User config (`~/.config/agenthicc/agenthicc.toml`)
5. Built-in defaults

## Managing config from the CLI

```bash
agenthicc config show          # print the resolved config
agenthicc config validate      # validate the current config
agenthicc config profiles      # show provider profiles
agenthicc config init          # create a commented config template
```

(Verified: `config show | validate | profiles | init` in
`src/agenthicc/cli/commands/config.py`.)

## CLI overrides

```bash
agenthicc --set section.key=value        # override any config key
agenthicc --set-secret section.key=ENV_VAR  # set a secret from an env var
agenthicc --config path/to/agenthicc.toml  # explicit config file
```

- `--set KEY=VALUE` can be repeated and overrides a dotted key.
- `--set-secret KEY=ENV_VAR` points a secret key at an environment variable
  (never put secrets in the config file).

(Verified in `src/agenthicc/cli/parser.py`.)

## Provider profiles

Provider connections are modeled as profiles (`ProviderProfile`):

| Field | Meaning |
|---|---|
| `provider` | `anthropic` / `openai` / `ollama` / `litellm` |
| `model` | Model id |
| `base_url` | API base URL |
| `api_key` / `api_key_env` | Inline key or env var name |
| `default_headers` / `default_query` | Extra HTTP headers / query params |
| `timeout_s` / `max_retries` | Request timeout / retry count |
| `temperature` / `top_p` / `max_completion_tokens` | Sampling params |
| `request_options` | Extra body/headers/query per request |
| `capabilities` | Feature flags |

(Verified: `ProviderProfile` and `RequestOptionSettings` in
`src/agenthicc/config.py`.)

## Secrets

`SecretReference` resolves a config value from an environment variable at
runtime. Use `api_key_env` in a profile or `--set-secret key=ENV_VAR` so keys
never appear in `agenthicc.toml`.

## Environment variables

- `ANTHROPIC_API_KEY` — default Anthropic key
- `OPENAI_API_KEY` — default OpenAI key

## Next

[Your first task →](03-first-task.md)
