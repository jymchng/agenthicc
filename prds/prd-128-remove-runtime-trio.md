# PRD-128 — Remove the unwired kernel runtime trio

## Problem

`src/agenthicc/runtime/` holds the original PRD-01/PRD-03 kernel agent-execution
layer:

| File | Symbols |
|---|---|
| `runtime/pool.py` | `AgentPool`, `AgentRecord` |
| `runtime/scheduler.py` | `Scheduler` |
| `runtime/comm_tools.py` | `CommunicationTools` (9 async tools incl. `mcp_connect`) |

This subsystem was superseded by lauren-ai's `AgentRunnerBase` (actual agent
execution) plus the `agenthicc.workflows` runners (`CodePlanRunner` /
`WorkflowRunner`) for orchestration. It is now **dead production code**: nothing
in the running application instantiates it.

### Verification — nothing live depends on it

1. **No live importers.** Zero `src/` files outside `runtime/` import
   `CommunicationTools`, `AgentPool`, `AgentRecord`, or `Scheduler`. The trio is
   not re-exported from the top-level `agenthicc/__init__.py`.

2. **The events it emits are emitted by nothing else.** The only emitters of
   `AgentSpawnRequest`, `TaskCreated`, `TaskAssigned`, `WorkflowNodeAdded`, and
   `WorkflowNodeRemoved` are `comm_tools.py` and `scheduler.py` themselves. Live
   runners emit only `IntentCreated`, `IntentStatusChanged`,
   `TransportRetryScheduled`, and the PRD-94 `WorkflowRun*` tracking events — none
   of which produce `spawn_agent` / `assign_task` / `start_workflow_node` effects.

3. **The effects it relies on are discarded.** Every live `EventProcessor` is
   constructed without an `effect_executor`, so it defaults to
   `NoOpEffectExecutor`. No other `EffectExecutor` implementation exists. The
   `spawn_agent` / `assign_task` / `start_workflow_node` effects are produced by
   reducers and dropped.

4. **Its former consumer is already gone.** CLAUDE.md documents a singular
   `workflow/` package (`WorkflowExecutor`, `WorkflowModifier`, `IntentPlanner`)
   as the consumer of `CommunicationTools.workflow_modify`. That package was
   removed in PRD-116; only the plural `agenthicc.workflows` system remains, and
   it never touches the trio.

5. **`mcp_connect` is test-only.** `CommunicationTools.mcp_connect` (PRD-30) is
   defined only on the trio; the live MCP path uses `McpToolRegistry` in
   `runners/`. No live caller of `mcp_connect` exists.

6. **The state it populates is read by nothing.** `AppState.agents` and
   `AppState.tasks` (the dicts these reducers fill) are read by no live code —
   the TUI store and event adapter ignore them.

The trio is therefore a **fully closed, self-referential dead loop**: it emits
events → reducers turn them into effects → effects go to `NoOp`. The only
remaining consumers are five test files that exercise the trio in isolation.

## Decision — remove it (Phase 1)

Delete the `runtime/` package and its tests, and bring the authoritative
in-repo docs (CLAUDE.md, AGENTS.md, `llms-full.txt`) and the `docs/` site into
sync. This honours the project rule: *delete every superseded symbol; no dual
paths; no legacy references.*

### Non-goals (deferred to a future Phase 2)

Removing the trio leaves second-order orphans that are reachable only by
`tests/unit/test_appstate_reducers.py`. Pruning them touches the genuinely-live
`kernel/reducer.py`, `kernel/state.py`, and `config.py` (shared with the live
Intent / `WorkflowRun` paths), so it is scoped as a deliberate follow-up:

- Reducer branches `_agent_spawn_request`, `_task_created`, `_task_assigned`,
  `_workflow_node_added`, `_workflow_node_removed`, `_agent_status_changed`.
- `EffectType.spawn_agent`, `EffectType.assign_task`,
  `EffectType.start_workflow_node`.
- `AppState.agents` / `AppState.tasks` fields, the `AgentInstance` / `Task`
  dataclasses, and the `with_agent` / `with_task` helpers.
- The now-inert config fields `agent_pool_size` and `max_parallel_tasks`.
- A full rewrite of the `docs/` narrative guides to describe the current
  `agents/` + `workflows/` architecture (they predate the workflows rewrite).

## Solution

### 1. Delete the code

Remove the entire `src/agenthicc/runtime/` package:
`__init__.py`, `comm_tools.py`, `pool.py`, `scheduler.py`.

### 2. Delete the tests

Remove the five files whose sole purpose is exercising the trio:
`tests/unit/test_agent_pool.py`, `tests/unit/test_comm_tools.py`,
`tests/unit/test_scheduler.py`, `tests/unit/test_mcp_connect.py`,
`tests/integration/test_runtime_cycle.py`.

### 3. Purge the docs

- **CLAUDE.md** — remove the `runtime/` directory-layout block, architecture
  decision §3 (tool-only agent communication) and §5 (Scheduler semaphore), the
  trio test-list rows, and the trio pitfall rows.
- **AGENTS.md** — remove the trio file-map rows, the "add a `CommunicationTools`
  method" how-to, the conftest fixture mention, and the trio pitfall rows.
- **`llms-full.txt`** — remove the whole `## Runtime — agenthicc.runtime`
  section (CommunicationTools / AgentPool / AgentRecord / Scheduler). Re-run
  `scripts/check_llms.py`.
- **`docs/` site** — delete `docs/reference/communication-tools.md` (100% trio)
  and the orphan `docs/guides/agents.md` (the "Writing Agents" guide built
  entirely on the trio); drop the `communication-tools.md` nav entry from
  `mkdocs.yml`; remove trio references from `docs/index.md`,
  `docs/reference/index.md`, `docs/guides/architecture.md`, and
  `docs/guides/hooks.md`.

### 4. Changelog + feature expectations

Add a CHANGELOG entry and a `prd-68-feature-expectations.md` section recording
the removal and the deferred Phase 2.

## Acceptance criteria

| # | Criterion |
|---|---|
| 128.1 | `src/agenthicc/runtime/` no longer exists |
| 128.2 | The five trio test files are removed |
| 128.3 | `grep -rn "agenthicc.runtime\|CommunicationTools\|AgentPool\|AgentRecord\|\bScheduler\b" src/` returns nothing |
| 128.4 | `docs/reference/communication-tools.md` and `docs/guides/agents.md` are removed; `mkdocs.yml` has no dangling nav entry |
| 128.5 | `llms-full.txt` has no `agenthicc.runtime` section; `uv run python scripts/check_llms.py` passes |
| 128.6 | `uv run pytest tests/ -q` passes (excluding the pre-existing unrelated failures) |
| 128.7 | `uv run mypy src/agenthicc` and `uv run ruff check src/ tests/` pass |

## Files changed

| File | Change |
|---|---|
| `src/agenthicc/runtime/` | Deleted (package) |
| `tests/unit/test_agent_pool.py` | Deleted |
| `tests/unit/test_comm_tools.py` | Deleted |
| `tests/unit/test_scheduler.py` | Deleted |
| `tests/unit/test_mcp_connect.py` | Deleted |
| `tests/integration/test_runtime_cycle.py` | Deleted |
| `docs/reference/communication-tools.md` | Deleted |
| `docs/guides/agents.md` | Deleted |
| `mkdocs.yml` | Drop Communication Tools nav entry |
| `CLAUDE.md` | Remove runtime section, decisions §3/§5, trio pitfalls/tests |
| `AGENTS.md` | Remove trio rows, how-to, fixture mention, pitfalls |
| `llms-full.txt` | Remove `agenthicc.runtime` section |
| `docs/index.md`, `docs/reference/index.md`, `docs/guides/architecture.md`, `docs/guides/hooks.md` | Remove trio references |
| `CHANGELOG.md` | Add removal entry |
| `prds/prd-68-feature-expectations.md` | Add removal section |
