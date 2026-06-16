# PRD-98 — Workflow Persistence and Resume

## Problem

`WorkflowRun`, `PhaseRunRecord`, and `WorkflowContext` (including all
`PhaseOutput` objects) are held entirely in-memory on `WorkflowRunner`.
If the session crashes at execute turn 30 of 40, or the user closes the
terminal mid-workflow, the entire run is lost.  `--resume` restores the
kernel event log and conversation turns but has no way to reconstruct the
workflow state or continue from the last completed phase.

## Goals

- The JSONL kernel event log contains enough data to reconstruct a
  `WorkflowContext` after a crash.
- `--resume` detects an incomplete workflow run and calls
  `WorkflowRunner.resume(context, start_phase)` so execution continues
  from the first incomplete phase.
- Completed phases are not re-run; shared memory is rehydrated from stored
  phase outputs so the single-agent context is preserved.
- The existing `restore_from_log` path is extended — no new persistence
  mechanism is introduced.

## Design

### Kernel events emitted by `WorkflowRunner`

Three events carry the full phase payload (superseding the lightweight
observability events already emitted):

```
WorkflowRunStarted
  { run_id, workflow_name, intent, phase_names[] }

WorkflowPhaseCompleted
  { run_id, phase_name, role, full_text, structured, approved, duration_s }

WorkflowRunCompleted
  { run_id, status }          # "complete" | "failed"
```

The reducer stores `WorkflowPhaseCompleted` payloads under the run_id so
`restore_from_log` produces a queryable `completed_phases` map.

### Resume path in `_run_tui_session`

```python
# After restore_from_log, check for incomplete workflow runs
_incomplete = _find_incomplete_workflow(k_state, session_id)
if _incomplete:
    run_id, workflow_name, intent, completed_phases = _incomplete
    defn = _workflow_registry.get(workflow_name)
    if defn:
        context = _rehydrate_context(intent, run_id, workflow_name,
                                     completed_phases, defn)
        start_phase = _first_incomplete_phase(defn, completed_phases)
        _wf_runner = WorkflowRunner(_wf_config, defn, mode_manager)
        await _wf_runner.resume(context, start_phase)
```

### `WorkflowRunner.resume(context, start_phase_name)`

```python
async def resume(
    self,
    context: WorkflowContext,
    start_phase: str,
) -> None:
    """Continue an interrupted workflow run.

    Rehydrates shared memory from completed phase outputs so the agent
    has full context, then drives the phase loop from start_phase.
    """
    self._shared_memory = ShortTermMemory(max_tokens=32_000)
    # Inject completed phase outputs as assistant messages so the agent
    # "remembers" what it already did.
    for name, output in context.phase_outputs.items():
        self._shared_memory.add_assistant(
            {"role": "assistant", "content": output.full_text}
        )
    phase_name = start_phase
    # ... existing phase loop ...
```

### `_rehydrate_context` helper

Reads `WorkflowPhaseCompleted` events from kernel state and reconstructs
`WorkflowContext.phase_outputs`:

```python
def _rehydrate_context(intent, run_id, workflow_name,
                       completed_phases, defn) -> WorkflowContext:
    ctx = WorkflowContext(intent=intent, run_id=run_id,
                         workflow_name=workflow_name)
    for phase_name, payload in completed_phases.items():
        ctx.add_output(PhaseOutput(
            phase_name=phase_name,
            role=payload["role"],
            full_text=payload["full_text"],
            structured=payload.get("structured"),
            approved=payload.get("approved"),
        ))
    return ctx
```

## File changes

| File | Change |
|---|---|
| `kernel/reducer.py` | Add `WorkflowRunStarted`, `WorkflowPhaseCompleted`, `WorkflowRunCompleted` handlers; store completed phases on kernel `AppState` |
| `kernel/state.py` | Add `workflow_runs: dict[str, WorkflowRunState]` to kernel `AppState` |
| `workflow/runner.py` | Emit the three events; add `resume(context, start_phase)` |
| `runners/tui_session.py` | After `restore_from_log`, call `_find_incomplete_workflow` and invoke `runner.resume()` if found |

## Acceptance criteria

- [ ] After a complete 4-phase run, the JSONL log contains all three event types.
- [ ] A simulated crash after phase 2: `--resume` restores phases 1–2 from the log and continues from phase 3.
- [ ] Phases 1–2 are not re-executed on resume; their outputs are visible in the resumed agent's memory.
- [ ] A cleanly completed workflow does not trigger resume on the next `--resume` invocation.
