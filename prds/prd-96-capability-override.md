# PRD-96 — CapabilityOverride: Replace Phase Mode Switching

## Problem

`PhaseSpec.mode_override` works by calling `mode_manager.set_by_name()` which
mutates `app_state.active_mode` — a Signal subscribed to by the entire TUI.
For a 40-turn execute phase every redraw reads the overridden mode.  The
`finally`-block restore is correct but brittle: if two concurrent workflows
ran (future multi-agent), they would race on `active_mode`.

`WorkflowRunner` must hold a `mode_manager` reference specifically to perform
this mode switch — coupling workflow execution to the TUI mode system.

`ToolCapabilityGate` already reads `active_mode().blocked_capabilities` on
every call — it is the right place to intercept capability restrictions.

## Goals

- `ToolCapabilityGate` accepts an optional `capability_override` frozenset that,
  when set, takes precedence over `active_mode().blocked_capabilities`.
- `WorkflowRunner._run_phase` passes `capability_override` to a custom gate
  instead of mutating `active_mode`.
- `PhaseSpec.mode_override` and the `mode_manager` dependency on
  `WorkflowRunner` are removed.

## Design

### `ToolCapabilityGate` change

```python
class ToolCapabilityGate:
    def __init__(
        self,
        app_state: AppState,
        capability_override: frozenset | None = None,
    ) -> None:
        self._app_state          = app_state
        self._capability_override = capability_override

    async def before_tool_call(self, ctx: Any) -> Any:
        blocked = (
            self._capability_override
            if self._capability_override is not None
            else self._app_state.active_mode().blocked_capabilities
        )
        if not blocked:
            return BeforeToolHookDecision.proceed()
        tool_caps = ctx.get_metadata(CAPABILITIES_KEY) or frozenset()
        denied = tool_caps & blocked
        if denied:
            return BeforeToolHookDecision.abort({...})
        return BeforeToolHookDecision.proceed()
```

### `WorkflowRunner._run_phase` change

```python
# Derive the capability set for this phase
from agenthicc.tools.capabilities import ToolCapability, WRITE_CAPS
_phase_caps: frozenset | None
if spec.mode_override == "Auto":
    _phase_caps = frozenset()      # no restrictions
else:
    _phase_caps = None             # inherit from active_mode

# Build hooks with the override
gate = ToolCapabilityGate(self._app_state, capability_override=_phase_caps)
_active_runner = AgentRunnerBase(..., global_hooks=[gate, approval_gate])
```

### `PhaseSpec` cleanup

Remove `mode_override: str | None`.  The execute phase in `code_plan` uses:

```python
PhaseSpec(name="execute", capability_override=frozenset(), ...)
```

Or more explicitly, a new `blocked_capabilities: frozenset | None = None` on
`PhaseSpec` (None = inherit from mode):

```python
PhaseSpec(name="execute", blocked_capabilities=frozenset(), ...)   # full access
PhaseSpec(name="plan",    blocked_capabilities=None, ...)          # inherit Plan mode
```

## File changes

| File | Change |
|---|---|
| `tools/capability_gate.py` | Add `capability_override` param to `ToolCapabilityGate` |
| `workflow/plugin.py` | Replace `mode_override: str` with `blocked_capabilities: frozenset \| None` on `PhaseSpec`; remove `mode_override` |
| `workflow/runner.py` | Remove `mode_manager` param; remove `set_by_name` / `finally` restore; pass `blocked_capabilities` to gate |
| `workflow/builtins.py` | `CodePlan` execute phase: `blocked_capabilities=frozenset()` instead of `mode_override="Auto"` |
| `runners/tui_session.py` | Remove `mode_manager=mode_manager` from `WorkflowRunner` call |

## Acceptance criteria

- [ ] `active_mode` signal is never mutated during a workflow phase.
- [ ] Write tools are blocked in plan/review/summarize phases and permitted in execute.
- [ ] `WorkflowRunner` has no `mode_manager` dependency.
- [ ] Concurrent workflows (two tasks) do not interfere with each other's capability gates.
- [ ] All existing tests pass.
