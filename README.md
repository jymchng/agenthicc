# agenthicc

`agenthicc` is a state-driven agent runtime for software-engineering work. It
runs agent turns in the current project, exposes filesystem/git/command tools,
supports configurable workflows and modes, and keeps durable session records
for inspection and resume.

The current product surfaces are:

- a Rich Live terminal workspace with approvals, overlays, modes, slash
  commands, workflow progress, and a pinned composer;
- a headless stdin interface that emits JSON-lines;
- an event-sourced kernel with immutable domain state and JSONL persistence;
- workflow, agent, tool, skill, mode, command, and MCP extension registries;
- session, project, and global memory plus durable conversation journaling;
- model-aware context budgeting, compaction, transport retries, and tool-result
  replay for interrupted turns.

The REST/WebSocket API and the older prompt-toolkit `tui.app` API are not part
of the current source tree. They are tracked as product decisions in
[`PRD-138`](./prds/prd-138-repository-improvement-roadmap.md), not as supported
interfaces.

## Requirements

- Python 3.11 or newer (`3.12` and `3.13` are exercised in CI)
- [`uv`](https://docs.astral.sh/uv/) for the recommended development workflow
- An LLM provider: Anthropic, OpenAI, Ollama, or LiteLLM, as configured

## Install from a checkout

```bash
git clone https://github.com/agenthicc/agenthicc.git
cd agenthicc
uv sync --extra dev
```

The package exposes both `agenthicc` and `python -m agenthicc` entry points:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
uv run agenthicc
# equivalent:
uv run python -m agenthicc
```

The package metadata currently declares `cloud` and `dev` extras. Do not use
the undocumented `tui` or `api` extras from older documentation; dependency
and packaging cleanup is tracked in PRD-138.

## Provider configuration

Anthropic is the default provider. Set one provider's credentials before
starting a real agent turn:

```bash
# Anthropic
export ANTHROPIC_API_KEY="sk-ant-..."

# OpenAI
export OPENAI_API_KEY="sk-..."
uv run agenthicc --set execution.provider=openai --set execution.model=gpt-4o

# Ollama needs no API key
uv run agenthicc --set execution.provider=ollama --set execution.model=llama3.2
```

You can set the provider, model, base URL, and execution options in
`.agenthicc/agenthicc.toml`, `agenthicc.toml`, or a user config file. See the
[configuration guide](./docs/guides/configuration.md) for precedence and the
supported settings.

## First run

Create a project config if desired:

```bash
uv run agenthicc config init
```

Bootstrap project-specific agent guidance with a reviewable local proposal:

```bash
uv run agenthicc init
uv run agenthicc init --write
```

The first command previews an `AGENTS.md` diff. Existing guidance is preserved
and requires `--write --force` for an explicit update. See the
[project bootstrap guide](./docs/guides/project-bootstrap.md).

Then launch the terminal workspace and enter a natural-language request:

```text
> inspect the authentication module, propose a safe refactor, and run its tests
```

The default session discovers built-in and project-local workflows, agents,
tools, skills, modes, and MCP servers. Writes, command execution, and network
access are subject to mode restrictions, capability metadata, and approval
settings. Review the [user-defined tools guide](./docs/guides/tools.md) before
adding executable project plugins; it documents the current sandbox and trust
boundaries.

For a non-interactive process, use headless mode. It prints a ready record and
one JSON line for each non-empty input line:

```bash
printf '%s\n' 'summarise the repository' | uv run agenthicc --headless
printf '%s\n' 'run the workflow' | uv run agenthicc --headless --workflow code_plan
uv run agenthicc workflows list
uv run agenthicc workflows run code_plan --intent 'implement the feature' --json
```

Headless mode is useful for smoke tests and pipelines. With `--workflow`, each
stdin line becomes an actual workflow run and its JSON result is emitted after
completion. Workflow execution uses the same lauren-ai runner, plugin registry,
session persistence, capability checks, and approval boundary as the TUI. It
does not imply a REST server.

## Terminal workspace

The current TUI is implemented by `tui/workspace/Workspace` and consists of:

1. a scroll buffer for conversation, tool, workflow, and system events;
2. a live status/composer/footer block owned by the workspace;
3. overlays for help, configuration, approvals, questions, plans, and trigger
   completion;
4. a single lifetime input session with POSIX and Windows terminal backends.

The workspace treats terminal resizing as one settled repaint, clearing
Rich's previous geometry before redrawing so an active Plan Review is not
duplicated in the scrollback.

Tool completions use the same operation-style header as file updates: reads,
searches, commands, and other tools show a `● Operation(...)` header, a result
summary, and a bounded numbered output preview. File changes retain their
unified diff preview.

Useful built-in slash commands include:

| Command | Purpose |
|---|---|
| `/help`, `/commands` | Inspect available commands |
| `/status`, `/history` | Inspect runtime status and session events |
| `/mode [name]` | Show or change the operating mode |
| `/workflow <name> \| reset` | Select the workflow for later turns |
| `/model [provider] [model]` | Inspect or switch the model selection |
| `/config` | Open the configuration overlay |
| `/init` | Preview or explicitly write project `AGENTS.md` guidance |
| `/compact` | Compact conversation memory |
| `/replay [session-id]` | Replay a saved conversation |
| `/cancel`, `/clear`, `/expand` | Control the current session or output |
| `/mcp`, `/skills` | Inspect MCP and skill integrations |
| `/create-tools <instructions>` | Ask the agent to create lauren-ai tools |
| `/create-commands <instructions>` | Ask the agent to create slash commands |

Use `Ctrl+C` according to the current input state; the input backend owns raw
terminal mode and restores it on shutdown. See the [TUI guide](./docs/guides/tui.md)
for modes, overlays, input, and platform rules.

## Architecture in one picture

```text
user input
    │
    ▼
TUISession / headless runner
    │  creates turns, selects workflow, injects tools
    ├──────────────────────────────┐
    ▼                              ▼
reactive TUI AppState         kernel EventProcessor
    │                              │
Workspace + input             Event → root_reducer → frozen kernel AppState
    │                              │
    └──────────────┬───────────────┘
                   ▼
          workflow + agent turns
                   │
          capability-gated tools
                   │
       session / project / global memory
```

The kernel `AppState` and the reactive TUI `AppState` are different types with
different responsibilities. The session runner currently owns the bridge
between them. This boundary is documented in the [architecture guide](./docs/guides/architecture.md)
and is a P0 design item in PRD-138.

## Configuration example

```toml
# .agenthicc/agenthicc.toml

[execution]
provider = "anthropic"
model = "claude-opus-4-8"
max_concurrent_intents = 8
max_parallel_tasks = 4
max_agent_turns = 200
auto_compact = true
transport_max_retries = 3

[memory]
project_memory_path = ".agenthicc/memory"
session_ttl_seconds = 86400

[security]
sandbox_mode = true
# Use the real absolute project path, not the illustrative /workspace path.
allowed_paths = ["/absolute/path/to/project"]
network_allow_list = []

[tools]
max_live_tool_calls = 5
```

Config layers are merged in this order: built-in defaults, user config,
project config, environment variables, then repeated `--set key=value`
overrides. Run `uv run agenthicc config show` to inspect the effective values;
never print secrets in support logs.

## Extension points

| Extension | Current location | Discovery |
|---|---|---|
| Tools | `.agenthicc/tools/`, `~/.agenthicc/tools/` | `TOOLS` export; capability metadata; review executable code manually |
| Agents | `.agenthicc/agents/`, `~/.agenthicc/agents/` | `AgentPlugin` subclasses or `AGENTS` export |
| Modes | `.agenthicc/modes/`, `~/.agenthicc/modes/` | Mode plugin loader |
| Workflows | `.agenthicc/workflows/`, `~/.agenthicc/workflows/` | `WorkflowPlugin` subclasses |
| Skills | `.agenthicc/skills/`, `~/.agenthicc/skills/` | `SKILL.md` directories |
| Commands | `.agenthicc/commands/`, `~/.agenthicc/commands/` | `COMMAND`/`COMMANDS` exports; manual code review |
| MCP | `[[tools.mcp_servers]]` | configured server bridge |

Read the [extension guide](./docs/guides/plugins.md) and the
[custom-command guide](./docs/guides/commands.md) before enabling project code
or dependency installation. Project-local Python is executable code and must
be reviewed deliberately.

## Persistence and resume

Session artifacts live below `~/.agenthicc/sessions/`:

- `<session-id>.jsonl` — kernel event log;
- `<session-id>/conversation.jsonl` — rendered conversation events;
- `<session-id>/conversation-journal.jsonl` — durable conversation-memory
  transitions used for crash recovery and tool replay;
- optional cassette files — recorded transport and approval interactions.

Export a portable, redacted support artifact with:

```bash
uv run agenthicc sessions inspect SESSION_ID
uv run agenthicc sessions export SESSION_ID --output session-export.json
```

Inspection reports artifact health, corruption, token usage, workflow status,
and whether a turn needs resume without printing conversation or tool payloads.
The export includes valid records from the kernel, conversation, journal, and
cassette stores. Credential-shaped values are redacted and malformed JSONL
records are reported in the manifest. Review prompts, tool results, paths, and
model output before sharing an export.

Project memory and the workspace file cache live below `.agenthicc/`; global
memory defaults to `~/.agenthicc/global.db`. See the [storage reference](./docs/reference/storage.md)
before deleting session or project state.

## Development

```bash
uv sync --extra dev

# Fast checks
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q

# Broader suites
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

Nox contains the CI session definitions (`noxfile.py`), including the embedded
`llms-full.txt` symbol check. Its dependency installation paths are being
aligned with `pyproject.toml`; see [contributing](./docs/contributing.md) and
PRD-138 before using the default all-session invocation on a clean checkout.

## Documentation map

- [Quickstart](./docs/guides/quickstart.md)
- [Architecture](./docs/guides/architecture.md)
- [Configuration](./docs/guides/configuration.md)
- [Project bootstrap](./docs/guides/project-bootstrap.md)
- [TUI](./docs/guides/tui.md)
- [Workflows](./docs/guides/workflows.md)
- [User-defined commands](./docs/guides/commands.md)
- [User-defined tools](./docs/guides/tools.md)
- [Extensions and plugins](./docs/guides/plugins.md)
- [Memory and storage](./docs/guides/memory.md)
- [Security](./docs/guides/security.md)
- [Testing](./docs/guides/testing.md)
- [Type safety](./docs/guides/type-safety.md)
- [CLI reference](./docs/reference/cli.md)
- [Kernel reference](./docs/reference/kernel.md)
- [Storage reference](./docs/reference/storage.md)
- [Repository improvement PRD](./prds/prd-138-repository-improvement-roadmap.md)

AI-assisted contributors should also read [`AGENTS.md`](./AGENTS.md),
[`CLAUDE.md`](./CLAUDE.md), [`llms.txt`](./llms.txt), and
[`llms-full.txt`](./llms-full.txt).

## License

MIT. See [LICENSE](./LICENSE).
