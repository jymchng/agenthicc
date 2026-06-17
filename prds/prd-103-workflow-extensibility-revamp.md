# PRD-103 — Workflow Extensibility Revamp: Custom Workflows & Custom Runners

## Problem

The workflow system conflates four distinct concerns into the same objects, making it hard for users to create custom workflows and impossible to run workflows outside the TUI:

| Concern | Where it lives today | Problem |
|---|---|---|
| **Graph topology** | `WorkflowPlugin` / `WorkflowGraph` | Fine — this is the right level |
| **Per-node agent config** | Scattered: `PhaseSpec.agent_type`, `AgentsRegistry`, hardcoded model in `WorkflowRunner.__init__` | No per-node model override; silent fallback on unknown agent type |
| **Execution strategy** | Hardcoded inside `_run_phase` / `_run_node` | Approval injection, completion signals, transition logic, continuation loops — all non-overridable |
| **Session singletons** | `WorkflowConfig` (11+ fields, all TUI-specific) | Cannot run a workflow without a full TUI session |

---

## Detailed Friction Analysis

### Q1 — Creating a custom 3-phase workflow today

A user must:

1. Create `~/.agenthicc/workflows/my_workflow.py`
2. Subclass `WorkflowPlugin`
3. Define `PhaseSpec` × 3 with correct `next`/`on_reject` chains
4. Understand `output_schema` (hardcoded: `"plan"`, `"review_result"`, `"free_text"`)
5. Understand `agent_type` (must exist in `AgentsRegistry` or silently falls back to `"auto"`)
6. Wait until runtime to discover graph topology errors (missing phase names, circular refs)

**Friction points:**

- No validation of phase graph at definition time — `_run_phase_loop()` (runner.py:145) logs an error and stops silently at runtime
- `output_schema` only supports hardcoded strings; `_parse_output_schema()` (plugin.py:216) is private and cannot be extended
- Unknown `agent_type` silently falls back: `self.get(agent_type) or self.get("auto")` (agents/registry.py:46)
- No per-phase model override — model is resolved once in `WorkflowRunner.__init__()` (runner.py:47) and baked into all turns

### Q2 — Per-phase LLM model selection today

**Not possible.**

- `WorkflowRunner.__init__()` (runner.py:31–47) resolves the model once from the transport config or global execution settings
- `self._model_id` is used for all phases; there is no `model_override` field on `PhaseSpec`
- Workaround requires creating separate `AgentPlugin` subclasses per model and registering them in the agents registry — a significant detour

### Q3 — Adding a custom approval overlay today

Requires patching `tui_session.py`:

```python
# _wire_approval_overlay() in tui_session.py — hardcoded dispatch:
if getattr(req, "kind", "tool") == "plan_review":
    overlay = PlanApprovalOverlay(req, approval_svc, workspace.overlays.hide)
else:
    overlay = ApprovalOverlay(req, approval_svc, workspace.overlays.hide)
```

- No registry or plugin hook for custom overlays
- `ApprovalRequest.kind` (tools/approval.py:37) is a free-form string, but only two values are recognized
- `kind="my_custom_review"` silently falls back to the generic `ApprovalOverlay`
- Adding a new overlay type requires forking the TUI code

### Q4 — Running a workflow programmatically (without TUI) today

Requires instantiating 12 objects:

```python
wf_config = WorkflowConfig(
    conv_store=...,       # TUI-specific — needed only for reactive UI + error logging
    app_state=...,        # TUI-specific — needed only for reactive UI mode updates
    processor=...,        # kernel event bus
    agent_runner=...,     # LLM transport
    approval_svc=...,     # TUI-specific — gates phase transitions via UI
    cfg=...,              # execution settings
    skills={},
    plugin_tools=[],
    mcp_registry=None,
    mention_cache=...,
    agents_registry=...,
    completed_turns=0,
)
runner = WorkflowRunner(wf_defn, wf_config)
await runner.run("do the thing")
```

- `conv_store.append_event("error", ...)` (runner.py:165, 283) crashes if passed `None`
- `self._cfg.app_state.active_mode()` (runner.py:340) crashes if `app_state` is None or a mock without `.active_mode()`
- `ApprovalService` hangs in approval phases if the event is never set (no TUI to set it)
- No documented headless path; the separate `runners/headless.py` is a different entry point that doesn't use `WorkflowRunner` at all

### Q5 — WorkflowConfig → WorkflowRunner coupling

| Field | Used in runner | TUI-only? | Can be None? |
|---|---|---|---|
| `conv_store` | error event logging, approval observation | TUI-specific | No — crashes |
| `app_state` | `workflow_run` signal, `active_mode()` | TUI-specific | No — crashes |
| `processor` | kernel event emission | Generic | No — required |
| `agent_runner` | LLM calls | Generic | No — required |
| `approval_svc` | human phase gates | TUI-optional | Yes — auto-approves |
| `cfg` | execution settings | Generic | No — required |
| `skills` | agent turn injection | TUI-optional | Yes |
| `plugin_tools` | tool filtering | Generic | Yes (empty list) |
| `mcp_registry` | MCP tool discovery | TUI-optional | Yes |
| `mention_cache` | mention resolution | TUI-optional | Yes |
| `agents_registry` | system prompt resolution | Generic | No — required |
| `completed_turns` | session progress tracking | TUI-optional | Yes (defaults 0) |

### Q6 — What is hardcoded in `_run_phase` / `_run_node`

1. **Human phase special-case** (runner.py:315): `if spec.agent_type == "human"` — custom phase types require a new `elif` or subclass
2. **Approval tool injection** (runner.py:331–342): `make_planner_tools` + `make_executor_tools` always injected regardless of phase type
3. **Continuation loop** (runner.py:374–401): hardcoded for `require_explicit_completion`; custom completion signals require subclassing
4. **Output schema parsing** (runner.py:449): `_parse_output_schema()` is private; custom schemas require forking
5. **Transition logic** (runner.py:540–544): `_determine_transition()` encodes `on_reject` / `next` only; conditional routing requires subclassing
6. **Mode switching** (runner.py:344–351): side-effects on `app_state`; incompatible with stateless custom runners

### Q7 — AgentsRegistry coupling

- Unknown `agent_type` silently falls back to `"auto"` (agents/registry.py:46)
- System prompt is read-only per-session; augmentation (suffix) is not supported
- Allowed capabilities are role-based and hardcoded in `ROLE_DEFAULT_ALLOWED` (agents/plugin.py:54–63); custom agents get `None` (full access)
- Model selection is global, not per-agent or per-workflow
- Different model per phase requires creating 3 separate agent classes registered in the agents registry — no shortcut

### Q8 — Minimal knowledge set today

A custom workflow author must currently understand:

1. `PhaseSpec` — 14 fields, complex interdependencies
2. `WorkflowPlugin` — ABC with `to_definition()`
3. `WorkflowRunner` — async entry point
4. `AgentDefinition` + `AgentsRegistry` — for any custom agent behavior
5. Output schemas — hardcoded string → XML parser chain
6. Mode overrides — Plan/Auto/Guard/Safe and what capabilities they block
7. Capability filtering — `frozenset[ToolCapability]` semantics
8. `WorkflowConfig` — 12 required constructor args, TUI-specific plumbing

They should not need to understand: `WorkflowConfig` internals, `EventProcessor`, reactive UI state, `ConversationStore`, `ApprovalService` internals, or session memory management.

---

## Goals

- A user can define a 3-phase custom workflow in < 20 lines of Python
- A workflow can specify a different LLM model per phase
- A custom approval overlay can be registered without patching TUI code
- A workflow can be run programmatically (headless) with 2-3 lines of setup
- Custom transition logic can be injected without subclassing `WorkflowRunner`
- Graph topology errors are caught at definition time, not runtime

---

## Design

### Proposal 1 — `NodeConfig`: per-node agent configuration

Add `NodeConfig` to `workflow/plugin.py`:

```python
@dataclass(frozen=True)
class NodeConfig:
    system_prompt:  str         = ""     # replaces system_prompt_override
    model:          str | None  = None   # None = inherit session default
    max_turns:      int         = 20
    temperature:    float | None = None
    thinking:       bool        = False
    tool_filter:    Any         = None   # frozenset[ToolCapability] | None
```

`PhaseNode` gains `node_config: NodeConfig | None = None`. Runner reads from `node_config` first, falls back to session defaults. No registry lookup, no silent fallback.

**Eliminates:**
- Per-phase model override impossibility (Q2)
- Silent `agent_type` fallback (Q7)
- `system_prompt_override` vs augmentation confusion

### Proposal 2 — Split `WorkflowConfig` into `WorkflowRuntime` + `WorkflowSession`

```python
@dataclass
class WorkflowRuntime:
    """Execution strategy — changes per runner, not per session."""
    agent_runner:    AgentRunnerBase
    tool_provider:   ToolProvider        # replaces plugin_tools + mcp_registry
    memory_factory:  Callable[[], ShortTermMemory] = ShortTermMemory
    on_phase_start:  Callable | None = None
    on_phase_end:    Callable | None = None
    on_error:        ErrorPolicy = ErrorPolicy.RAISE   # raise | retry | skip

@dataclass
class WorkflowSession:
    """TUI session singletons — optional for headless runs."""
    conv_store:   ConversationStore
    app_state:    AppState
    processor:    EventProcessor
    approval_svc: ApprovalService | None = None
    mode_manager: ModeManager | None     = None
    cfg:          AgenthiccConfig        = field(default_factory=AgenthiccConfig)
```

`WorkflowRunner.__init__(graph, runtime, session=None)`.

When `session=None`:
- No reactive UI updates
- No kernel events emitted
- Approval gates auto-approve
- Errors propagate as exceptions

Minimal programmatic use:

```python
runtime = WorkflowRuntime(agent_runner=runner, tool_provider=MyTools())
await WorkflowRunner(my_graph, runtime).run("add auth")
```

**Eliminates:** 12-field `WorkflowConfig` burden (Q4, Q5), `conv_store.append_event` crashes in headless mode.

### Proposal 3 — `TransitionResolver` and `PhaseExecutor` protocols

```python
class TransitionResolver(Protocol):
    def resolve(
        self, node: PhaseNode, result: NodeResult, data_bus: DataBus,
    ) -> str | None:
        """Return the next node name, or None for terminal."""

class PhaseExecutor(Protocol):
    async def execute(
        self, node: PhaseNode, intent: str, data_bus: DataBus,
        runtime: WorkflowRuntime,
    ) -> NodeResult:
        """Execute one node and return its result."""
```

`WorkflowRunner` accepts:
```python
resolver: TransitionResolver = DefaultTransitionResolver()
executor: PhaseExecutor      = DefaultPhaseExecutor()
```

Custom routing based on output content:

```python
class ContentRouter(TransitionResolver):
    def resolve(self, node, result, data_bus):
        if "error" in result.output.get("summary", "").lower():
            return next((e.target for e in node.edges if e.label == "retry"), None)
        return next((e.target for e in node.edges if e.label == "complete"), None)

runner = WorkflowRunner(graph, runtime, resolver=ContentRouter())
```

**Eliminates:** `_determine_transition` escape hatches (Q6), custom phase types requiring subclassing.

### Proposal 4 — `OverlayRegistry` as a first-class concept

New file: `tui/overlays/registry.py`

```python
class OverlayRegistry:
    _global: ClassVar[OverlayRegistry] = OverlayRegistry()

    def register(self, kind: str, factory: OverlayFactory) -> None: ...
    def resolve(self, kind: str) -> OverlayFactory: ...

    @classmethod
    def global_instance(cls) -> OverlayRegistry: ...
```

TUI startup registers built-ins:

```python
OverlayRegistry.global_instance().register("plan_review",   PlanApprovalOverlay)
OverlayRegistry.global_instance().register("tool_approval", ApprovalOverlay)
```

User plugin (no TUI patching required):

```python
# ~/.agenthicc/overlays/code_review.py
from agenthicc.tui.overlays import OverlayRegistry
OverlayRegistry.global_instance().register("code_review", CodeReviewOverlay)
```

`_wire_approval_overlay` in `tui_session.py` becomes:

```python
overlay_cls = OverlayRegistry.global_instance().resolve(req.kind)
overlay = overlay_cls(req, approval_svc, workspace.overlays.hide)
```

`EdgeGate.kind` is now an open string that maps to any registered overlay.

**Eliminates:** hardcoded overlay dispatch (Q3), need to fork TUI for custom approval UI.

### Proposal 5 — Compile-time graph validation

Add `WorkflowGraph.validate() -> list[ValidationError]` called by `WorkflowRegistry.register()`:

```python
def validate(self) -> list[ValidationError]:
    errors = []
    node_names = set(self.nodes)
    for name, node in self.nodes.items():
        for edge in node.edges:
            if edge.target is not None and edge.target not in node_names:
                errors.append(ValidationError(
                    node=name, edge=edge.label,
                    message=f"targets unknown node {edge.target!r}",
                ))
    reachable = self._reachable_from(self.entry)
    unreachable = node_names - reachable
    if unreachable:
        errors.append(ValidationError(
            node=self.entry,
            message=f"unreachable nodes: {unreachable}",
        ))
    return errors
```

Fail loudly at load time, not silently 30 seconds into a workflow.

**Eliminates:** runtime `_run_graph` crash on unknown node name (Q1).

### Proposal 6 — `WorkflowBuilder` fluent API

New file: `workflow/builder.py`

```python
from agenthicc.workflow import WorkflowBuilder

my_workflow = (
    WorkflowBuilder("my_workflow")
    .phase("research",
           model="claude-haiku-4-5",
           prompt="Research the codebase thoroughly.")
    .then("implement",
          model="claude-opus-4-8",
          prompt="Implement the changes.")
    .then("summarize",
          prompt="Summarise what was planned and done.")
    .bind_to_mode("Custom")
    .build()
)
```

`WorkflowBuilder.build()` returns a `WorkflowGraph`. The user never sees `PhaseNode`, `EdgeSpec`, `NodeConfig`, or `WorkflowPlugin` unless they need fine-grained control.

For approval gates:

```python
WorkflowBuilder("plan_and_execute")
    .phase("plan", prompt="...", model="claude-opus-4-8")
    .then("execute", prompt="...", gate="plan_review")   # shows PlanApprovalOverlay
    .then("summarize", prompt="...")
    .build()
```

**Eliminates:** knowledge burden for basic use cases (Q8).

---

## What the minimal surface area becomes after the revamp

| Task | Current complexity | After revamp |
|---|---|---|
| 3-phase workflow | `WorkflowPlugin` + `PhaseSpec` × 3 + loader discovery | `WorkflowBuilder.phase().then().then().build()` |
| Per-phase model | Not possible | `NodeConfig(model="claude-haiku-4-5")` or `.phase(..., model=...)` in builder |
| Custom approval UI | Patch `tui_session.py` | `OverlayRegistry.global_instance().register("kind", MyOverlay)` |
| Programmatic run | Instantiate 12 objects | `WorkflowRunner(graph, WorkflowRuntime(agent_runner))` |
| Custom transition | Subclass `WorkflowRunner` | Implement `TransitionResolver` protocol (3 lines) |
| Custom agent behaviour | Create `AgentPlugin` + register | Put behaviour in `NodeConfig.system_prompt` or custom `PhaseExecutor` |

---

## File changes

| File | Change |
|---|---|
| `workflow/plugin.py` | Add `NodeConfig`; update `PhaseNode` to carry `node_config` |
| `workflow/runtime.py` (new) | `WorkflowRuntime`, `WorkflowSession`, `ToolProvider`, `TransitionResolver`, `PhaseExecutor` protocols, `ErrorPolicy` |
| `workflow/builder.py` (new) | `WorkflowBuilder` fluent API |
| `workflow/runner.py` | Accept `WorkflowRuntime` + optional `WorkflowSession`; dispatch to `PhaseExecutor` / `TransitionResolver`; remove hardcoded TUI coupling |
| `workflow/config.py` | `WorkflowConfig` → `WorkflowSession` alias (backward compat) |
| `tui/overlays/registry.py` (new) | `OverlayRegistry` with global instance and `register` / `resolve` |
| `runners/tui_session.py` | Use `OverlayRegistry`; build `WorkflowSession`; pass to runner |

---

## Acceptance criteria

- [ ] `WorkflowBuilder` creates a valid `WorkflowGraph` in ≤ 10 lines for a 3-phase workflow
- [ ] `NodeConfig.model` overrides the session model for that node's LLM calls
- [ ] A custom overlay is shown when `EdgeGate.kind` matches a registered key — no TUI patching
- [ ] `WorkflowRunner(graph, WorkflowRuntime(agent_runner=r, tool_provider=t)).run(intent)` works without a `WorkflowSession`
- [ ] A custom `TransitionResolver` can route based on output content with < 10 lines of code
- [ ] `WorkflowGraph.validate()` catches unknown edge targets and unreachable nodes at registration time
- [ ] All existing built-in workflows (`code_plan`, `supervised`, `architect`) continue to work unchanged
- [ ] All existing tests pass; new tests cover each new API surface
