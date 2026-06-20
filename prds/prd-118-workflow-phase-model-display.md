# PRD-118 — Per-Phase Model Display in Status Bar

## Summary

When a workflow phase runs with a per-phase model override (e.g.
`execute_model = "deepseek-v4-flash"` while the global model is
`claude-opus`), the status bar line 2 shows the **actual model in use for
that phase** instead of the global session model.  The display reverts
automatically when the workflow run ends — no cleanup required.

---

## Motivation

`AppState.update_workflow_phase()` already accepts a `model_id` parameter
(PRD-115) but never stores or displays it.  A user who configures
`[workflows.code_plan] execute_model = "deepseek-v4-flash"` has no visual
confirmation that the cheaper model is being used during execution.

---

## Design

### Step 1 — `WorkflowRun.current_phase_model: str`

A new field on the `WorkflowRun` dataclass (default `""`).  Non-empty when
the current phase uses a model that differs from the session default.

```python
@dataclass
class WorkflowRun:
    ...
    current_phase_model: str = ""
```

### Step 2 — `update_workflow_phase` stores it

```python
updated = dataclasses.replace(
    current,
    ...
    current_phase_model = model_id,
)
```

Same for the `WorkflowRun(...)` constructor branch.

### Step 3 — Status bar line 2 shows the phase model

`StatusComponent.render()` already reads `workflow_run` for the phase
progress row.  Line 2 (model name) now prefers `current_phase_model` when
a workflow is running and the field is non-empty:

```python
model = conv.model_name()   # session default

_wf = self._state.workflow_run()
if _wf is not None and _wf.status == "running" and _wf.current_phase_model:
    model = _wf.current_phase_model
```

### Revert is automatic

`WorkflowRun` is set to `None` (or `status="complete"`) when the workflow
ends.  The condition `_wf is not None and _wf.status == "running"` becomes
`False`, and `conv.model_name()` (the session model) is used again — no
explicit cleanup.

---

## Before / After

**Before** (execute phase with `execute_model = "deepseek-v4-flash"`):
```
openai/deepseek-v4-flash    ← session model shown, not what's running
```

**After**:
```
deepseek-v4-flash           ← actual phase model shown during execute
```

Reverts to `openai/deepseek-v4-flash` once the workflow completes.

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `WorkflowRun.current_phase_model: str` field exists, defaults to `""`. |
| 2 | `update_workflow_phase(model_id=...)` stores `model_id` in `current_phase_model`. |
| 3 | `update_workflow_phase(model_id="")` stores `""` — global model shown. |
| 4 | Status bar line 2 shows `current_phase_model` when it is non-empty and the run is active. |
| 5 | Status bar line 2 reverts to `conv.model_name()` when run ends or `current_phase_model` is empty. |
| 6 | No `Any` introduced; `WorkflowRun` field is typed `str`. |

---

## Files Changed

| File | Change |
|---|---|
| `workflows/plugin.py` | Add `current_phase_model: str = ""` to `WorkflowRun` |
| `tui/conversation_store.py` | Wire `model_id` into `current_phase_model` in both branches of `update_workflow_phase` |
| `tui/workspace/components.py` | `StatusComponent.render()` prefers `wf_run.current_phase_model` when set |
