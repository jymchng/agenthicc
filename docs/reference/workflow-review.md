# Workflow Package Review — Code Quality & Architecture

**Package:** `src/agenthicc/workflows/`
**Scope:** Full audit of all 17 source files
**Findings:** 9 architectural issues · 10 code quality issues · 9 bugs · 5 integration gaps

Findings are ranked within each section: **Critical** → **High** → **Medium** → **Low**.

---

## A. Architectural Issues

### A1 ★ Dead `phases` list in `CodePlan` — two sources of truth (Critical)

`code_plan/definition.py:50–111` declares four `PhaseSpec` objects with detailed prompts, retry caps, and capability flags. `CodePlan.runner_factory` returns a `CodePlanRunner` that **ignores the phases list entirely** — it has its own hardcoded state machine, its own prompts (`_PLAN_PROMPT`, etc.), and its own retry loops.

The `phases` list looks authoritative — it appears in IDE completions, in `WorkflowDefinition.phases`, and in every tool that lists workflow structure. But it is inert configuration that can never be changed to affect runtime behavior.

**Fix:** Either (a) have `CodePlanRunner` read `self._def.phases` for prompts, retry caps, and mode overrides — making the definition the single source of truth, or (b) remove the `phases` list from `CodePlan` entirely and add a docstring explaining that `CodePlanRunner` drives behavior directly.

---

### A2 ★ Two parallel tool-filtering implementations (High)

`default/runner.py:645 _filter_tools()` and `code_plan/runner.py:648 _base_tools()` implement nearly identical logic: iterate `plugin_tools`, union with MCP tools, filter by `mode_blocked`. Differences:
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

`plugin.py:386–391` defines `determine_transition()` as a customization hook. No call site uses it — `WorkflowRunner._determine_transition()` (line 668) has its own private copy of the same logic and never calls the plugin method.

**Fix:** Either wire `WorkflowRunner._determine_transition` to call `self._def.plugin.determine_transition()` so authors can override routing, or delete the dead method.

---

### A5 `runner.py` and `builtins.py` shims serve no purpose (Medium)

`workflows/runner.py` and `workflows/builtins.py` are documented as backward-compat shims. Neither module is imported by any source file — `__init__.py` already re-exports every symbol they forward. They add confusion about canonical import paths.

**Fix:** Delete both files. (See project policy: no backward-compat shims when the codebase isn't in production.)

---

### A6 `build_workflow_runner()` factory is an orphan (Medium)

`default/runner.py:680` defines `build_workflow_runner()`, a trivial `WorkflowRunner(...)` constructor call exported in `__init__.py`. No caller in the codebase uses it — all dispatch goes through `defn.build_runner()` (PRD-110). It predates the `runner_factory` pattern.

**Fix:** Delete `build_workflow_runner()` and remove from `__all__`.

---

### A7 Parallel phase execution has no kernel events and swallows exceptions (Medium)

`default/runner.py:218–246`: when `spec.parallel_with` is non-empty, `asyncio.gather` runs phases concurrently without emitting `WorkflowPhaseStarted` / `WorkflowPhaseCompleted` events. Parallel phase failures are caught and logged (`log.error`) but the workflow continues as if they succeeded, leaving missing outputs in `context.phase_outputs`.

**Fix:** Emit `WorkflowPhaseStarted` before the gather and `WorkflowPhaseCompleted` for each result. On `isinstance(output, Exception)`, route through `spec.on_error` if set, else fail the workflow.

---

### A8 `WorkflowPlugin` mutable class-level list defaults (Medium)

`plugin.py:343–344`: `mode_bindings: list[str] = []` and `phases: list[PhaseSpec] = []` are mutable class-level lists shared across all instances. Any in-place `.append()` mutates the default for all subclasses. `to_definition()` calls `tuple(self.phases)` which is safe, but the mutation risk is real.

**Fix:** Type as `ClassVar[list[...]] = []` or change to instance defaults via `field(default_factory=list)` if `WorkflowPlugin` becomes a dataclass.

---

### A9 `classmethods` used as storable callables — `defn` vs `cls` confusion (Low)

`WorkflowPlugin.runner_factory` and `params_factory` are `@classmethod` methods stored on `WorkflowDefinition` via `to_definition()`. When called from `build_runner()`, the first positional argument is the `WorkflowDefinition` instance, not the plugin class. `CodePlan.runner_factory` papers over this with `# type: ignore[override]` and a renamed parameter. This works but is confusing and non-standard.

**Fix:** Make `runner_factory` and `params_factory` plain `@staticmethod` class attributes with explicit signature `(defn, config, mode_manager) -> BaseWorkflowRunner`, removing the `cls` binding entirely.

---

## B. Code Quality Issues

### B1 ★ `typing.Any` where real types are available (High)

Violates the project "No Any in new files" rule:

| Location | Current | Should be |
|---|---|---|
| `base.py:18` | `-> Any` | `-> WorkflowContext \| CodePlanContext` or TypeVar |
| `base.py:28` | `context: Any` | `context: WorkflowContext` |
| `memory_tools.py:17–18` | `memory_router: Any`, `semantic_index: Any` | `MemoryRouter \| None`, `SemanticIndex \| None` under `TYPE_CHECKING` |
| `phase_tools.py:24` | `approval_svc: Any` | `ApprovalService \| None` under `TYPE_CHECKING` |
| `code_plan/state.py:48` | `shared_memory: Any` | `ShortTermMemory \| None` under `TYPE_CHECKING` |

---

### B2 `PhaseRole` is not an Enum (Medium)

`plugin.py:54`: `class PhaseRole(str)` — seven plain string class attributes. No exhaustiveness checking, no IDE autocomplete on `.` completion, no `list(PhaseRole)` enumeration.

**Fix:** `class PhaseRole(str, enum.Enum): PLANNER = "planner"` etc. Existing string equality (e.g. `spec.agent_type == "planner"`) continues to work because `StrEnum` members compare equal to their string values.

---

### B3 `_parse_output_schema` exported in `__all__` despite private name (Medium)

`__init__.py:32` exports `_parse_output_schema`. A leading-underscore symbol in `__all__` is a contradiction.

**Fix:** Rename to `parse_output_schema` and update the one call site in `default/runner.py`.

---

### B4 Inline `import dataclasses as _dataclasses` inside hot method (Low)

`default/runner.py:372`: `import dataclasses as _dataclasses` inside `_run_phase()` which is called once per phase. `dataclasses` is already imported at module top (line 5).

**Fix:** Remove inline re-import; use the module-level `dataclasses` alias.

---

### B5 `_PHASE_INDEX` out-of-sync risk with `CodePlanState` (Low)

`code_plan/runner.py:99–101`: `_PHASE_INDEX = {"plan": 0, "execute": 1, "review": 2, "summarize": 3}`. If a new state is added to `CodePlanState` without updating `_PHASE_INDEX`, the new phase silently shows as "Phase 1" in the TUI.

**Fix:**
```python
_PHASE_INDEX = {s.name.lower(): i for i, s in enumerate(CodePlanState) if not s.is_terminal}
```

---

### B6 Magic number `10` in three independent places (Low)

`code_plan/runner.py:41–43`: `_MAX_PLAN_ATTEMPTS = _MAX_EXECUTE_ATTEMPTS = _MAX_REVIEW_ATTEMPTS = 10`. `default/runner.py:411,441`: fallback `else 10`. `code_plan/definition.py:57,72,89`: `max_iterations=10` (ignored by `CodePlanRunner`). Three independent copies of the same number with no coordination.

**Fix:** Use the module-level constants from `code_plan/runner.py` as defaults in `code_plan/definition.py`. Remove the `else 10` from `default/runner.py` in favour of a named constant.

---

### B7 `PhaseOutput.structured` and `metadata` typed as bare `dict` (Low)

`plugin.py:251,253`: `structured: dict | None` and `metadata: dict`. Should be `dict[str, object]`.

---

### B8 `WorkflowPlugin.determine_transition()` has dead `ctx` parameter (Low)

`plugin.py:387–391`: `ctx: WorkflowContext` is accepted but never read inside the method body.

**Fix:** Remove the parameter (breaking change, but the method is already dead code per A4).

---

### B9 `code_plan/runner.py:173` — `"plan".title()` = `"Plan"` (Low)

Initial `current_phase` on the `wf_run` uses `"plan".title()` (= `"Plan"`) while every subsequent update sets it to `phase_name` which is `"plan"`. The TUI receives inconsistent casing between the first and subsequent frames.

**Fix:** `current_phase="plan"`.

---

### B10 Duplicate `asyncio`/`uuid` re-imports inside closures in `phase_tools.py` (Low)

`phase_tools.py:348–350`: `import asyncio as _asyncio`, `import uuid as _uuid`, `import json as _json` inside the `ask_user` closure. Both `asyncio` and `uuid` are already module-level imports.

**Fix:** Use the module-level names directly.

---

## C. Bugs

### C1 ★ `code_plan` run `phase_history` never populated; `phases_run` always 0 (Critical)

`code_plan/runner.py`: `wf_run.phase_history` is initialised as `[]` and **never modified** throughout the state machine. `WorkflowRunCompleted` at line 245 emits `phases_run: len(wf_run.phase_history)` = 0 always. `default/runner.py` appends a `PhaseRunRecord` per phase at lines 232–242.

**Fix:** After each phase method returns a new `CodePlanState`, append a `PhaseRunRecord` to `wf_run` and call `workflow_run.set(wf_run)`.

---

### C2 ★ `resume()` loses `execute_summary` and `review_summary` (Critical)

`code_plan/runner.py:278–282`: only `ctx.plan` is restored from the saved context. `ctx.execute_summary` (used in `_review` system prompt) and `ctx.review_summary` (used in `_summarize`) are left as empty strings. Resuming into REVIEW produces a system prompt with `[EXECUTION SUMMARY]\n` (blank) and resuming into SUMMARIZE produces `"What was implemented: (see conversation)"`.

**Fix:** Restore all three fields from `context.phase_outputs`:
```python
if "execute" in completed:
    ctx.execute_summary = context.phase_outputs["execute"].full_text
if "review" in completed:
    ctx.review_summary = context.phase_outputs["review"].full_text
```

---

### C3 ★ Headless `require_plan_finalization` exits without a plan (High)

`default/runner.py:347–359`: `plan_event`, `execute_event`, `review_event` are only created when `approval_svc is not None`. In headless mode, all three are `None`. The condition at line 465 (`elif spec.require_plan_finalization and plan_event is not None`) evaluates to `False`, so the single-turn `else` branch runs instead. After the turn, line 549 also checks `plan_event is not None` → `False`, so `full_text = "".join(output_buf)` with `approved=None`. The `on_reject="plan"` guard is never triggered because `approved` is `None` not `False`. The plan phase exits successfully with no plan.

**Fix:** In headless mode, create the events anyway, and/or add an explicit check: if `require_plan_finalization` is True and no plan was finalized, return `approved=False` explicitly.

---

### C4 Parallel exception handling continues with missing phase output (High)

`default/runner.py:229–232`: on parallel phase failure, `log.error` is called and the loop continues. The failed phase's output is missing from `context.phase_outputs`. Later phases that reference it via `context.as_system_block()` silently receive an incomplete context.

**Fix:** Treat parallel phase exceptions like sequential ones — set `wf_run.status="failed"` and return.

---

### C5 `resume()` blind to unexpected `completed` sets (Medium)

`code_plan/runner.py:283–288`: `resume_map.get(frozenset(completed), CodePlanState.PLAN)` silently restarts from PLAN for any set not in the four hardcoded cases — including already-complete runs (`{plan, execute, review, summarize}`).

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

`default/runner.py:303–320`: the method walks `_determine_transition()` to find the first incomplete phase. When the last saved phase had `approved=False` and `on_reject` points back to an already-completed phase, the walk can produce unexpected results or be cut short by the `seen` guard.

**Fix:** Walk by `spec.next` exclusively (ignoring `on_reject`) when scanning for the resume point — `on_reject` is a runtime routing decision, not a topological position.

---

### C7 `make_questions_tool` never injected into `WorkflowRunner` phases (Medium)

`code_plan/runner.py:333` injects `make_questions_tool`. `WorkflowRunner._run_phase()` never calls it. Users writing custom generic workflows cannot use `ask_user`.

**Fix:** Add `+ make_questions_tool(self._cfg.approval_svc)` to `filtered` in `_run_phase()`.

---

### C8 Mode restore in `_run_phase.finally` is accidentally correct (Low)

`default/runner.py:509–511`: the `finally` block restores `_original_mode` even when `set_by_name()` failed (returned `None`). In the failure case the mode was not changed, so restoring is a no-op — correct by accident. A future refactor could break this.

**Fix:** Capture whether the mode was actually changed: `_mode_changed = (spec.mode_override and self._mode_manager.set_by_name(...) is not None)` and only restore when `_mode_changed`.

---

### C9 PRD-111 `exec_cfg` replacement condition has a silent no-op path (Low)

`default/runner.py:380–384`:
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

`default/runner.py:396–399`:
```python
role_prompt = (
    spec.system_prompt_override
    or self._cfg.agents_registry.get_role_system_prompt(spec.agent_type)
)
```
When `system_prompt_override = ""` (the default), the registry prompt is used. This is correct. But if a user explicitly sets `system_prompt_override = " "` (a space) to suppress the role prompt without triggering the empty-string fallback, they get a single space as the system prompt suffix — potentially confusing.

---

### D5 `CodePlanRunner._set_phase()` uses `update_workflow_phase()` but `run()` still manually builds the initial `WorkflowRun` (Low)

`code_plan/runner.py:166–168`: the first `WorkflowRun` is built manually in `run()` before the state machine starts. The phase methods then call `_set_phase()` which replaces it. The initial manual construction is redundant with the first `_set_phase("plan", 0, ctx)` call. Two slightly different code paths for what should be one.

**Fix:** Remove the manual `WorkflowRun` construction from `run()` and initialise it via `_set_phase("plan", 0, ctx)` instead, or create a `_init_workflow_run()` helper.

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
