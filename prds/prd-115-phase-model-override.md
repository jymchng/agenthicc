# PRD-115 — Per-Phase Model Override and Phase-Aware TUI Updates

## Summary

Three tightly related improvements to the workflow execution layer:

1. **Fix `_resolve_model()`** — `exec_cfg.model` is ignored today; the per-phase
   model override from PRD-111 (`WorkflowParams`) and PRD-114 (`CodePlanRunner`
   class attributes) is silently inoperative. Fix this by making
   `AgentTurnRunner._resolve_model()` respect `exec_cfg.model` when it is set.

2. **`AppState.update_workflow_phase()`** — a single method that any phase method
   calls to update all TUI workflow state atomically, replacing scattered
   `dataclasses.replace(wf_run, ...)` boilerplate.

3. **Per-phase model class attributes + `_phase_model()` helper** on
   `CodePlanRunner` — declarative per-phase models that read from TOML/CLI at
   runtime (via `WorkflowParams`) with class attributes as static fallback.

---

## Problem

### Bug: per-phase model override is silently broken

`AgentTurnRunner._resolve_model()` (agent_turn.py:118–122) reads the transport's
baked-in config model.  It ignores `exec_cfg` entirely.  Therefore:

- `WorkflowRunner`'s PRD-111 per-phase model override constructs the right
  `exec_cfg` replacement but the modified model never reaches `@agent_decorator`.
- Any future `CodePlanRunner` phase-level override would have the same problem.

### Boilerplate: every phase method manually rebuilds `WorkflowRun`

Each `_plan()`, `_execute()`, `_review()`, `_summarize()` call
`dataclasses.replace(wf_run, current_phase=..., current_phase_index=...) +
self._cfg.app_state.workflow_run.set(wf_run)` — repeated logic with no single
authoritative update path.

### Gap: `CodePlanRunner` has no per-phase model path

`WorkflowRunner` reads per-phase models from `WorkflowParams`; `CodePlanRunner`
has its own state machine and never calls that path.

---

## Design

### Layer 1 — Fix `AgentTurnRunner._resolve_model()`

Priority: `exec_cfg.model` (non-empty) > transport config > `"unknown"`.

```python
def _resolve_model(self) -> None:
    ctx = self._ctx
    # exec_cfg.model carries per-phase overrides from WorkflowParams / runner
    # class attributes.  Use it when non-empty; fall back to transport config.
    override = getattr(ctx.exec_cfg, "model", "") if ctx.exec_cfg else ""
    if override:
        self._model_id = override
    else:
        transport = getattr(ctx.runner, "_transport", None)
        cfg       = getattr(transport, "_config", None)
        self._model_id = getattr(cfg, "model", "unknown") if cfg else "unknown"
    self._model_short = self._model_id.split("/")[-1]
```

### Layer 2 — `AppState.update_workflow_phase()`

Single atomic update replacing per-phase `dataclasses.replace` boilerplate.

```python
def update_workflow_phase(
    self, *,
    workflow_name:  str,
    phase_name:     str,
    phase_index:    int,
    total_phases:   int,
    run_id:         str,
    intent:         str,
    model_id:       str = "",
) -> None
```

Internally uses `dataclasses.replace` on the existing `workflow_run` value, or
creates a fresh `WorkflowRun` if none is set.

### Layer 3 — `CodePlanRunner` per-phase model infrastructure

**Class attributes** (static defaults; empty string = use global model):

```python
class CodePlanRunner(BaseWorkflowRunner):
    plan_model:    str = ""
    execute_model: str = ""
    review_model:  str = ""
    summary_model: str = ""
```

**`_phase_model(name)` helper** — reads TOML/CLI first, class attribute second:

```python
def _phase_model(self, phase_name: str) -> str:
    if self._cfg.params is not None:
        m = self._cfg.params.model_for_phase(phase_name, "")
        if m:
            return m
    return getattr(self, f"{phase_name}_model", "") or ""
```

**`_run_turn(model_override=...)` parameter** — when non-empty, replaces
`exec_cfg.model` before calling `_run_agent_turn`:

```python
async def _run_turn(self, ..., model_override: str = "") -> None:
    exec_cfg = (
        dataclasses.replace(self._cfg.cfg.execution, model=model_override)
        if model_override and dataclasses.is_dataclass(self._cfg.cfg.execution)
        else self._cfg.cfg.execution
    )
    await _run_agent_turn(..., exec_cfg=exec_cfg, ...)
```

**`_set_phase(name, index, ctx)` helper** — calls `update_workflow_phase`:

```python
def _set_phase(self, phase_name: str, phase_index: int, ctx: CodePlanContext) -> None:
    self._cfg.app_state.update_workflow_phase(
        workflow_name=self.workflow_name,
        phase_name=phase_name,
        phase_index=phase_index,
        total_phases=self.total_phases,
        run_id=ctx.run_id,
        intent=ctx.intent,
        model_id=self._phase_model(phase_name) or self._model_id,
    )
```

Each phase method replaces its boilerplate with one call:

```python
async def _plan(self, ctx):
    self._set_phase("plan", 0, ctx)
    await self._run_turn(..., model_override=self._phase_model("plan"), ...)
```

---

## End-User Config

```toml
# agenthicc.toml
[workflows.code_plan]
plan_model    = "deepseek-v4-pro"    # flagship for planning
execute_model = "deepseek-v4-flash"  # cheap for execution
review_model  = ""                   # empty → global execution.model
```

```bash
agenthicc --set workflows.code_plan.plan_model=deepseek-v4-pro
```

Subclass override (static):

```python
class CodePlanDocsRunner(CodePlanRunner):
    plan_model = "deepseek-v4-pro"   # only the plan phase
```

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `_resolve_model()` uses `exec_cfg.model` when non-empty, transport config otherwise. |
| 2 | `WorkflowRunner`'s existing PRD-111 per-phase model override now reaches `@agent_decorator`. |
| 3 | `AppState.update_workflow_phase()` sets `workflow_run` signal atomically from named args. |
| 4 | `CodePlanRunner.plan_model`, `execute_model`, `review_model`, `summary_model` class attrs default to `""`. |
| 5 | `_phase_model(name)` reads `WorkflowParams.model_for_phase()` first, class attr second. |
| 6 | `_run_turn(model_override=...)` passes a replaced `exec_cfg` with the per-phase model. |
| 7 | `_set_phase(name, index, ctx)` calls `update_workflow_phase` and eliminates per-phase boilerplate. |
| 8 | Each phase method (`_plan`, `_execute`, `_review`, `_summarize`) uses `_set_phase` + `_phase_model`. |
| 9 | `[workflows.code_plan] plan_model = "..."` in TOML overrides the model for the plan phase only. |
| 10 | A subclass setting `plan_model = "..."` as a class attribute applies when no TOML override exists. |

---

## Files Changed

| File | Change |
|---|---|
| `runners/agent_turn.py` | Fix `_resolve_model()` |
| `tui/conversation_store.py` | Add `AppState.update_workflow_phase()` |
| `workflows/code_plan/runner.py` | Per-phase model attrs; `_phase_model()`; `_run_turn(model_override)`; `_set_phase()`; update phase methods |
