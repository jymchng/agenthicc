# PRD-112 — Workflow Package Structure Reorganisation

## Summary

Workflow-specific code is moved into its corresponding subpackage.
`code_plan`-specific classes (`CodePlan`, `CodePlanParams`) move into
`workflows/code_plan/`.  Generic builtin workflows and `WorkflowRunner`
move into a new `workflows/default/` subpackage.  Old paths are retained as
thin re-export shims for backward compatibility.

---

## Before

```
workflows/
  builtins.py      ← mixed: CodePlan + CodePlanParams + 4 generic workflows
  runner.py        ← WorkflowRunner (top-level, no subpackage)
  code_plan/
    __init__.py
    runner.py      ← CodePlanRunner
    state.py       ← CodePlanState, CodePlanContext
```

---

## After

```
workflows/
  builtins.py      ← re-export shim only (backward compat)
  runner.py        ← re-export shim only (backward compat)
  code_plan/
    __init__.py    ← exports everything in the package
    definition.py  ← CodePlan (WorkflowPlugin), CodePlanParams  ✦ NEW
    runner.py      ← CodePlanRunner (unchanged)
    state.py       ← CodePlanState, CodePlanContext (unchanged)
  default/         ✦ NEW
    __init__.py
    definition.py  ← PlanOnly, ReviewOnly, Supervised, Architect
    runner.py      ← WorkflowRunner, build_workflow_runner
```

---

## Rationale

| Problem | Fix |
|---|---|
| `CodePlan` (the plugin class) and `CodePlanParams` live in `builtins.py` far from `CodePlanRunner` and `CodePlanState` | Moved into `code_plan/definition.py` — all code_plan code in one place |
| `WorkflowRunner` is a top-level file with no subpackage home | Moved into `default/runner.py` — mirrors code_plan's structure |
| Generic workflows scattered in `builtins.py` | Moved into `default/definition.py` — clearly grouped |
| `loader.py` imports from mixed-concern `builtins.py` | Now imports from canonical subpackage paths |

---

## Backward Compatibility

`workflows/runner.py` and `workflows/builtins.py` are retained as one-line
re-export shims.  Any code importing from the old paths continues to work.
These shims can be removed in a future cleanup once all callers are updated.

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `CodePlan` and `CodePlanParams` are importable from `agenthicc.workflows.code_plan`. |
| 2 | `CodePlan` and `CodePlanParams` are defined in `workflows/code_plan/definition.py`. |
| 3 | `WorkflowRunner` is importable from `agenthicc.workflows.default`. |
| 4 | `WorkflowRunner` is defined in `workflows/default/runner.py`. |
| 5 | `PlanOnly`, `ReviewOnly`, `Supervised`, `Architect` are defined in `workflows/default/definition.py`. |
| 6 | `loader.load_builtin_workflows()` imports from the canonical subpackage paths. |
| 7 | Old import paths (`workflows.runner`, `workflows.builtins`) still work via shims. |
| 8 | `workflows/__init__.py` exports all public symbols from both subpackages. |
| 9 | All existing tests continue to pass. |

---

## Files Changed

| File | Change |
|---|---|
| `workflows/code_plan/definition.py` | New — `CodePlan`, `CodePlanParams` |
| `workflows/code_plan/__init__.py` | Updated — exports `CodePlan`, `CodePlanParams` |
| `workflows/default/__init__.py` | New — subpackage root |
| `workflows/default/definition.py` | New — `PlanOnly`, `ReviewOnly`, `Supervised`, `Architect` |
| `workflows/default/runner.py` | New — `WorkflowRunner`, `build_workflow_runner` |
| `workflows/runner.py` | Changed to re-export shim |
| `workflows/builtins.py` | Changed to re-export shim |
| `workflows/loader.py` | Updated imports to canonical paths |
| `workflows/__init__.py` | Updated exports from canonical paths |
| `workflows/plugin.py` | Updated lazy `WorkflowRunner` import to `default.runner` |
