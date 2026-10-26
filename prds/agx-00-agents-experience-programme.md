---
title: "AGX-00: Agents Experience Programme — study, measurements and roadmap"
status: Proposed
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P0
series: AGX
related_prds: [prd-139, prd-141, prd-150, prd-157, prd-202, prd-206, prd-209, prd-219, prd-227, prd-232, prd-233, prd-244, prd-250]
tags: [agents, tui, performance, ux, roadmap]
---

# AGX-00 — Agents Experience Programme

The **AGX** series ("Agents eXperience") proposes how to make `agenthicc agents`
and the `agenthicc` terminal UI fast and pleasant to use. This document records
the study the series is based on: how the agents screen works today, what it
costs, how OpenCode, Claude Code, Codex and Grok Build present their agents, and
the order in which the AGX PRDs should land. Every AGX PRD cites the evidence
here instead of repeating it.

## 1. Summary

`agenthicc agents` is a capable control surface — durable sessions, a queue,
approvals and input, deletion with progress, PRD-209's off-loop refreshes — but
it is slow to reach, slow to open at scale, and hard to read:

- **Reaching it is slow and indirect.** It is a separate CLI command that you
  cannot open from inside a session. `agenthicc agents --json` spends 1.25 s of
  CPU and imports 440 modules on an empty registry; the interactive TUI needs
  6–8 s of CPU and 932 modules before its first prompt, against PRD-176's 2 s
  budget.
- **Opening it at scale is slow.** With 10,000 sessions a fresh process needs
  3.6–6.0 s to show the first page from the JSON snapshot, and 33 s when the
  snapshot is missing. PRD-209's budget for the first useful frame is 250 ms.
- **The screen is hard to read.** It calls itself "Background Sessions",
  truncates statuses to `wai…`, loses a column entirely, spends a column on an
  internal mode-application state, caps the list at 10 rows per page and shows a
  20-field details dump without what a waiting session actually needs.
- **The agent model is fragmented.** Running work is split across `agents`,
  `jobs`, `sessions list`, `agents --run`, `/ps`, `/workers`, `/inbox` and
  `/schedules`; subagents inside a session appear only as scroll-back lines;
  agent definitions have no listing or editing surface at all.

The other agents converge on a different shape: running work is **one keystroke
away from the prompt**, lists are **grouped by what needs you**, children are
**navigable** (open, follow, stop), definitions are **plain files with a
management view**, and rendering is **incremental**. Claude Code's agent view
(`claude agents`, §5.1) is the closest analogue to `agenthicc agents` and the
main reference for AGX-05 to AGX-07 and AGX-10. The AGX PRDs bring agenthicc
there.

## 2. The AGX series

| PRD | Title | Area | Priority |
|---|---|---|---|
| [AGX-01](agx-01-lazy-cli-command-manifest.md) | Lazy CLI command manifest and import budgets | Performance | P0 |
| [AGX-02](agx-02-fast-interactive-startup.md) | Fast interactive startup — time to first prompt | Performance | P0 |
| [AGX-03](agx-03-indexed-session-registry.md) | Indexed session registry for O(page) open and search | Performance | P0 |
| [AGX-04](agx-04-event-driven-agents-updates.md) | Event-driven live updates and targeted maintenance | Performance | P1 |
| [AGX-05](agx-05-agents-hub-information-architecture.md) | Agents hub: one surface for running work, attention, library and history | UX | P0 |
| [AGX-06](agx-06-agents-list-redesign.md) | Agents list redesign: status-first rows, attention grouping, search | UX | P0 |
| [AGX-07](agx-07-agent-detail-and-live-transcript.md) | Agent detail, live transcript and actions | UX | P1 |
| [AGX-08](agx-08-subagent-panel-and-navigation.md) | Subagent panel and child-session navigation | UX | P1 |
| [AGX-09](agx-09-agent-library-and-switching.md) | Agent library: Markdown definitions, management and switching | UX | P1 |
| [AGX-10](agx-10-attention-routing-and-notifications.md) | Attention routing: badges, notifications and cross-session approvals | UX | P1 |
| [AGX-11](agx-11-tui-render-pipeline.md) | TUI render pipeline: frame scheduling, caches and synchronized output | Performance | P1 |
| [AGX-12](agx-12-agent-turn-hot-path.md) | Agent-turn hot path: copy-on-write memory and incremental validation | Performance | P1 |
| [AGX-13](agx-13-unified-keymap-and-accessibility.md) | Unified keymap, generated help, mouse and accessibility | UX | P2 |
| [AGX-14](agx-14-performance-budgets-and-diagnostics.md) | CPU-time performance budgets in CI and perf diagnostics | Process | P0 |

### 2.1 Dependencies and order

```text
AGX-14 budgets ─┬─> AGX-01 CLI manifest ──> AGX-02 interactive startup
                ├─> AGX-03 indexed registry ──> AGX-04 event-driven updates
                ├─> AGX-11 render pipeline
                └─> AGX-12 agent-turn hot path

AGX-05 hub IA ──┬─> AGX-06 list ──> AGX-07 detail/transcript
                ├─> AGX-08 subagent panel ──> AGX-10 attention routing
                ├─> AGX-09 agent library
                └─> AGX-13 keymap/help/mouse (applies to all surfaces)
```

Land AGX-14 first so every later PRD is held to a measured budget. AGX-01,
AGX-03 and AGX-05/06 are the largest user-visible wins; AGX-03 also supplies the
search index AGX-06 needs, and AGX-04 needs AGX-03's change cursor.

## 3. How `agenthicc agents` works today

- **Entry.** `agenthicc agents` (`src/agenthicc/cli/commands/background.py:295`)
  opens `BackgroundManager` (`src/agenthicc/tui/workspace/background_manager.py`,
  3,036 lines, about 100 instance fields). `agents --json` prints the registry;
  `agents --run RUN_ID` opens a separate `GoalRunManager` (190 lines). `jobs` is
  an alias. `sessions list` opens a third list screen, `SessionManager` (278
  lines). None of them can be opened from inside a TUI session; `/ps`,
  `/inbox` and `/schedules` are separate in-session overlays, and `/workers`
  prints a table into scroll-back.
- **Data.** `BackgroundStore` (`src/agenthicc/background/store.py`) folds an
  append-only `events.jsonl` into session records, checkpoints a JSON snapshot
  (`projection-v1.json`) and keeps an order index. An unfiltered page is a slice
  of that index; any filter or search is a linear scan with substring matching
  (`_matching`, `_query_page_locked`).
- **Loop.** The manager waits up to `refresh_interval_s` (0.25 s by default) for
  a key read on a worker thread, requests an async page refresh every tick,
  schedules maintenance every 5 s —
  `recover_stale_batch` walks up to 1,024 sessions probing workers and pending
  input (`src/agenthicc/background/supervisor.py:2322`) — and redraws the whole
  frame with Rich `Live`.
- **Layout.** Header `Background Sessions · page X/Y`, a table of at most 10
  rows (`MAX_SESSION_ROWS_PER_PAGE`), a fixed-height details panel and a hint
  footer. Enter opens a full details page; Enter again attaches the session to a
  foreground TUI.
- **Agent definitions.** Seven built-in roles plus trusted Python
  `AgentPlugin` files in `.agenthicc/agents/*.py` and `~/.agenthicc/agents/*.py`
  (`src/agenthicc/agents/registry.py:135`). There is no listing, creation,
  editing or switching surface.
- **Subagents.** `SubagentPool` keeps each worker's tool calls private and
  journals only final results (`src/agenthicc/subagents/pool.py`); the TUI
  prints a spawn list, one line per finished worker and a summary
  (`src/agenthicc/tui/workspace/appender.py:942`), plus a status-bar counter.

## 4. Measurements

Measured on 2026-10-23 on the shared 8-vCPU Linux development host. Load varied
between 3 and 41 during the study, so CPU time (user + system) is reported where
wall time would mislead. The scripts are reproducible from the commands given in
each PRD.

### 4.1 CLI and startup

| Invocation (empty registry, best of 3, load ≈ 4) | CPU |
|---|---:|
| `python -c pass` | 0.04 s |
| `python -c "import rich.console"` | 0.21 s |
| `agenthicc --version` (fast path) | 0.23 s |
| `agenthicc --help` | 0.39 s |
| `agenthicc sessions list` (non-TTY) | 1.12 s |
| `agenthicc agents --json` / `jobs list --json` | 1.25 s |
| `agenthicc runs --json` | 1.43 s |

- `agents --json` imports **440 modules** (even the `--version` fast path
  imports 204, because `__main__` imports `asyncio`). Command discovery imports all 14
  built-in command modules (`src/agenthicc/cli/registry.py:250`), which pulls in
  `aiohttp` through `session_service.transport` (0.49 s cumulative under
  `-X importtime`), `runners.tui_session` and the TUI input stack.
- The interactive TUI shows its first prompt after **7.8–11.1 s** (load ≈ 19) and
  **6.2–8.0 s of CPU**, having imported **932 unique modules**. The heaviest are
  the `lauren_ai` package, whose `__init__` eagerly imports 178 modules
  including pydantic and anyio (≈ 3.0 s cumulative); `aiohttp` via
  `session_service.transport` (≈ 1.5 s); `httpx`/`httpcore` (≈ 1.1 s, and
  `httpx` imports `click` through its CLI module); and `rich.console` with its
  emoji table. Timings are under `-X importtime` at load ≈ 19, so proportions,
  not absolutes, are the signal. Separately, `auth.current_bundle()` runs
  keyring backend discovery on the event loop during start-up, importing the
  macOS keyring backend and `secretstorage` on Linux.

### 4.2 Agents screen

`scripts/benchmark_agents_manager.py` (in-process, after imports):

| Registry | First frame (placeholder) | Cold projection | Key → render p95 | Incremental refresh p95 | Maintenance pass |
|---|---:|---:|---:|---:|---:|
| 1,000 sessions / 50,000 events (8.2 MB) | 71 ms | 1.12 s | 0.92 ms | 0.33 ms | 1.40 s |
| 10,000 sessions / 1,000,000 events (150 MB) | 55 ms | 51.0 s | 1.08 ms | 0.32 ms | 2.29 s |

A **fresh process** at 10,000 sessions / 1,000,000 events — what a user actually
waits for:

| Case | Time to first page |
|---|---:|
| No snapshot (full replay; writes a 16.7 MB `projection-v1.json`) | 33.1 s (32.2 s CPU) |
| Snapshot present | 3.6–6.0 s |
| SQLite prototype (`--compare-sqlite`, 1k registry) page + count query | 0.44 ms |

An **idle** agents screen on a 1,000-session registry, opened in a
pseudo-terminal and left alone for 60 s, used 0.28–0.56 s of CPU per 10 s
(3–6% of a core) indefinitely. In its first 10 s, maintenance reconciled every
fixture session with an expired heartbeat and appended 1,000 `updated` events
(1.9 MB): `BackgroundStore.update()` writes the whole session record into
every update event, whatever changed.

Interactive rendering is already fast once data is loaded; the costs are in
reaching the process, loading the index and polling maintenance. PRD-209's
benchmark measured the placeholder frame, not the first *useful* frame of a
fresh process.

### 4.3 Main TUI rendering

One live-block build and render at 120×40 (`Workspace._build` + Rich):

| State | p50 | p95 |
|---|---:|---:|
| Idle | 2.0–2.7 ms | ≈ 78 ms |
| Composer with 4,000 characters | 5.3 ms | 81 ms |
| Composer with 200 lines | 83 ms | 103 ms |

The idle tail survives with garbage collection disabled. The footer
re-parses Rich markup on every frame to fit its hints
(`src/agenthicc/tui/workspace/components.py:562`), about 60% of profiled render
time. Every keystroke redraws, so a 200-line prompt lags by ≈ 83 ms per key.

### 4.4 Agent turn

The 510-step `goal_flow` hermetic replay costs 165 s of CPU uninstrumented and
481 s under branch coverage. 3,381 full deep copies of the conversation (2,179
from lauren-ai's `ShortTermMemory.snapshot()`, 1,164 from
`JournaledShortTermMemory.ensure_valid_and_persist()`) and 2,935 full
tool-history validations account for about 70% of it (PRD-250 status section).

### 4.5 UI audit of the current screen

Rendered with six seeded sessions at 120 and 80 columns:

- Statuses are raw enum values and truncate: `waiting_ap…` at 120 columns;
  `wai…` for both `waiting_approval` and `waiting_input` at 80.
- The `WS` (workspace) column is not rendered at either width.
- The `Mode` column shows `pending: default/pe…`, an internal mode-application
  state, on every row.
- Titles are cut to about 22 characters while other columns keep space.
- The details panel says "Latest text: no text activity" while the row shows
  the activity.
- At 80 columns the footer wraps to a second line; the help screen omits pin,
  approve/reject, worktree actions and Space.
- The details page lists 20 fields, many `—`, with internal terms
  ("Mode application: pending · attempt 0"); a session waiting for approval does
  not say what needs approving; there is no transcript, diff or cost.

## 5. How other agents present their agents

Researched on 2026-10-23 from official documentation unless marked
*third-party*.

### 5.1 Claude Code's agent view — the closest analogue

`claude agents` is the same idea as `agenthicc agents`: one screen for the
background sessions you have started. It is the strongest reference for this
series:

- **Reachable from anywhere.** From a session, ← on an empty prompt opens it
  (the footer shows `← for agents`, then `← 2 agents` while sessions wait on
  you); Esc returns to the conversation. `defaultToAgentsView` makes it the
  start screen.
- **Grouped by what you need to do.** Pinned, Ready for review, Needs input,
  Working, Completed; Ctrl+S groups by directory instead. Each row has a state
  icon, the session name, a one-line summary written by a small model, its age,
  and a pull-request label. Empty lists still show each section header with a
  description.
- **Peek before attaching.** Space opens a panel with the exact question or
  latest output, how long it has waited, and a reply input (`!` sends a shell
  command, `/stop` stops the session). Enter or → attaches; ← detaches back to
  the list.
- **Start work from the list.** A dispatch input at the bottom starts a new
  background agent; `@<dir>` targets a directory and `/model`, `/effort` set
  defaults for the next dispatch.
- **Keys.** Ctrl+R rename, Ctrl+T pin, Ctrl+X stop (twice within 2 s deletes),
  Ctrl+F find (`n:` filter by name or first prompt), Alt+↑/↓ jump between
  groups, Shift+↑/↓ reorder, `?` shortcuts — rebindable in the `Agents` context
  of `keybindings.json`.
- **Attention.** Terminal notifications when a session needs input, finishes or
  fails, with `Notification` hooks `agent_needs_input` and `agent_completed`;
  the terminal title reads `2 awaiting input · claude agents`.
- **Resources.** Each background session is its own process under a supervisor,
  which stops finished or waiting sessions left unattached for about an hour
  (pinned ones excepted); attaching to a stopped session shows its transcript
  tail immediately while the process restarts.
- **Safety.** Dispatching from an untrusted directory shows the workspace-trust
  dialog first; deleting a session refuses to discard unpushed worktree commits
  until you confirm again.

### 5.2 Comparison

| Capability | Claude Code | OpenCode | Codex CLI | Grok Build |
|---|---|---|---|---|
| Live work list | Subagent panel below the prompt: a tree with `(+N)` descendant counts; finished rows linger 30 s. `/tasks` lists background work, running above done; Enter opens a transcript, `x` stops; rows show model and effort | Each subagent runs in a child session | `/subagents` inspects and switches between running agent threads | `/tasks` lists background tasks, subagents and scheduled tasks; Ctrl+G toggles a tasks pane; `/dashboard` (Ctrl+\\) opens an Agent Dashboard; `/workflows` is a fullscreen run dashboard |
| Navigate into a child | Open a transcript from `/tasks`; type into it to resume the subagent | `session_child_first` (Leader+Down), `session_child_cycle` (Right/Left), `session_parent` (Up) | Switch threads with `/subagents` | Dashboard shows each session's worktree (*third-party*) |
| Background running work | Ctrl+B | Subagent tool runs foreground or background | — | Ctrl+B demotes a running command |
| Child approvals | Prompt surfaces in the main session naming the subagent; Esc denies that one call without stopping it | — | Approval overlay shows the source thread; `o` opens it first | — |
| Definitions | Markdown + YAML frontmatter; project, user, plugin and `--agents` JSON precedence; detected within seconds. `/agents` (Running/Library tabs) was retired after v2.1.197 | Markdown or JSON; `mode`, `model`, `steps`, per-tool `permission`, `color`, `hidden`; `opencode debug agents` lists them | TOML (`name`, `description`, `developer_instructions`, model and feature overrides); sandbox and approval policy cannot be overridden per agent | `/config-agents` (alias `/agents`) manages definitions; `/personas` manages behaviour overlays |
| Switching agents | `claude --agent <name>` runs a session as that agent | Tab cycles primary agents; `@name` invokes a subagent | — | Choose the active agent in `/config-agents` (*third-party*) |
| Background sessions | **Agent view** (`claude agents`, or ← on an empty prompt): every background session across projects, grouped Pinned / Ready for review / Needs input / Working / Completed; Space peeks with a reply box; a dispatch input starts new agents (§5.1) | `/sessions` (Ctrl+X L); `session list` as table or JSON; `run --attach` to a running server avoids cold boot | `codex resume` picker (current directory; `--all`) | `/resume` picker; recent sessions on the welcome screen; `grok sessions list/search/delete` |
| Limits | 20 concurrent subagents, nesting depth 3 | — | `agents.max_concurrent_threads_per_session` | Up to 8 parallel subagents in git worktrees (*third-party*) |
| Rendering | Renderer rewritten to diff terminal cells and emit minimal escape sequences; opt-in fullscreen mode (*secondary sources*) | OpenTUI: Zig core, diffs between frames, 60 FPS target | Frame scheduler that coalesces and rate-limits redraws; cached transcript wrapping (*commit mirror*) | "Rich, mouse-interactive, fullscreen" TUI |

Patterns the AGX PRDs adopt:

1. **Running work is one keystroke from the prompt** (Claude Code's agent view
   on ←, its subagent panel and `/tasks`; Grok's tasks pane and dashboard).
   → AGX-05, AGX-08.
2. **Lists are grouped by what the user must do next** (agent view's Needs
   input / Ready for review / Working / Completed), with a peek panel to answer
   without attaching. → AGX-06, AGX-07.
3. **Children are navigable**: open, follow, type into, stop. → AGX-07, AGX-08.
4. **Approvals name their source and can open it.** → AGX-08, AGX-10.
5. **Definitions are plain files with a view to list, create and choose them**,
   and edits apply without a restart. → AGX-09.
6. **Rendering is incremental and rate-limited**, with caches for unchanged
   text. → AGX-11.
7. **Client/server or warm processes avoid repeated cold boots** (OpenCode
   `run --attach`). → AGX-01, AGX-02.

## 6. Shared budgets

AGX-14 owns the enforcement; the targets are:

| Budget | Target |
|---|---|
| `agenthicc agents --json`, empty registry | ≤ 0.30 s CPU |
| `agenthicc --help` | ≤ 0.25 s CPU |
| Modules imported by a listing command | ≤ 200 (440 today) |
| Interactive TUI, time to first prompt (reference host, idle) | ≤ 1.0 s wall, ≤ 1.2 s CPU |
| Agents screen first useful frame, fresh process, 10k sessions | p95 ≤ 250 ms |
| Agents screen search over 10k sessions | p95 ≤ 50 ms |
| Change visible in the agents screen after an event | p95 ≤ 250 ms |
| Idle agents screen and idle TUI | ≤ 1% of one core |
| Keystroke echo in the composer, prompts ≤ 10,000 characters | p95 ≤ 16 ms render |
| Agent-turn bookkeeping per provider step at 500 messages | ≤ 5 ms |

## 7. Non-goals for the series

- Changing the durable authority: `events.jsonl` and the conversation journal
  stay the source of truth; indexes and caches are derived and rebuildable.
- Loosening security: no AGX feature lets a definition, shortcut or UI action
  exceed the session's Safe/Plan/Yolo ceiling or extension trust.
- Rewriting the TUI in another language or framework. AGX-11 improves the
  Rich-based pipeline; a rewrite would need its own decision.

## Sources

- Claude Code agent view: <https://code.claude.com/docs/en/agent-view>
- Claude Code subagents: <https://code.claude.com/docs/en/sub-agents>
- Claude Code keybindings: <https://code.claude.com/docs/en/keybindings>
- Claude Code renderer rewrite (secondary): <https://steipete.me/posts/2025/signature-flicker>
- OpenCode agents: <https://opencode.ai/docs/agents/>
- OpenCode CLI: <https://opencode.ai/docs/cli/>
- OpenCode TUI: <https://opencode.ai/docs/tui/>
- OpenTUI performance: <https://www.mintlify.com/anomalyco/opentui/advanced/performance>
- Codex subagents: <https://learn.chatgpt.com/docs/agent-configuration/subagents>
- Codex CLI reference: <https://developers.openai.com/codex/cli/reference>
- Codex frame scheduler and transcript caches (commit mirror): <https://forge.lthn.ai/core/core-agent-ide/commit/90f37e854992c14d66147787f92b90f604246e42>
- Grok Build modes and commands: <https://docs.x.ai/build/modes-and-commands>
- Grok Build keyboard shortcuts: <https://docs.x.ai/build/keyboard-shortcuts>
- Grok Build subagents: <https://docs.x.ai/build/features/subagents>
- Grok Build sessions: <https://docs.x.ai/build/features/sessions>
- Grok Build background tasks: <https://docs.x.ai/build/features/background-tasks>
- Grok Build cheat sheet (*third-party*): <https://www.scriptbyai.com/grok-build-cheat-sheet/>
