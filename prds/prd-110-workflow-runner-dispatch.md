# PRD-110 — Workflow Runner Dispatch via Factory Method

## Summary

Replace the hardcoded `if wf_defn.name == "code_plan":` dispatch in
`tui_session.py` with a `runner_factory` classmethod on `WorkflowPlugin`
and a `build_runner()` method on `WorkflowDefinition`.

Each workflow plugin owns the decision of which runner class to use.
`tui_session.py` becomes a single-line callsite with no knowledge of runner
class names.

---

## Problem

Runner selection is duplicated in two places in `runners/tui_session.py`:

**`run_turn()` lines 568–571:**
```python
if _wf_defn.name == "code_plan":
    _wf_runner = CodePlanRunner(_wf_config, ctx.mode_manager)
else:
    _wf_runner = WorkflowRunner(_wf_defn, _wf_config, ctx.mode_manager)
```

**`_resume_workflow_task()` lines 742–745:**
```python
if wf_defn.name == "code_plan":
    runner = CodePlanRunner(self._wf_config_base, ctx.mode_manager)
else:
    runner = WorkflowRunner(wf_defn, self._wf_config_base, ctx.mode_manager)
```

Consequences:
- Adding a new specialized runner requires editing `tui_session.py` in two places.
- `tui_session.py` imports `CodePlanRunner` even though it only passes through a config object.
- The runner choice is physically separated from the workflow definition that motivated it.

---

## Design

### `runner_factory` classmethod on `WorkflowPlugin`

Every `WorkflowPlugin` subclass declares how to build its runner via a
classmethod with a standardized signature:

```python
class WorkflowPlugin:
    @classmethod
    def runner_factory(
        cls,
        defn: WorkflowDefinition,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> BaseWorkflowRunner:
        from agenthicc.workflows.runner import WorkflowRunner
        return WorkflowRunner(defn, config, mode_manager)
```

`CodePlan` overrides it to return `CodePlanRunner`:

```python
class CodePlan(WorkflowPlugin):
    @classmethod
    def runner_factory(cls, defn, config, mode_manager):
        from agenthicc.workflows.code_plan import CodePlanRunner
        return CodePlanRunner(config, mode_manager)
```

### `runner_factory` field on `WorkflowDefinition`

`WorkflowPlugin.to_definition()` binds the class's `runner_factory` into the
`WorkflowDefinition` it produces:

```python
WorkflowDefinition(
    ...
    runner_factory=type(self).runner_factory,  # bound classmethod
)
```

### `build_runner()` on `WorkflowDefinition`

```python
def build_runner(
    self,
    config: WorkflowConfig,
    mode_manager: ModeManager | None = None,
) -> BaseWorkflowRunner:
    if self.runner_factory is not None:
        return self.runner_factory(self, config, mode_manager)
    from agenthicc.workflows.runner import WorkflowRunner
    return WorkflowRunner(self, config, mode_manager)
```

### `tui_session.py` callsites — both become one line

```python
# run_turn():
_wf_runner = _wf_defn.build_runner(_wf_config, ctx.mode_manager)

# _resume_workflow_task():
runner = wf_defn.build_runner(self._wf_config_base, ctx.mode_manager)
```

`tui_session.py` no longer imports `CodePlanRunner` or `WorkflowRunner` for
dispatch purposes.

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `WorkflowDefinition` has a `runner_factory` field (callable or None). |
| 2 | `WorkflowDefinition.build_runner(config, mode_manager)` returns the correct runner. |
| 3 | `WorkflowPlugin.to_definition()` stores the class's `runner_factory` on the definition. |
| 4 | The default `runner_factory` returns `WorkflowRunner`. |
| 5 | `CodePlan.runner_factory` returns `CodePlanRunner`. |
| 6 | `tui_session.py` contains no `if … name == "code_plan":` branches for runner selection. |
| 7 | `tui_session.py` does not import `CodePlanRunner` for dispatch. |
| 8 | A third-party `WorkflowPlugin` can supply its own runner by overriding `runner_factory`. |
| 9 | TOML-loaded and Python-loaded plugins both carry the factory through `to_definition()`. |

---

## Files Changed

| File | Change |
|---|---|
| `workflows/plugin.py` | `WorkflowDefinition.runner_factory` field + `build_runner()`; `WorkflowPlugin.runner_factory()` classmethod default |
| `workflows/builtins.py` | `CodePlan.runner_factory()` override |
| `runners/tui_session.py` | Two if/else blocks → `defn.build_runner()`; drop `CodePlanRunner` dispatch imports |
