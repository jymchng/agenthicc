# Installation

## Requirements

- **Python 3.11 or newer** (3.12 and 3.13 are exercised in CI)
- [`uv`](https://docs.astral.sh/uv/) for the recommended development workflow
- An LLM provider: Anthropic (default), OpenAI, Ollama, or LiteLLM

## Install

```bash
git clone https://github.com/jymchng/agenthicc.git
cd agenthicc
uv sync --extra dev
```

You can then run it with `uv run agenthicc` (or `uv run python -m agenthicc`).

## Verify the install

```bash
uv run agenthicc --version
# agenthicc 0.1.0
```

## Configure a provider

```bash
# Anthropic (default)
export ANTHROPIC_API_KEY="sk-ant-..."

# OpenAI
uv run agenthicc --set execution.provider=openai --set execution.model=gpt-4o

# Ollama (no API key needed)
uv run agenthicc --set execution.provider=ollama --set execution.model=llama3.2
```

The `--set KEY=VALUE` flag overrides config for a single run and can be
repeated. To persist changes, use `agenthicc config set` or edit
`agenthicc.toml` (see [Configuration](02-configuration.md)).

## Next

[Configuration →](02-configuration.md)
