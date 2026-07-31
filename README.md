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
tools, skills, modes, and MCP servers. New sessions start in Safe mode. Reads
run directly; writes, command execution, git changes, network access, and
unannotated tools ask for approval. Plan hard-blocks side effects, while Yolo
is the unrestricted mode formerly named Auto. Access is enforced by mode
restrictions, capability metadata, and approval settings. Review the
[user-defined tools guide](./docs/guides/tools.md) before adding executable
project plugins; it documents the current sandbox and trust boundaries.

To create a specialized workflow, select `/workflow create_workflow` and enter
the intent in the next input. It runs `design → generate → validate → summarize`
on the same state-machine pattern as `code_plan`: the design is presented for your
approval, the generate phase writes complete Python source to
`.agenthicc/workflows/<name>.py` with a runtime prompt for every phase, and the
validate phase imports that file and loops back to generate until it loads
cleanly — an approval of a file that does not import is overridden. For
non-trivial behavior the agent is guided to create a `code_plan`-style custom
runner with typed states, context, per-state functions, explicit `match`
transitions, and resumable execution; simple workflows can use declarative
`PhaseSpec` values. Run `/workflows reload` and then `/workflow <name>` after
authoring.

Project tools and slash commands are authored separately through the
`/create-tools` and `/create-commands` skills; they are not workflow selectors.

The generated workflow is expected to ship its own state-machine runner — typed
state enum, typed context, one bounded method per state, an explicit
`while not state.is_terminal` / `match` driver, `resume()`, and transitions that
only ever happen on a tool call. Design turns inspect the real authoring API with
the built-in `describe_phasespec`, `list_tool_capabilities`, `list_agent_roles`,
`describe_runner_pattern`, and `show_example_workflow` tools, which read from the
running code rather than from prose. Each authoring phase has its own prompt and
bounded multi-turn budget; tune the caps with
`[execution].authoring_max_phase_turns` and
`[execution].authoring_max_generation_attempts`.

Every session also carries five read-only tools for reading agenthicc itself —
`list_agenthicc_docs`, `read_agenthicc_doc`, `search_agenthicc_docs`,
`inspect_agenthicc_source`, and `search_agenthicc_source`. They serve the `docs/`
tree plus `llms.txt`, `llms-full.txt`, and `README.md`, and resolve any
`agenthicc` module or symbol (including private names) by parsing the file rather
than importing it. All five are read-only, so they stay available in Plan mode.

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

All clients can inspect the same client-neutral session projection. The new
`session` commands use the shared snapshot, command, and replay contracts:

```bash
uv run agenthicc session list --json
uv run agenthicc session show SESSION_ID --json
uv run agenthicc session events SESSION_ID --after 12
uv run agenthicc session export SESSION_ID --output session-export.json
uv run agenthicc session send SESSION_ID --text 'continue the work'
uv run agenthicc session control SESSION_ID cancel
```

The default session service is in-process and stores its projection under
`~/.agenthicc/session-service/`. `agenthicc session serve` is an explicit
loopback-only HTTP/SSE attachment transport; non-loopback binding requires a
bearer token. It is an adapter over the existing session/kernel runtime, not a
second agent server. See the [client-neutral session guide](./docs/guides/session-service.md).

## Terminal workspace

The current TUI is implemented by `tui/workspace/Workspace` and consists of:

1. a scroll buffer for conversation, tool, workflow, and system events;
2. a live status/composer/footer block owned by the workspace;
3. overlays for help, command and skill listings, configuration, approvals,
   questions, plans, and trigger completion;
4. a single lifetime input session with POSIX and Windows terminal backends.

The workspace treats terminal resizing as one settled repaint, clearing
Rich's previous geometry before redrawing so an active Plan Review is not
duplicated in the scrollback. While approvals, plan reviews, or questions are
waiting, the status animation and cached active-work timer stay fixed; the
wall-clock duration is retained for turn telemetry.

Tool completions use the same operation-style header as file updates: reads,
searches, commands, and other tools show a `● Operation(...)` header, a result
summary, and a bounded numbered output preview. File changes retain their
unified diff preview; long contiguous change blocks are abbreviated to six
edge rows with a single `...` omission marker.

Useful built-in slash commands include:

| Command | Purpose |
|---|---|
| `/help`, `/commands` | Inspect available commands in an overlay |
| `/tools [reload]`, `/workflows [reload]` | Inspect or reload tools/workflows; `/tools` labels each tool `builtin` or `plugin` |
| `/status`, `/history` | Inspect runtime status and session events |
| `/ps [terminal-id]`, `/stop [terminal-id|all]` | Inspect or stop owned background terminals; `/stop` stops all |
| `/mode [name]` | Show or change the operating mode |
| `/workflow <name> \| reset` | Select a workflow (the interactive picker autocompletes registered names); use `/workflow create_workflow` to author one directly in `.agenthicc/workflows/` |
| `/model [provider] [model]` | Inspect or switch the model selection |
| `/config` | Open the configuration overlay |
| `/init` | Preview or explicitly write project `AGENTS.md` guidance |
| `/compact` | Compact conversation memory |
| `/replay [session-id]` | Replay a saved conversation |
| `/cancel`, `/clear`, `/expand` | Control the current session or output |
| `/mcp`, `/skills [reload]` | Inspect MCP and skills; reload or open their overlay |
| `/usage` | Show durable session usage, cost quality, run state, and queued input |
| `/create-tools <instructions>` | Ask the agent to create lauren-ai tools |
| `/create-commands <instructions>` | Ask the agent to create slash commands |

`/config` opens its local configuration overlay immediately, including while a
response is streaming.

Use `Ctrl+C` according to the current input state; the input backend owns raw
terminal mode and restores it on shutdown. See the [TUI guide](./docs/guides/tui.md)
for modes, overlays, input, busy-run command policies, and platform rules. ESC
returns the input state to idle immediately after cancelling a run, so the
double-Ctrl+C exit sequence remains responsive on Windows.

## Background sessions

Long-running work can be detached from an active session with `/bg` or
`/background`. Run `agenthicc agents` (or `agenthicc jobs`) to open the
background-session manager, where you can inspect, follow, resume, retry,
cancel, and safely delete sessions. `Ctrl+X` deletes the selected session only
after confirmation; `u` restores it from recoverable trash. See the
[background sessions guide](./docs/guides/background-sessions.md) for workflow
support, approvals, input requests, retention, and privacy details.

Execution tools remain foreground by default. Pass `background=true` to
`run_bash` or `run_command` to receive an owned `term-...` handle, then call
`wait_terminal` when the result is needed. While a wait is active, `/ps`,
`/stop`, and `Esc` remain responsive; `/stop` stops all owned background
terminals, while `Esc` stops the terminal currently being awaited. Terminal
handles and bounded output are local-only and scoped to the originating
session.

For finite builds, use an explicit `cwd` and a seconds-based `timeout`; a
non-zero exit, timeout, cancellation, or spawn failure is always reported as a
failed command. For development servers, use
`lifecycle="service"` with a readiness probe rather than waiting for a process
that is intended to remain alive. See the [command execution guide](./docs/guides/command-execution.md)
for result states, readiness controls, workflow gates, and diagnostics.

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
and is a P0 design item in PRD-138. For the full evidence-backed checkout
audit, see the [current repository state reference](./docs/reference/repository-state.md).

## Configuration example

```toml
# .agenthicc/agenthicc.toml

[execution]
provider = "anthropic"
model = "claude-opus-4-8"
max_concurrent_intents = 8
max_parallel_tasks = 4
max_agent_turns = 200
authoring_max_generation_attempts = 20
authoring_max_phase_turns = 20
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
group_exploratory_calls = true  # presentation-only grouping of marked reads
browser_backend = "cloakbrowser"  # cloakbrowser, playwright, or none

# Optional browser automation; enabled as a deny-all surface until configured.
[tools.cloakbrowser]
enabled = true
allowed_domains = ["https://example.com"]
allow_all_domains = false
```

CloakBrowser is an optional dependency. Install it only when browser tools are
needed with `pip install 'agenthicc[cloakbrowser]'` or `uv sync --extra
cloakbrowser`; base installations do not import or require it.

Microsoft Playwright is an alternative backend. Select it explicitly and
install its optional package and browser runtime:

```bash
uv sync --extra playwright
uv run playwright install chromium
```

From another uv project, such as a sibling `python-password-generator`
checkout, use an editable requirement because `uv sync --extra` reads the
consumer project's extras:

```bash
uv run --no-project --with-editable '../agenthicc[playwright]' playwright install chromium
OPENAI_API_KEY='...' OPENAI_MODEL='...' OPENAI_BASE_URL='...' \
  uv run --no-project --with-editable '../agenthicc[playwright]' agenthicc --continue
```

```toml
[tools]
browser_backend = "playwright"

[tools.playwright]
enabled = true
browser_type = "chromium"
allowed_domains = ["example.com"]
allow_all_domains = false
```

Playwright exposes the same bounded browser operations with `playwright_*`
names. Domain, DNS, private-address, artifact, quota, and checkpoint policies
are shared with the CloakBrowser adapter. The two backends are mutually
exclusive per session, and neither optional package is imported unless its
backend is selected.

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
| MCP | `[[tools.mcp_servers]]` | configured server bridge; structured results preserved |

Explicit skills use the `$skill-name` or `$alias` trigger; `/` remains reserved
for commands. `/commands` lists slash commands only; use `/skills` to inspect
discovered skills. The former
`/skill-name` spelling is not executed.

In a registry overlay, press Enter on an entry to open its details. Press Enter
again on the details page to place the invocation in the input panel:
`/workflow <name>` for workflows, the command's canonical `/name` for
commands, and `$<skill-name>` for skills. The text is prepared but not
submitted.

Install validated skills from a local path, direct HTTPS `SKILL.md` URL, or a
generic GitHub repository with `agenthicc skills add SOURCE`. Both
`https://github.com/owner/repo.git` and `owner/repo` sources are supported;
repository sources discover and install all valid skills by default. Use
`--skill NAME[,NAME]` to select specific skills, `--all` to make full
installation explicit, and `--global` for user-global scope. Existing skill
directories are never overwritten. Review downloaded instructions before
invoking a newly installed skill.

Register an MCP server without hand-editing TOML with
`agenthicc mcp add NAME URL`. Project scope is the default; use `--global` for
the user configuration, `--transport` to select the existing bridge transport,
and `--token-env ENV_VAR` to persist a token reference without exposing the
secret to the CLI. A local Lauren MCP directory or `server.py` is also accepted
for stdio servers and is converted to an `lmcp run ... --stdio` launcher. The
command validates and appends configuration but does not connect to the server
itself.

Read the [extension guide](./docs/guides/plugins.md) and the
[custom-command guide](./docs/guides/commands.md) before enabling project code
or dependency installation. Project-local Python is executable code and must
be reviewed deliberately.

## Persistence and resume

Session artifacts live below `~/.agenthicc/sessions/`:

- `<session-id>.jsonl` — kernel event log;
- `<session-id>/conversation.jsonl` — rendered conversation events; resumed
  TUI sessions replay this transcript through the scroll appender without
  duplicating the stored records;
- `<session-id>/conversation-journal.jsonl` — durable conversation-memory
  transitions used for crash recovery and tool replay;
- `<session-id>/workflows/<run-id>/checkpoint.json` — atomic, bounded workflow
  context checkpoints used by `/workflow resume` after an Esc pause;
- optional cassette files — recorded transport and approval interactions.

Direct chat, Plan mode, `code_plan`, and `create_workflow` share the session's
stable conversation ID and journal-backed provider memory. Workflow phase state
is checkpointed separately, while the reactive conversation store remains a UI
projection.

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
- [Background sessions](./docs/guides/background-sessions.md)
- [Workflows](./docs/guides/workflows.md)
- [Custom workflows and TOML configuration](./docs/guides/custom-workflows-and-config.md)
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
