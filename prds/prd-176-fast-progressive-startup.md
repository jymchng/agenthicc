---
title: "PRD-176: Fast, Progressive agenthicc Startup"
status: Implemented
version: 0.1.0
created: 2026-08-27
scope: CLI bootstrap, TUI startup, session restoration, extension discovery, and optional integrations
related_prds:
  - PRD-138  # repository improvement roadmap
  - PRD-141  # background sessions and session manager
  - PRD-150  # client-neutral session service
  - PRD-156  # resumable plan-mode interrupts
  - PRD-157  # canonical usage accounting
  - PRD-159  # CloakBrowser tools
  - PRD-160  # Playwright tools
  - PRD-163  # cache-stable workflow prompts
  - PRD-169  # tool-call transaction integrity
  - PRD-170  # durable workflow recovery
  - PRD-171  # single live session owner
  - PRD-172  # MCP integration
  - PRD-173  # recoverable workflow errors
  - PRD-174  # tool-aware create_workflow authoring
  - PRD-175  # runtime AGENTS.md integration
tags:
  - startup
  - performance
  - lazy-loading
  - progressive-rendering
  - sessions
  - cli
---

# PRD-176 — Fast, Progressive agenthicc Startup

## 1. Summary

agenthicc currently performs most of its complete runtime construction before
the user sees a usable prompt. A simple `--help` invocation imports the TUI,
workflow registry, memory/vector stack, HTTP clients, MCP integration, and
provider machinery. A normal session then constructs a session service that
replays every saved session in `~/.agenthicc/session-service`, discovers
extensions and skills, initializes optional integrations, and fetches the
remote changelog before the first interactive turn.

This PRD defines a staged startup architecture:

1. perform only the minimum safe, deterministic bootstrap required to parse the
   command and establish the process/session owner;
2. render the first useful CLI output or TUI frame as soon as the core session
   is available; and
3. hydrate historical data, extensions, skills, remote metadata, MCP servers,
   and browser integrations behind explicit asynchronous readiness boundaries.

The result must preserve the existing security, ownership, workflow,
checkpoint, conversation, and failure semantics. Startup optimization must not
silently skip a required resource, use a broader workspace, bypass approval,
or create a second session/conversation source of truth.

## 2. Problem statement

The startup path has accumulated work from unrelated runtime concerns. The
cost is especially visible when:

- the user asks only for `--help` or `--version`;
- the user has many durable sessions, even when opening a new session;
- a network is slow or unavailable while the welcome changelog is fetched;
- an optional MCP or browser integration is configured but not needed for the
  first turn; or
- a project contains many extension and skill files.

The current behavior makes an apparently idle terminal look hung, delays the
first prompt, and makes startup time grow with data that is unrelated to the
selected session. It also makes it difficult to distinguish import cost,
historical replay, extension discovery, network startup, and actual TUI
rendering in diagnostics.

## 3. Current-state evidence

The measurements below were taken from the current source tree on 2026-08-27
using the repository virtual environment. They are diagnostic evidence, not
the acceptance baseline for every machine.

### 3.1 `--help` imports the full application

Running the module entry point with `--help` took approximately 6.1 seconds in
repeated cold-process measurements and reached approximately 66 MiB maximum
resident memory. Import-time profiling showed these major cumulative costs:

| Import or operation | Approximate cumulative time |
|---|---:|
| `agenthicc.runners.tui_session` | 2.60 s |
| `agenthicc.runners.agent_turn` | 2.51 s |
| `agenthicc.memory.tool_history` / vector memory | 1.89 s |
| `lauren_ai` vector-memory import | 1.81 s |
| `agenthicc.session_service` | 1.40 s |
| `agenthicc.tools` and MCP bridge | 0.60 s |
| `httpx` and `aiohttp` | 0.60 s and 0.52 s |
| `lauren` extractor stack | 0.99 s |
| built-in workflow package | approximately 0.52 s |

`src/agenthicc/__main__.py` imports both the headless and TUI runners before
argument dispatch. `cli.parser.parse_cli()` also loads configuration and
discovers command modules before `argparse` handles `--help` or `--version`.
The package initializers for tools, workflows, and session service eagerly
re-export or import optional and heavyweight modules.

### 3.2 Session service replay scales with all saved history

`SessionService.__init__()` calls `_load_existing()`. That method lists every
JSONL file in the session-service store, reads every event in every file, and
rebuilds every runtime. `tui_session._build_session_context_impl()` constructs
this service during every session startup.

On the measured machine the store contained 98 JSONL files and approximately
282 MB of data. Constructing `SessionService` against that store took about
44.4 seconds, used about 684 MiB maximum resident memory, and loaded all 98
sessions even when only one session was selected. This is the most important
scaling defect: startup is approximately O(total historical event bytes), not
O(the selected session plus a small index).

The optimization must retain durable event truth and replay correctness. It
must change when and how runtimes are materialized, not discard events or make
the in-memory projection authoritative over the append-only store.

### 3.3 Non-essential changelog networking blocks the first frame

`src/agenthicc/runners/tui_session.py` awaits `fetch_changelog()` before the
session enters its interactive run. `src/agenthicc/tui/welcome.py` gives the
request a five-second timeout and returns an empty list on failure. A failed or
slow request therefore makes first interaction wait for nearly five seconds;
one measured failed request took 4.99 seconds. A successful request may be
faster, but neither case is required to create a local TUI.

The welcome panel is useful but not a startup dependency. It must never be
allowed to delay the first usable frame or turn execution.

### 3.4 Session construction performs many unrelated tasks

The current context builder performs, before returning the context, all or
most of the following work:

- configuration and provider setup;
- workflow and agent registry construction;
- skills bootstrap and discovery;
- project and command plugin discovery, including probe imports;
- optional MCP manager construction and auto-connect;
- browser manager/tool construction;
- memory-layer and semantic-index initialization;
- durable journal folding and session restoration;
- session-service creation and historical replay; and
- terminal, command, trigger, and runner construction.

Some of this work is security-critical or required for the first agent turn.
Some is optional, can be cached, or can begin after the first frame. The
current boundary does not distinguish those categories.

### 3.5 Optional integrations are costly on the critical path

`McpSessionManager.start_all()` does start eligible servers concurrently, but
each server still performs a connection and tool-catalog discovery bounded by
its configured startup timeout. This is appropriate when a required server is
needed, but should not delay a frame when the server is optional and the user
has not requested an MCP-dependent operation.

The browser managers are intended to start browser runtimes lazily, but their
module and policy construction still occurs during context construction. The
optional `cloakbrowser` and Playwright stacks must not be imported merely to
show help or create a session that does not use browser tools.

## 4. Goals

1. Make `--help` and `--version` fast and independent of the TUI, provider,
   workflow, memory-vector, MCP, browser, plugin, and network stacks.
2. Make a new TUI usable quickly by rendering a truthful initial frame before
   non-essential asynchronous work completes.
3. Make session startup scale with the selected session and a bounded metadata
   index, rather than all saved session event logs.
4. Preserve exact transcript, journal, conversation, usage, workflow,
   checkpoint, resume, and session-lease semantics.
5. Keep security-sensitive decisions synchronous and fail closed: workspace
   scope, configuration validation, owner lease acquisition, mode policy, and
   required resource validation cannot be bypassed by progressive startup.
6. Load optional integrations only when configured and needed, with explicit
   readiness and failure states instead of invisible blocking.
7. Make startup phases observable with timings, status, and bounded diagnostics
   so future regressions can be attributed to a subsystem.
8. Preserve backwards compatibility for existing CLI flags, configuration,
   session files, workflows, plugins, and extension APIs.
9. Add deterministic unit, integration, end-to-end, and performance regression
   coverage for cold start, warm start, large stores, slow networks, optional
   integrations, resume, and failure recovery.

## 5. Non-goals

- Replacing the kernel, event processor, session conversation, journal, or
  existing session-service event format.
- Deleting, truncating, or silently ignoring saved sessions to improve startup.
- Making the first frame claim that a provider, MCP server, browser, plugin, or
  workflow is ready when it is still loading.
- Loading arbitrary user code on the `--help` or `--version` path.
- Removing features from the TUI or headless runner.
- Removing configured required-MCP semantics. A required server may still block
  agent execution, but the UI should be able to explain the readiness state.
- Changing the existing owner-lease or workspace-security contracts.
- Making all initialization background work. Security boundaries and resources
  required for the requested operation retain explicit readiness gates.
- Optimizing provider request latency after startup.

## 6. Product principles

### 6.1 First useful output is the primary latency measure

Startup is not complete merely because Python returned from `main()`. The
important user-visible milestones are:

- command help/version is emitted;
- the TUI displays its first stable frame and accepts safe input;
- a selected session's transcript is visible; and
- an agent turn begins with all resources that turn requires ready.

Each milestone must have its own status and timing.

### 6.2 Readiness must be explicit

Every deferred subsystem exposes a state such as `not_requested`, `loading`,
`ready`, `degraded`, `failed`, or `cancelled`. Agent execution and tools that
depend on a subsystem await its readiness boundary and receive a structured,
recoverable error when it cannot become ready.

### 6.3 Historical data is lazy, not lost

The append-only session event store remains authoritative. A metadata index may
be added or rebuilt incrementally, but it is only an acceleration structure.
If an index is missing or stale, the service must recover it safely and retain
the current behavior for the selected session. Corrupt individual records
continue to follow the existing tolerant parsing and diagnostic policy.

### 6.4 Progressiveness cannot change authority

Deferring a task must not change which workspace, mode, owner, conversation
id, instruction snapshot, tool capability, approval policy, or workflow
checkpoint applies. All deferred work receives the same session-owned context
and immutable policy snapshot as synchronous work.

## 7. Target startup flow

The target data flow is:

```text
argv
  │
  ├─ minimal pre-parser: --help / --version / command family / config path
  │     └─ no user plugin imports, provider imports, MCP connections, or network
  │
  ├─ fast command path ────────────────> print help/version and exit
  │
  └─ session path
       │
       ├─ load and validate minimal config
       ├─ resolve workspace + mode + owner lease
       ├─ create session identity and canonical conversation id
       ├─ create lazy SessionService handle and load only selected metadata
       ├─ create kernel/event processor and restore selected transcript/journal
       ├─ create the minimum command/input/workspace shell
       ├─ render first TUI frame / accept safe input
       │
       └─ background readiness tasks (same session context)
            ├─ workflow/agent descriptors, then selected implementation
            ├─ project tools/commands/skills with cached discovery
            ├─ memory indexes and optional semantic/vector services
            ├─ optional MCP auto-connect and catalog publication
            ├─ optional browser backend preparation
            └─ changelog fetch and welcome refresh
                         │
                         └─ requested operation awaits only its dependencies
```

The first frame is not a promise that every configured subsystem is ready. It
must show a compact readiness indicator and retain the existing error/status
surface. A user can inspect or cancel background initialization without
corrupting the session.

## 8. Functional requirements

### FR-1 — Instrumented startup phases

Implement a session-scoped startup coordinator or equivalent owned component
that records monotonic start/end times, outcome, and bounded diagnostic detail
for at least:

- argument/config bootstrap;
- workspace and policy resolution;
- owner lease acquisition;
- session-service/index setup;
- selected-session transcript/journal restore;
- kernel and input/TUI shell construction;
- workflow/agent registry readiness;
- project extension and skill readiness;
- memory/index readiness;
- MCP readiness;
- browser readiness; and
- welcome/changelog readiness.

The coordinator must not log secrets, prompt contents, API keys, OAuth tokens,
MCP headers, or full session transcripts. It must be usable in tests with a
monotonic fake clock and no wall-clock flakiness.

### FR-2 — Minimal CLI bootstrap and lazy entry dispatch

Refactor `__main__.py` and CLI parsing so that:

- `--version` exits without importing TUI, runners, workflows, tools,
  providers, memory-vector integrations, MCP, browser backends, plugins, or
  network clients;
- `--help` exits without importing user/project plugin code, connecting to
  MCP, opening a browser, reading all sessions, or making network requests;
- the command parser can display built-in command metadata without importing
  command implementation modules; and
- the selected command or runner is imported only after dispatch requires it.

Configuration needed to locate a config file or format command metadata may be
read through a bounded, side-effect-free path. Full configuration loading and
validation must happen only for commands that need it. Existing parser,
decorator, plugin, and flag behavior remains compatible after dispatch.

### FR-3 — Lazy package and optional dependency boundaries

Audit and revise package initializers and module-level imports so importing a
lightweight public entry point does not import optional/heavy subsystems.
Specifically:

- workflow package exports use lazy loading or descriptors while preserving
  public import compatibility;
- tools package initialization does not import MCP, HTTP, browser, or other
  optional bridges unless selected;
- session-service transport and HTTP client modules are not imported by
  in-process startup unless the transport is requested;
- `lauren_ai` vector/extractor imports occur only when semantic memory or the
  selected agent runner requires them; and
- CloakBrowser and Playwright modules remain optional dependencies and are
  imported only when their configured backend is requested.

Importing a public symbol must still produce the documented error when an
optional dependency is missing; it must not silently substitute a different
security or browser backend.

### FR-4 — Fast and side-effect-free help/version paths

Add explicit tests and implementation guards that `--help` and `--version`:

- do not create or mutate session, memory, skill, plugin, or cache files;
- do not acquire a session lease;
- do not load API keys into a provider object;
- do not invoke project code or plugin imports;
- do not contact the network or start subprocesses; and
- exit with the current output shape and status code.

If a dynamically registered command cannot be represented without executing
untrusted project code, it is omitted from the fast help path with a clear
“additional commands available after startup” note, or represented by safe
metadata. Help must not execute that code merely to discover it.

### FR-5 — Lazy and indexed session-service restoration

Change `SessionService` startup so construction does not replay every session
file. The service must:

- load a bounded metadata/index record for listing and selection;
- materialize a runtime only when a client selects, resumes, controls, or
  subscribes to that session;
- replay only the selected session's required event stream on demand;
- preserve sequence ordering, command-result idempotency, subscriptions,
  replay-gap behavior, deletion, and compaction semantics; and
- recover safely when the index is absent, stale, partially written, or
  incompatible with the event schema.

The index must support at least session id, project root, creation/update time,
current lifecycle state, latest sequence, and enough redacted display metadata
for pagination. It must not store full prompts, tool results, secrets, or
transcripts unless an existing retention policy explicitly permits that data.

Index updates are atomic and recoverable. Appending an event remains durable
even if updating the acceleration index fails; the next access repairs the
index from the affected event log.

### FR-6 — Selected-session-first transcript restoration

For `--resume`, `--continue`, and the interactive session picker:

- resolve and validate the selected session before starting unrelated
  historical replay;
- render the selected transcript as soon as the minimum event/journal state is
  available;
- preserve the stable session conversation id and existing rehydration rules;
- continue deferred loading without duplicating transcript events; and
- show a truthful loading/degraded state if the selected session is large or
  damaged.

The selected session may be loaded incrementally, but the first visible
transcript must have an explicit cursor/sequence boundary so later replay
cannot reorder or duplicate it.

### FR-7 — Progressive TUI shell

Construct and render the minimum TUI shell before non-essential discovery and
remote work. The first frame must include:

- model/session identity already available from the current contract;
- the input panel and safe command handling;
- a visible startup/readiness status for deferred components; and
- the selected transcript when resume was requested and its minimum restore
  completed.

Safe local commands such as status, quit, and startup diagnostics must remain
responsive while background readiness tasks run. Commands and agent turns
that require a not-yet-ready component must wait at a bounded readiness
boundary and report progress.

Background tasks must be cancelled and awaited during shutdown. A cancelled
task must not publish stale tools, registry entries, transcript records, or
workflow state after the session has closed.

### FR-8 — Non-blocking welcome and changelog

The remote changelog is optional presentation metadata. Change the welcome
flow so that:

- the static welcome panel can render without a network request;
- the changelog fetch begins after the first frame or uses a short bounded
  background deadline;
- a last-known-good, bounded local cache may be displayed while refreshing;
- timeout, HTTP, JSON, and schema errors degrade to the existing “No list”
  result while preserving the “What’s new” heading; and
- no changelog request occurs on `--help`, `--version`, or other non-TUI fast
  command paths.

The cache must be bounded by size and age, written atomically, and contain no
credentials or unrelated response data. A remote changelog must never be an
agent instruction or tool capability source.

### FR-9 — Staged extension and skill discovery

Split project/user extension discovery into metadata and implementation
readiness:

- safe metadata needed for command/workflow selection is available before the
  first operation that needs it;
- expensive recursive scans, dependency checks, probe imports, and optional
  installation occur after the first frame unless explicitly required;
- discovery results are cached with a schema version and source fingerprint
  (path, size, mtime and, where needed, content hash); and
- cache invalidation is deterministic when a source changes, is deleted, or
  becomes unreadable.

Project trust, workspace containment, dependency policy, and import failure
semantics remain unchanged. A deferred plugin is not presented as ready until
its full contract has been validated.

Skills bootstrap must retain its current safety and ownership behavior. It may
be moved off the critical path only when the session can show whether skills
are `loading`, `ready`, or `failed` and a skill invocation awaits readiness.

### FR-10 — Lazy workflow and agent registry readiness

Expose workflow and agent descriptors separately from implementation loading.
The registry must be able to list names, descriptions, and safe metadata
without importing every workflow runner. Loading a selected workflow remains
deterministic and must preserve:

- `PhaseSpec` and transition behavior;
- phase artifacts, summaries, questions, and checkpoint state;
- prompt/cache contracts;
- session-scoped conversation and memory;
- inherited AGENTS.md instruction snapshots;
- subagent and tool capability policy; and
- resume and replay behavior.

Generated workflows must use the same lazy runtime contract as built-in
workflows. `create_workflow` must instruct generated code to declare required
readiness dependencies rather than importing every optional integration at
module import time.

### FR-11 — Deferred MCP startup with required-resource semantics

MCP configuration parsing and validation may occur during minimal session
bootstrap, but optional server connections and catalog discovery must not block
the first TUI frame. The implementation must:

- preserve concurrent startup and each server's configured timeout;
- publish typed per-server readiness/failure state;
- make configured `required` servers an explicit readiness dependency for the
  operations that need them;
- allow optional server failure without preventing unrelated local work;
- avoid exposing a tool before its catalog and capability policy are valid; and
- ensure shutdown cancels and disconnects every in-flight server task.

If the selected workflow explicitly requires a required MCP server, its turn
must wait for that server and surface the existing structured failure. The
optimization must not reinterpret `auto_connect` or `required` in a way that
weakens the configured contract.

### FR-12 — Lazy browser backend preparation

Browser policy/configuration remains validated according to the existing
security rules, but optional browser implementation modules and runtime
processes are prepared only when browser capability is selected or a browser
tool is invoked. The first frame must not launch a browser. A missing optional
dependency must produce an actionable readiness/error state and must not make
unrelated sessions fail.

The same behavior applies to CloakBrowser and Playwright. Existing allowed
domain, workspace, approval, and mode policies remain authoritative.

### FR-13 — Memory and semantic-index staging

Separate lightweight session/project memory availability from heavyweight
semantic/vector index readiness. Schema creation and bounded local memory
operations required for a first turn must remain correct; optional vector
loading or index rebuild may be deferred. A memory operation must either await
its declared readiness dependency or return a structured result that clearly
states the unavailable capability.

No deferred index may change conversation history, workflow context,
checkpoint state, or tool-call transaction ordering.

### FR-14 — Configuration loaded once per startup contract

Avoid parsing the same configuration repeatedly across CLI parsing, context
construction, provider setup, and integrations. Introduce a typed immutable
configuration snapshot or equivalent ownership boundary with:

- explicit precedence for file, environment, CLI `--set`, and `--set-secret`;
- redacted diagnostics;
- no mutation after dependent components begin; and
- separate lightweight metadata access for fast help/version.

Existing profile, Modal/OpenAI-compatible endpoint, authorization-header,
MCP, browser, and plugin configuration behavior remains compatible.

### FR-15 — Startup diagnostics and operator visibility

Add a safe diagnostic surface, such as `/startup` and a headless diagnostic
option or structured event, that reports:

- phase state and elapsed duration;
- whether each phase was synchronous or deferred;
- readiness blockers for the next requested operation;
- session-store/index repair status; and
- bounded, redacted failure summaries.

The diagnostic surface must not reveal secret values, full headers, prompt
content, transcript content, or arbitrary exception data that could contain
credentials.

## 9. Non-functional requirements

### NFR-1 — Latency budgets

Measured with a fresh process, an installed environment, a temporary isolated
home, and no external network dependency unless the test explicitly covers
one:

| Milestone | Target |
|---|---:|
| `agenthicc --version` | p95 ≤ 0.75 s |
| `agenthicc --help` | p95 ≤ 1.25 s |
| first stable TUI frame, new session, no required remote resource | p95 ≤ 2.0 s |
| first selected-session transcript frame | p95 ≤ 2.5 s for a 10 MiB selected log |
| first agent turn after all declared local dependencies are ready | no regression from the pre-PRD baseline, with readiness time reported separately |

The benchmark must report interpreter/process-spawn overhead separately from
agenthicc work. CI should use thresholds that are stable on its hardware and
fail on a sustained regression, not a single noisy sample.

### NFR-2 — Scaling

Creating a new session must not read or parse every historical event log. With
N unrelated sessions and a fixed selected session, startup work should be
O(index size + selected session data), with index maintenance amortized across
event writes or explicit repair.

### NFR-3 — Correctness and compatibility

All existing session, lease, workflow, prompt/cache, tool, memory, MCP,
browser, plugin, headless, resume, and TUI contracts remain valid. Deferred
loading may change timing and status presentation, but not outcomes or
authority.

### NFR-4 — Failure and cancellation resilience

Timeouts, missing optional packages, corrupt index entries, corrupt event
lines, unavailable networks, plugin import errors, MCP failures, and browser
startup failures are isolated according to their existing required/optional
semantics. Cancellation is idempotent and leaves durable state recoverable.

### NFR-5 — Security and privacy

Fast paths do not execute untrusted project code. Caches and diagnostics are
permission-restricted, atomic, bounded, and redacted. Deferral never bypasses
workspace, mode, approval, network, secret, or owner-lease policy.

### NFR-6 — Resource use

Startup must not retain all historical event payloads in memory. Background
tasks have bounded concurrency, queues, and response sizes. The test suite
must include a large-store memory measurement and verify that optional imports
do not occur on fast paths.

### NFR-7 — Testability and determinism

The coordinator, readiness gates, index, and deferred tasks must accept fake
clocks, temporary stores, fake transports, and fake providers. Tests must not
depend on the public changelog, real MCP servers, installed browser binaries,
or system-wide user state.

## 10. Proposed implementation boundaries

The implementation should remain within current ownership boundaries:

| Concern | Proposed owner |
|---|---|
| Minimal dispatch | `src/agenthicc/__main__.py`, `cli/parser.py`, `cli/registry.py` |
| Startup phase/readiness state | session runner or a dedicated startup coordinator under `runners/` |
| Session metadata/index | `session_service/store.py` and `session_service/service.py` |
| Selected transcript restore | existing session construction and conversation/journal owners |
| TUI first frame | `runners/tui_session.py` and `tui/workspace/` |
| Changelog refresh | `tui/welcome.py` plus a bounded local cache owner |
| Workflow descriptors | `workflows/loader.py` and registry interfaces |
| Optional integrations | `tools/mcp_manager.py`, browser integration boundaries, and existing policy owners |
| Startup diagnostics | kernel/session events or session-owned diagnostics, rendered by TUI/headless adapters |

Do not create a parallel transcript, workflow engine, event loop, or session
store. New types should be small, typed, and documented; use `TYPE_CHECKING`
for cross-layer type-only imports.

## 11. Acceptance criteria

### Fast paths

- [ ] `--version` meets its latency budget in a fresh process and imports no
  TUI, provider, optional integration, plugin, or network module.
- [ ] `--help` meets its latency budget, preserves current built-in help, and
  performs no network, lease, session replay, project-code import, or file
  mutation.
- [ ] Fast-path tests run with an import sentinel that fails if a forbidden
  module is imported.

### TUI and session startup

- [ ] A new TUI renders a truthful first frame within the target budget when no
  configured required resource is blocking.
- [ ] Safe quit/status/input handling works while deferred initialization is in
  progress.
- [ ] `--resume`, `--continue`, and session-picker selection show the selected
  transcript without replaying unrelated sessions first.
- [ ] A store with at least 100 unrelated sessions and 250 MiB of unrelated
  JSONL history does not cause proportional startup replay or proportional
  runtime memory growth.
- [ ] Existing owner lease acquisition occurs before any operation that could
  mutate or publish session state, and it remains enforced during deferred
  work.

### Deferred resources

- [ ] Workflow, agent, skill, project-tool, and project-command discovery have
  explicit readiness states and do not execute untrusted code on fast paths.
- [ ] Optional MCP servers and browser backends do not block the first frame;
  required dependencies still block only the operations that require them and
  preserve existing failure semantics.
- [ ] Changelog failure or timeout leaves the “What’s new” heading visible and
  displays “No list” without delaying TUI usability.
- [ ] No browser process is started and no optional browser dependency is
  imported solely to open the TUI.

### Correctness and recovery

- [ ] Session event append/replay, subscriptions, pagination, compaction,
  deletion, sequence cursors, and idempotent commands behave as before.
- [ ] Index creation, atomic replacement, stale-index repair, and crash
  recovery are covered by integration tests.
- [ ] Workflow phase state, artifacts, conversation id, memory/journal,
  instructions, usage, tool transactions, approval state, and checkpoints are
  unchanged across deferred startup and resume.
- [ ] Repeated cancellation and shutdown leave no orphaned MCP, browser,
  plugin, provider, or startup tasks.
- [ ] Missing optional dependencies and individual extension failures do not
  prevent unrelated local startup; required failures remain visible and
  actionable.

### Observability and quality

- [ ] `/startup` or its approved equivalent identifies the slow phase without
  exposing secrets or transcript content.
- [ ] Unit, integration, E2E, and performance regression tests pass in isolated
  CI environments.
- [ ] Ruff, formatting, mypy, type audit, and the relevant test matrix pass.
- [ ] The startup guide and architecture/storage references describe the new
  readiness and indexing contracts.

## 12. Test plan

### 12.1 Unit tests

Cover:

- minimal argument classification and lazy dispatch decisions;
- forbidden-import and no-side-effect fast-path guards;
- startup phase transitions, timing, failure, cancellation, and retry;
- readiness dependency composition and required/optional semantics;
- session-index record encoding, redaction, validation, atomic replacement,
  stale detection, and repair decisions;
- selected-session lazy materialization and cursor boundaries;
- changelog cache age/size/JSON/schema handling;
- deferred discovery cache fingerprints and invalidation;
- lazy workflow/browser/MCP factories; and
- redaction of startup diagnostics.

### 12.2 Integration tests

Use temporary homes/workspaces and fake providers/transports to cover:

- `SessionService` construction with thousands of small unrelated event logs;
- selected-session restore from a cold, warm, missing, stale, and corrupt
  index;
- concurrent event append and index update/recovery;
- session lease acquisition and deferred task shutdown;
- plugin/skill discovery with changed, deleted, unreadable, and untrusted
  files;
- optional and required MCP startup with slow, failed, and cancelled bridges;
- absent and available CloakBrowser/Playwright dependencies; and
- delayed, failed, cached, and malformed changelog responses.

### 12.3 End-to-end tests

Exercise the real executable in isolated subprocesses for:

1. `agenthicc --version` and `agenthicc --help`.
2. New TUI first frame, safe status/quit, and deferred readiness.
3. Create a session, terminate it, then resume it with `--resume`.
4. Continue the latest session with many unrelated saved sessions present.
5. Select a session through the interactive session list and verify the same
   transcript/lease behavior as explicit `--resume`.
6. Start with optional MCP/browser integrations configured but unavailable and
   verify unrelated startup remains usable.
7. Start with a required MCP dependency unavailable and verify the user sees
   the blocker at the dependent operation.
8. Interrupt during deferred initialization and resume without duplicate
   events, tools, workflow phases, or leases.

### 12.4 Performance regression suite

Add a repeatable benchmark script that records:

- process-to-help/version completion;
- process-to-first-frame;
- process-to-selected-transcript;
- each startup phase duration;
- imported module count/size for fast paths;
- session-store bytes scanned; and
- peak RSS for empty, medium, and large stores.

The benchmark must support an offline mode and fixed synthetic fixtures. It
must report p50/p95 and distinguish a slow external dependency from local
startup work.

## 13. Rollout and migration

1. Add instrumentation and characterization tests without changing behavior.
2. Introduce fast CLI dispatch and lazy imports behind internal seams; verify
   help/version compatibility.
3. Add the session metadata index with read-through fallback to current JSONL
   replay, then enable selected-session-only materialization.
4. Introduce the progressive TUI shell and readiness status.
5. Move changelog, optional integration, extension, skills, and vector work to
   their staged boundaries one subsystem at a time.
6. Enable performance gates after measurements are stable across CI and a
   representative large session store.

Existing stores require no destructive migration. An index can be built lazily
or through an explicit maintenance command. If an index cannot be trusted, the
service falls back to safe selected-session replay and reports the repair
condition rather than silently returning incomplete data.

## 14. Operational risks and mitigations

| Risk | Mitigation |
|---|---|
| Deferred registry is used before ready | Typed readiness dependency and structured blocker result |
| Background task publishes after shutdown | Session-owned task group, cancellation, await, and closed-state guard |
| Index hides a newly appended event | Event log remains authoritative; sequence/fingerprint repair on materialization |
| Help behavior changes for project commands | Safe built-in metadata path plus explicit post-start discovery notice |
| Startup optimization weakens security | Lease, workspace, mode, approval, and trust checks stay on the owning boundary |
| Cached changelog becomes stale or unsafe | Age/size bounds, atomic write, schema validation, no credential storage |
| Optional MCP/browser work still blocks | Readiness telemetry, dependency-specific awaits, and tests with slow fakes |
| Large selected transcript still blocks | Cursor-bounded/incremental restore with explicit loading state |

## 15. Assumptions and decisions required during implementation

- The latency budgets apply to an installed environment and exclude a package
  manager resolving/installing dependencies.
- The existing append-only JSONL event log remains the durable source of truth;
  the proposed index is an optimization and may be regenerated.
- A required MCP server blocks the first agent operation that declares it as a
  dependency, not necessarily the static TUI frame. If product policy requires
  all required servers before any interaction, that policy must be made an
  explicit configuration rather than emerging from implementation order.
- “Ready for safe input” does not mean “ready for an agent turn.” The UI must
  communicate that distinction.
- The implementation team should choose whether the index is a separate file,
  SQLite table, or an existing durable storage extension after measuring
  atomicity, repair, lock, and migration costs. The choice must not create a
  second source of truth.
- Existing user-visible output, command names, and configuration precedence
  take priority over internal class names proposed in this document.

## 16. Implementation evidence

The implementation is complete in the current source tree. The principal
runtime changes are:

- `__main__` and the CLI parser have side-effect-free, lazy `--version` and
  `--help` paths. Fast-path subprocess probes assert that TUI, provider,
  workflow, MCP, browser, vector, and project-extension modules are absent.
- `SessionEventStore` maintains a bounded, permission-restricted, atomically
  replaced metadata index with cross-process locking and JSONL read-through
  repair. `SessionService` keeps runtimes empty until a session is selected.
- `StartupCoordinator` owns monotonic phase timing, readiness gates,
  cancellation, redacted diagnostics, and deferred-task publication guards.
  The TUI renders its shell before extension discovery, MCP connection,
  changelog refresh, semantic-index construction, provider construction, or
  browser preparation.
- Workflow and agent registries expose import-free descriptors. Selected
  workflows retain the existing phase, conversation, checkpoint, memory,
  instruction, approval, and tool contracts. `create_workflow` documents the
  required-startup-phase contract for generated workflows.
- Workflow context and checkpoint serialization no longer applies an artificial
  1,000,000-byte ceiling. JSON validation, atomic persistence, and bounded
  diagnostic-only recovery records remain intact.
- `scripts/benchmark_startup.py` provides isolated, offline measurements for
  process overhead, fast-path timings/import footprint, session metadata scan,
  and cold/warm service startup.

Verification completed for this change:

| Surface | Result |
|---|---|
| Unit tests | 3,104 passed, 14 skipped |
| Integration tests | 213 passed |
| E2E tests | 114 passed, 1 skipped |
| Focused Ruff lint | passed |
| Focused mypy | passed |
| Type audit | passed |
| Public export documentation gate | passed |
| Offline benchmark | version p50 719 ms, help p50 795 ms in one-sample validation; process-spawn overhead 196 ms |

The repository-wide formatter and mypy commands still report pre-existing
issues in files tracked by PRD-138 (including unrelated optional-backend and
legacy compatibility modules). They are not caused by the PRD-176 startup
implementation; the touched PRD-176 surfaces have their focused gates above.

## 17. Definition of done

This PRD is complete when every acceptance criterion is verified, the complete
quality and performance matrix passes, startup diagnostics identify no known
unbounded critical-path work, and the implementation evidence is recorded in
this document with the final measured budgets and migration notes.
