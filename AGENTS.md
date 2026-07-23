# AGENTS.md — Agent guidance for agenthicc

This is the operational companion to [CLAUDE.md](CLAUDE.md). Follow the
repository's current source tree, not historical PRD examples. The repository
improvement backlog is [`PRD-138`](prds/prd-138-repository-improvement-roadmap.md).

## Before changing code

1. Read the relevant current module and its tests.
2. Check `git status --short`; preserve unrelated user changes.
3. Search for all consumers with `rg` before renaming or deleting a symbol.
4. Confirm whether a similarly named type belongs to the kernel or reactive TUI
   state model.
5. Keep new behaviour inside the existing ownership boundary in `CLAUDE.md`.

## Current runtime boundaries

| Concern | Canonical implementation |
|---|---|
| Domain state | `src/agenthicc/kernel/state.py` (`kernel.AppState`, frozen) |
| Domain events/effects | `src/agenthicc/kernel/events.py` |
| Pure reduction | `src/agenthicc/kernel/reducer.py` |
| Event loop and persistence | `src/agenthicc/kernel/processor.py` |
| Session construction | `src/agenthicc/runners/session_context.py` |
| Interactive orchestration | `src/agenthicc/runners/tui_session.py` |
| Headless stdin runner | `src/agenthicc/runners/headless.py` |
| Reactive UI state | `src/agenthicc/tui/conversation_store.py` |
| Rich workspace | `src/agenthicc/tui/workspace/` |
| Input and triggers | `src/agenthicc/tui/input/`, `src/agenthicc/tui/trigger.py`, `src/agenthicc/tui/triggers/` |
| Terminal portability | `src/agenthicc/tui/terminal/`, `cbreak_reader.py` |
| Workflow engine | `src/agenthicc/workflows/` |
| Agent registry | `src/agenthicc/agents/` |
| Tools and integrations | `src/agenthicc/tools/`, `agent_tools.py` |
| Configuration/security | `config.py`, `security.py`, `tools/sandbox.py`, `plugins/trust.py` |
| Memory and durability | `memory/`, `tools/fs/file_cache.py`, `tui/runtime/session_log.py` |

There is currently no `src/agenthicc/api/`, `tui/app.py`, `tui/transcript.py`,
`tui/events.py`, `tools/hooks.py`, or `tools/executor.py`. Do not use those
historical paths in new work; update stale references as part of documentation
or migration work.

## Environment and commands

```bash
export ANTHROPIC_API_KEY="sk-ant-..."  # default
export OPENAI_API_KEY="sk-..."         # set execution.provider=openai
# Ollama needs no key

uv sync --extra dev
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
uv run agenthicc
uv run agenthicc --headless
```

`noxfile.py` is the CI session definition. It currently contains dependency
and documentation-check drift tracked in PRD-138; do not claim the all-session
Nox command is a clean-checkout gate until that work is complete.

## By-task lookup

### Adding an event or reducer handler

1. Document the event payload keys in `kernel/events.py`.
2. Add a pure `_reduce_*` function and `_HANDLERS` entry in `kernel/reducer.py`.
3. Add a synchronous reducer unit test.
4. Add processor/effect integration coverage when observable side effects or
   subscriptions are involved.
5. Update `llms-full.txt` and the architecture docs for public events.

### Adding or changing workflow behaviour

1. Use `PhaseSpec`, `WorkflowPlugin`, and the existing registry/loader.
2. Test normal transitions, rejection loops, retries, parallel phases, and
   resume state.
3. Ensure `CodePlan` metadata and `CodePlanRunner` behaviour do not become two
   sources of truth.
4. Preserve phase outputs, history, approval state, and summaries across
   resume.
5. Update `docs/guides/workflows.md` and the workflow findings in
   `docs/reference/workflow-review.md`.

### Adding a tool or integration

1. Use `Tool`/`ToolResultEnvelope` or the existing lauren-ai callable-tool
   convention.
2. Attach capability metadata and test the mode/capability boundary.
3. Use `WorkspaceView` for paths, `NetworkGuard` for network destinations, and
   `agenthicc_http_client()` for HTTP.
4. Catch transient network errors in external tools and return a structured,
   recoverable error where the tool contract requires it.
5. Add tests for success, denial, malformed input, timeout, and side-effect
   duplication on retry.

### Adding a slash command

1. Add it to `commands/builtins.py` or the supported command plugin export.
2. Give it a handler, or intercept it in `TUISession` when it needs session
   fields.
3. Test trigger-picker discovery and submitted execution separately.
4. Do not add a second built-in list: the legacy exports in
   `tui/input/completions.py` adapt the canonical command registry.

### Extending the TUI

1. Add reactive fields/mutations to `tui/conversation_store.py`.
2. Render persistent UI in `tui/workspace/`; render scroll events through
   `ScrollBufferAppender`.
3. Put keyboard behaviour in `tui/input/capabilities.py` and trigger behaviour
   in `tui/triggers/`.
4. Do not import `msvcrt`, `termios`, or `tty` outside their dedicated backend
   modules. `get_backend()` owns platform selection and `Key` is canonical in
   `cbreak_reader.py`.
5. Test both interactive and non-interactive terminal paths.

### Extending memory or persistence

1. Use `SessionMemoryLayer` for process-local values, `ProjectMemoryLayer` for
   project SQLite/artifacts, and `GlobalMemoryLayer` for user-wide SQLite.
2. Route access through `MemoryRouter`.
3. Keep reads lock-free where the layer contract requires it and serialize
   writes with the owning tier's lock.
4. Add temp-directory integration tests and corruption/restart coverage for
   durable formats.
5. Update `docs/reference/storage.md` whenever a file or retention policy
   changes.

## Safety and typing rules

- Never weaken a security default to make a test or demo pass.
- Resolve exact paths before destructive actions; never recursively delete a
  broad workspace target.
- Do not expose API keys, OAuth tokens, plugin secrets, or session contents in
  logs or docs.
- Use concrete parameterized annotations. Do not introduce `Any` when a real
  type is knowable; do not use bare `list` or `dict`.
- Use `TYPE_CHECKING` for cross-package type-only imports and quoted annotations
  when decoration-time `get_type_hints()` requires it.
- Frozen kernel state is updated through events/copy-on-write helpers, never by
  direct field mutation.
- Start `EventProcessor.run()` before emitting; `drain()` otherwise waits for a
  loop that does not exist.

## Required documentation updates

Update the relevant artifacts in the same change:

- public Python symbols → `llms-full.txt` and, when appropriate, `llms.txt`;
- user-visible behaviour → `README.md` and a guide under `docs/guides/`;
- architecture or persistence → `docs/guides/architecture.md` or
  `docs/reference/storage.md`;
- contributor workflow → `CLAUDE.md`, this file, and
  `docs/contributing.md`;
- new product scope → a numbered PRD under `prds/` and the PRD index.

## Definition of done

For a code change, run the checks relevant to the touched surface and report
any environment blocker explicitly:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/ -q
```

Also run `uv run nox -s llms_check` when public exports change. A clean release
gate additionally requires a successful docs build, package build/check, and
the unit/integration/E2E matrix once the P0 packaging work in PRD-138 lands.
