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
6. Study before you implement: read every file in the call chain, from the
   failing site back to the entry point, and the contract between the
   components involved. Do not fix from an error message and the crashing line
   alone.
7. Stage explicit paths only. Never sweep in, revert or reformat changes you
   did not make.

## Working rules for agents

- **Your own session does the work.** Work sequentially. Use subagents only
  when the operator allows it for the task, and never hand part of your task
  to another Claude session or a peer session; decline such offers. When
  subagents are allowed, give each mutating one its own git worktree off the
  feature branch with disjoint files, and merge their results yourself.
- **Replace, don't layer.** The project is not in production. When a change or
  PRD supersedes a symbol, delete it, fix every caller and test, and drop the
  `isinstance` branches and `# type: ignore` comments that only supported the
  old path. No shims, aliases, dual paths, `Optional` escape hatches or
  `SimpleNamespace` fakes of old objects. Confirm with
  `grep -rn "<OldSymbol>" src/ tests/ docs/` before closing the task.
- **No `Any` in new files.** Every parameter and return value has a concrete
  type; use `object` only as a last resort.
- **One commit per completed PRD** on the current branch. Push, open pull
  requests or change GitHub settings only when the task asks for it; never use
  `--no-verify`; never apply branch protection (the owner decided `main` stays
  unprotected; CI plus the local hooks are the gates).
- **Report faithfully.** Show failures with their output, say what you did not
  run, and claim "green" only for a run that started after your last edit.
  Delegated work is judged by what the runtime observed, not by what a worker
  said.

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
| Repository schedules | `src/agenthicc/schedules/` (SQLite definitions/occurrences; service lifecycle in `cli/commands/session_service.py`) |
| Agent registry | `src/agenthicc/agents/` |
| Subagent policy and communication | `src/agenthicc/subagents/policy.py`, `communication.py`, `pool.py` |
| Tools and integrations | `src/agenthicc/tools/`, `agent_tools.py` |
| Configuration/security | `config.py`, `security.py`, `tools/sandbox.py`, `plugins/trust.py` |
| Memory and durability | `memory/`, `tools/fs/file_cache.py`, `tui/runtime/session_log.py` |

There is currently no `src/agenthicc/api/`, `tui/app.py`, `tui/transcript.py`,
or `tui/events.py`. The compatibility modules `tools/hooks.py` and
`tools/executor.py` are present and adapt lauren-ai's canonical hook/executor
contracts; they do not define a separate runtime. Do not use the absent API or
historical TUI paths in new work; update stale references as part of
documentation or migration work.

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

Linux development and CI environments need the `bubblewrap` (`bwrap`) OS
package for worktree-environment command execution and its isolation tests.
These commands fail closed when Bubblewrap is unavailable rather than falling
back to an unrestricted shell.

`uv sync --extra dev` is an *exact* sync and silently uninstalls optional
extras (`book`, `playwright`, `cloakbrowser`) that the developer virtualenv
normally carries; sync with
`uv sync --extra dev --extra book --extra playwright --extra cloakbrowser --inexact`.
`uv run` syncs inexactly and is safe. The host can be heavily loaded: check
`uptime` and re-run a timing-sensitive failure in isolation before calling it a
regression. `nox -s coverage` takes about an hour — run it in the background
and re-run it in full after the last fix before claiming green.

`noxfile.py` is the CI session definition. Every session runs on nox's uv
backend, which installs the project and its one `dev` extra into the session's
own virtualenv, so each tool runs at its declared version on the session's
interpreter. TUI and API modules ship in the base package with no separate
extras, and the documentation and release toolchain (MkDocs, MkDocs Material,
pymdown-extensions, twine) is part of `dev`. List the real sessions with
`uv run nox --list`:

| Nox session | Runs |
|---|---|
| `tests` (3.11, 3.12, 3.13) | Full suite on one supported Python, e.g. `tests-3.12` |
| `tests_unit` (3.11, 3.12, 3.13) | `pytest tests/unit` |
| `tests_integration` (3.11, 3.12, 3.13) | Integration and cassette replay tests plus workflow scenarios S2, S4, and S9 |
| `tests_e2e` | `pytest tests/e2e` |
| `workflow_recovery` | Checkpoint compatibility, stack links, and guarded resume through the TUI |
| `tests_windows` | The lock, Windows filesystem and Windows terminal tests |
| `coverage` | Full suite with branch and subprocess coverage; floor in `[tool.coverage.report] fail_under` |
| `lint` | `ruff check` and `ruff format --check` |
| `format` | `ruff format`, applying changes in place |
| `typecheck` | `mypy src/agenthicc` |
| `typecheck_contracts` | mypy over the typing boundary tests and the ratchet script |
| `type_audit` | `scripts/type_audit.py --check docs/reference/type-safety-baseline.json` |
| `docs` | `mkdocs build --strict` |
| `build` | `uv build` (wheel and sdist) |
| `build_check` | `twine check --strict dist/*` |
| `llms_check` | Every `agenthicc.__all__` and `agenthicc.kernel.__all__` symbol has a `###` heading in `llms-full.txt` |
| `clean` | Remove build, test, and coverage artifacts |

`.github/workflows/ci.yml` runs all of these gates and ends in the aggregate
**Required checks** job, the single status to wait on before merging. CI runs on
pull requests to `main`/`dev` and on pushes to those branches only, so on a
feature branch the local hooks are your gate. `main` is intentionally not
branch-protected: do not run `scripts/apply_branch_protection.sh` or change
repository settings unless asked. The release workflow re-runs the CI for the
tagged commit before building.

## Surfaces and gates

Two surfaces carry different review cost. Identify which one a change belongs
to before you start, and run that surface's gate.

| Surface | Paths | Gate |
|---|---|---|
| Documentation | `docs/`, `README.md`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `skills/`, `llms.txt`, `llms-full.txt` | `uv run mkdocs build --strict`; `uv run nox -s llms_check` when public symbols change |
| Source | `src/`, `tests/`, `scripts/`, `noxfile.py`, `pyproject.toml`, `.github/`, `mkdocs.yml` | The full matrix: `uv run nox -s lint`, `-s typecheck`, `-s type_audit`, and the unit/integration/E2E sessions |

Three details matter here:

- `mkdocs.yml` is site *configuration*, not a documentation page. Editing nav
  wiring changes the docs build and belongs to the source surface even though
  the artifact it controls is documentation.
- `README.md` sits outside `docs/`, so `mkdocs build --strict` never validates
  it. Its links and anchors need checking separately.
- `prds/` and `CHANGELOG.md` are historical and user-visible records. Update
  them for provenance, but do not treat a PRD as current API documentation.

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

### Changing repository schedule behaviour

1. Keep schedule definitions/revisions/occurrences in `schedules.store`; the
   runner is a projection/dispatcher, not a second persistence authority.
2. Preserve unique occurrence keys, deterministic background-session IDs,
   service ownership, and the cross-process dispatch lock.
3. Validate workflow/profile/mode changes and pause revised schedules until
   their exact next-run preview has been explicitly confirmed.
4. Test recurrence/DST with a fake clock, crash/restart reconciliation, CLI
   lifecycle in subprocesses, isolated mutating worktrees, and TUI controls.
5. Update `docs/guides/scheduled-workflows.md`, the CLI/storage references, and
   PRD status when behavior or persistence changes.

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

### Committing and pushing

1. Read `git status --short` first; stage explicit paths; keep other people's
   hunks out (see `CLAUDE.md`, "Commits, hooks and pushing", for the
   `git show HEAD:<file>` blob technique).
2. Commit hooks run ruff. Push hooks run mypy, the type-audit ratchet and the
   whole unit suite — about ten minutes — so run `git push` in the background
   and do not touch the tree until it exits.
3. If git has no identity, set the four `GIT_AUTHOR_*`/`GIT_COMMITTER_*`
   environment variables for the command; do not edit git config.
4. Use conventional commit subjects (`feat(scope): …`, `!` for breaking) and
   merge pull requests with merge commits.

### Changing subagent behaviour

1. Working roles inherit every parent tool and the session's turn limit;
   investigative roles keep explicit read-only lists; no worker inherits
   `control`-capability tools or `spawn_subagents`.
2. No hidden nudge turns or retries: a worker that only narrates fails with
   `missing_mutation` or `missing_action`.
3. Report runtime-observed evidence (`completion_evidence`), never prose.
4. Each parallel file-changing worker gets its own git worktree, started from a
   snapshot of the parent's uncommitted state and applied back with `git apply`.
5. Test the named failure reasons. See SUBAGENT-12 to SUBAGENT-15 and the
   "Changing subagents" section of `CLAUDE.md`.

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
  type is knowable; do not use bare `list` or `dict`. In a new file `Any`
  appears zero times: import real types under `TYPE_CHECKING`, name structural
  types with aliases, and use `object` only as a last resort.
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

Also run `uv run nox -s llms_check` when public exports change. A full release
gate additionally requires a successful docs build (`uv run mkdocs build
--strict`), package build and check (`uv run nox -s build`, then
`uv run nox -s build_check`), and the unit/integration/E2E matrix
(`uv run nox -s tests`).
