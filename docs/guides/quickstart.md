# Quickstart

Get agenthicc running in under five minutes.

---

## Step 0 — Set your LLM API key

Agenthicc needs an LLM provider to run agents. Set the API key before launching:

```bash
# Anthropic Claude (default — required unless using Ollama)
export ANTHROPIC_API_KEY="sk-ant-api03-..."
```

For OpenAI or Ollama, see the [Configuration guide](configuration.md#llm--model-configuration).

---

## Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.12 | 3.13 also supported |
| [uv](https://docs.astral.sh/uv/) | 0.4+ | Recommended installer |

Verify your Python version:

```bash
python --version   # Python 3.12.x or 3.13.x
```

---

## Installation

### With uv (recommended)

```bash
uv add agenthicc
```

To include the terminal UI and REST API extras:

```bash
uv add "agenthicc[tui,api]"
```

### With pip

```bash
pip install "agenthicc[tui,api]"
```

Verify the installation:

```bash
python -m agenthicc --version
```

---

## Creating a configuration file

agenthicc reads `agenthicc.toml` from the current working directory (or the
path given by `$AGENTHICC_CONFIG`).  Create a minimal config:

```toml
# agenthicc.toml

[settings]
max_concurrent_intents = 5
max_parallel_tasks      = 10
agent_pool_size         = 15
snapshot_every_n_events = 50
event_log_path          = ".agenthicc/events.jsonl"
snapshot_path           = ".agenthicc/snapshot.json"

[security]
default_action = "allow"

# Optional: register a named agent type
[agents.researcher]
module = "myproject.agents.researcher"
class  = "ResearchAgent"
```

The `[settings]` table maps directly to `SystemSettings`.  All keys are
optional; the defaults shown above are used when omitted.

---

## First run

```bash
# TUI mode (requires the tui extra)
agenthicc

# or equivalently
python -m agenthicc
```

If `prompt_toolkit` is not installed, agenthicc falls back to **headless
mode** automatically, emitting one JSON line per kernel event to stdout.

To force headless mode explicitly:

```bash
agenthicc --headless
```

---

## What you will see on first launch

The full-screen TUI has three regions:

```
┌─────────────────────────────────────────────────────┐
│  transcript region  (scrolling, grows upward)       │
│                                                     │
│  [system] agenthicc ready. Session <id>             │
│  [system] 0 active agents                           │
├─────────────────────────────────────────────────────┤
│  0 agents | $0.000 | 0 tok                          │  ← status line
├─────────────────────────────────────────────────────┤
│ >                                                   │  ← input bar
└─────────────────────────────────────────────────────┘
```

**Transcript region** — every agent turn, tool result, and log message appears
here, auto-scrolled to the latest entry.

**Status line** — shows a live count of active agents, cumulative cost in USD,
and total tokens consumed.

**Input bar** — submit intents and slash commands here.

### Slash commands

| Command | Effect |
|---|---|
| `/status` | Opens the agent status overlay |
| `/history` | Shows the last 10 event log entries |

Press `Escape` to dismiss any overlay.  Press `Ctrl-C` to exit.

---

## Submitting your first intent

Type a natural-language instruction and press `Enter`:

```
> Summarise the README.md in three bullet points
```

agenthicc will:

1. Parse the intent and assign it a unique `intent_id`.
2. Emit an `IntentReceived` event to the kernel.
3. The planner decomposes the intent into a workflow DAG.
4. Nodes are dispatched to agents in the pool.
5. Results stream back into the transcript as `ApplicationLog` events.

---

## Understanding the output

Each line in the transcript follows this format:

```
[<agent_name>] <message>
```

For tool calls you will see:

```
[orchestrator] calling tool: summarise_file(path="README.md")
[orchestrator] tool result: {"summary": ["...", "...", "..."]}
```

Cost and token counts update on the status line after every model call.

---

## Running in headless mode

For CI pipelines or scripted usage, headless mode emits newline-delimited JSON:

```bash
echo '{"intent": "list files in /tmp"}' | agenthicc --headless
```

Each output line is a JSON object:

```json
{"ts": 1718000000.0, "event_type": "ApplicationLog", "event_id": "abc123",
 "payload": {"level": "INFO", "message": "...", "data": {}},
 "source_agent_id": "agent_xyz"}
```

---

## Replaying the event log

agenthicc persists every event to an append-only JSONL file
(`.agenthicc/events.jsonl` by default).  You can replay it to reconstruct
state after a crash:

```python
import asyncio
from agenthicc.kernel import AppState, restore_from_log

async def main():
    state = AppState.create()
    state = await restore_from_log(".agenthicc/events.jsonl", state)
    print(f"Restored {len(state.intents)} intents, {len(state.agents)} agents")

asyncio.run(main())
```

---

## Next steps

- [Memory guide](memory.md) — session, project, and global memory tiers
- [Lifecycle hooks](hooks.md) — intercept and recover from any execution stage
- [Kernel reference](../reference/kernel.md) — full `AppState`, event, and
  processor API
