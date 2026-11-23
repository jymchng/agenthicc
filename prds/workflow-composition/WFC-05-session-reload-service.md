---
title: "WFC-05: One session reload service for tools, skills, workflows and commands"
status: Implemented
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P0
related_prds: [WFC-00, WFC-06, WFC-07, WFC-08, WFC-09, WFC-10, WFC-11, prd-24, prd-46, prd-114, TUIR-04]
tags: [reload, plugins, skills, commands, event-loop, headless]
---

# WFC-05 — One session reload service

## 1. Summary

Replace the four operator-only reload methods on `TUISession` with a
session-owned `ReloadService` that reloads any subset of
`{tools, skills, workflows, commands}`, returns one typed `ReloadReport`, runs
discovery off the event loop, publishes a kernel event, works headless, and can
be called by the session when an authoring workflow returns — not only by a
person typing a slash command.

## 2. Problem (current source)

| Fact | Where |
|---|---|
| Four separate methods with unrelated result types: `_reload_skills -> SkillDiscoveryResult`, `_reload_tools`, `_reload_workflows -> tuple[bool, str]`, `_reload_commands -> tuple[bool, str]`. | `runners/tui_session.py:4115`, `:4179`, `:4230`, `:4262` |
| They are injected as callbacks into slash-command context only (`CommandContext.reload_skills/reload_commands/reload_tools/reload_workflows`), consumed by the `/skills`, `/commands`, `/tools` and `/workflow` reload subcommands. Nothing in a workflow, a return path, or a headless session can call them. | `:3897-3900`; `commands/command.py:131-134`; `commands/builtins.py:889`, `:1251`, `:1302`, `:1442` |
| Discovery (`discover_project_tools`, `build_workflow_registry`, `discover_skills_with_diagnostics`) runs synchronously on the event loop. TUIR-00 measured a 93.7 s stall from filesystem work on the loop; no lag budget covers reload. | `plugins/discovery.py`, `workflows/registry.py`, `skills/loader.py` |
| `_reload_skills` mutates the shared skills dict with `clear()` then `update()`, so a concurrent reader can observe an empty mapping. | `:4115-4160` |
| No event records a reload, so replay cannot tell which tools or skills a turn saw. | kernel events |
| `_reload_tools` must also swap `self._wf_config_base` through `dataclasses.replace` because the config is frozen — a symptom of there being no shared surface (WFC-08). | `:4179-4228` |

## 3. Goals

1. One API, one report type, callable from commands, the return-pop handler,
   the headless runner and tests.
2. Atomic publication per surface: readers see the old surface or the new one,
   never a partial one.
3. Discovery off the event loop, within the TUIR-04 lag budget.
4. A **preview** mode that reports what a reload *would* do without publishing,
   so authoring workflows can validate "would this load?" (WFC-07, WFC-09).
5. Every reload bumps a session generation and emits a kernel event.

## 4. Non-goals

- MCP reload (`McpSessionManager.reload_all` stays its own surface).
- A file watcher. The service is explicit-trigger only; it does not preclude a
  watcher later.
- Deciding trust (WFC-11). The service *reports* untrusted files; it never
  trusts them.

## 5. Design

### 5.1 API

New module `runners/reload.py`; the service is constructed by `SessionContext`
and owned/closed by the session (CLAUDE.md invariant).

```python
ReloadSurface = Literal["tools", "skills", "workflows", "commands"]
ReloadReason = Literal["command", "workflow_return", "startup", "api"]

@dataclass(frozen=True, slots=True)
class ReloadChange:
    surface: ReloadSurface
    name: str
    kind: Literal["added", "updated", "removed", "unchanged"]

@dataclass(frozen=True, slots=True)
class ReloadDiagnostic:
    surface: ReloadSurface
    path: str
    code: Literal["untrusted", "untrusted_modified", "import_error",
                  "missing_dependency", "name_conflict", "pending_marker",
                  "invalid_manifest"]
    message: str
    digest: str | None            # present for untrusted/modified: the digest to inspect or trust

@dataclass(frozen=True, slots=True)
class ReloadReport:
    generation: int
    reason: ReloadReason
    requested: frozenset[ReloadSurface]
    published: frozenset[ReloadSurface]   # surfaces that swapped
    changes: tuple[ReloadChange, ...]
    diagnostics: tuple[ReloadDiagnostic, ...]
    @property
    def ok(self) -> bool: ...             # published == requested
    def summary(self) -> str: ...         # "+2 tools · 1 untrusted · skills unchanged"

class ReloadService:
    async def reload(self, surfaces: frozenset[ReloadSurface], *, reason: ReloadReason) -> ReloadReport: ...
    async def preview(self, surfaces: frozenset[ReloadSurface]) -> ReloadReport: ...
    @property
    def generation(self) -> int: ...
```

### 5.2 Algorithm

1. Take a single-flight lock; concurrent callers coalesce onto the in-flight
   report when their surfaces are covered.
2. Order: **skills → tools → workflows → commands** (skill commands register
   into the command registry; commands go last).
3. For each surface: run discovery in a worker thread
   (`asyncio.to_thread`) into a **candidate**; validate it (trust, import,
   conflicts); on success swap the live object **on the loop** in one step
   (`registry.replace_with`, or replacing the immutable surface — WFC-08). On
   failure keep the old surface and add diagnostics.
4. Tools keep today's partial-publish rule: a broken file is reported, healthy
   files still publish; only "failures and zero loadable tools" retains the old
   set.
5. Bump `generation` once per call that published at least one surface; emit
   `session_reloaded {generation, reason, changes_summary}` (kernel event with a
   pure reducer handler).
6. `preview` runs steps 3's discovery and validation, publishes nothing, bumps
   nothing, emits nothing.

### 5.3 Skills dict

The shared skills mapping handed to workflow config and command contexts is
replaced by an immutable-surface holder (same mechanism as WFC-08). The
`clear()`/`update()` pair is deleted; readers call `holder.current()`.

### 5.4 Commands

`/skills reload`, `/commands reload`, `/tools reload` (the command the current
`create_tools` text already tells users to run) and `/workflow reload` become
thin callers of `reload({surface})`. A new `/reload [surface…]` reloads any
set; with no argument it reloads all four. Registered in
`commands/builtins.py` so completion, help and aliases come from the one
registry (CLAUDE.md "Adding a slash command"), with picker and execution tests.
The command context's four `reload_*` callbacks are replaced by one
`reload: Callable[[frozenset[ReloadSurface]], Awaitable[ReloadReport]]`.

### 5.5 Headless

`SessionContext.reload` exists in both runners. Headless emits the report as a
JSON line event (`{"event":"reload", …}`).

## 6. Requirements

- **FR-1** Discovery never runs on the event loop thread; swaps do, and touch no
  disk.
- **FR-2** A reload that fails on one surface still publishes the others and
  reports `ok == False`.
- **FR-3** `diagnostics` never contain file contents or secrets — path, code,
  digest and a bounded message only.
- **FR-4** The report type is the only reload result type; `tuple[bool, str]`
  returns are deleted.
- **FR-5** Reload while a turn is in flight is allowed. A published surface is
  visible to consumers at their defined boundary (WFC-08), never mid-callback.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | `ReloadService.reload` with one broken and two healthy tool files publishes the two, reports one `import_error`, and `ok == False` only if a *requested surface* failed to publish. |
| A2 | A reader polling `holder.current()` from a second thread during a skills reload never observes an empty or partial mapping (stress test, 10 k reads). |
| A3 | With 200 tool files and 50 skills, the event loop's worst stall during `reload` is < 50 ms and p95 < 10 ms on the TUIR-04 benchmark harness. |
| A4 | `preview` leaves `generation`, registries and the event log unchanged. |
| A5 | `/reload`, `/skills reload`, `/tools reload`, `/commands reload`, `/workflow reload` all appear in the picker and execute through the service. |
| A6 | Replaying a log with two `session_reloaded` events reproduces both generations. |
| A7 | `grep -rn "_reload_skills\|_reload_tools\|_reload_workflows\|_reload_commands\|reload_skills=\|reload_tools=" src tests docs` returns nothing. |

## 8. Test plan

- Unit: report arithmetic, ordering, coalescing, preview purity
  (`tests/unit/test_reload_service.py`).
- Integration: real files under `tmp_path` for each surface, trust store
  override, concurrent-reader stress test
  (`tests/integration/test_reload_service_surfaces.py`).
- Performance: `tests/performance/test_reload_lag.py`, reusing the TUIR-04
  sampler and budget style.
- Command tests for picker visibility and execution.

## 9. Symbols removed

`TUISession._reload_skills`, `_reload_tools`, `_reload_workflows`,
`_reload_commands`; the four callback fields in the command context; the
`_wf_config_base` replacement dance in `_reload_tools`. Docs
(`docs/guides/commands.md`, `plugins.md`, `tools.md`, `custom-workflows-and-config.md`)
and `llms-full.txt` are updated in the same change.

## 10. Risks

| Risk | Mitigation |
|---|---|
| A thread-run discovery imports plugin modules that assume the main thread. | Import-time work moves to the thread, instantiation to the loop; a plugin that fails thread-import is reported, not crashed. Documented in `plugins.md`. |
| Users rely on the old `(bool, str)` messages. | The report's `summary()` keeps the same information; commands print it. |
| Reload storms. | Single-flight + coalescing; authoring returns call it once per pop. |

## Amendment — 2026-11-22

The existing kernel uses PascalCase event names. The reducer handles
`SessionReloaded` and headless workflow stdin emits `"event": "reload"`.
The TUI supports both the new `/workflow reload` form and the existing
`/workflows reload` form. Scope selection and interactive command execution
use the session’s shared service. Tool and skill snapshots are also shared
with workflow configuration, so reload needs no config replacement.

## Implementation status

Service and event commit `c757516`. Runner and command integration commit
`7bd642c`.

| Criterion | Automated evidence |
|---|---|
| A1 | `test_tools_publish_healthy_files_and_preserve_failed_surfaces` |
| A2 | `test_skill_reader_sees_one_complete_snapshot_during_reload` |
| A3 | `test_reload_discovering_two_hundred_tools_and_fifty_skills_meets_lag_budget` |
| A4 | `test_preview_leaves_generation_surfaces_and_events_unchanged` |
| A5 | `test_picker_offers_the_reload_command_and_registry_reload_arguments`, `test_submitting_each_reload_form_calls_the_async_service`, `test_workflow_reload_submission_uses_the_session_service` |
| A6 | `test_reload_order_and_replay_generations` writes two events to JSONL and replays the log. |
| A7 | `test_reload_migration_has_no_previous_callback_names` |

`test_headless_workflow_stream_runs_each_nonempty_stdin_line` asserts the
headless JSON report. `test_reload_runs_during_an_active_turn_without_queuing`
asserts live TUI dispatch. `test_tool_reload_uses_source_even_when_timestamp_and_size_match`
covers original and reloaded tool behavior.
