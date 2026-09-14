# Installation

## Requirements

- **Python 3.11 or newer** (3.12 and 3.13 are exercised in CI)
- [`uv`](https://docs.astral.sh/uv/) for the recommended workflow
- An LLM provider: Anthropic (default), OpenAI, Ollama, or LiteLLM

## Install

```bash
git clone https://github.com/agenthicc/agenthicc.git
cd agenthicc
uv sync --extra dev
```

Then run it with `uv run agenthicc` or `uv run python -m agenthicc`.

## Verify the install

```bash
uv run agenthicc --version
```

```text
agenthicc 0.1.0
```

`--version` and `--help` are answered before command discovery, so they work
even when a project extension is broken.

## Configure a provider

```bash
# Anthropic (default)
export ANTHROPIC_API_KEY="sk-ant-..."

# OpenAI
agenthicc --set execution.provider=openai --set execution.model=gpt-4o

# Ollama (no API key needed)
agenthicc --set execution.provider=ollama --set execution.model=llama3.2
```

`--set KEY=VALUE` overrides configuration for a single run and can be repeated.

!!! warning "There is no `agenthicc config set`"
    The `config` group has exactly four subcommands: `show`, `validate`,
    `profiles`, and `init`. To persist a change, edit `agenthicc.toml` (or run
    `agenthicc config init` to scaffold one) — see
    [Configuration](02-configuration.md).

## Next

[Configuration →](02-configuration.md)

Depth: [Configuration guide](../guides/configuration.md) ·
[Quickstart](../guides/quickstart.md)
