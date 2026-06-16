# PRD-91 — Plan Mode Write Enforcement and Phase Mode Switching

## Background

Two related bugs observed after PRD-90 (single-agent workflow):

**Bug A — Plan phase can call write tools**

PRD-87 removed Plan mode from `_BLOCKED` in `mode_manager.py` with this
comment: *"Plan / Review restrictions are now enforced by the workflow's
agent tool filtering instead."*  The workflow's `code_plan` phases all use
`agent_type="auto"` with `allowed_capabilities=None`, so nothing actually
filters write tools.  Result: the plan phase agent can call `write_file`,
`patch_file`, `run_bash` freely, bypassing the approval gate entirely.

**Bug B — `plan_event` not set → `approved=None` → workflow proceeds to execute**

When the agent ignores `finalize_plan()` (after two rejections in the
transcript), `plan_event.is_set()` is `False`.  The output resolver falls
back to `"".join(output_buf)`, `approved` stays `None`, and
`_determine_transition` takes `spec.next = "execute"` because
`None is False` evaluates to `False`.

**Bug C — no `on_reject` on the plan phase**

Even if `approved=False` were returned correctly, `code_plan`'s plan phase
has no `on_reject`, so there is nowhere to loop back to.

---

## Goals

- Plan mode enforces write/execute/network blocking at the `ToolCapabilityGate`
  layer — the single source of truth for capability enforcement.
- The execute phase of `code_plan` runs under Auto mode so the agent can
  write files and run commands.
- `_run_phase` returns `approved=False` whenever approval tools were injected
  but `finalize_plan()` was never called, triggering `on_reject`.
- `code_plan` plan phase has `on_reject="plan"` with `max_iterations=5`.

## Non-Goals

- Changing Guard mode, Ask mode, or Safe mode behaviour.
- Changing `plan_only`, `review_only`, `supervised`, or `architect` workflows.

---

## Change 1 — Restore Plan mode write blocking

**File**: `tui/runtime/mode_manager.py`

```python
_BLOCKED: dict[str, frozenset] = {
    "Plan": _RESTRICTED,   # ← restored (was removed in PRD-87)
    "Ask":  _RESTRICTED,
    "Safe": _RESTRICTED,
}
```

`ToolCapabilityGate` reads `active_mode().blocked_capabilities` on every
tool call.  With `_RESTRICTED` restored, any write/execute/network tool
called during the plan phase is aborted by the gate before it executes.

---

## Change 2 — `PhaseSpec.mode_override`

**File**: `workflow/plugin.py`

```python
@dataclass(frozen=True)
class PhaseSpec:
    ...
    mode_override: str | None = None   # NEW
```

When set, `_run_phase` switches `app_state.active_mode` to the named mode
for the duration of the agent turn, then restores the original mode.
`ToolCapabilityGate` reads `active_mode()` on each tool call, so the
override takes effect immediately and is restored automatically.

---

## Change 3 — `_run_phase`: mode switching + `approved=False` guard

**File**: `workflow/runner.py`

### Mode switch around `_run_agent_turn`

```python
_original_mode = self._app_state.active_mode()
if spec.mode_override:
    from agenthicc.tui.runtime.mode_manager import build_default_registry  # noqa
    _reg = self._app_state.active_mode.__class__   # unused; use mode_manager ref
    # Switch via the registry stored on the runner's app_state signal
    _override_mode = next(
        (m for m in self._mode_manager.all() if m.name == spec.mode_override),
        None,
    )
    if _override_mode:
        self._app_state.active_mode.set(_override_mode)
try:
    await _run_agent_turn(...)
finally:
    if spec.mode_override:
        self._app_state.active_mode.set(_original_mode)
```

`WorkflowRunner` receives a `mode_manager` reference so it can look up the
named mode object.  `app_state.active_mode.set(...)` is the same signal
`ModeManager.set_by_name` writes — no new infrastructure needed.

### `approved=False` when `plan_event` not set

```python
if plan_event is not None:
    if plan_event.is_set() and "plan" in plan_data:
        full_text = plan_data["plan"]
        # fall through to normal PhaseOutput
    else:
        # Approval tools were injected but finalize_plan() was never called.
        # Treat this phase as rejected so on_reject fires.
        return PhaseOutput(
            phase_name=spec.name,
            role=spec.agent_type,
            full_text="".join(output_buf),
            approved=False,
            agent_id=uuid.uuid4().hex[:8],
            duration_s=time.monotonic() - t0,
        )
```

---

## Change 4 — `code_plan` phase updates

**File**: `workflow/builtins.py`

```python
PhaseSpec(name="plan",     ..., on_reject="plan", max_iterations=5,
          mode_override=None),     # stays in Plan mode → writes blocked
PhaseSpec(name="execute",  ..., mode_override="Auto"),  # switches to Auto → writes OK
PhaseSpec(name="review",   ..., mode_override=None),    # stays in Plan mode
PhaseSpec(name="summarize",..., mode_override=None),    # stays in Plan mode
```

---

## `WorkflowRunner` wiring

`WorkflowRunner.__init__` receives a `mode_manager` reference from
`tui_session.py`:

```python
_wf_runner = WorkflowRunner(
    ...,
    mode_manager=mode_manager,   # NEW
)
```

`WorkflowRunner` stores `self._mode_manager = mode_manager` and uses it to
look up the mode object for `spec.mode_override`.

---

## File changes

| File | Change |
|---|---|
| `tui/runtime/mode_manager.py` | Restore `"Plan": _RESTRICTED` in `_BLOCKED` |
| `workflow/plugin.py` | Add `mode_override: str \| None = None` to `PhaseSpec` |
| `workflow/runner.py` | Accept `mode_manager`; apply mode switch in `_run_phase`; return `approved=False` when `plan_event` not set |
| `workflow/builtins.py` | `code_plan`: plan phase `on_reject="plan"` + `max_iterations=5`; execute phase `mode_override="Auto"` |
| `runners/tui_session.py` | Pass `mode_manager=mode_manager` to `WorkflowRunner` |

---

## Acceptance criteria

- [ ] In Plan mode, calling `write_file` during the plan phase returns a
      structured capability-blocked error; the file is not modified.
- [ ] In Plan mode, calling `run_bash` during the plan phase is blocked.
- [ ] The execute phase can call `write_file` and `run_bash` (Auto mode
      has no blocked capabilities).
- [ ] After the execute phase the mode returns to Plan mode.
- [ ] When the plan phase agent does not call `finalize_plan()`, the phase
      returns `approved=False` and `on_reject="plan"` fires.
- [ ] After 5 iterations without a finalized plan the workflow fails with
      a clear error.
- [ ] All existing unit and integration tests pass.
