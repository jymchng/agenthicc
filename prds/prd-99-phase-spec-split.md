# PRD-99 — PhaseSpec Split: Separate Topology from Configuration

## Problem

`PhaseSpec` has become a kitchen-sink dataclass mixing four distinct concerns:

| Concern | Fields |
|---|---|
| Graph topology | `next`, `on_reject`, `on_error`, `parallel_with`, `max_iterations` |
| Agent config | `agent_type`, `system_prompt_override`, `allowed_capabilities*` |
| Execution config | `max_turns`, `output_schema` |
| Runtime overrides | `mode_override` (PRD-91), `blocked_capabilities` (PRD-96) |

Every new workflow feature adds a field to a dataclass that is already
complicated enough that workflow authors need to read the full type to
understand what to set.

## Goals

- Split `PhaseSpec` into two focused dataclasses: `PhaseTopology` (the graph
  node) and `PhaseConfig` (how the agent runs at this node).
- `PhaseSpec` becomes a named alias for `tuple[PhaseTopology, PhaseConfig]`
  or a thin wrapper providing a single constructor for common cases.
- Workflow authors who only need topology get a clean API.
- Workflow authors who need deep customisation use `PhaseConfig` explicitly.

## Design

### `PhaseTopology` — the graph node

```python
@dataclass(frozen=True)
class PhaseTopology:
    name:           str
    next:           str | None     = None
    on_reject:      str | None     = None
    on_error:       str | None     = None
    max_iterations: int            = 0        # 0 = unlimited
    parallel_with:  tuple[str,...] = ()
```

### `PhaseConfig` — how the agent runs

```python
@dataclass(frozen=True)
class PhaseConfig:
    agent_type:             str                        = "auto"
    system_prompt_override: str                        = ""
    blocked_capabilities:   frozenset | None           = None
    context_summary_fn:     Callable | None            = None
    max_turns:              int                        = 20
    output_schema:          str | None                 = None
```

### `PhaseSpec` — convenience wrapper

```python
@dataclass(frozen=True)
class PhaseSpec:
    """Convenience wrapper combining topology and config.

    Pass all fields directly as before; they are delegated internally.
    Existing code works without modification.
    """
    # topology
    name:           str
    next:           str | None     = None
    on_reject:      str | None     = None
    on_error:       str | None     = None
    max_iterations: int            = 0
    parallel_with:  tuple[str,...] = ()
    # config
    agent_type:             str            = "auto"
    system_prompt_override: str            = ""
    blocked_capabilities:   object         = None
    context_summary_fn:     object         = None
    max_turns:              int            = 20
    output_schema:          str | None     = None

    @property
    def topology(self) -> PhaseTopology:
        return PhaseTopology(
            name=self.name, next=self.next, on_reject=self.on_reject,
            on_error=self.on_error, max_iterations=self.max_iterations,
            parallel_with=self.parallel_with,
        )

    @property
    def config(self) -> PhaseConfig:
        return PhaseConfig(
            agent_type=self.agent_type,
            system_prompt_override=self.system_prompt_override,
            blocked_capabilities=self.blocked_capabilities,
            context_summary_fn=self.context_summary_fn,
            max_turns=self.max_turns,
            output_schema=self.output_schema,
        )
```

### Migration path

- All existing `PhaseSpec(...)` constructors work unchanged — `PhaseSpec`
  keeps all existing fields as a convenience wrapper.
- New workflows can use `PhaseTopology` + `PhaseConfig` directly for clarity.
- `WorkflowRunner._run_phase(spec)` accesses `spec.config.*` and
  `spec.topology.*` — no breaking change at the runner level.

### Enabling complex workflows

With the split, a workflow can define reusable configs:

```python
READ_ONLY_CONFIG  = PhaseConfig(blocked_capabilities=WRITE_CAPS, max_turns=8)
EXEC_CONFIG       = PhaseConfig(blocked_capabilities=frozenset(), max_turns=40)

class MyWorkflow(WorkflowPlugin):
    phases = [
        PhaseSpec("plan",    next="execute", **asdict(READ_ONLY_CONFIG)),
        PhaseSpec("execute", next="review",  **asdict(EXEC_CONFIG)),
        PhaseSpec("review",  next="done",    **asdict(READ_ONLY_CONFIG),
                  output_schema="review_result", on_reject="execute"),
    ]
```

## File changes

| File | Change |
|---|---|
| `workflow/plugin.py` | Add `PhaseTopology` and `PhaseConfig`; add `.topology` and `.config` properties to `PhaseSpec` |

## Acceptance criteria

- [ ] `PhaseTopology` and `PhaseConfig` are importable from `agenthicc.workflow`.
- [ ] All existing `PhaseSpec(...)` call sites work unchanged.
- [ ] `WorkflowRunner` accesses `spec.config.*` and `spec.topology.*`.
- [ ] A workflow using reusable `PhaseConfig` instances passes all integration tests.
