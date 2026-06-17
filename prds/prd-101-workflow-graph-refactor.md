# PRD-101 — Workflow Graph Refactor: PhaseNode, EdgeSpec, DataBus, EdgeGate

## Problem

The current workflow system (`workflow/plugin.py`, `workflow/runner.py`) has
accumulated four separate completion mechanisms bolted on over time, each adding
a new flag to `PhaseSpec` and a new branch to `_run_phase`:

| Phase | Mechanism | How it signals completion |
|---|---|---|
| plan | `plan_event` (asyncio.Event) + `require_plan_finalization=True` | `finalize_plan()` tool |
| execute | `execute_event` + `require_explicit_completion=True` | `mark_execute_complete()` tool |
| review | `output_schema="review_result"` + XML parsing | `<review>approved</review>` text tag |
| summarize | `output_schema="free_text"` | end of turn (no gate) |

This design has three concrete bugs and two structural problems.

### Concrete bugs

1. **`plan_event` injected into every phase**: both `plan_event` and `execute_event`
   are created whenever `approval_svc is not None`, making `plan_event is not None`
   true for review and summarize. The phase gate fires for those phases and returns
   `approved=False` because `plan_event.is_set()` is always False outside the plan
   phase. This was the direct cause of the review→summarize transition bug.

2. **`_determine_transition` tristate**: `approved: bool | None` where `None` and
   `True` behave identically (both take `spec.next`). `False` only does something
   if `on_reject` is set; otherwise it also takes `spec.next`. The semantics are
   invisible at the call site.

3. **`__next_phase__` escape hatch**: added to express the "retry review itself"
   case because the `approved`/`on_reject`/`spec.next` model could not express it
   directly.

### Structural problems

1. **`_run_phase` does everything**: tool injection, event creation, continuation
   loop branching, event checking, XML parsing, output construction — 160 lines
   that grow with every new phase type.

2. **No per-node LLM configuration**: every phase uses the session model. There
   is no slot for specifying a different model, temperature, or system prompt at
   the node level; `system_prompt_override` on `PhaseSpec` is a workaround.

---

## Goals

- One completion mechanism for all phases: a single `complete_phase` tool.
- Transitions expressed as typed graph edges, not `on_reject`/`next` string pairs.
- Human-in-the-loop gates on any edge via `EdgeGate`, dispatched through an
  overlay registry (not hardcoded `if kind == "plan_review"` branches).
- Per-node `LLMConfig` (model, system prompt).
- `DataBus` replaces `WorkflowContext`: structured dicts between nodes, not
  truncated `full_text` snippets.
- `_run_graph` (the outer loop) only follows edges; `_run_node` only runs one
  node. All gate/event/output logic lives in the completion tool closure.

---

## Design

### New types

#### `lauren_ai.LLMConfig` and `lauren_ai.AgentConfig` (no custom wrapper)

The PRD does not define its own `LLMConfig`. Instead `PhaseNode` references the
two config types already provided by lauren-ai:

**`lauren_ai.LLMConfig`** — provider connection config. Key fields:

```python
# from lauren_ai._config
@dataclass(frozen=True)
class LLMConfig:
    provider:    Literal["anthropic", "openai", "ollama", "litellm"]
    model:       str
    api_key:     str | None = None
    base_url:    str | None = None
    max_tokens:  int        = 4096
    temperature: float      = 1.0
    timeout:     float      = 60.0
    # …factory helpers: LLMConfig.for_anthropic(), .for_openai(), .for_testing()
```

Used on a `PhaseNode` to switch to a different provider or model for that node.
`None` (the default) means "use the session transport as-is".

**`lauren_ai.AgentConfig`** — per-call agent behaviour. Key fields:

```python
# from lauren_ai._config
@dataclass(frozen=True)
class AgentConfig:
    system_prompt:             str            = ""
    max_turns:                 int            = 10
    max_tokens_per_turn:       int            = 4096
    temperature:               float          = 1.0
    memory_window_tokens:      int            = 8000
    max_cost_usd:              float | None   = None
    parallel_tool_calls:       bool           = False
    thinking:                  bool           = False
    thinking_budget_tokens:    int            = 10000
    reasoning_effort:          str | None     = None
    summarize_at:              float | None   = None
```

`system_prompt` here replaces the old `PhaseSpec.system_prompt_override`.
`max_turns` replaces `PhaseNode.max_turns` (removed as a top-level field).
`AgentConfig` is passed verbatim to `run_stream(config_override=node.agent_config)`
in `_run_node`, so every field takes effect at the LLM call boundary.

**Relationship between the two types:**

`LLMConfig` selects *which provider and model* to call (transport layer).
`AgentConfig` configures *how the agent behaves* for that call (system prompt,
turn limit, thinking, cost cap). A node may set one, both, or neither.

```python
# example: plan node — custom system prompt + thinking enabled
PhaseNode(
    name="plan",
    agent_config=AgentConfig(
        system_prompt="You are in the PLANNING phase …",
        max_turns=20,
        thinking=True,
        thinking_budget_tokens=16_000,
    ),
    …
)

# example: summarize node — cheaper model + tight turn cap
PhaseNode(
    name="summarize",
    llm_config=LLMConfig.for_anthropic(model="claude-haiku-4-5-20251001"),
    agent_config=AgentConfig(
        system_prompt="Write a concise summary …",
        max_turns=4,
    ),
    …
)
```

**Implementation note — `llm_config`:** when `node.llm_config` is set, `_run_node`
builds a new transport for that config using `build_llm_config(node.llm_config)`.
When `None`, the session transport is reused (the common path). Building a new
transport is only needed for cross-provider or cross-model phase overrides.

#### `EdgeGate`

```python
@dataclass(frozen=True)
class EdgeGate:
    kind:  str = "plan_review"
    """Overlay identifier looked up in the TUI overlay registry.
    Built-in values: 'plan_review' (PlanApprovalOverlay),
                     'tool_approval' (ApprovalOverlay).
    Plugins register custom kinds at startup."""

    title: str = ""
    """Optional header text override shown in the overlay."""
```

The `kind` string is the only coupling between the graph definition and the TUI
layer. Adding a new overlay type requires only: (a) implementing the overlay
class, and (b) registering it in `_OVERLAY_REGISTRY`.

#### `EdgeSpec`

```python
@dataclass(frozen=True)
class EdgeSpec:
    target: str | None
    """Destination node name. None = terminal (workflow ends after this edge)."""

    label:  str
    """Semantic name the agent uses when calling complete_phase(next=label).
    Examples: 'approve', 'reject', 'complete', 'revise'."""

    gate:   EdgeGate | None = None
    """When set, complete_phase suspends the agent and shows the overlay before
    committing the transition. None = automatic (no human step)."""
```

Replaces `spec.next`, `spec.on_reject`, `spec.on_error`, and
`approval_required: bool`.

#### `PhaseNode`

```python
@dataclass(frozen=True)
class PhaseNode:
    name:               str
    """Unique node identifier within the workflow graph."""

    agent_config:       AgentConfig | None = None
    """Per-node agent behaviour (system_prompt, max_turns, temperature, thinking, …).
    Passed to run_stream(config_override=…). None = use session defaults.
    system_prompt here replaces PhaseSpec.system_prompt_override.
    max_turns here replaces the old top-level PhaseNode.max_turns field."""

    llm_config:         LLMConfig | None   = None
    """Per-node provider/model override. None = use the session transport.
    Only needed when a node must call a different model or provider."""

    agent_type:         str               = "auto"
    """Key into AgentsRegistry for role-based capability defaults."""

    edges:              tuple[EdgeSpec, ...] = ()
    """Outgoing edges. Empty tuple = terminal node."""

    allowed_capabilities: object          = None
    """frozenset[ToolCapability] | None — tool capability allowlist."""

    mode_override:      str | None        = None
    """RuntimeMode name to activate for the duration of this node's turn."""

    max_continuations:  int               = 10
    """Maximum continuation loop iterations before the node gives up.
    Replaces max_iterations. max_turns per iteration lives in agent_config."""

    parallel_with:      tuple[str, ...]   = ()
    """Names of sibling nodes to run concurrently."""
```

Replaces `PhaseSpec` and its seven accumulated flags (`require_plan_finalization`,
`require_explicit_completion`, `output_schema`, `on_reject`, `next`, `on_error`,
`max_iterations`). `system_prompt_override` and `max_turns` move into `AgentConfig`.

#### `WorkflowGraph`

```python
@dataclass(frozen=True)
class WorkflowGraph:
    name:                 str
    """Unique workflow identifier used in registry lookups and kernel events."""

    entry:                str
    """Name of the first node to run."""

    nodes:                dict[str, PhaseNode]
    """Ordered mapping of node_name → PhaseNode. Insertion order = definition
    order used for phase_index display (Phase N/M)."""

    description:          str            = ""
    mode_bindings:        tuple[str,...] = ()
    source:               str            = "builtin"
    path:                 str | None     = None
    max_total_phase_runs: int            = 0
    """Opt-in global cap (0 = unlimited). Same semantics as
    WorkflowDefinition.max_total_phase_runs."""
```

Replaces `WorkflowDefinition` and its `phases: tuple[PhaseSpec, ...]` list.

#### `DataBus`

```python
@dataclass
class DataBus:
    intent:  str
    run_id:  str
    outputs: dict[str, dict]
    """node_name → structured output dict written by complete_phase()."""

    def set(self, node_name: str, data: dict) -> None: ...
    def get(self, node_name: str) -> dict | None: ...
    def as_context_block(self) -> str:
        """Structured prompt injection (replaces WorkflowContext.as_system_block).

        Example output:
            [WORKFLOW CONTEXT]
            Original intent: add JWT auth

            plan:
              approach: "JWT with refresh tokens"
              files: ["auth.py", "middleware.py"]

            execute:
              files_modified: ["auth.py"]
              tests_passing: false
        """
```

Replaces `WorkflowContext`. `DataBus.outputs` carries structured dicts, not
`PhaseOutput` objects. Full values are available; no 200-char truncation.

#### `NodeResult`

```python
@dataclass
class NodeResult:
    node_name:  str
    edge_label: str | None
    """Edge label taken by the agent (e.g. 'approve', 'reject'). None = terminal
    or failed (agent never called complete_phase)."""

    output:     dict
    """Structured output from complete_phase(output=...). Empty on failure."""

    duration_s: float
```

Replaces `PhaseOutput` for runner-internal use. `PhaseRunRecord` (the audit trail)
is unchanged.

---

### The unified completion tool

`make_completion_tool(node, data_bus, transition_event, transition_data, approval_svc)`
returns a single `@tool()`-decorated callable injected into every phase.

```
complete_phase(output: dict, next: str) → dict

  output — structured data written to DataBus; downstream nodes read from it.
  next   — edge label to follow. Valid labels are enumerated in the docstring
           at tool-generation time so the agent sees them in the tool schema.

For terminal nodes (no edges), complete_phase accepts output only (no next).

When edge.gate is set:
  → ApprovalRequest(kind=edge.gate.kind, tool_input=output, title=edge.gate.title)
  → approval_svc.request_approval() suspends the agent
  → TUI shows _OVERLAY_REGISTRY[req.kind]
  → user responds
  → if allowed:   write output to DataBus, set transition_event, return ok
  → if denied:    return {"approved": False, "feedback": response.message}
                  agent stays in the continuation loop and revises
```

This single tool replaces:
- `finalize_plan` + `request_plan_approval` (plan phase)
- `mark_execute_complete` (execute phase)
- `<review>approved/rejected</review>` XML tag parsing (review phase)

---

### Overlay registry

`tui_session.py` (or a dedicated `overlays/registry.py`) maintains:

```python
_OVERLAY_REGISTRY: dict[str, type] = {
    "plan_review":   PlanApprovalOverlay,
    "tool_approval": ApprovalOverlay,
}
```

`_on_approval_change` becomes:

```python
def _on_approval_change() -> None:
    req = app_state.pending_approval()
    if req is None:
        if isinstance(workspace.overlays.widget,
                      tuple(_OVERLAY_REGISTRY.values())):
            workspace.overlays.hide()
        return
    overlay_cls = _OVERLAY_REGISTRY.get(req.kind, ApprovalOverlay)
    overlay = overlay_cls(req, approval_svc, workspace.overlays.hide)
    workspace.overlays.show(overlay)
```

Plugins register custom overlay kinds at startup:

```python
_OVERLAY_REGISTRY["my_review"] = MyCustomOverlay
```

---

### Runner changes

#### `_run_graph` replaces `_run_phase_loop`

Strictly one responsibility: walk the edge graph.

```
_run_graph(intent, data_bus, wf_run, run_id, start_node):

  node_name = start_node
  while node_name is not None:
      node = graph.nodes[node_name]
      update wf_run.current_phase, current_phase_index
      emit WorkflowPhaseStarted
      result = await _run_node(node, intent, data_bus)
      data_bus.set(node_name, result.output)
      record = PhaseRunRecord(...)
      emit WorkflowPhaseCompleted(edge_label=result.edge_label, output=result.output)
      node_name = _follow_edge(node, result.edge_label)
  emit WorkflowRunCompleted
```

#### `_follow_edge` replaces `_determine_transition`

```python
def _follow_edge(self, node: PhaseNode, edge_label: str | None) -> str | None:
    for edge in node.edges:
        if edge.label == edge_label:
            return edge.target   # may be None (terminal edge)
    return None   # no matching label, or no edges → terminal
```

Three lines. No tristate `approved`. No `__next_phase__` escape hatch.

#### `_run_node` replaces `_run_phase`

```
_run_node(node, intent, data_bus) → NodeResult:

  1. filter_tools(node)           — capability-filtered tool list
  2. make_completion_tool(node)   — one transition tool injected per node
  3. set mode_override
  4. resolve config_override:
       node.agent_config or AgentConfig()  — passed to run_stream()
       node.llm_config (if set)            — builds a per-node transport
  5. continuation loop:
       attempt=1: full node prompt (_build_node_prompt using DataBus)
       attempt>1: "Continue — you have not yet called complete_phase()."
       each iteration: _run_agent_turn(..., config_override=node.agent_config)
       break when transition_event.is_set() or max_continuations reached
  6. restore mode_override (finally)
  7. return NodeResult from transition_data
```

No flags. No branching on phase name. No separate event types. Every node uses
the same loop.

**Human nodes** (`agent_type == "human"`): `_run_node` skips the LLM loop,
calls `approval_svc.request_approval` directly with the appropriate `kind`, maps
`response.allowed` to an edge label (`True → "approve"`, `False → "reject"`), and
returns `NodeResult`. No separate `_run_human_phase` method.

---

### `code_plan` migration

```python
from lauren_ai import AgentConfig, LLMConfig

class CodePlanGraph(WorkflowPlugin):
    name          = "code_plan"
    description   = "Plan → Execute → Review → Summary (single agent, shared memory)"
    mode_bindings = ["Plan"]

    graph = WorkflowGraph(
        name  = "code_plan",
        entry = "plan",
        nodes = {
            "plan": PhaseNode(
                name         = "plan",
                agent_config = AgentConfig(
                    system_prompt=(
                        "You are in the PLANNING phase. Explore the repository, then "
                        "produce a detailed implementation plan. Call complete_phase() "
                        "with next='approve' to submit for review, or next='revise' to "
                        "loop back and improve the plan."
                    ),
                    max_turns=20,
                    thinking=True,                  # enable extended thinking for planning
                    thinking_budget_tokens=16_000,
                ),
                max_continuations = 5,
                edges = (
                    EdgeSpec("execute", "approve",
                             gate=EdgeGate(kind="plan_review")),
                    EdgeSpec("plan",    "revise"),
                ),
            ),
            "execute": PhaseNode(
                name         = "execute",
                agent_config = AgentConfig(
                    system_prompt=(
                        "You are in the EXECUTION phase. Implement the approved plan "
                        "step by step. Call complete_phase(next='complete') when ALL "
                        "tasks are done."
                    ),
                    max_turns=40,
                    parallel_tool_calls=True,       # run independent tool calls concurrently
                ),
                mode_override     = "Auto",
                max_continuations = 10,
                edges = (
                    EdgeSpec("review", "complete"),
                ),
            ),
            "review": PhaseNode(
                name         = "review",
                agent_config = AgentConfig(
                    system_prompt=(
                        "You are in the REVIEW phase. Inspect the changes and run the "
                        "tests. Call complete_phase(next='approve') if all tests pass, "
                        "or complete_phase(next='reject', output={'issues': [...]}) to "
                        "send back to execution."
                    ),
                    max_turns=8,
                ),
                max_continuations = 3,
                edges = (
                    EdgeSpec("summarize", "approve"),
                    EdgeSpec("execute",   "reject"),
                ),
            ),
            "summarize": PhaseNode(
                name         = "summarize",
                # cheaper/faster model for the summary — no writing needed
                llm_config   = LLMConfig.for_anthropic(
                    model="claude-haiku-4-5-20251001"
                ),
                agent_config = AgentConfig(
                    system_prompt=(
                        "You are in the SUMMARY phase. Write a concise summary of "
                        "what was planned, implemented, and verified. "
                        "Call complete_phase()."
                    ),
                    max_turns=4,
                ),
                max_continuations = 1,
                edges = (),   # terminal
            ),
        },
    )
```

---

### What is removed

| Removed | Replaced by |
|---|---|
| `PhaseSpec` | `PhaseNode` |
| Custom agenthicc `LLMConfig` | `lauren_ai.LLMConfig` (provider/model/temperature/…) |
| `PhaseSpec.system_prompt_override` | `AgentConfig.system_prompt` (`lauren_ai.AgentConfig`) |
| `PhaseSpec.max_turns` (top-level field) | `AgentConfig.max_turns` (passed as `config_override`) |
| `WorkflowDefinition` | `WorkflowGraph` |
| `WorkflowContext` | `DataBus` |
| `PhaseOutput` (for routing) | `NodeResult` |
| `plan_event`, `execute_event` | One `transition_event` per `_run_node` call |
| `require_plan_finalization` | `EdgeSpec(gate=EdgeGate(kind="plan_review"))` |
| `require_explicit_completion` | All nodes use `complete_phase`; loop is uniform |
| `finalize_plan`, `request_plan_approval` | `complete_phase(next="approve")` on plan node |
| `mark_execute_complete` | `complete_phase(next="complete")` on execute node |
| `<review>` XML + `_parse_output_schema` routing | `complete_phase(next="approve"/"reject")` on review node |
| `PhaseSpec.on_reject`, `PhaseSpec.next` | `EdgeSpec.target` |
| `PhaseSpec.output_schema` (routing use) | `NodeResult.edge_label` |
| `_determine_transition` | `_follow_edge` (3 lines) |
| `_run_phase` | `_run_node` |
| `_run_phase_loop` | `_run_graph` |
| `_run_human_phase` | `agent_type=="human"` handled in `_run_node` preamble |
| `make_planner_tools`, `make_executor_tools` | `make_completion_tool` |
| `WorkflowContext.as_system_block()` (truncated text) | `DataBus.as_context_block()` (structured) |
| Hardcoded `if kind == "plan_review"` in tui_session | `_OVERLAY_REGISTRY` lookup |

`PhaseRunRecord`, `WorkflowRun`, `WorkflowConfig`, the kernel event types, and
`WorkflowRegistry` are unchanged.

---

## File changes

| File | Change |
|---|---|
| `workflow/plugin.py` | Add `LLMConfig`, `EdgeGate`, `EdgeSpec`, `PhaseNode`, `WorkflowGraph`, `DataBus`, `NodeResult`. Keep `PhaseRunRecord`, `WorkflowRun`. Deprecate `PhaseSpec`, `WorkflowDefinition`, `WorkflowContext`, `PhaseOutput`. |
| `workflow/runner.py` | Rewrite: `_run_graph`, `_run_node`, `_follow_edge`, `make_completion_tool`. Remove `_run_phase`, `_run_phase_loop`, `_determine_transition`, `_run_human_phase`. Update `run()`, `resume()`, `_find_resume_phase` (→ `_find_resume_node`). |
| `workflow/phase_tools.py` | Replace `make_planner_tools`, `make_executor_tools` with `make_completion_tool`. |
| `workflow/builtins.py` | Migrate all `WorkflowPlugin` subclasses to use `WorkflowGraph` + `PhaseNode`. |
| `runners/tui_session.py` | Replace hardcoded `if kind == "plan_review"` with `_OVERLAY_REGISTRY` lookup. |
| `workflow/__init__.py` | Export new types; deprecate old types with import aliases. |
| `tests/` | Update all tests that construct `PhaseSpec`, `WorkflowDefinition`, `WorkflowContext`, or `PhaseOutput` directly. |

---

## Acceptance criteria

- [ ] All existing integration and e2e workflow tests pass unchanged (after
  updating fixture helpers to use new types).
- [ ] `code_plan` completes the full plan → execute → review → summarize path.
- [ ] A rejected plan loops back correctly (plan `revise` edge or `approve` edge
  denied → agent stays in plan loop).
- [ ] Ctrl+C during any node cancels immediately and propagates through
  `_run_node` → `_run_graph`.
- [ ] `complete_phase` tool docstring enumerates the node's available edge labels
  so the LLM receives them in the tool schema.
- [ ] A plugin can register a custom overlay kind and have it shown when an edge
  with `gate=EdgeGate(kind="custom")` is taken.
- [ ] `DataBus.as_context_block()` emits structured key–value output from all
  prior nodes; no 200-char truncation.
- [ ] Per-node `lauren_ai.AgentConfig` is passed as `config_override` to
  `run_stream()`; `system_prompt`, `max_turns`, `thinking`, and `parallel_tool_calls`
  all take effect at the LLM boundary.
- [ ] Per-node `lauren_ai.LLMConfig` (when set) builds a separate transport for
  that node, allowing a different provider or model (e.g. Haiku for summarize).
- [ ] `_follow_edge` replaces `_determine_transition`; no `approved: bool | None`
  tristate anywhere in the runner.
- [ ] `PhaseRunRecord` and `WorkflowRun` are unchanged; kernel events
  `WorkflowPhaseCompleted` now carry `edge_label` instead of `approved`.
