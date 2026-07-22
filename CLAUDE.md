# CLAUDE.md — Development guide for agenthicc

This file is a concise maintainer guide. The user-facing project description is
in [README.md](README.md); the prioritized improvement backlog is
[`PRD-138`](prds/prd-138-repository-improvement-roadmap.md).

## Project model

agenthicc is a Python 3.11+ agent runtime built around lauren-ai. The current
interactive path is:

```text
CLI → session context → reactive TUI workspace
                    ↘ workflow runner → agent turn → tools
                                      ↘ kernel EventProcessor → reducer → log
```

There are two intentionally different state containers:

- `agenthicc.kernel.state.AppState` is frozen domain state. It changes only by
  applying events through `root_reducer` and is the durable/auditable model.
- `agenthicc.tui.conversation_store.AppState` is mutable reactive presentation
  state. It owns input, overlays, conversation rendering, metrics, mode display,
  approvals, and workflow progress.

Do not import one as a replacement for the other. When a feature crosses the
boundary, document and test the bridge in `runners/tui_session.py` or the
relevant runner context.

The repository does not currently contain `agenthicc.api`, `tui.app`,
`tui.transcript`, or the old lifecycle-hook executor modules. Do not add code or
docs against those paths without first resolving the product decision in
PRD-138.

## Environment

```bash
export ANTHROPIC_API_KEY="sk-ant-..."  # default provider
export OPENAI_API_KEY="sk-..."         # with execution.provider = "openai"
# Ollama requires no key; configure execution.provider = "ollama"
```

The default model is resolved by `config.PROVIDER_DEFAULT_MODELS`; do not copy
a model name into documentation without checking that mapping first.

## Useful commands

```bash
uv sync --extra dev

uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/agenthicc
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q

uv run agenthicc                 # Rich TUI
uv run agenthicc --headless     # stdin → JSON-lines
uv run agenthicc config show
uv run agenthicc sessions list
```

`noxfile.py` defines CI sessions and an embedded `llms-full.txt` check. Keep
those sessions aligned with the extras and tools declared by `pyproject.toml`;
the current mismatch is tracked as PRD-138 P0.5.

## Ownership map

| Area | Canonical files | Responsibility |
|---|---|---|
| Package entry point | `src/agenthicc/__main__.py`, `src/agenthicc/cli/` | CLI parsing, command discovery, dispatch |
| Kernel state | `kernel/state.py` | Frozen domain dataclasses and copy-on-write helpers |
| Kernel events | `kernel/events.py` | Event/effect serialization and event contract |
| Kernel reduction | `kernel/reducer.py` | Pure handlers and `_HANDLERS` registry |
| Kernel runtime | `kernel/processor.py` | Queue, run loop, persistence, subscribers, effects |
| Configuration | `config.py`, `security.py` | TOML/env/CLI merge and security policy translation |
| Session orchestration | `runners/session_context.py`, `runners/tui_session.py`, `runners/headless.py` | Runtime construction, turn routing, shutdown |
| Workflows | `workflows/` | Phase specs, runners, registry, built-in code-plan workflow |
| Agents | `agents/` | Built-in and filesystem-discovered agent definitions |
| Tools | `tools/`, `agent_tools.py` | Tool contracts, capabilities, approvals, MCP, FS/git/exec integrations |
| Security | `tools/sandbox.py`, `tools/capability_gate.py`, `security.py`, `plugins/trust.py` | Paths, network, capabilities, trust |
| Memory | `memory/`, `tools/fs/file_cache.py` | Tiers, journal, compaction, semantic index, durable file cache |
| Reactive TUI | `tui/conversation_store.py`, `tui/workspace/`, `tui/input/` | Signals, rendering, overlays, input capabilities |
| Terminal | `tui/terminal/`, `tui/cbreak_reader.py` | Platform-specific raw mode and key decoding |
| Runtime commands | `commands/`, `tui/runtime/commands.py`, `tui/triggers/` | Slash commands, command bus, trigger picker |
| Extension loading | `plugins/`, `skills/`, `modes/`, `commands/plugin_loader.py` | Discovery, validation, trust, precedence |
| Test fixtures | `tests/conftest.py`, `tests/conftest_cassette.py` | Shared state, processor, cassette fixtures |
| Public LLM docs | `llms.txt`, `llms-full.txt` | AI-consumed package/API documentation |

## Change patterns

### Adding an event

1. Define the payload contract in `kernel/events.py` or its docstring.
2. Add a pure handler in `kernel/reducer.py` and register it in `_HANDLERS`.
3. Add a reducer unit test and, when relevant, processor/effect coverage.
4. Update `llms-full.txt` if the event or symbol is public.
5. Update the architecture/storage docs if the event changes persistence or UI.

### Adding a workflow phase or workflow

1. Use `PhaseSpec` and `WorkflowPlugin` in `workflows/plugin.py`.
2. Register through the workflow registry/loader; do not create a second
   discovery convention.
3. Test transitions, retries, rejection loops, parallel phases, and resume.
4. Pass the complete `WorkflowConfig`/turn context, including memory and
   semantic index dependencies when the phase needs them.
5. Reconcile declarative phase metadata with runtime behaviour; an inert
   configuration field is a bug even if the happy path works.

### Adding a tool

1. Prefer the existing lauren-ai tool decorator and capability metadata when
   the tool is a callable; use `Tool` for a class-based integration.
2. Return structured, JSON-serializable results and classify errors.
3. Use `WorkspaceView` for filesystem paths and the shared HTTP client for
   network calls. Never instantiate a private `httpx.AsyncClient` in a tool.
4. Add capability, approval, timeout, failure, and output-bound tests.
5. For project/user plugins, document trust and dependency requirements.

### Adding a slash command

1. Register it in `commands/builtins.py` or through the supported command
   plugin loader so the trigger picker can see it.
2. Provide a handler, or explicitly intercept it in `TUISession` when it needs
   session-local state (as `/workflow` and `/compact` currently do).
3. Keep completion, dispatch, aliases, argument hints, and help output in one
   registry. The legacy names in `tui/input/completions.py` are compatibility
   adapters over that canonical registry.
4. Test both picker visibility and execution.

### Extending the TUI or terminal

- Presentation state belongs in `tui/conversation_store.py`.
- Long-lived rendering belongs in `tui/workspace/`.
- Input behaviour belongs in `tui/input/` capability handlers.
- `get_backend()` is the only terminal-platform selection point.
- POSIX calls stay in `posix_backend.py`; Windows console calls stay in
  `windows_backend.py`; `Key` remains canonical in `cbreak_reader.py`.
- Test non-TTY startup, resize, Unicode/color fallback, paste, Ctrl+C, and
  Shift+Tab on the relevant backend.

## Invariants

- `root_reducer` is pure: no I/O, awaiting, mutation, or global state.
- Kernel `AppState` is frozen; use events and `with_*` helpers.
- Start `EventProcessor.run()` before emitting and await `drain()` before
  asserting state.
- A session owns and closes its processor, workspace, journal, cache, MCP
  registry, and background tasks.
- File access must stay inside `WorkspaceView`; network access must pass the
  configured allow-list.
- Child agent capability scopes may only restrict their parent.
- Tool results and conversation transitions must remain serializable and
  replay-safe.
- New or modified Python signatures use concrete parameterized types; do not
  introduce `Any` when a real type is knowable.
- Public symbols exported by `__all__` need a `### Symbol` entry in
  `llms-full.txt` until the checker is replaced by a generated reference.

## Test placement

| Test type | Location | Use |
|---|---|---|
| Unit | `tests/unit/` | Pure reducers, parsers, registries, configuration, rendering, security |
| Integration | `tests/integration/` | Real processor, memory/database, plugin/tool/workflow boundaries |
| E2E | `tests/e2e/` | Full session, cassettes, TUI/runtime and cross-component behaviour |

Pytest uses `asyncio_mode = "auto"`. Use the shared fixtures and mark tests
with the configured `unit`, `integration`, or `e2e` marker where appropriate.

## Documentation rule

When behaviour changes, update the relevant guide and the LLM documentation in
the same change. If a module is removed or renamed, search the whole repository
for its old path before declaring the migration complete. Keep historical PRDs
as history, but label them so they cannot be mistaken for current API docs.
