# PRD-89 — Plan Workflow Guards: Approval Enforcement and Mode Reset

## Background

Two bugs were observed after implementing the code_plan workflow (PRD-87/88):

1. **Approval bypass**: When the user rejects a plan, the model can still call
   `finalize_plan()` immediately without re-seeking approval.  The shared
   closure in `phase_tools.py` has no machine-enforceable barrier, so
   `plan_event` gets set regardless, and the workflow transitions to the
   execute phase with an unapproved plan.

2. **Mode not reset**: After `WorkflowRunner.run()` completes, the active mode
   stays on "Plan".  Every subsequent user message triggers another full
   workflow run until the user manually cycles the mode.

---

## Goals

- `finalize_plan()` is a hard gate: it fails with a structured error if
  `request_plan_approval()` has not returned `approved=True` in the same
  planning phase.  The model must seek (and receive) approval before it can
  hand off to execution.
- After a workflow completes successfully, the active mode automatically
  switches back to Auto and a brief notification confirms the transition.
- No changes to the overlay, runner, or hook infrastructure.

## Non-Goals

- Preventing the model from calling tools in an arbitrary order beyond the
  approval gate described above.
- Blocking workflow progression when the planner exhausts `max_turns` without
  ever calling `finalize_plan` (this is a separate concern; the timeout path
  falls through with `plan_event.is_set() == False`).

---

## Bug 1 — Approval enforcement in `phase_tools.py`

### Root cause

`request_plan_approval` and `finalize_plan` share `plan_event` and
`plan_data` but share **no approval state**.  `finalize_plan` unconditionally
sets the event and writes the plan regardless of what
`request_plan_approval` returned.

### Fix

Add `approval_state: dict` to the shared closure.

```
make_planner_tools(approval_svc, plan_event, plan_data)
│
├─ approval_state = {"granted": False}   ← NEW shared dict
│
├─ request_plan_approval(plan)
│    response = await approval_svc.request_approval(req)
│    approval_state["granted"] = response.allowed   ← WRITE
│    return {"approved": response.allowed, "feedback": ...}
│
└─ finalize_plan(plan)
     if not approval_state["granted"]:             ← READ
         return {"ok": False, "error": "..."}      ← HARD GATE
     plan_data["plan"] = plan
     plan_event.set()
     return {"ok": True, ...}
```

The gate is in the tool body itself, so it fires through the normal
`ToolExecutor.execute()` → `_dispatch()` path.  No hook changes are needed.
The model receives a structured `{"ok": False, "error": "..."}` result and
must call `request_plan_approval()` again before `finalize_plan()` will
succeed.

---

## Bug 2 — Mode reset after successful workflow

### Root cause

`_run_turn` in `tui_session.py` dispatches to `WorkflowRunner.run()` but
performs no post-completion action.  The active mode never changes.

### Fix

After `await _wf_runner.run(text)`, inspect `app_state.workflow_run()`:

```python
await _wf_runner.run(text)

_wf_result = app_state.workflow_run()
if (
    _wf_result is not None
    and getattr(_wf_result, "status", None) == "complete"
    and app_state.active_mode().default_workflow is not None
):
    mode_manager.set_by_name("Auto")
    app_state.conversation.notification.set(
        "✓ Workflow complete — switched to Auto mode"
    )
```

The guard `app_state.active_mode().default_workflow is not None` prevents
spurious Auto→Auto transitions if a future caller invokes a workflow outside
a workflow-bound mode.

---

## File changes

| File | Change |
|---|---|
| `workflow/phase_tools.py` | Add `approval_state = {"granted": False}` to `make_planner_tools` closure; `request_plan_approval` writes `approval_state["granted"]`; `finalize_plan` checks it and returns `{"ok": False, "error": "…"}` when not granted |
| `runners/tui_session.py` | After `await _wf_runner.run(text)`, check `workflow_run().status == "complete"` and call `mode_manager.set_by_name("Auto")` with a notification |

---

## Acceptance criteria

- [ ] `finalize_plan()` returns `{"ok": False, "error": "…"}` when called
      without a prior approved `request_plan_approval()` in the same phase.
- [ ] `finalize_plan()` succeeds and sets `plan_event` only when
      `approval_state["granted"]` is `True`.
- [ ] A rejection followed by a revised plan approval followed by
      `finalize_plan()` succeeds (approval state resets correctly on each
      `request_plan_approval` call).
- [ ] After `code_plan` workflow completes with `status="complete"`, the
      active mode is `"Auto"` and a notification is visible for ~2 s.
- [ ] If the workflow ends with `status="failed"`, the mode does NOT switch.
- [ ] All existing unit and integration tests pass.
