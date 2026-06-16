# PRD-94 — Kernel as First-Class State: Integrate WorkflowRun into Event Sourcing

## Problem

The kernel (`kernel/`) contains a production-grade event-sourcing system:
immutable `AppState`, MPSC `EventProcessor`, pure `root_reducer`, and JSONL
persistence.  Only two events are emitted from the live session:
`IntentCreated` and `IntentStatusChanged`.  `WorkflowRun`, `PhaseRunRecord`,
and `PhaseOutput` live entirely in-memory on `WorkflowRunner`.

Consequences:
- A session crash at execute turn 30 loses the entire workflow — no resume.
- The kernel's `Workflow` / `WorkflowNode` / `Task` types (PRD-02) are never
  populated by the live runner.
- The TUI's `ConversationStore` and the kernel's `AppState` are two parallel
  state machines with no integration.

## Goals

- `WorkflowRunner` emits meaningful kernel events at every phase boundary so
  the JSONL log contains enough data to resume a crashed workflow.
- On `--resume`, `restore_from_log` + new workflow handlers reconstruct
  `WorkflowRun` and `WorkflowContext` from the log.
- The kernel's `Workflow` / `WorkflowNode` types are populated so existing
  kernel tests and the `DAGExecutor` are exercisable against live data.

## Design

### New kernel event types

| Event | Payload | Reducer action |
|---|---|---|
| `WorkflowRunStarted` | `run_id, workflow_name, intent, phase_names` | Creates `Workflow` in kernel state |
| `WorkflowPhaseCompleted` | `run_id, phase_name, role, full_text, approved, structured` | Creates `WorkflowNode` with result |
| `WorkflowRunCompleted` | `run_id, status` | Marks `Workflow` complete/failed |

### Emission points in `WorkflowRunner`

```python
# In run():
await self._processor.emit(Event.create("WorkflowRunStarted", {
    "run_id": run_id, "workflow_name": self._def.name,
    "intent": intent, "phase_names": self._def.phase_names(),
}))

# In _run_phase() after PhaseOutput is produced:
await self._processor.emit(Event.create("WorkflowPhaseCompleted", {
    "run_id": self._run_id, "phase_name": spec.name,
    "role": spec.agent_type, "full_text": output.full_text,
    "approved": output.approved,
    "structured": output.structured or {},
}))
```

### Resume path

```python
# In _run_tui_session, after restore_from_log:
wf_state = k_state.workflows.get(resume_run_id)
if wf_state:
    context = _reconstruct_workflow_context(wf_state)
    runner  = WorkflowRunner(definition=..., ...)
    await runner.resume(context)
```

`WorkflowRunner.resume(context)` skips already-completed phases
(detected from `wf_state.nodes`) and continues from the first incomplete one.

## File changes

| File | Change |
|---|---|
| `kernel/reducer.py` | Add handlers for `WorkflowRunStarted`, `WorkflowPhaseCompleted`, `WorkflowRunCompleted` |
| `workflow/runner.py` | Emit the three new events at appropriate boundaries; store `run_id` on self; add `resume(context)` |
| `runners/tui_session.py` | On `--resume`, detect incomplete workflow runs and call `runner.resume()` |

## Acceptance criteria

- [ ] After a workflow run, the JSONL log contains `WorkflowRunStarted`, one `WorkflowPhaseCompleted` per phase, and `WorkflowRunCompleted`.
- [ ] `restore_from_log` produces a kernel `AppState` with a populated `Workflow` entry.
- [ ] `--resume` on a session with an incomplete workflow continues from the last completed phase.
- [ ] All existing kernel tests pass.
