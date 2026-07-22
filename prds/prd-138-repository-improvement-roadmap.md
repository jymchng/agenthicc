# PRD-138 — Repository Improvement Roadmap

**Status:** Proposed  
**Date:** 2026-07-22  
**Scope:** Repository quality, runtime architecture, user experience, security,
packaging, testing, and documentation  
**Audience:** Maintainers, contributors, plugin authors, and release managers

## 1. Executive summary

agenthicc already contains the foundations of a capable terminal agent runtime:
an event-sourced kernel, resumable conversation journal, workflow and agent
registries, capability-aware tools, a Rich-based TUI, three memory tiers, MCP
bridging, model-aware context management, and an extensive test suite.

The largest risk is not a missing headline feature. It is the distance between
the current implementation and the contract implied by the repository's
documentation and tooling. Several documents still describe removed modules,
the advertised REST API is not present in `src/agenthicc`, the packaging file
does not declare the extras referenced by the README and Nox sessions, and the
kernel state model coexists with a separate reactive TUI state model without a
single documented boundary.

This PRD defines a sequenced improvement program. It is intentionally broader
than a bug list: each item includes the user or maintainer outcome, the
implementation direction, and evidence that can prove completion.

## 2. Repository baseline

The baseline was inspected on 2026-07-22.

| Area | Current evidence | Interpretation |
|---|---|---|
| Package | `src/agenthicc/`, 164 Python source files | Broad feature surface with several integration boundaries |
| Tests | `tests/unit/`, `tests/integration/`, `tests/e2e/`, 118 Python test files | Good breadth; still needs a single authoritative release gate |
| Runtime | `runners/tui_session.py`, `runners/headless.py`, `workflows/` | TUI is the primary interactive product; headless is stdin/JSON-lines |
| Kernel | `kernel/state.py`, `events.py`, `reducer.py`, `processor.py` | Immutable event-reduced state with optional JSONL persistence |
| TUI | `tui/conversation_store.py`, `tui/workspace/`, `tui/input/`, `tui/terminal/` | Current UI is the reactive Rich Live workspace, not the older prompt-toolkit model |
| Persistence | `~/.agenthicc/sessions/`, conversation journal, kernel JSONL, project/global SQLite | Several durable stores need a documented ownership and migration policy |
| Packaging | `pyproject.toml` version `0.1.0`, only `cloud` and `dev` extras | README claims `tui` and `api` extras that are not declared |
| Documentation | `README.md`, `CLAUDE.md`, `AGENTS.md`, `docs/`, `knowledge/`, `prds/` | Rich but duplicated; several files refer to absent modules or endpoints |
| Roadmap | 137 existing PRD files plus this PRD | A master index and lifecycle state for PRDs are missing |

## 3. Findings that motivate the roadmap

### 3.1 Documentation and implementation drift

The following claims do not match the current source tree:

- `agenthicc.api.server.create_app` and the REST/WebSocket endpoints are
  documented, but there is no `src/agenthicc/api/` package.
- `agenthicc.tui.app`, `tui.transcript`, and `tui.events` are referenced by
  README, guides, and agent instructions, but those modules are absent.
- The current TUI is built from `tui/workspace/Workspace`,
  `tui/conversation_store/AppState`, and `tui/input/UnifiedInputSession`.
- The repository instructions describe `LifecycleHook`, `HookRunner`,
  `ToolExecutor`, and `ToolSandbox` modules that are not present; the current
  tool layer uses `Tool`, `ToolResultEnvelope`, `WorkspaceView`, capability
  metadata, approval services, and the `lauren-ai` runner.
- `README.md` includes a long unrelated password-generator transcript and
  references files such as `docs/contributing.md` and
  `docs/configuration.md` at paths that do not exist.
- `docs/guides/quickstart.md`, `docs/guides/configuration.md`, and the root
  README use configuration keys and installation extras that are not aligned
  with `config.py` and `pyproject.toml`.

**Impact:** Users can follow instructions that cannot work, and contributors
can edit the wrong integration seam. Documentation correctness is therefore a
release-quality concern, not editorial polish.

### 3.2 Packaging and verification drift

- `pyproject.toml` declares no `tui`, `api`, or `all` extras, while README and
  `noxfile.py` reference them.
- `noxfile.py` embeds the `llms-full.txt` checker, while the contributor guides
  refer to `scripts/check_llms.py`; no such script exists.
- The lint workflow allows mypy and LLM-documentation failures on pull
  requests, so the advertised definition of done is weaker than the merge
  contract.
- The docs workflow calls `uv sync --extra dev` and `mkdocs build`, but MkDocs
  is not declared in the project dependency metadata.
- The CLI version string and package version are hard-coded independently;
  release metadata can drift.
- The missing `agenthicc.skills.web_search` module has been restored with
  Brave search and visible page-text extraction; the full local test suite now
  collects and passes. The remaining verification drift is the absent
  `scripts/check_llms.py` helper described above.

### 3.3 Two state models without a first-class contract

The kernel has a frozen `kernel.AppState` reduced from events. The interactive
TUI has a mutable/reactive `tui.conversation_store.AppState` containing signals,
input state, overlays, workflow display, and conversation events. The session
runner owns both. This can be a valid presentation architecture, but the
boundary is implicit and several older documents incorrectly treat them as one
type.

**Impact:** New features may update only one state model, persistence may not
cover user-visible state, and tests can exercise a path that is not used by the
real session.

### 3.4 Workflow configuration and runtime duplication

`docs/reference/workflow-review.md` records concrete findings in
`workflows/default/` and `workflows/code_plan/`, including duplicated tool
filtering, a declarative `CodePlan.phases` list that is not the sole runtime
source of truth, transition customization that may be bypassed, incomplete
resume context, and parallel-phase failure handling. These findings should be
verified against the current branch and either fixed or retired with a test.

### 3.5 Security and failure-mode risks

- The default security path allow-list is `['/workspace']`, which is not
  automatically the current project directory.
- Project-local Python plugins and CLI commands execute imported code; trust,
  auto-install, shadowing, and headless behaviour need a single threat model.
- Reducer failures are logged and the event is dropped by `EventProcessor`;
  callers do not receive a failed-event result.
- Subscriber queues silently drop state snapshots when full.
- HTTP timeout configuration exists in `ToolSettings` and the shared HTTP
  client, but the TOML conversion path does not currently populate every tool
  setting.
- The headless runner currently reduces `IntentCreated` and reports status; it
  is not equivalent to a full TUI session runner.

## 4. Goals and non-goals

### Goals

1. Make every public instruction and example executable against the current
   repository, or label it explicitly as planned.
2. Establish one documented architecture contract for kernel state, reactive
   UI state, workflows, tools, persistence, and extension loading.
3. Make security defaults, plugin trust, approvals, retries, resume, and error
   reporting observable and testable.
4. Make packaging, docs builds, type checks, LLM API documentation, and tests a
   reproducible release gate.
5. Reduce duplicated registries and compatibility shims that create competing
   sources of truth.
6. Preserve the project's strong pure-reducer, capability-boundary, and
   durable-journal invariants while improving the surrounding contracts.

### Non-goals

- Replacing `lauren-ai` or designing a new provider abstraction in this PRD.
- Adding a web API merely because old documentation mentions one; the API must
  first be chosen as a supported product surface.
- Rewriting every tool or workflow at once.
- Removing existing PRDs or user worktree changes.

## 5. Improvement backlog

Priority meanings:

- **P0:** correctness, safety, or release-blocking drift.
- **P1:** high-value product or maintainability improvement.
- **P2:** strategic improvement after the contracts above are stable.

### P0 — establish a truthful, safe baseline

#### P0.1 Documentation truth pass

**Outcome:** README, docs, CLAUDE.md, AGENTS.md, `llms.txt`, and the PRD index
describe the same supported surfaces.

**Work:**

- Remove or rewrite absent API/TUI/hook module references.
- State clearly that the supported headless interface is stdin + JSON-lines
  until a server package exists.
- Document current Rich workspace components and the two state models.
- Add a docs link checker and a smoke test for import paths in code examples.
- Add a status field to architecture and product docs: implemented,
  experimental, planned, or historical.

**Acceptance:** no current documentation points to a non-existent local module
without a `planned` or `historical` label; all README commands are checked in
CI.

#### P0.2 Decide and implement the headless API boundary

**Outcome:** The project either has a supported server API or no longer claims
to have one.

**Options:**

1. Implement `agenthicc.api` with explicit optional dependencies, lifecycle
   ownership, authentication, intent submission, status, event streaming,
   backpressure, and integration tests; or
2. Remove API claims and configuration fields until a server PRD is approved.

**Acceptance:** one decision is recorded; package metadata, docs, CLI help,
   workflows, and tests agree with that decision.

#### P0.3 Define the state boundary

**Outcome:** Contributors know which state is authoritative for which concern.

**Work:**

- Keep kernel `AppState` authoritative for durable domain events and reducer
  state.
- Keep reactive TUI `AppState` authoritative for ephemeral presentation and
  input state, or merge the models only after a measured migration plan.
- Introduce an explicit adapter/bridge document and test each event path from
  kernel event to UI event or explain why it is presentation-only.
- Rename imports in docs and examples so `AppState` is always qualified when
  both models are in scope.

**Acceptance:** architecture tests cover a representative intent, workflow,
tool, approval, retry, and completion path; no new code imports the wrong
`AppState` by accident.

#### P0.4 Security baseline and plugin trust contract

**Outcome:** A fresh install fails closed without surprising path or code
execution access.

**Work:**

- Resolve default allowed paths relative to the project, or require an
  explicit path and explain the trade-off at startup.
- Centralize trust decisions for tools, agents, modes, skills, commands, and
  MCP servers.
- Ensure headless mode never silently auto-installs or executes untrusted
  project code.
- Add threat-model documentation for imported Python plugins, dependency
  installation, credential expansion, and MCP transport connections.
- Add tests for traversal, symlink escape, network allow-list matching,
  capability down-scoping, and trust-file tampering.

**Acceptance:** security tests run on every pull request; the startup output
identifies skipped or trusted extensions without leaking credentials.

#### P0.5 Release metadata and check gate

**Outcome:** one command proves whether a change is releasable.

**Work:**

- Make CLI version derive from package metadata.
- Declare or remove all optional extras used by docs, Nox, and workflows.
- Add MkDocs and any documentation-only dependencies to a documented group.
- Extract the LLM symbol check into a real script or make Nox the sole
  documented source and remove the stale script references.
- Make mypy, docs build, docs link checking, llms check, and tests blocking on
  protected branches.
- Add a supported-Python matrix that matches `requires-python` and CI.

**Acceptance:** a clean checkout can install the documented development
environment and run lint, format, type, docs, LLM-doc, unit, integration, and
E2E checks without undeclared extras or hidden local dependencies.

The first implementation task should also restore a green test collection:
either provide the intended skill module and dependency contract or update the
stale test/loader boundary with an explicit compatibility decision.

### P1 — strengthen runtime correctness and developer experience

#### P1.1 Workflow single-source-of-truth refactor

Verify and address the findings in `docs/reference/workflow-review.md`:

- unify tool filtering for generic and code-plan runners;
- either make `CodePlan.phases` drive execution or remove the inert declaration;
- wire plugin transition hooks or remove dead customization points;
- inject memory and question tools consistently;
- preserve `phase_history`, execution summaries, and review summaries on resume;
- make parallel phase failures produce terminal workflow state and events;
- validate `parallel_with`, retry loops, and resume graphs before execution;
- remove dead factories and compatibility shims only after import usage is
  measured.

**Acceptance:** each retained finding has a regression test and each retired
finding has a short explanation tied to current code.

#### P1.2 Event processor failure and backpressure semantics

Define whether reducer exceptions, effect failures, slow subscribers, and
shutdown cancellation are fatal, retryable, or observable errors.

Candidate changes include typed event outcomes, an error event/dead-letter
queue, bounded subscriber policies, `queue.join()`-based draining, graceful
stop with task ownership, and tests for concurrent producers.

**Acceptance:** no event can disappear silently without a documented policy;
metrics or logs identify event id, event type, and failure reason.

#### P1.3 Durable storage lifecycle and migrations

Document and unify the ownership of:

- kernel session JSONL and snapshots;
- TUI conversation JSONL;
- conversation journal and durable idempotency records;
- project memory SQLite and artifacts;
- global memory SQLite;
- workspace file cache and cassette recordings.

Add schema versions, atomic compaction/rotation, corruption recovery tests,
retention controls, and a `sessions inspect/export` workflow.

**Acceptance:** a crash/restart/resume matrix proves which state survives and
which state is intentionally ephemeral.

#### P1.4 Context, retry, and idempotency observability

Expose a consistent per-turn diagnostic record containing model, context
window, usable budget, compaction, retry attempt, replayed tool calls, token
usage, cost, and terminal reason. Ensure every retry path is idempotent or
explicitly marked non-retryable.

**Acceptance:** cassette tests cover timeout, partial stream, provider error,
compaction, tool replay, and hard-crash resume with no duplicate side effect.

#### P1.5 One command and trigger registry

`commands/builtins.py` is the current command registry, while
`tui/input/completions.py` now provides only compatibility adapters over it.
Picker visibility, dispatch, aliases, argument hints, plugin source, and
session-stateful interception are consolidated behind the unified registry.

**Acceptance:** every registered command appears in completion and has either a
handler or an explicit session interceptor; tests cover `/workflow` and
`/compact` as stateful exceptions. The compatibility registry is also tested
against the canonical command objects.

#### P1.6 Tool execution contract

Unify callable-based lauren-ai tools and `Tool` subclasses behind a typed
execution context, result envelope, capability metadata, approval decision,
timeout, and error taxonomy. Add a generated tool catalog showing built-ins,
capabilities, source, and destructive behaviour.

**Implementation status (2026-07-22):** the shared adapter is implemented in
`agenthicc.tools.executor`. lauren-ai remains authoritative for dispatch,
context injection, hook ordering, approval signals, approved-call resumption,
and provider-facing results. Agenthicc supplies compatibility adapters for
legacy `Tool` classes, typed `ToolBase` classes, catalog metadata, timing,
sandbox handles, and stable error classification. The focused contract and
integration suites cover filesystem, Git, exec, Outlook, MCP, and plugin
registrations at 94% coverage across the changed execution surface.

S3 is currently a filesystem backend selected by the existing router rather
than a standalone `Tool` class; it therefore uses the same contract when
exposed through a callable or tool wrapper, while the backend-specific tests
remain responsible for its storage semantics. The adapter deliberately does
not retry side-effecting calls; retry and replay remain owned by lauren-ai's
runner and idempotency layer.

**Acceptance:** file, git, exec, Outlook, MCP, S3, and plugin tools all report
consistent success, denial, timeout, network, and provider errors.

#### P1.7 Configuration validation and effective-config reporting

Validate unknown sections/keys, types, ranges, provider/model combinations,
paths, MCP transports, and security settings. Show the source layer for each
effective value in `config show`. Ensure every field on settings dataclasses is
loaded from TOML, or mark it intentionally runtime-only.

**Acceptance:** invalid configuration fails before an agent turn; a redacted
effective config can be saved in support reports; secrets never print.

#### P1.8 Packaging and installation UX

Add an explicit dependency matrix for core, TUI, MCP, cloud/S3, Outlook, docs,
and development. Publish wheels and sdists in a clean environment, test
`python -m agenthicc`, and document provider setup without assuming a local
checkout.

**Acceptance:** install instructions work with both `uv` and `pip`; optional
features fail with an actionable message rather than import-time ambiguity.

### P2 — scale, simplify, and improve product reach

#### P2.1 Public API and generated reference

After the API decision, generate endpoint/schema docs from the implementation
and expose a versioned client or stable event schema. If no server is planned,
invest instead in a stable Python API for session and workflow orchestration.

#### P2.2 Event schema versioning and compatibility

Add event schema versions, migration functions, stable payload dataclasses, and
golden logs. Unknown event types should be reported with policy-controlled
forward compatibility rather than silently ignored.

#### P2.3 TUI accessibility and cross-platform parity

Test terminal widths, Unicode fallback, color-disabled mode, non-TTY startup,
Windows key decoding, resize handling, pasted input, and screen-reader-friendly
text. Keep Rich workspace rendering and input backends independently testable.

#### P2.4 Extension SDK

Provide documented, versioned interfaces for tools, agents, modes, workflows,
skills, commands, and MCP servers. Include lifecycle hooks only after defining
their execution and security semantics in the current runtime.

#### P2.5 Performance and resource controls

Measure startup, model-turn overhead, memory growth, event-log replay, SQLite
contention, large file reads, semantic search, and concurrent subagents. Add
benchmarks before replacing working primitives with more infrastructure.

#### P2.6 PRD and knowledge-base governance

Create a generated PRD index with status, owner, supersedes/superseded-by links,
implementation commit, and verification links. Fold duplicate findings from
`knowledge/`, docs reviews, changelog entries, and PRDs into one searchable
decision log.

## 6. Recommended delivery sequence

| Phase | Deliverables | Exit condition |
|---|---|---|
| 0 — Truth baseline | Documentation rewrite, API decision, state-boundary document, stale-path scan | Public docs match the checked-in source tree |
| 1 — Release gate | Packaging extras, metadata, docs dependencies, real llms checker, blocking CI | Clean checkout passes all documented checks |
| 2 — Runtime safety | Plugin threat model, config validation, processor failure semantics | Security and failure tests are deterministic |
| 3 — Workflow durability | Workflow review fixes, resume matrix, command registry consolidation | No known workflow finding lacks a test or retirement note |
| 4 — Product expansion | API or stable Python API, extension SDK, TUI parity, benchmarks | New surfaces have versioned contracts and generated docs |

## 7. Measurement plan

Track these metrics before and after each phase:

- percentage of README/docs code blocks executed in CI;
- number of stale local-module links and undocumented public symbols;
- clean-install success rate for core and each optional feature group;
- reducer/event failure visibility and dropped subscriber count;
- successful crash/resume cases with zero duplicate side effects;
- workflow resume correctness across every phase and rejection path;
- startup time, event replay time, memory growth, and tool-call latency;
- percentage of extension types covered by trust and capability tests.

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Documentation cleanup hides useful historical design context | Keep historical PRDs and label them; link current docs to the relevant decision |
| State-model unification becomes a large rewrite | First document and test the boundary; defer merging models until evidence supports it |
| Tightening security breaks existing plugins | Add explicit trust/approval migration messages and a temporary audit mode |
| Making CI blocking exposes accumulated type or docs debt | Fix in small gates; publish the exact failing command and owner |
| API implementation expands scope prematurely | Resolve the product decision before adding dependencies or endpoints |

## 9. Definition of done for this PRD

This PRD is complete when:

1. The repository has one current architecture and contributor guide.
2. README and `docs/` contain only verified current instructions or explicit
   planned/historical labels.
3. Packaging and CI commands are executable from a clean checkout.
4. The API surface decision is implemented in metadata, code, docs, and tests.
5. P0 findings have regression coverage and a maintainer-approved roadmap for
   P1/P2 work.
6. The PRD index links this document and records its implementation status.
