# Quickstart

This guide gets a local checkout to a first TUI or headless run.

## Prerequisites

- Python 3.11+; CI exercises 3.12 and 3.13.
- `uv`.
- An Anthropic/OpenAI/LiteLLM credential, or a running Ollama server.

## Install

```bash
git clone https://github.com/agenthicc/agenthicc.git
cd agenthicc
uv sync --extra dev
```

The current `pyproject.toml` declares `cloud` and `dev` extras. There is no
separate `tui` or `api` extra in this checkout.

## Configure a provider

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or use an alternate provider:

```bash
export OPENAI_API_KEY="sk-..."
uv run agenthicc --set execution.provider=openai --set execution.model=gpt-4o

# Ollama: no API key
uv run agenthicc --set execution.provider=ollama --set execution.model=llama3.2
```

## Launch the TUI

```bash
uv run agenthicc
```

The session creates a durable id, loads configuration and extensions, starts a
kernel processor, mounts a Rich workspace, and waits for input. Enter a normal
sentence to start a turn. Use `/help` for commands.

If standard input is not an interactive terminal, the input backend exits
cleanly. Use `--headless` for a pipeline instead.

## Headless mode

Headless mode reads one intent per non-empty stdin line:

```bash
printf '%s\n' 'list the top-level source packages' | uv run agenthicc --headless
```

Example output has a ready record followed by intent status:

```json
{"status": "ready", "mode": "headless"}
{"event_type": "IntentCreated", "intent_id": "...", "status": "pending"}
```

This runner is intentionally minimal. The interactive TUI session constructs
the full workflow/agent/tool stack; headless mode is currently best treated as
a deterministic stdin/kernel smoke interface.

## Create a project config

```bash
uv run agenthicc config init
```

This writes `.agenthicc/agenthicc.toml`. A small safe starting point is:

```toml
[execution]
provider = "anthropic"
max_parallel_tasks = 4
auto_compact = true

[memory]
project_memory_path = ".agenthicc/memory"

[security]
sandbox_mode = true
allowed_paths = ["/absolute/path/to/this/project"]
network_allow_list = []
```

Use the actual project path. `/workspace` is only a conventional default in
the dataclass and may not contain your checkout.

## Sessions

```bash
uv run agenthicc sessions list
uv run agenthicc sessions show SESSION_ID
uv run agenthicc sessions export SESSION_ID --output session-export.json
uv run agenthicc --continue
uv run agenthicc --resume SESSION_ID
```

`--continue` resolves the latest session for the current directory. `--resume`
uses the given session id and can recover an interrupted direct turn through the
durable conversation journal. The export command creates a redacted JSON
support artifact containing the session's durable logs and metadata. Review
prompts, tool results, paths, and model output before sharing the file.

## Next steps

- [TUI guide](tui.md)
- [Configuration](configuration.md)
- [Workflows](workflows.md)
- [Extensions](plugins.md)
- [Storage reference](../reference/storage.md)
