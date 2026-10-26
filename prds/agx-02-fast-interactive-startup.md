---
title: "AGX-02: Fast interactive startup — time to first prompt"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P0
series: AGX
related_prds: [agx-00, agx-01, agx-14, prd-176, prd-191]
tags: [startup, tui, performance, imports]
---

# AGX-02 — Fast interactive startup: time to first prompt

## Implementation status (2026-10-23)

Implemented. A new TUI session in a pseudo-terminal (120×40, empty `HOME`, no
provider key), measured on the development host at load average ≈ 50 on 8
vCPUs, so CPU times are inflated:

| Measure | Before | After |
|---|---:|---:|
| Modules loaded at first frame | 665 | 363 |
| CPU until input is accepted | 5.4–5.8 s | 2.0–2.4 s |
| lauren-ai, pydantic, anyio, keyring, secretstorage, `rich._emoji_codes` at first frame | loaded | none |
| First frame → agent runtime ready (warm cache) | — | 5.6 s |

What moved behind the first frame (§5.2–5.4):

- **Agent runtime.** `TUISession` starts an `agent_runtime` phase right after
  the frame. On worker threads it folds the conversation journal
  (`provider_journal_restore`, through the new
  `SessionConversation.open_deferred`/`load`) and builds the provider config,
  transport and runner (`provider`, through `_LazyAgentRunner.warm_up`).
  `SessionContext.session_memory` became a property over the conversation, so
  any earlier access folds synchronously and stays correct. Messages sent
  while the runtime loads are queued exactly like messages sent while a turn
  runs; read-only and control commands answer at once. Paused-workflow,
  provider-history and interrupted-turn recovery run once the memory exists,
  before queued input. Turns, workflow resumes and `/compact` wait for both
  phases and report a failed one. Headless sessions keep eager loading.
- **lauren-ai imports.** Besides the runtime, `agenthicc.memory` (whose
  package init imported the vector store), `tools.capabilities` (decorators now
  import lauren-ai when applied) and `tools.approval` (type-only import) had
  pulled lauren-ai onto the first-frame path.
- **Account check.** `TokenStore` caches the derived tier in
  `~/.agenthicc/account-tier.json` (mode 600, no token material, rewritten by
  `save`/`clear`, fresh for 24 h); the TUI resolves it on a worker thread after
  the frame. `AuthClient.get_token()` reads and saves through a worker thread.
- **Rendering.** The session console and status/footer helpers pass
  `emoji=False` (§5.5).
- **Stores.** Memory layers, the background operator inbox and the workspace
  file cache create their SQLite databases on first use; `importlib.metadata`
  loads only for plugins that declare requirements. The agents manager, which
  needs its inbox at once, prepares it on a worker thread as it opens, so its
  first approval, input or deletion does not pay for the schema.
- **Diagnostics.** `/startup` prints process start → first frame, first frame
  → agent runtime ready and modules at the first frame (§5.6), from
  `StartupCoordinator.milestones()`; the process start is
  `agenthicc.STARTED_AT`.

Deviations:

1. The first-frame module budget is ≤ 370, not ≤ 350: asyncio and its
   transports account for 87 of the 363 modules, and the welcome panel's table
   layout for about 6 more.
2. The tier cache does not expire with the access token, which lives for an
   hour and says nothing about the plan; login, refresh and logout rewrite it.
3. No test-only hook was added to `Workspace`; the start-up test wraps
   `StartupCoordinator.finish` in the probe process instead.
4. Wall-time and CPU budgets (§6) are enforced by the AGX-14 benchmark job on a
   reference host. Most of the remaining runtime-ready time is lauren-ai's
   eager package `__init__` (≈ 2.3 s CPU and 401 modules here, against ≈ 0.7 s
   and 172 modules for the submodules agenthicc uses); the upstream lazy
   `__init__` in §5.2 remains the follow-up that shortens it.

Found on the way: extension discovery fingerprinted the whole `~/.agenthicc`
directory — every session journal and log — instead of `~/.agenthicc/skills`,
so its cache never hit and its cost grew with history. Fixed.

Tests: `tests/e2e/test_first_frame_budget_e2e.py` (pseudo-terminal deny-list
and module budget), `tests/unit/test_tui_runtime_gate.py`,
`tests/unit/test_deferred_agent_runtime.py`, `tests/unit/test_account_tier.py`,
`tests/unit/test_lazy_session_stores.py`, and milestone tests in
`tests/unit/test_startup.py`.

## 1. Summary

PRD-176 made start-up progressive and set a budget of p95 ≤ 2.0 s for the first
stable TUI frame. Today `agenthicc` needs 6–8 s of CPU and imports 932 unique
modules before its prompt appears, and no benchmark notices, because
`scripts/benchmark_startup.py` measures `--version`, `--help` and session-service
probes but never the TUI. Put the agent runtime, provider HTTP stack, session
transport and account checks behind the first frame, make the measurement part
of CI, and restore PRD-176's promise with a tighter budget.

## 2. Problem and evidence

### 2.1 Measurement

`agenthicc` launched in a pseudo-terminal (120×40, empty `HOME`, no provider
key), timed until the composer prompt is drawn (AGX-00 §4.1):

| Run | Wall to first prompt | CPU until prompt |
|---|---:|---:|
| 1 | 7.82 s | 6.24 s |
| 2 | 11.11 s | 7.31 s |
| 3 | 10.41 s | 6.95 s |

Load was ≈ 19 on 8 vCPUs, which inflates wall time; the 6–8 s of CPU is
the load-independent signal and alone exceeds PRD-176's 2 s budget.

### 2.2 What runs before the prompt

`python -X importtime -m agenthicc` in the same pseudo-terminal, cut at the
first prompt, records 932 unique modules. Largest contributors (cumulative,
under `-X importtime`, so proportions matter more than absolutes):

| Import | ≈ Cost | Why it is on the critical path |
|---|---:|---|
| `lauren_ai` package (178 modules: pydantic, pydantic-core, anyio, `lauren`) | 3.0 s | Any `lauren_ai.*` import runs the package `__init__`, which eagerly imports the whole public API |
| `aiohttp` via `agenthicc.session_service.transport` | 1.5 s | The local session server transport is imported while building the session |
| `httpx` / `httpcore` | 1.1 s | Provider and shared HTTP clients are constructed eagerly; `httpx` also imports `click` through its optional CLI module when click is installed |
| `rich.console`, `rich._emoji_codes` | 0.6 s + 0.3 s | Rendering is needed, but emoji replacement in markup loads a large table on first use |
| `agenthicc.background`, `agenthicc.worktrees`, `agenthicc.runners.tui_session` | 0.4–0.6 s each | Large modules with eager imports of their own |

### 2.3 A blocking account check

After the transcript preview, `TUISession` checks whether to show ads
(`src/agenthicc/runners/tui_session.py:4157`):

```python
auth = AuthClient()
bndl = auth.current_bundle()          # → keyring.get_password(...)
```

This runs synchronously on the event loop. The first `keyring` call performs
backend discovery, which on Linux imports every backend, including
`keyring.backends.macOS.api`, and `secretstorage`, which talks to D-Bus. A slow
or absent secret service stalls input and rendering.

### 2.4 No regression signal

PRD-176's acceptance checklist covers `--version`, `--help` and the session
service. Nothing exercises a real TUI start, so the regression from its 2 s
budget went unnoticed.

## 3. How others approach it

- OpenCode documents `opencode run --attach <url>` to reuse a running server
  and avoid cold MCP boots; the TUI and server are separate processes.
- Codex ships a native binary.
- Claude Code's start-up is not documented in the sources reviewed; its
  subagent docs note that the built-in Explore and Plan agents skip CLAUDE.md
  and the git status snapshot "to stay fast", the same principle of keeping
  optional context off the critical path.

## 4. Goals and non-goals

**Goals**

1. The prompt accepts keys after loading only what the first frame needs:
   configuration, terminal backend, workspace rendering, conversation store,
   session lease and (when resuming) the transcript preview.
2. Agent runtime, provider clients, session transport, MCP, plugins, account
   and ads checks start after the first frame, off the event loop where they
   can block.
3. A pseudo-terminal benchmark measures time and CPU to first prompt in CI
   (AGX-14), and `/startup` shows the same milestones.

**Non-goals**

- A persistent background daemon that pre-imports the runtime. It may follow if
  the budget cannot be met in-process; it needs its own ownership, upgrade and
  security design.
- Changing what a submitted turn waits for: the first turn may wait for the
  agent runtime to finish loading, and says so.

## 5. Design

### 5.1 Critical-path contract

Define the first-frame module set explicitly. A start-up test launches the TUI
in a pseudo-terminal, records `sys.modules` at the moment the first frame is
flushed (through a test-only hook in `Workspace`) and fails if any module from a
deny-list is present:

```text
lauren_ai, httpx, httpcore, aiohttp, keyring, secretstorage, pydantic,
agenthicc.session_service.transport, agenthicc.mcp*, agenthicc.tools.playwright*,
agenthicc.tools.cloakbrowser*
```

### 5.2 Lazy agent runtime

- `TUISession` imports the agent-turn runner and lauren-ai types inside a
  readiness task started right after the first frame (PRD-176's
  `StartupCoordinator` already models such phases). A turn submitted before it
  completes waits on that phase and shows "Loading agent runtime…".
- **Upstream (lauren-ai):** make `lauren_ai/__init__.py` resolve its public
  names lazily (PEP 562 `__getattr__` with an explicit `__all__`), so importing
  one submodule no longer imports 178 modules. Agenthicc pins the lauren-ai
  version that ships this.

### 5.3 Lazy network stacks

- The shared HTTP client and provider clients are created on first use, not at
  session construction. `import httpx` moves with them.
- `agenthicc.session_service.transport` is imported only when the local
  session server is enabled, inside its start-up phase.

### 5.4 Account and ads check off the critical path

- Run `AuthClient().current_bundle()` with `asyncio.to_thread` after the first
  frame.
- Cache the derived tier (`pro` or not, with the bundle's expiry) in a
  permission-restricted file with a 24-hour freshness window, so ordinary starts
  never touch `keyring`. The cache stores no token material.

### 5.5 Rendering imports

- Pass `emoji=False` where agenthicc builds markup that never contains emoji
  shortcodes (status bar, footer, tables), so `rich._emoji_codes` loads only
  when a transcript actually needs it.

### 5.6 Diagnostics

Extend `/startup` with `process_start → first_frame`, `first_frame →
runtime_ready` and the module count at first frame, so users and bug reports
show the same numbers the benchmark records.

## 6. Budgets and acceptance criteria

Measured by the AGX-14 pseudo-terminal benchmark on an idle reference host,
best of 5:

| Milestone | Budget |
|---|---:|
| Wall time to first prompt, new session | ≤ 1.0 s |
| CPU to first prompt | ≤ 1.2 s |
| Unique modules at first frame | ≤ 350 |
| Wall time to first prompt, resumed session with a 10 MiB journal | ≤ 1.5 s |
| Agent runtime ready after first frame | ≤ 2.0 s |

1. None of the deny-listed modules are loaded at first frame.
2. `keyring` is not imported during an ordinary start with a fresh tier cache.
3. A turn submitted during runtime loading is accepted, waits visibly and runs
   normally once ready.
4. `/startup` reports the new milestones.

## 7. Test plan

- Pseudo-terminal start-up tests: deny-list at first frame, first-prompt time
  (AGX-14 budget job), resumed-session variant.
- Unit: tier cache freshness, permission bits and absence of token material.
- E2E: submitting before runtime readiness; MCP and plugin readiness still
  reported by `/startup`.

## 8. Risks

- **First-turn latency moves rather than disappears.** Mitigation: start the
  runtime phase immediately after the first frame; most users read the screen
  for longer than the load takes.
- **Upstream dependency.** The lauren-ai lazy `__init__` needs a coordinated
  release. Until then, agenthicc can import the runtime in the readiness task,
  which already removes it from the critical path.

## Sources

- OpenCode CLI (`run --attach`): <https://opencode.ai/docs/cli/>
- Claude Code subagents (Explore and Plan skip CLAUDE.md to stay fast): <https://code.claude.com/docs/en/sub-agents>
