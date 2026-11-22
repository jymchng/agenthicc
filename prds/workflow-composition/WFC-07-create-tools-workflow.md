---
title: "WFC-07: create_tools — rename make_agenthicc_tool and create one or several tools per run"
status: Proposed
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P0
related_prds: [WFC-00, WFC-01, WFC-02, WFC-05, WFC-08, WFC-11, WFC-14, prd-24, prd-147]
tags: [workflows, tools, authoring, rename]
---

# WFC-07 — `create_tools`

## 1. Summary

Rename the built-in `make_agenthicc_tool` workflow to **`create_tools`** and
make it author **one or several tools in a single run**. It returns a typed
`WorkflowReturn` of staged tool artifacts; the session then commits, trusts and
reloads them automatically — there is no approval step (WFC-11, WFC-05) — and
the agent uses them on its next turn (WFC-08).

> The request calls the existing workflow `make_agenthicc_tools`; the shipped
> name is `make_agenthicc_tool` (singular). This PRD renames the shipped one.

## 2. Problem (current source)

- The workflow is registered as `make_agenthicc_tool`
  (`workflows/loader.py:67`) with a package, classes and exports named for one
  tool: `MakeAgenthiccToolWorkflow`, `MakeToolContext`, `MakeToolParams`,
  `MakeToolRunner`, `MakeToolState`, `ToolParam`
  (`workflows/__init__.py:86-94`).
- `MakeToolContext` holds a single `tool_name`, `tool_file_path`, `parameters`,
  `capabilities`, `dependencies`, `class_form`, `plan`
  (`workflows/make_agenthicc_tool/runner.py` ~`:110-135`). The planning tool
  `submit_tool_plan(tool_name, description, parameters, …)` accepts exactly one
  tool and enforces `.agenthicc/tools/` as the destination.
- The finalize phase tells the **user** to run `/tools reload` (~`:758`), the
  manual step the series removes.
- A prompt-only bootstrap skill `create-tools` (`skills/bootstrap.py:222`) shares
  the surface name and instructs the same job without any of the workflow's
  validation, so there are two ways to do one thing.
- **The verdict belongs to the agent.** The validate phase ends on the agent's
  `approve_tool(summary)`; nothing compares the written file with the plan
  (`runner.py` ~`:290-342`). **Containment is a substring test**: the reported
  `file_path` only has to *contain* `.agenthicc/tools/` (~`:273`), and the
  runner never scans what was written. **Repair is unbounded**: a reject returns
  to GENERATE with no cycle counter (`:663-728`; only `_MAX_ATTEMPTS = 5` bounds
  attempts inside one phase). WFC-14 defines the shared contract that fixes all
  three; this PRD supplies the tool-specific rules.
- The workflow writes straight into the live `.agenthicc/tools/` directory. The
  file stays untrusted, so it does not load, and the user is left to trust it
  by hand outside the session, so a tool an agent creates is never usable by that
  agent in the same session.

## 3. Goals

1. The workflow is named `create_tools` everywhere; `make_agenthicc_tool`
   ceases to exist.
2. One run plans, generates, validates and stages N ≥ 1 tools; a run is atomic:
   all tools publish or none.
3. The run ends by returning staged artifacts that the session publishes, trusts
   and loads automatically, not by instructing a human to trust and reload.
4. The rename leaves no alias, shim or duplicate surface.

## 4. Non-goals

- The generic verdict, scoping and repair-budget rules (WFC-14) and the
  publish/trust mechanics (WFC-11); this PRD says how each applies to a
  set of tools and adds the tool-specific checks.
- Command plugins (`create-commands`) and MCP tools.
- Hot-swapping a tool's behaviour in an *in-flight* call (WFC-08).

## 5. Design

### 5.1 Rename (single change, no alias)

| Old | New |
|---|---|
| workflow name `make_agenthicc_tool` | `create_tools` |
| package `workflows/make_agenthicc_tool/` | `workflows/create_tools/` |
| `MakeAgenthiccToolWorkflow` | `CreateToolsWorkflow` |
| `MakeToolContext / Params / Runner / State` | `CreateToolsContext / Params / Runner / State` |
| `ToolParam` | `ToolParam` (unchanged; it describes one tool parameter) |
| `submit_tool_plan` | `submit_tools_plan` |

`workflows/loader.py` descriptor, the lazy-export table in
`workflows/__init__.py`, `llms-full.txt`, docs and tests all move together.
`WorkflowRegistry.register_alias` is **not** used. A stale `make_agenthicc_tool`
checkpoint fails the existing validation/`PLUGIN_INCOMPATIBLE` path with the
message "this workflow was renamed to create_tools; start a new run", and
`/workflow reset <run-id>` discards it.

The prompt-only `create-tools` bootstrap skill is deleted (an unmodified seeded
copy is removed through the bootstrap marker mechanism; an edited copy is left
and reported). The `$create-tools` command disappears; the workflow is the
single way.

### 5.2 Plan for a set

```python
@dataclass
class ToolPlan:
    name: str                       # snake_case identifier, unique within the run
    module: str                     # file stem; defaults to name; tools may share a module
    description: str
    parameters: list[ToolParam]
    capabilities: list[str]         # ToolCapability names
    dependencies: list[str]
    requires_confirmation: bool
    class_form: bool

@dataclass
class CreateToolsContext:
    intent: str
    run_id: str
    state: CreateToolsState
    repair: RepairBudget            # WFC-14, per run, persisted
    plans: list[ToolPlan]
    modules: dict[str, str]         # module -> staged relative path (runner-scanned)
    reports: dict[str, ValidationReport]   # module -> deterministic report (WFC-14)
    fail_reason: str
    …
```

`submit_tools_plan(tools: list[ToolPlanInput])` accepts one to `N` plans,
validates the **set**: unique names; no collision with any built-in tool, MCP
tool or already-live project tool (a deliberate *update* names the existing
tool); every `module` a valid identifier; parameter/capability vocabulary as
today.

### 5.3 Phases

`analyze → generate → validate → finalize`, as today, with set semantics:

| Phase | Behaviour for a set |
|---|---|
| analyze | Plans the set; may decide "this is two modules, three tools". |
| generate | Writes each module under `.agenthicc/.staging/<run_id>/tool/<module>.py` through the WFC-14 scoped writer (§5.3 there): the root is runner-derived, the model chooses names only, and the runner scans the root for the manifest instead of trusting a reported path. The `.py` suffix only; ≤ 12 modules; ≤ 64 KB each. |
| validate | The §5.4 checks run **per module**, then **across the set**, and produce a `ValidationReport` per module. The verdict is WFC-14's `decide`: the agent can reject, never approve over an error. The would-it-load check is `ReloadService.preview({"tools"})` against the staged set, run in the sandboxed validator (WFC-05 §5.2, WFC-11 §5.5). |
| finalize | Builds the return; makes **no** claim that tools are live. |

Repair is bounded **per run**, not per tool (WFC-14 §5.4): one `RepairBudget`
shared by every module; exhaustion fails the whole run with the last reports and
starts no further provider turn. A failing module fails the set — there is no
partial publish chosen by the model.

### 5.4 Tool-specific deterministic checks

Static only: the source is parsed with `ast`, never imported or executed in the
agent process. Each finding has a stable code (WFC-14 `Finding`).

| Check | Code | Severity |
|---|---|---|
| Module parses; ≤ 64 KB | `tool.syntax` | error |
| Top level contains only imports, constants, annotated assignments, function/class definitions, a docstring and `TOOLS`; no top-level calls (importing the module has no side effects) | `tool.import_side_effect` | error |
| `TOOLS` is a list of names defined in the module and equals the module's planned tools exactly | `tool.export_mismatch` | error |
| Each tool's `@tool(name=…)` equals the plan name; names are unique across the set; none equals a built-in, MCP or live project tool unless the plan marks an update | `tool.name_mismatch`, `tool.name_collision` | error |
| Parameter names, annotations and defaults match the plan; every parameter and the return use concrete types (no `typing.Any`) | `tool.signature_mismatch`, `tool.any_type` | error |
| Return annotation is JSON-serializable (`dict[str, object]` or a typed dict) and the docstring documents every parameter | `tool.return_type`, `tool.docstring` | error |
| Capabilities in the decorator metadata equal the plan's; imports imply required capabilities (network client → `network`; `subprocess`/`os.system`/`exec`/`eval` → `execute`; filesystem calls outside `WorkspaceView` → `filesystem`) and a missing one is an error | `tool.capability_mismatch` | error |
| No private HTTP client: a call constructing `httpx.AsyncClient`/`httpx.Client`/`requests.Session` (use the shared client) | `tool.private_http_client` | error |
| Declared `dependencies` are importable names; stdlib names need no declaration | `tool.dependency` | warning |
| Output-bounding: a tool that can return unbounded data (reads files or network) has a documented limit parameter or an obvious truncation | `tool.unbounded_output` | warning |

These mirror the "Adding a tool" rules in `CLAUDE.md`. Static checks cannot prove
semantic behaviour or correct capability classification; the capability gate
enforces declared capabilities on every call, and the publish notice and
provenance (WFC-11) record them with the network hosts.

### 5.5 Return and availability

```python
WorkflowReturn(
    status="completed",
    summary="Created 3 tools in 2 modules: fetch_rss, parse_rss, summarize_feed",
    outputs={"tools": "fetch_rss,parse_rss,summarize_feed"},
    artifacts=(PublishedArtifact(kind="tool", name="fetch_rss", …, state="staged"), …),
)
```

On pop the session runs the WFC-11 §5.2 publish step — gate → commit → trust →
`reload({"tools"})` → provenance → notice — and then delivers (WFC-06 §5.4). The
agent's resume turn lists each tool with its `loaded` / `load_failed` / `staged`
state and its first model request includes the loaded tools (WFC-08).

### 5.6 Invocation declaration

`CreateTools.invocation = WorkflowInvocation(agent_callable=True,
subagent_eligible=True, publishes=("tool",), when_to_use="Create one or several
new agenthicc tools the current session can call once they are published", returns="Names
and state of each tool created")`.

## 6. Requirements

- **FR-1** After the change, `grep -rn "make_agenthicc_tool\|MakeTool\|submit_tool_plan\|create-tools" src tests docs skills llms.txt llms-full.txt README.md`
  returns only the migration message string and historical PRDs/CHANGELOG.
- **FR-2** A run with N plans stages N tools or none; no module is moved on
  partial success.
- **FR-3** A single-tool run behaves as a degenerate set; no separate code path.
- **FR-4** Capability, network and dependency declarations in the plan are
  carried into `PublishedArtifact` for the publish notice and provenance (WFC-11).
- **FR-5** CLAUDE.md "Adding a tool" rules are validated deterministically:
  `WorkspaceView` for paths, the shared HTTP client (no private
  `httpx.AsyncClient`), JSON-serializable results, bounded output.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | Registry names contain `create_tools` and not `make_agenthicc_tool`; `/workflow make_agenthicc_tool` reports an unknown workflow. |
| A2 | A scripted run plans three tools across two modules; all three stage, validate and return as artifacts. |
| A3 | A duplicate tool name, or a name equal to a built-in, fails `submit_tools_plan` with a typed error before any file is written. |
| A4 | One failing module fails the run; nothing is moved out of staging. |
| A5 | After the run completes — with no prompt — the three tools are trusted and loaded, and the agent's first resumed request lists them (WFC-08 A1). |
| A6 | The repair budget is shared: two failing modules consume one run budget, and exhaustion stops provider turns. |
| A7 | A stale pre-rename checkpoint is refused with the migration message. |
| A8 | FR-1's search returns only the allowed hits. |

## 8. Test plan

- Unit: plan-set validation, rename guard (a test that fails if the old name is
  registered), return construction.
- Integration: staged generation with a scripted transport, set validation,
  atomic failure, repair budget (`tests/integration/test_create_tools_set.py`).
- E2E: WFC-13 S4.
- The existing `make_agenthicc_tool` tests are moved and updated, not copied.

## 9. Documentation and cleanup

`docs/guides/tools.md`, `plugins.md`, `workflows.md`, `llms.txt`,
`llms-full.txt`, README mentions, `skills/` references, `CHANGELOG.md` entry
(`BREAKING: make_agenthicc_tool renamed to create_tools`). The in-tree
`approve_tool` / `confirm_generation_complete(file_path, …)` path-claim logic is
deleted in favour of WFC-14's runner scan and verdict.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Users have muscle memory for the old name or `$create-tools`. | Deliberate break; the unknown-workflow message names `create_tools`. |
| Atomic sets make one bad tool block good ones. | Intentional: the model plans a coherent set; re-run for the failing subset. |
| Larger generation scope raises cost. | Per-run repair budget (WFC-14 §5.4) and per-module scoped generation. |
| Static checks both over- and under-reject. | Stable finding codes, `warning` severity for advisory rules, the sandboxed would-load preview, and call-time capability gating, which is independent of the validator (WFC-11 §5.4). |
