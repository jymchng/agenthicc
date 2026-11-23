---
title: "WFC-08: A live tool surface — newly created tools are callable on the next request"
status: Implemented
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P0
related_prds: [WFC-00, WFC-01, WFC-05, WFC-07, WFC-11, prd-24, prd-76, SUBAGENT-13, TUIR-04]
tags: [tools, reload, subagents, capabilities, replay]
---

# WFC-08 — A live tool surface

## 1. Summary

Replace the four private tool snapshots with one **`ToolSurface`**: an immutable,
generation-numbered snapshot published by the `ReloadService` and *read* by every
consumer at a defined boundary. A tool created by `create_tools` becomes
callable in the agent's first model request after the return packet, and in
every worker spawned afterwards.

## 2. Problem (current source)

A tool created mid-session is invisible until each of these is rebuilt:

| Layer | Snapshot | Where |
|---|---|---|
| Workflow config | `plugin_tools` on a frozen `WorkflowConfig`, replaced with `dataclasses.replace` by `_reload_tools` | `workflows/config.py:56`; `tui_session.py:4179-4228` |
| Direct turn | registry built from `ctx.project_plugin_tools` at turn start | `runners/agent_turn.py:1466` |
| Phased runner | `_tool_bundle_cache` built once per runner | `workflows/code_plan/runner.py:1052-1067` |
| Subagent pool | `_all_tools` fixed at construction | `subagents/pool.py:1462`, `:2613` |

Consequences: the agent that just created `fetch_rss` cannot call it; a worker
spawned afterwards inherits a stale list; and nothing records which tools a turn
could see, so replay cannot reproduce a request.

## 3. Goals

1. One authoritative `ToolSurface` per session, replaced atomically by reload.
2. The first model request after a reload-bearing return sees the new tools.
3. Workers spawned after the reload see them; workers already running keep the
   generation they started with, and that is documented.
4. Every turn records the surface generation it used, so replay knows the tool
   list.
5. Capability gating and trust apply before a tool enters a surface.

## 4. Non-goals

- Mutating a tool inside a model request already in flight.
- MCP tool refresh (separate registry).
- Cross-session tool sharing.

## 5. Design

### 5.1 Types

New module `tools/surface.py`.

```python
@dataclass(frozen=True, slots=True)
class ToolSurface:
    generation: int
    tools: tuple[ToolLike, ...]            # sorted by tool name
    sources: Mapping[str, str]             # tool name -> "builtin" | "project" | "user" | "mcp"

class ToolSurfaceHolder:
    def current(self) -> ToolSurface: ...
    def publish(self, surface: ToolSurface) -> None: ...   # called only by ReloadService
```

The holder is session-owned (closed with the session) and handed to consumers
instead of a list. Only `ReloadService` calls `publish`. A surface contains only
tools that passed the capability gate (`tools/capability_gate.py`) and the trust
check; excluded files are `ReloadDiagnostic`s, not tools.

### 5.2 Consumers

| Consumer | Change |
|---|---|
| `WorkflowConfig` | `plugin_tools` and `all_plugin_tools()` replaced by `tool_surface: ToolSurfaceHolder`. `_wf_config_base` replacement in reload is deleted. |
| `agent_turn` | Reads `holder.current()` once at turn start and records `surface.generation` on the turn context and in the turn-started event. |
| Tool boundary | At the `next_queued_message` seam (`agent_turn.py:1604`) the loop compares `holder.current().generation` with the turn's; on change it rebuilds the registry for the **next** model call. **Spike (first task):** confirm lauren-ai's agent loop accepts a changed tool list between iterations. If it does not, the same-turn pickup is dropped and the guarantee in §5.3 stands alone. |
| `CodePlanRunner` and other phased runners | `_tool_bundle_cache` is keyed by generation and rebuilt at each phase start, not each runner construction. |
| `SubagentPool` | Resolves `holder.current()` at **spawn** time per task; `_all_tools` fields are deleted. |

### 5.3 The guarantee

> After a `WorkflowReturn` whose artifacts include loaded tools is delivered, the
> parent's resume turn starts with `holder.current().generation >= ` the reload
> generation, and its first model request lists those tools.

This holds without any in-flight swap because return delivery is a *new turn*
(WFC-01 §5.4). Same-turn pickup (a direct agent that writes a tool and reloads
mid-turn) is an enhancement gated on the §5.2 spike.

### 5.4 Replay

`turn_started` events carry `tool_surface_generation`; `session_reloaded`
(WFC-05) carries the added/updated/removed tool names. Replaying a log can
reconstruct the exact tool list for any turn by folding `session_reloaded`
events. Tool results remain serializable and replay-safe (CLAUDE.md invariant);
a replay that meets a call to a tool removed since is reported, not crashed.

### 5.5 Collisions

A tool name present in more than one source resolves as today (project over
user over built-in) **except** that an authored tool may never shadow a
built-in (WFC-11 §5.7); the surface build reports `name_conflict` and keeps the
built-in.

## 6. Requirements

- **FR-1** `holder.current()` is O(1) and lock-free for readers.
- **FR-2** No module outside `ReloadService` calls `publish`; a test asserts it.
- **FR-3** A worker's tool list is fixed for its life; the pool exposes the
  generation in each worker's transcript header.
- **FR-4** Removing the four private snapshots deletes, not wraps, the old
  fields.
- **FR-5** Surface construction is covered by the WFC-05 lag budget.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | Scripted agent: `enter_workflow("create_tools")` → the run completes with no trust prompt → on the resume turn the recorded first model request's tool list contains the new tool, and a call to it returns its result. |
| A2 | A worker spawned after the reload lists the tool; a worker spawned before and still running does not. |
| A3 | A `code_plan` run in flight when a reload publishes a new tool uses it from its next phase. |
| A4 | Replaying the event log reproduces, per turn, the tool names visible to that turn. |
| A5 | A tool failing the capability gate or trust check never appears in `tools`, and a diagnostic names it. |
| A6 | `grep -rn "_tool_bundle_cache\|plugin_tools\|project_plugin_tools\|_all_tools" src` shows no remaining private tool-list snapshot. |
| A7 | (If the spike succeeds) a direct agent that writes a tool file and calls reload sees it on the next model call of the **same** turn. |

## 8. Test plan

- Unit: surface sorting, generation monotonicity, publish-only-from-service
  (`tests/unit/test_tool_surface.py`).
- Integration: real `agent_turn` with a recording transport capturing request
  tool lists across a reload; pool spawn before/after; phased runner rebuild
  (`tests/integration/test_live_tool_surface.py`).
- Spike test for the lauren-ai tool-list swap, marked and documented either way.
- E2E: WFC-13 S4.

## 9. Documentation and cleanup

`docs/guides/tools.md`, `subagents.md`, `architecture.md`, `llms-full.txt`
(`ToolSurface`, `ToolSurfaceHolder`). The four snapshot fields in §2 are removed
and their callers updated in the same change.

## 10. Risks

| Risk | Mitigation |
|---|---|
| A tool list change invalidates a provider prompt cache. | Tool lists change only on reload, at the turn boundary; cost is one cache miss per reload. |
| A removed tool is called by an in-flight worker. | Workers keep their generation; the call executes against the object they hold, which remains valid. |
| The lauren-ai loop cannot swap tools mid-turn. | The spike has a defined fallback; the primary guarantee does not depend on it. |

## Amendment — 2026-11-22

Lauren-ai 1.6.2 computes provider schemas once before the loop in both
`run` and `run_stream`. Replacing the agent's tool map alone leaves the old
schemas on the next request. The follow-up probe retained the list returned by
`_get_tool_schemas` and updated it in place alongside the tool map before
the next request. Both loops then advertised and successfully dispatched the
new tool in the same run. Use the existing tool-boundary runner hook to refresh
that list for WFC-08 A7 and to narrow it for WFC-10. Keep the new-turn guarantee
in §5.3. Evidence is `test_lauren_tool_schema_refresh_between_requests`.

## Implementation status

| Criterion | Evidence |
| --- | --- |
| A1 | `test_create_tools_publishes_and_dispatches_on_the_first_parent_request`. |
| A2 | `test_workers_hold_the_tool_generation_selected_at_spawn`. |
| A3 | `test_code_plan_reads_the_reloaded_catalog_at_the_next_phase`. |
| A4, A7 | `test_direct_turn_uses_new_schema_and_dispatch_after_reload` observes both model requests and the reconstructed turn metadata. |
| A5 | `test_reload_reports_authored_tools_with_no_capability_declaration` and `test_edited_published_bytes_leave_the_provider_surface`. |
| A6 | `test_tool_surface_publication_belongs_to_reload_service` and the legacy-symbol scan. |
