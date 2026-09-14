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

For an OpenAI-compatible deployment such as Modal, define a profile so the
same endpoint and request options are used by direct turns and workflows:

```toml
[execution]
profile = "modal"

[providers.modal]
provider = "openai"
model = "moonshotai/Kimi-K3"
base_url = "https://your-endpoint.modal.run/v1"
api_key_env = "MODAL_API_KEY"
```

Then set `MODAL_API_KEY` and verify the connection settings without starting
an agent turn:

```bash
uv run agenthicc config profiles
uv run agenthicc config validate
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

Use `--mode MODE` to choose the initial runtime mode (`Safe`, `Plan`, or
`Yolo`; compatibility aliases are accepted) and `--workflow NAME` to select
the initial TUI workflow. An explicit workflow takes precedence over the
mode's default workflow. With `--headless`, `--workflow NAME` runs that
workflow for each non-empty stdin line, while `--mode` controls the mode
supplied to its runner.

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
uv run agenthicc sessions inspect SESSION_ID
uv run agenthicc sessions inspect SESSION_ID --json
uv run agenthicc sessions export SESSION_ID --output session-export.json
uv run agenthicc --continue
uv run agenthicc --resume SESSION_ID --mode Yolo --workflow code_plan
```

`--continue` resolves the latest session for the current directory. `--resume`
uses the given session id and can recover an interrupted direct turn through the
durable conversation journal. The export command creates a redacted JSON
support artifact containing the session's durable logs and metadata. Review
prompts, tool results, paths, and model output before sharing the file.
Inspection prints a safe operational summary of artifact health, corrupt lines,
token/cost totals, workflow status, and any incomplete turn; `--json` emits the
same summary for automation without including conversation or tool payloads.

Only one local agenthicc process may own a session at a time. If another
terminal is already using the selected session, `--continue`, `--resume`, or
Enter in `sessions list` exits quickly with `session_already_active` (exit code
3) before transcript or provider startup. Close or continue the session in the
reported process. A crashed process may be reclaimed automatically only when
its PID/process-start identity proves it is dead; an unknown or malformed
owner is deliberately protected.

## Next steps

- [TUI guide](tui.md)
- [Configuration](configuration.md)
- [Workflows](workflows.md)
- [Extensions](plugins.md)
- [Storage reference](../reference/storage.md)

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `agenthicc --version` prints something unexpected | A wrapper or an installed console script is being used instead of the checkout | Run `PYTHONPATH=src python -m agenthicc --version`; the literal expected string is `agenthicc 0.1.0` |
| Configuration appears to be ignored | A second config file, or CLI overrides applied in a different order | Check the resolved path with `agenthicc mcp list --json`, whose `path` field names the file actually read |
| A mode name is rejected in the TUI | `/mode` accepts the selectable names and their aliases | Use `Safe`, `Plan`, or `Yolo`, or an alias such as `auto`, `guard`, `ask`, `review`. `Replay` is internal and not selectable |
| A project command is unavailable | Project commands are discovered only on the normal session path | `--help` and `--version` deliberately skip discovery; run a normal session to see them |
| Session history is not where you left it | The session store is keyed by project root | Run from the same directory, or resume explicitly with `--resume ID` |
| A run starts a new session every time | `--continue` was not passed | `--continue` resumes the most recent session for the current directory |
| A permission prompt appears in a script | Safe mode with an operator-less caller | Headless denies by default; pass `--dangerously-skip-permissions` only inside a sandbox you control |
| Model or provider settings have no effect | The active profile or provider/model resolution disagrees with the config | Validate with `agenthicc config validate`, then `config show`; exact `[execution]` keys win over the library fallback |
| A custom workflow name is not found | It is not registered in the workflow table | Check the real list with `agenthicc workflows list`; eight builtins ship in the box |
| Generated files land in the wrong place | A relative working directory | Pass explicit paths; the process working directory is never changed by the runtime |

### Eight workflows, not five

The README's workflow table under-reports the registry. The builtin table
declares eight: `code_plan` (alias `Plan`), `copy_website`, `create_workflow`,
`goal_flow`, `make_agenthicc_tool`, `make_book`, `reconstruct_site`, and
`site_imitate`. Always confirm with a real listing rather than quoting a table.

### `--headless` without `--workflow`

This does not raise. It exits 0 and emits a readiness record with no
`workflow` key:

```bash
PYTHONPATH=src python -m agenthicc --headless < /dev/null
```

```text
{"status": "ready", "mode": "headless", "session_id": "4c783bb1ff244a66bf3243d28aead0da"}
```

`session_id` is a fresh identifier on every run — the keys, and the
`"status": "ready"` value, are what stay stable. Because no workflow was
selected, per-line records for stdin input are `IntentCreated`-class events,
not a `WorkflowRunCompleted` summary.
