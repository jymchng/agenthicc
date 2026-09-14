# Workflow Package Review — Historical Findings

> This is an evidence record, not a claim that every finding is still open.
>
> **Citation convention.** Every `file:line` reference below is written as a
> full path from the repository root (for example
> `src/agenthicc/workflows/plugin.py:206`) so it can be resolved by a reader or
> a script without knowing which module currently owns the symbol. The one
> exception is called out inline: the original audit also cited `base.py`,
> which has since been removed. Line numbers are from the working tree at the
> date of the snapshot and drift as the file changes; treat a line number as a
> starting point, not as a guarantee.
> Re-check each item against the current branch before implementing it. The
> prioritized repository roadmap is [PRD-138](https://github.com/agenthicc/agenthicc/blob/main/prds/prd-138-repository-improvement-roadmap.md).

**Package:** `src/agenthicc/workflows/`
**Scope:** Original audit of 17 source files; current package contains 21 Python files
**Findings:** 9 architectural issues · 10 code quality issues · 9 bugs · 5 integration gaps

## PRD-179 generated-runner lifecycle audit — 2026-08-28

The generated-workflow contract now has one shared lifecycle surface in
`agenthicc.workflows.phase_lifecycle`. `PhaseAnnotation` projects the actual
PhaseSpec cursor to `AppState` and `WorkflowRunHandle`; it is separate from the
cache-stable prompt. `checkpoint_phase_boundary()` persists committed phase
output and the next cursor before another provider turn, including terminal
boundaries, and propagates `PhaseBoundaryError` to the session failure
finalizer. `reconcile_phase_cursor()` is the pure pre-prompt cursor resolver;
reconstruct_site supplies integrity-verified phase receipts so a stale INIT
checkpoint cannot replay completed research phases.

`create_workflow` exposes `describe_phase_lifecycle()` and
`show_phase_lifecycle_template()` to authoring agents. Strict validation now
requires lifecycle evidence for new custom runners, while the default legacy
validation mode remains compatible with existing manually installed plugins.
The generated template and smoke path exercise the same annotation, boundary,
resume, and error-propagation contract.

## PRD-185 dynamic `goal_flow` audit — 2026-08-31

Resolved in the current implementation. `goal_flow` now owns a canonical
stable-ID `GoalRecord` list, while the historical string/index arrays remain
derived compatibility projections. `append_goal(goal)` and
`insert_goal(index, goal)` are available only during implementation and
verification, are explicitly non-transitioning, and checkpoint their mutation
before returning success. Verification completes the captured record by ID,
not by its pre-turn numeric index, so an insertion before the active goal
cannot verify or replay the wrong record. The checkpoint codec writes a
versioned list, active ID, monotonic list revision, bounded receipts, and
migrates legacy parallel-array checkpoints at their saved cursor. Completion
rejects pending/active records. Unit, integration, and scripted-provider E2E
tests cover schemas, insertion shifts, rollback, migration, scheduling, and
normal dynamic completion.

## Current status audit — 2026-07-29

The findings below originated from an earlier package review. **The prose under
each `### A1`…`### D5` heading is the original text, preserved as the evidence
record — it is not a current bug list.** Read every heading together with the
status table below, which was re-derived from the working tree.

| Finding | Current status | Verified evidence |
|---|---|---|
| A1 / D3 — specialized runners versus `PhaseSpec` metadata | Open | `CodePlan.phases` declared at `src/agenthicc/workflows/code_plan/definition.py:59`; `require_*` flags at `src/agenthicc/workflows/code_plan/definition.py:67`; `CodePlanRunner` owns prompts/retries in `code_plan/runner.py` |
| A2 — duplicate tool filtering | Open | `src/agenthicc/workflows/default/runner.py:983::_filter_tools` and `src/agenthicc/workflows/code_plan/runner.py:1000::_base_tools` are still separate implementations |
| A3 — `default/runner.py` drops `memory_router`/`semantic_index` | Needs targeted revalidation | neither name is referenced anywhere in `default/runner.py`; confirm the generic runner intentionally omits them before closing |
| A4 — `WorkflowPlugin.determine_transition()` dead code | **Resolved** | no `determine_transition` on `WorkflowPlugin`; the live `_determine_transition` is `src/agenthicc/workflows/default/runner.py:1015` and is called at `:391` and `:478` |
| A5 — `runner.py` and `builtins.py` shims | **Resolved** | `code_plan/` now contains only `__init__.py`, `definition.py`, `phase_tools.py`, `runner.py`, `state.py` |
| A6 — `build_workflow_runner()` orphan | **Resolved** | replaced by `plugin_cls.build_runner()`; a tombstone comment remains at `src/agenthicc/workflows/default/runner.py:1028` |
| A7 / C4 — parallel failure handling | **Resolved in current generic runner** | `src/agenthicc/workflows/default/runner.py:327-331` records the gate error, sets `status="failed"`, `current_phase=None`, and returns. Note: `asyncio.gather` at `:297` does **not** pass `return_exceptions=True`; the gate-error path is what resolves this, so an earlier revision of this table cited the wrong mechanism |
| A8 — mutable class-level list defaults | Open | `src/agenthicc/workflows/plugin.py:116-117` (`available`, `mutations`), `:149` (`sections`), `:471-472` (`mode_bindings`, `phases`) |
| A9 — `classmethods` as storable callables | Open | `@classmethod` still used throughout `plugin.py` (first at `:485`) |
| B1 — `typing.Any` where real types exist | **Resolved** | no `: Any` / `-> Any` in `memory_tools.py`, `code_plan/state.py`, `code_plan/phase_tools.py`, `plugin.py`, or `base_runner.py`. The finding cites `base.py`, which does not exist — the module is `base_runner.py` |
| B2 — `PhaseRole` is not an Enum | Open | `src/agenthicc/workflows/plugin.py:206` is still `class PhaseRole(str)` with 7 plain string attributes |
| B3 — `_parse_output_schema` exported despite private name | Open | exact string is in the package `__all__` at `src/agenthicc/workflows/__init__.py:33` and `:146` (not `plugin.__all__`, which does not exist) |
| B4 — inline `import dataclasses as _dataclasses` | Open, relocated | now `src/agenthicc/workflows/default/runner.py:616` (the finding cited `code_plan/phase_tools.py`) |
| B5 — `_PHASE_INDEX` out-of-sync risk | Open | `src/agenthicc/workflows/code_plan/runner.py:131`, consumed at `:250`, `:284`, `:417` |
| B6 — magic number `10` in three places | **Resolved** | no `max_turns=10` remains in `code_plan/runner.py` |
| B7 — `PhaseOutput.structured`/`metadata` typed as bare `dict` | Open | `src/agenthicc/workflows/plugin.py:363` |
| B8 — `determine_transition()` dead `ctx` parameter | **Resolved** | subsumed by A4: the method no longer exists |
| B9 — `"plan".title()` | Open | `src/agenthicc/workflows/code_plan/runner.py:225` |
| B10 — duplicate `asyncio`/`uuid` re-imports in closures | Open | `src/agenthicc/workflows/code_plan/phase_tools.py:462` |
| C1 — `phase_history` never populated | Open | `CodePlanRunner.run()`'s direct state loop does not append a `PhaseRunRecord`; `phase_history` is written only by the generic runner |
| C2 — `resume()` loses `execute_summary`/`review_summary` | Open | `resume()` body references neither `execute_summary` nor `review_summary` |
| C3 — headless `require_plan_finalization` exits without a plan | **Resolved** | no `require_plan_finalization` remains in `code_plan/runner.py` |
| C5 — `resume()` blind to unexpected `completed` sets | Open | `resume()` still uses a fixed set map |
| C6 — `_find_resume_phase()` wrong phase with `on_reject` loops | Open | `src/agenthicc/workflows/default/runner.py:453` |
| C7 / D2 — `make_questions_tool` injection | Open | injected at `src/agenthicc/workflows/default/runner.py:1010` and `src/agenthicc/workflows/code_plan/runner.py:449`; the original claim that it was never injected is now too strong |
| C8 — mode restore in `_run_phase.finally` accidentally correct | Open — behavioural, not machine-checkable | `src/agenthicc/workflows/code_plan/runner.py:786::finally` |
| C9 — PRD-111 `exec_cfg` silent no-op path | Open | `src/agenthicc/workflows/code_plan/runner.py:878` |
| D1 — per-phase TOML only for `code_plan` | Needs targeted revalidation | no `phase_config`/`per_phase` symbol in `plugin.py`; the mechanism may have moved or been removed |
| D4 — `system_prompt_suffix` not passed when override empty | Open | `src/agenthicc/workflows/default/runner.py:673` |
| D5 — `_set_phase()` versus manual `WorkflowRun` construction | Open | `src/agenthicc/workflows/code_plan/runner.py:838::_set_phase` and `:851::update_workflow_phase` |

Summary of the re-derivation: **33 findings — 21 open, 7 resolved, 2 needing
targeted revalidation, 2 special cases (C4 tracked with A7; D2 merged into C7).**

When implementing a finding, update this table and add a regression test. Do
not copy a historical code line or line number without checking the current
branch first — A4, A5, A6, B1, B6, B8, and C3 each cite code that has since been
removed or renamed, and the A7 row previously cited a `return_exceptions=True`
argument that was never present.

Re-derive the machine-checkable subset of this table with:

```bash
# A8 — mutable class-level defaults still present?
grep -nE '^\s+(available|mutations|sections|mode_bindings|phases): list\[.*\] = \[\]' \
  src/agenthicc/workflows/plugin.py

# A4 — is the dead WorkflowPlugin transition hook really gone?
grep -rn 'def determine_transition' src/agenthicc/workflows/

# A2 — do both tool filters still exist?
grep -rn 'def _filter_tools' src/agenthicc/workflows/default/runner.py
grep -rn 'def _base_tools'   src/agenthicc/workflows/code_plan/runner.py
```

Findings are ranked within each section: **Critical** → **High** → **Medium** → **Low**.

---

## A. Architectural Issues

### A1 ★ Dead `phases` list in `CodePlan` — two sources of truth (Critical)

`src/agenthicc/workflows/code_plan/definition.py:50–111` declares four `PhaseSpec` objects with detailed prompts, retry caps, and capability flags. `CodePlan.runner_factory` returns a `CodePlanRunner` that **ignores the phases list entirely** — it has its own hardcoded state machine, its own prompts (`_PLAN_PROMPT`, etc.), and its own retry loops.

The `phases` list looks authoritative — it appears in IDE completions, in `WorkflowDefinition.phases`, and in every tool that lists workflow structure. But it is inert configuration that can never be changed to affect runtime behavior.

**Fix:** Either (a) have `CodePlanRunner` read `self._def.phases` for prompts, retry caps, and mode overrides — making the definition the single source of truth, or (b) remove the `phases` list from `CodePlan` entirely and add a docstring explaining that `CodePlanRunner` drives behavior directly.

---

### A2 ★ Two parallel tool-filtering implementations (High)

`src/agenthicc/workflows/default/runner.py:645 _filter_tools()` and `src/agenthicc/workflows/code_plan/runner.py:648 _base_tools()` implement nearly identical logic: iterate `plugin_tools`, union with MCP tools, filter by `mode_blocked`. Differences:
- `_filter_tools` additionally applies `phase_allowed` capability intersection
- `_base_tools` appends memory tools unconditionally

Any bug fix or new tool source must be applied in both places.

**Fix:** Extract a module-level `_build_tool_list(cfg, *, mode_blocked, phase_allowed=None, include_memory=True)` in `config.py` or a `tools.py` helper. Both runners call it.

---

### A3 ★ `default/runner.py` silently drops `memory_router` and `semantic_index` (High)

`_run_phase` builds `_turn_kwargs` at lines 387–404. Neither `memory_router` nor `semantic_index`.

**Fix:** Add `memory_router=self._cfg.memory_router, semantic_index=self._cfg.semantic_index` to `_turn_kwargs`.

---

### A4 `WorkflowPlugin.determine_transition()` is dead code (High)

`src/agenthicc/workflows/plugin.py:386–391` defines `determine_transition()` as a customization hook. No call site uses it — `WorkflowRunner._determine_transition()` (line 668) has its own private copy of the same logic and never calls the plugin method.

**Fix:** Either wire `WorkflowRunner._determine_transition` to call `self._def.plugin.determine_transition()` so authors can override routing, or delete the dead method.

---

### A5 `runner.py` and `builtins.py` shims serve no purpose (Medium)

`workflows/runner.py` and `workflows/builtins.py` are documented as backward-compat shims. Neither module is imported by any source file — `__init__.py` already re-exports every symbol they forward. They add confusion about canonical import paths.

**Fix:** Delete both files. (See project policy: no backward-compat shims when the codebase isn't in production.)

---

### A6 `build_workflow_runner()` factory is an orphan (Medium)

`src/agenthicc/workflows/default/runner.py:680` defines `build_workflow_runner()`, a trivial `WorkflowRunner(...)` constructor call exported in `__init__.py`. No caller in the codebase uses it — all dispatch goes through `defn.build_runner()` (PRD-110). It predates the `runner_factory` pattern.

**Fix:** Delete `build_workflow_runner()` and remove from `__all__`.

---

### A7 Parallel phase execution has no kernel events and swallows exceptions (Medium)

`src/agenthicc/workflows/default/runner.py:218–246`: when `spec.parallel_with` is non-empty, `asyncio.gather` runs phases concurrently without emitting `WorkflowPhaseStarted` / `WorkflowPhaseCompleted` events. Parallel phase failures are caught and logged (`log.error`) but the workflow continues as if they succeeded, leaving missing outputs in `context.phase_outputs`.

**Fix:** Emit `WorkflowPhaseStarted` before the gather and `WorkflowPhaseCompleted` for each result. On `isinstance(output, Exception)`, route through `spec.on_error` if set, else fail the workflow.

---

### A8 `WorkflowPlugin` mutable class-level list defaults (Medium)

`src/agenthicc/workflows/plugin.py:343–344`: `mode_bindings: list[str] = []` and `phases: list[PhaseSpec] = []` are mutable class-level lists shared across all instances. Any in-place `.append()` mutates the default for all subclasses. `to_definition()` calls `tuple(self.phases)` which is safe, but the mutation risk is real.

**Fix:** Type as `ClassVar[list[...]] = []` or change to instance defaults via `field(default_factory=list)` if `WorkflowPlugin` becomes a dataclass.

---

### A9 `classmethods` used as storable callables — `defn` vs `cls` confusion (Low)

`WorkflowPlugin.runner_factory` and `params_factory` are `@classmethod` methods stored on `WorkflowDefinition` via `to_definition()`. When called from `build_runner()`, the first positional argument is the `WorkflowDefinition` instance, not the plugin class. `CodePlan.runner_factory` papers over this with `# type: ignore[override]` and a renamed parameter. This works but is confusing and non-standard.

**Fix:** Make `runner_factory` and `params_factory` plain `@staticmethod` class attributes with explicit signature `(defn, config, mode_manager) -> BaseWorkflowRunner`, removing the `cls` binding entirely.

---

## B. Code Quality Issues

### B1 ★ `typing.Any` where real types are available (High)

Violates the project "No Any in new files" rule:

| Location | Original finding | Current status |
|---|---|---|
| *the base workflow-context parameter* | `-> Any` | Module cited as `base.py`, since removed; current module is `src/agenthicc/workflows/base_runner.py` (51 lines) and declares no `Any` |
| `src/agenthicc/workflows/memory_tools.py:17–18` | `memory_router: Any`, `semantic_index: Any` | Resolved — now `MemoryRouter \| None`, `SemanticIndex \| None` under `TYPE_CHECKING` |
| `src/agenthicc/workflows/code_plan/phase_tools.py:24` | `approval_svc: Any` | Resolved — now `ApprovalService \| None` under `TYPE_CHECKING` |
| `src/agenthicc/workflows/code_plan/state.py:48` | `shared_memory: Any` | Resolved — now `ShortTermMemory \| None` under `TYPE_CHECKING` |

---

### B2 `PhaseRole` is not an Enum (Medium)

`src/agenthicc/workflows/plugin.py:54`: `class PhaseRole(str)` — seven plain string class attributes. No exhaustiveness checking, no IDE autocomplete on `.` completion, no `list(PhaseRole)` enumeration.

**Fix:** `class PhaseRole(str, enum.Enum): PLANNER = "planner"` etc. Existing string equality (e.g. `spec.agent_type == "planner"`) continues to work because `StrEnum` members compare equal to their string values.

---

### B3 `_parse_output_schema` exported in `__all__` despite private name (Medium)

`src/agenthicc/workflows/__init__.py:32` exports `_parse_output_schema`. A leading-underscore symbol in `__all__` is a contradiction.

**Fix:** Rename to `parse_output_schema` and update the one call site in `default/runner.py`.

---

### B4 Inline `import dataclasses as _dataclasses` inside hot method (Low)

`src/agenthicc/workflows/default/runner.py:372`: `import dataclasses as _dataclasses` inside `_run_phase()` which is called once per phase. `dataclasses` is already imported at module top (line 5).

**Fix:** Remove inline re-import; use the module-level `dataclasses` alias.

---

### B5 `_PHASE_INDEX` out-of-sync risk with `CodePlanState` (Low)

`src/agenthicc/workflows/code_plan/runner.py:99–101`: `_PHASE_INDEX = {"plan": 0, "execute": 1, "review": 2, "summarize": 3}`. If a new state is added to `CodePlanState` without updating `_PHASE_INDEX`, the new phase silently shows as "Phase 1" in the TUI.

**Fix:**
```python
_PHASE_INDEX = {s.name.lower(): i for i, s in enumerate(CodePlanState) if not s.is_terminal}
```

---

### B6 Magic number `10` in three independent places (Low)

`src/agenthicc/workflows/code_plan/runner.py:41–43`: `_MAX_PLAN_ATTEMPTS = _MAX_EXECUTE_ATTEMPTS = _MAX_REVIEW_ATTEMPTS = 10`. `src/agenthicc/workflows/default/runner.py:411,441`: fallback `else 10`. `src/agenthicc/workflows/code_plan/definition.py:57,72,89`: `max_iterations=10` (ignored by `CodePlanRunner`). Three independent copies of the same number with no coordination.

**Fix:** Use the module-level constants from `code_plan/runner.py` as defaults in `code_plan/definition.py`. Remove the `else 10` from `default/runner.py` in favour of a named constant.

---

### B7 `PhaseOutput.structured` and `metadata` typed as bare `dict` (Low)

`src/agenthicc/workflows/plugin.py:251,253`: `structured: dict | None` and `metadata: dict`. Should be `dict[str, object]`.

---

### B8 `WorkflowPlugin.determine_transition()` has dead `ctx` parameter (Low)

`src/agenthicc/workflows/plugin.py:387–391`: `ctx: WorkflowContext` is accepted but never read inside the method body.

**Fix:** Remove the parameter (breaking change, but the method is already dead code per A4).

---

### B9 `src/agenthicc/workflows/code_plan/runner.py:173` — `"plan".title()` = `"Plan"` (Low)

Initial `current_phase` on the `wf_run` uses `"plan".title()` (= `"Plan"`) while every subsequent update sets it to `phase_name` which is `"plan"`. The TUI receives inconsistent casing between the first and subsequent frames.

**Fix:** `current_phase="plan"`.

---

### B10 Duplicate `asyncio`/`uuid` re-imports inside closures in `phase_tools.py` (Low)

`src/agenthicc/workflows/code_plan/phase_tools.py:348–350`: `import asyncio as _asyncio`, `import uuid as _uuid`, `import json as _json` inside the `ask_user` closure. Both `asyncio` and `uuid` are already module-level imports.

**Fix:** Use the module-level names directly.

---

## C. Bugs

### C1 ★ `code_plan` run `phase_history` never populated; `phases_run` always 0 (Critical)

`code_plan/runner.py`: `wf_run.phase_history` is initialised as `[]` and **never modified** throughout the state machine. `WorkflowRunCompleted` at line 245 emits `phases_run: len(wf_run.phase_history)` = 0 always. `default/runner.py` appends a `PhaseRunRecord` per phase at lines 232–242.

**Fix:** After each phase method returns a new `CodePlanState`, append a `PhaseRunRecord` to `wf_run` and call `workflow_run.set(wf_run)`.

---

### C2 ★ `resume()` loses `execute_summary` and `review_summary` (Critical)

`src/agenthicc/workflows/code_plan/runner.py:278–282`: only `ctx.plan` is restored from the saved context. `ctx.execute_summary` (used in `_review` system prompt) and `ctx.review_summary` (used in `_summarize`) are left as empty strings. Resuming into REVIEW produces a system prompt with `[EXECUTION SUMMARY]\n` (blank) and resuming into SUMMARIZE produces `"What was implemented: (see conversation)"`.

**Fix:** Restore all three fields from `context.phase_outputs`:
```python
if "execute" in completed:
    ctx.execute_summary = context.phase_outputs["execute"].full_text
if "review" in completed:
    ctx.review_summary = context.phase_outputs["review"].full_text
```

---

### C3 ★ Headless `require_plan_finalization` exits without a plan (High)

`src/agenthicc/workflows/default/runner.py:347–359`: `plan_event`, `execute_event`, `review_event` are only created when `approval_svc is not None`. In headless mode, all three are `None`. The condition at line 465 (`elif spec.require_plan_finalization and plan_event is not None`) evaluates to `False`, so the single-turn `else` branch runs instead. After the turn, line 549 also checks `plan_event is not None` → `False`, so `full_text = "".join(output_buf)` with `approved=None`. The `on_reject="plan"` guard is never triggered because `approved` is `None` not `False`. The plan phase exits successfully with no plan.

**Fix:** In headless mode, create the events anyway, and/or add an explicit check: if `require_plan_finalization` is True and no plan was finalized, return `approved=False` explicitly.

---

### C4 Parallel exception handling continues with missing phase output (High)

`src/agenthicc/workflows/default/runner.py:229–232`: on parallel phase failure, `log.error` is called and the loop continues. The failed phase's output is missing from `context.phase_outputs`. Later phases that reference it via `context.as_system_block()` silently receive an incomplete context.

**Fix:** Treat parallel phase exceptions like sequential ones — set `wf_run.status="failed"` and return.

---

### C5 `resume()` blind to unexpected `completed` sets (Medium)

`src/agenthicc/workflows/code_plan/runner.py:283–288`: `resume_map.get(frozenset(completed), CodePlanState.PLAN)` silently restarts from PLAN for any set not in the four hardcoded cases — including already-complete runs (`{plan, execute, review, summarize}`).

**Fix:** Add guards:
```python
if frozenset({"plan","execute","review","summarize"}) <= frozenset(completed):
    return  # already complete
state = resume_map.get(frozenset(completed))
if state is None:
    log.warning("resume: unexpected completed set %s — restarting", completed)
    state = CodePlanState.PLAN
```

---

### C6 `_find_resume_phase()` can return wrong phase when on_reject loops exist (Medium)

`src/agenthicc/workflows/default/runner.py:303–320`: the method walks `_determine_transition()` to find the first incomplete phase. When the last saved phase had `approved=False` and `on_reject` points back to an already-completed phase, the walk can produce unexpected results or be cut short by the `seen` guard.

**Fix:** Walk by `spec.next` exclusively (ignoring `on_reject`) when scanning for the resume point — `on_reject` is a runtime routing decision, not a topological position.

---

### C7 `make_questions_tool` never injected into `WorkflowRunner` phases (Medium)

`src/agenthicc/workflows/code_plan/runner.py:333` injects `make_questions_tool`. `WorkflowRunner._run_phase()` never calls it. Users writing custom generic workflows cannot use `ask_user`.

**Fix:** Add `+ make_questions_tool(self._cfg.approval_svc)` to `filtered` in `_run_phase()`.

---

### C8 Mode restore in `_run_phase.finally` is accidentally correct (Low)

`src/agenthicc/workflows/default/runner.py:509–511`: the `finally` block restores `_original_mode` even when `set_by_name()` failed (returned `None`). In the failure case the mode was not changed, so restoring is a no-op — correct by accident. A future refactor could break this.

**Fix:** Capture whether the mode was actually changed: `_mode_changed = (spec.mode_override and self._mode_manager.set_by_name(...) is not None)` and only restore when `_mode_changed`.

---

### C9 PRD-111 `exec_cfg` replacement condition has a silent no-op path (Low)

`src/agenthicc/workflows/default/runner.py:380–384`:
```python
if _phase_model != getattr(_base_exec, "model", _phase_model)
```
The fallback `_phase_model` means: if `_base_exec` has no `.model` attribute, the condition is `_phase_model != _phase_model` = `False` and the override is silently dropped.

**Fix:** `if _phase_model != self._model_id and dataclasses.is_dataclass(_base_exec)`.

---

## D. PRD-111 / PRD-115 Integration Gaps

### D1 Per-phase TOML config only works for `code_plan` (High)

`WorkflowParams.get_phase_models()` returns `{}` for all generic builtin workflows. Only `CodePlanParams` overrides it. A user who writes a custom `PlanAndExecute` workflow using `WorkflowRunner` cannot configure per-phase models via TOML without also writing a `WorkflowParams` subclass and `params_factory` override.

**Suggestion:** Add a default `get_phase_models()` implementation to `WorkflowParams` that reads from an arbitrary `phase_models: dict[str, str]` field so any workflow can benefit from TOML config without a custom subclass.

---

### D2 `ask_user` / `make_questions_tool` unavailable to generic workflows (D7 above merged) — see C7.

---

### D3 `CodePlan.phases` carries `require_*` flags that are never read by `CodePlanRunner` (Medium)

`code_plan/definition.py` sets `require_plan_finalization=True`, `require_explicit_completion=True`, `require_explicit_review=True` on the phase specs. `CodePlanRunner` never reads them — it has its own handshake logic. If someone inspects `WorkflowDefinition.phases` to determine capabilities, they get correct data. But the actual runtime uses none of it. This is the A1 issue at the integration layer.

---

### D4 `WorkflowRunner` does not pass `system_prompt_suffix` from `spec.system_prompt_override` when empty (Low)

`src/agenthicc/workflows/default/runner.py:396–399`:
```python
role_prompt = (
    spec.system_prompt_override
    or self._cfg.agents_registry.get_role_system_prompt(spec.agent_type)
)
```
When `system_prompt_override = ""` (the default), the registry prompt is used. This is correct. But if a user explicitly sets `system_prompt_override = " "` (a space) to suppress the role prompt without triggering the empty-string fallback, they get a single space as the system prompt suffix — potentially confusing.

---

### D5 `CodePlanRunner._set_phase()` uses `update_workflow_phase()` but `run()` still manually builds the initial `WorkflowRun` (Low)

`src/agenthicc/workflows/code_plan/runner.py:166–168`: the first `WorkflowRun` is built manually in `run()` before the state machine starts. The phase methods then call `_set_phase()` which replaces it. The initial manual construction is redundant with the first `_set_phase("plan", 0, ctx)` call. Two slightly different code paths for what should be one.

**Fix:** Remove the manual `WorkflowRun` construction from `run()` and initialise it via `_set_phase("plan", 0, ctx)` instead, or create a `_init_workflow_run()` helper.

## PRD-178 implementation record — reconstruct_site research boundary

The evidence-complete research work described by PRD-178 is implemented in
the current `reconstruct_site` package. This is a current-state record for
future audits, not a historical finding.

| Requirement | Current implementation | Regression coverage |
|---|---|---|
| Research-first phase topology | The authoritative 41-phase plan includes `responsive_research` and the tool-controlled `research_gate`; static, application, production, and custom profiles share the same validated metadata. | `tests/unit/test_reconstruct_research_prd178.py`, `tests/integration/test_reconstruct_research_prd178.py` |
| Route/viewport/state/interaction coverage | `CoverageMatrix` expands the discovered surface inventory across the deterministic mobile/tablet/desktop viewport matrix and records complete, unavailable, waived, stale, or contradictory evidence. | Unit matrix and stale/round-trip tests; performance expansion test |
| Durable baseline and evidence | `ObservationReceipt`, `InteractionTrace`, and `FidelityBaseline` are validated, hash-addressed, persisted through `ReconstructEvidenceStore`, and rehydrated by reference from checkpoints. | Integration checkpoint/rehydration tests and E2E artifact assertions |
| Implementation gate | Strict approval requires complete coverage; degraded approval requires explicit unavailable-cell exceptions. Rejection returns to a validated research phase and leaves implementation blocked. | Unit gate tests, integration missing-evidence test, E2E degraded-gate run |
| Resume and cache behavior | Research state, gate decision, baseline references, receipts, and the compact coverage digest survive resume. The existing shared conversation and stable prompt/tool bundle remain the workflow cache boundary. | Integration rehydration and existing workflow resume suites |
| Browser degradation and security | Browser absence is recorded as unavailable evidence; no synthetic screenshot is created, and browser/network policy remains owned by the existing guarded integrations. | E2E no-browser degraded run and existing browser policy suites |

The earlier PRD-177 evidence contract remains compatible: old visual and
interaction artifact shapes are accepted during rehydration, while new runs
write the typed PRD-178 shapes. The remaining historical findings above are
not implied to be resolved by PRD-178.

---

## Priority Improvement Roadmap

### Immediate (bugs with user-visible impact)

1. **C1** — Populate `code_plan` `phase_history`; fix `phases_run=0` in events
2. **C2** — Restore `execute_summary`/`review_summary` in `code_plan/runner.resume()`
3. **A3** — Pass `memory_router`/`semantic_index` in `WorkflowRunner._run_phase()`
4. **B9** — Fix `"plan".title()` → `"plan"` in initial `WorkflowRun`

### Short-term (architectural correctness)

5. **A1** — Align `CodePlan.phases` with `CodePlanRunner` or delete the dead phases list
6. **C3** — Fix headless `require_plan_finalization` producing no plan
7. **A4** — Wire or delete `WorkflowPlugin.determine_transition()`
8. **C7** — Inject `make_questions_tool` into `WorkflowRunner._run_phase()`

### Medium-term (clean-up)

9. **A2** — Extract shared `_build_tool_list()` helper
10. **B1** — Replace `Any` with real types everywhere
11. **B2** — Convert `PhaseRole` to `StrEnum`
12. **A5/A6** — Delete `runner.py` shim, `builtins.py` shim, `build_workflow_runner()`
13. **B5** — Generate `_PHASE_INDEX` from `CodePlanState` members
