---
title: "AGX-05: Agents hub — one surface for running work, attention, history and library"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P0
series: AGX
related_prds: [agx-00, agx-03, agx-06, agx-07, agx-08, agx-09, agx-10, agx-13, prd-139, prd-141, prd-202, prd-204, prd-206, prd-227, prd-233]
tags: [agents, tui, ux, information-architecture, cli]
---

# AGX-05 — Agents hub: one surface for running work, attention, history and library


## Implementation status (2026-10-24)

Implemented in `src/agenthicc/tui/workspace/agents_hub/` (`AgentsHub`,
`AgentsHubOverlay`, `model`, `render`, `detail`, `layout`, `prefs`, `sources`,
`editor`), with AGX-06 and AGX-07 and on AGX-13's key map.

- **Tabs (§5.1).** Running (active, queued, pinned and recently finished
  agents; goal runs with their workers nested and collapsible; the next five
  scheduled runs), Needs you (pending inbox items and waiting or lost agents
  without one, oldest first), History (finished agents and saved foreground
  sessions merged by recency, read 200 at a time) and Library (definitions by
  source). Labels carry live counts; Needs you is highlighted when non-zero.
  The Library count appears once definitions are read: in a session after the
  first rows, in the shell when the tab is first shown.
- **Entry points (§5.2).** `agenthicc agents [SESSION_ID] [--tab TAB] [--run
  RUN_ID]`; `/agents [tab]`, `/inbox` (Needs you), `/workers` (Running), Ctrl+G
  and ← on an empty prompt open the overlay. ← after a deletion or history
  recall within two seconds asks for a second press; a ← inside a bracketed
  paste is text, never a key. Attaching from the overlay ends the session with
  a `SessionSwitch`; `_run_tui_session` runs the target in its project and
  reopens the original session when it ends. `/background` hands off and then
  opens the hub with the new agent selected.
- **Dispatch (§5.3).** `+` opens a multi-line dispatch editor (Ctrl+J newline)
  that starts an agent as `agenthicc run --background` would, in the selected
  row's project; `@DIR`, `/workflow NAME`, `/mode NAME` and `/model ID` are
  parsed as whole tokens, and alone on the line set the defaults for the next
  dispatch. An untrusted project gets the `agenthicc trust` confirmation.
- **One component (§5.4).** The same `AgentsHub` runs full screen
  (`run()`) and as an overlay (`serve()`); `BackgroundManager`,
  `GoalRunManager`, `SessionManager` and `BackgroundInboxOverlay` are deleted.
- **CLI (§5.5).** The `jobs` group moved under `agents` unchanged and `jobs`
  is removed; `sessions list` on a terminal opens the History tab.

Acceptance: (1) one component and key map, Esc restores the conversation
(`test_agents_hub_in_session.py`, including an overlay opened during a running
turn whose approval leaves the turn and transcript untouched); (2) every hub
action resolves and appears in help (`test_every_hub_action_is_in_help_…`) and
every migrated subcommand runs through the CLI (`tests/e2e/test_agents_cli_e2e.py`);
(4) no manager class or `"jobs"` remains in `src/`; (5) screens and help say
"Agents". (3) A session keeps one `AgentsService` (store, executor, inbox) from
the hub's first opening, imports the hub on a worker thread once the agent
runtime is ready, shows the first rows before reading saved sessions,
schedules or definitions and before the first maintenance pass, and the
operator inbox and schedule store no longer rewrite their schemas (two
synchronous commits each) on every open. On a 10,000-session registry an
opening's main-thread work is about 11 ms (best of 25, queries included);
`scripts/benchmark_agents_manager.py` reports `overlay_open` CPU and wall
percentiles. On the shared development host (8 cores at load 40–65) the
process CPU p50 was 20–90 ms and wall p50 190–300 ms, so the wall-clock p95 is
not demonstrable there; AGX-14 enforces the CPU-time budget in CI.

The in-session subagent rows in Running arrived with AGX-08. Deferred to their
PRDs: `/agent NAME` in the dispatch line (AGX-09) and the status-bar badge
shortcut (AGX-10).

## 1. Summary

"Agents" in agenthicc means at least four different things spread over eight
surfaces, none of which is reachable from inside a session. Replace them with
one **Agents hub**: a single TUI component with four tabs — **Running**,
**Needs you**, **History** and **Library** — opened by `agenthicc agents` from
the shell and by `/agents` or one key from inside a session. The three existing
list screens are deleted and the `jobs` command group moves under `agents`.

## 2. Problem and evidence

### 2.1 Names do not match

- `agenthicc agents` opens a screen titled **Background Sessions**; `jobs` is a
  second name for the same screen.
- *Agent definitions* (built-in roles and `.agenthicc/agents/*.py`) are what the
  runtime calls agents, but `agenthicc agents` never shows them.
- Help text, docs and the screen use "session", "job", "worker" and "agent"
  for overlapping things.

### 2.2 Eight surfaces, three implementations

| Surface | What it shows | Implementation |
|---|---|---|
| `agenthicc agents`, `agenthicc jobs` | Background sessions | `BackgroundManager`, 3,036 lines, async |
| `agenthicc agents --run RUN_ID` | One goal run's agents | `GoalRunManager`, 190 lines, 4 Hz auto-refresh |
| `agenthicc sessions list` | Saved foreground sessions | `SessionManager`, 278 lines, 4 Hz auto-refresh |
| `/workers` | Durable parallel worker tasks | Table printed into scroll-back |
| `/inbox` | Pending approvals and questions | `BackgroundInboxOverlay` |
| `/ps` | Background terminals | `TerminalListOverlay` (PRD-202) |
| `/schedules` | Scheduled workflows | `ScheduleJobsOverlay` |
| Subagent lines in the transcript | The current session's subagents | Scroll-back renderer |

Each has its own keys, layout and refresh model. A user in a session who wants
to check a background agent must quit or open another terminal.

## 3. How others organise it

| Product | Running work | Definitions | Sessions |
|---|---|---|---|
| Claude Code | **Agent view** (`claude agents`, or ← on an empty prompt): every background session across projects, grouped by state, with a dispatch input to start new ones; in-session subagent panel and `/tasks` | Files in `.claude/agents/`; the `/agents` screen had **Running** and **Library** tabs until v2.1.197 | `/resume` inside agent view opens past sessions of the repository |
| Grok Build | `/tasks` (tasks, subagents, scheduled tasks); tasks pane on Ctrl+G; `/dashboard`; `/workflows` | `/config-agents` (alias `/agents`) | `/resume`, `/sessions` |
| Codex | `/subagents` switches between running threads | TOML files | `codex resume` picker |
| OpenCode | Child sessions navigable from the parent | Markdown/JSON; `opencode debug agents` | `/sessions` (Ctrl+X L) |

All four keep running work **inside** the session, one command or key away,
and Claude Code's former tabs split *what is running* from *what can run*.
Claude Code's agent view is the closest analogue to this hub: one gesture (←
on an empty prompt) moves between the conversation and the list of background
sessions, and Esc returns.

## 4. Goals and non-goals

**Goals**

1. One component, one keymap and one data model for every agent list.
2. The same hub opens from the shell (full screen) and from a session
   (overlay), and Esc returns to the conversation without disturbing it.
3. Consistent vocabulary: an **agent** is a running or finished instance
   (background session, worker, subagent); an **agent definition** is a Library
   entry; a **run** groups agents started by one goal.
4. Every operation available today remains reachable.

**Non-goals**

- Background terminals (`/ps`) are processes, not agents; they stay a separate
  overlay, reachable from an agent's detail view.
- New lifecycle semantics. Resume and retry stay explicit (PRD-219).

## 5. Design

### 5.1 Tabs

```text
 Agents   Running 4 · Needs you 2 · History 214 · Library 9          ? help
```

| Tab | Rows | Default order |
|---|---|---|
| **Running** | Active background sessions, goal runs as trees (run → main agent → workers and subagents), queued sessions, the next scheduled occurrences; in-session, also the current session's live subagents | Grouped as in AGX-06 §5.3: pinned, needs you, ready for review, running, queued, recently finished |
| **Needs you** | Every pending approval, question and input request across sessions (PRD-227 inbox) | Oldest first |
| **History** | Finished, failed, cancelled and archived background sessions, plus saved foreground sessions | Most recent first; searchable (AGX-03) |
| **Library** | Agent definitions by source: project, user, built-in, plugin (AGX-09) | Source, then name |

Tab labels carry live counts. `Needs you` is highlighted when non-zero
(AGX-10 uses the same count for the status-bar badge).

### 5.2 Entry points

| From | Command or key | Opens |
|---|---|---|
| Shell | `agenthicc agents` | Hub, Running tab, full screen |
| Shell | `agenthicc agents --tab needs-you` (`running`, `history`, `library`) | That tab |
| Shell | `agenthicc agents SESSION_ID` | That agent's detail (AGX-07) |
| Shell | `agenthicc agents --run RUN_ID` | Running, filtered to the run |
| Session | ← on an empty composer, `/agents`, or Ctrl+G (configurable, AGX-13) | Hub as an overlay |
| Session | Status-bar badge shortcut (AGX-10) | Needs you |

Attaching from the shell hub opens the foreground TUI as today; attaching from
the in-session overlay offers to switch sessions and returns to the original
one on detach.

← only opens the hub when the composer is empty and was not emptied by a
deletion or history navigation in the last two seconds; in that case the
footer says `Press ← again to open agents` and a second press is required
(Claude Code added the same guard after users switched by accident). A ← that
arrives inside pasted input never switches.

### 5.3 Dispatch input

The Running tab ends with an input line that starts a new background agent:

```text
 ❯ Investigate why the nightly build is slow                 @app-1  /agent reviewer
```

- Enter submits the prompt as `agenthicc run --background` would, in
  the selected row's project (or the current one); Ctrl+J inserts a newline, as
  in the composer. The new agent's row is selected, so a second Enter attaches.
- `@<directory>` targets another project; `/agent NAME` picks a definition
  (AGX-09); `/model` and `/mode` set defaults for the next dispatch.
- Dispatching into a directory that is not trusted shows the existing
  `agenthicc trust` confirmation first; a script without a terminal gets an
  error instead.
- `/background` in a session moves that session into the hub's Running tab, as
  today, and the hub opens with it selected.

### 5.4 One component

A new package `src/agenthicc/tui/workspace/agents_hub/` provides:

- `HubModel` — projects rows of kinds `session`, `worker`, `subagent`, `run`,
  `scheduled`, `inbox_item` and `definition` from the indexed registry
  (AGX-03), the foreground session index, the inbox store, the schedule store
  and, in-session, the live subagent pool (AGX-08).
- Shared primitives — virtual list, detail pane, filter bar, tab bar, footer
  and help generated from the keymap (AGX-13).
- One render loop, event-driven (AGX-04) and rendered through the shared
  pipeline (AGX-11), used in both full-screen and overlay modes.

`BackgroundManager`, `GoalRunManager` and `SessionManager` are deleted once the
hub covers them; `/workers` and `/inbox` become shortcuts to the Running and
Needs-you tabs.

### 5.5 CLI consolidation

The `jobs` group moves under `agents`, keeping verbs and flags:

| Today | After |
|---|---|
| `agenthicc jobs list/status/cancel/resume/retry/priority/approve/reject/input` | `agenthicc agents list/status/cancel/resume/retry/priority/approve/reject/input` |
| `agenthicc jobs inbox list/answer/approve/reject/expire` | `agenthicc agents inbox …` |
| `agenthicc jobs notifications/rename/labels/purge` | `agenthicc agents notifications/rename/labels/purge` |
| `agenthicc jobs` (screen) | `agenthicc agents` |
| `agenthicc sessions list` (TTY selector) | `agenthicc agents --tab history`; non-TTY output unchanged |

`jobs` is removed rather than kept as an alias, per the repository's
no-compatibility-shim rule; docs, `llms-full.txt` and tests are updated in the
same change.

## 6. Acceptance criteria

1. `agenthicc agents` and `/agents` render the same component with the same
   keys; Esc from the overlay restores the conversation exactly.
2. Every operation in the mapping table and every key the current manager
   supports is reachable in the hub (a test enumerates them).
3. The in-session overlay opens in ≤ 100 ms p95 with 10k registry sessions.
4. `rg "class BackgroundManager|class GoalRunManager|class SessionManager"` and
   `rg "\"jobs\""` in `src/` find nothing.
5. Screens and help say "Agents"; "Background Sessions" no longer appears.

## 7. Test plan

- Golden renders of each tab at 80 and 120 columns (with AGX-06's layout).
- Overlay lifecycle: open during a streaming turn, approve an inbox item, close;
  the turn continues and the transcript is unchanged.
- CLI E2E for every migrated `agents` subcommand.

## 8. Risks

- **Scope.** The hub touches many surfaces. Mitigation: land the component with
  Running and Needs you first (replacing `BackgroundManager` and `/inbox`), then
  History and Library.
- **Muscle memory for `jobs`.** Mitigation: the CHANGELOG and `agenthicc --help`
  list the move; the project is pre-production.

## Sources

- Claude Code agent view: <https://code.claude.com/docs/en/agent-view>
- Claude Code subagents (`/tasks`, former `/agents` tabs): <https://code.claude.com/docs/en/sub-agents>
- Grok Build modes and commands: <https://docs.x.ai/build/modes-and-commands>
- Grok Build background tasks (tasks pane): <https://docs.x.ai/build/features/background-tasks>
- Codex subagents (`/subagents`): <https://learn.chatgpt.com/docs/agent-configuration/subagents>
- OpenCode TUI (`/sessions`): <https://opencode.ai/docs/tui/>
