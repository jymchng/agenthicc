# PRD-87 — AgentsRegistry and Workflow Architectural Revamp

## Background

PRD-81 introduced workflows as sequences of phases, each calling
`_run_agent_turn` with a string system prompt assembled from a `PhaseRole`
lookup table and a `tool_access` string mapped to a blocked capability
frozenset.  This has three structural problems identified in the
architecture discussions:

1. **Flimsiness of string-based tool access** — `tool_access: str` is one
   of four magic strings (`"full"`, `"read_only"`, `"none"`, `"inherit"`)
   converted to frozensets via a dict lookup.  A typo is silently ignored.
   `ToolCapability` already exists and should be used directly.

2. **No reusable agent definitions** — every phase builds a throwaway
   `_AgenthiccAgent` class inline in `_run_agent_turn`.  The system prompt
   is a hardcoded dict keyed on role names.  There is no way to reuse,
   extend, shadow, or test an agent type independently.

3. **Workflow ↔ Mode coupling is dual-maintained** — TOML `mode_bindings`
   and a hardcoded `_WORKFLOW` dict in `mode_manager.py` must both be kept
   in sync.  TOML files add a non-Python parsing layer with null-literal
   workarounds.  A `RuntimeMode` can only bind to one workflow.

---

## Goals

- Introduce an `AgentsRegistry` that stores named, `@agent(...)`-decorated
  Python classes as the canonical source of truth for each agent's system
  prompt and model configuration.
- Replace `PhaseSpec.role: PhaseRole` + `tool_access: str` with
  `agent_type: str` + `allowed_capabilities: frozenset[ToolCapability] | None`,
  typed directly against `ToolCapability`.
- Delete all TOML workflow files; replace with Python `WorkflowPlugin`
  subclasses in `workflow/builtins.py`.
- Give each `RuntimeMode` a `default_workflow: str | None` and a
  `workflows: tuple[str, ...]` (all available), derived automatically from
  the `WorkflowRegistry.mode_bindings_map()` — no hardcoded `_WORKFLOW` dict.
- Allow a Mode to own multiple workflows (one default, others available via
  `/workflow <name>`) and a Workflow to be referenced by multiple Modes.

## Non-Goals

- Multi-agent parallel phases (existing `parallel_with` on `PhaseSpec` is
  kept, each parallel phase still runs one agent).
- Changes to the headless runner.
- Changes to tool plugins, skills, or MCP registry.

---

## Architecture

### Capability hierarchy per turn

```
RuntimeMode
  blocked_capabilities: frozenset[ToolCapability]   ← ceiling for all agents
      ↓
PhaseSpec
  agent_type: str                   ← name in AgentsRegistry
  allowed_capabilities: frozenset[ToolCapability] | None
      ↓  (None → role default from ROLE_DEFAULT_ALLOWED)
Tool list passed to @use_tools(...)
  = all_session_tools
    ∩ {tools whose caps ⊆ phase.resolved_allowed_caps}  (when non-None)
    ∩ {tools whose caps ∩ mode.blocked_capabilities == ∅}
```

The mode's `ToolCapabilityGate` remains in the hook chain as a runtime
safety net.  `PhaseCapabilityGate` is removed — the primary restriction
is the filtered tool list, constructed before the agent is instantiated.

---

## New module: `agents/`

### `agents/plugin.py`

```python
from agenthicc.tools.capabilities import ToolCapability

# Convenience sets replacing the string shorthands
READ_CAPS = frozenset({
    ToolCapability.READ, ToolCapability.GIT_READ, ToolCapability.SEARCH,
})
WRITE_CAPS = frozenset({
    ToolCapability.WRITE, ToolCapability.GIT_WRITE,
    ToolCapability.EXECUTE, ToolCapability.NETWORK,
})

@dataclass(frozen=True)
class AgentDefinition:
    name:                 str
    agent_class:          type   # @agent(model=None, system=…)-decorated class
    allowed_capabilities: frozenset[ToolCapability] | None = None
    source:               str = "builtin"

class AgentPlugin:
    """ABC for user/project-defined agents loaded from .agenthicc/agents/."""
    name:                 str  = ""
    allowed_capabilities: frozenset[ToolCapability] | None = None
    replaces:             str | None = None   # builtin name to shadow
    source:               str = "user"
    # Subclass must also be decorated with @agent(system=…)
```

### `agents/builtin.py`

Pre-defined `@agent(...)`-decorated classes — the canonical source of
truth for each agent's system prompt:

```python
from lauren_ai._agents import agent

@agent(model=None, system=(
    "You are a careful planning agent. Produce a numbered step-by-step plan. "
    "Do NOT execute any tools that modify files or run commands. "
    "Wrap your final plan in <plan>…</plan> tags."
))
class PlannerAgent: ...

@agent(model=None, system=(
    "You are an execution agent. Follow the plan step by step. "
    "Use tools to implement each step. Report progress after each step."
))
class ExecutorAgent: ...

# ReviewerAgent, ExplorerAgent, VerifierAgent …
```

`model=None` — the session model is injected at instantiation time via
`make_instance()`.

### `agents/registry.py`

```python
class AgentsRegistry:
    def register(self, defn: AgentDefinition) -> None: ...
    def get(self, name: str) -> AgentDefinition | None: ...
    def all(self) -> list[AgentDefinition]: ...

    def make_instance(
        self,
        agent_type: str,
        filtered_tools: list,
        model_id: str,
    ) -> tuple[type, object]:
        """Create a per-turn agent class and instance.

        Reads the system prompt from the registered @agent(...)-decorated
        class.  Creates a fresh decorated class with the session model and
        filtered tool list so the shared base class is never mutated.
        """
        from lauren_ai._agents import agent as agent_decorator, use_tools
        defn = self.get(agent_type) or self.get("auto")
        from lauren_ai._agents import AGENT_META
        system = getattr(getattr(defn.agent_class, AGENT_META, None), "system", "") or ""

        @agent_decorator(model=model_id, system=system)
        @use_tools(*filtered_tools)
        class _TurnAgent: ...

        return _TurnAgent, _TurnAgent()

def build_agents_registry(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> AgentsRegistry:
    """Builtin agents → user-global → project-local."""
```

Discovery paths:

```
~/.agenthicc/agents/*.py     # user-global; exports AGENTS = [AgentPlugin subclass, ...]
.agenthicc/agents/*.py       # project-local (shadows user by name)
```

### ROLE_DEFAULT_ALLOWED (in `agents/plugin.py`)

```python
ROLE_DEFAULT_ALLOWED: dict[str, frozenset[ToolCapability] | None] = {
    "planner":  READ_CAPS,
    "executor": None,        # all capabilities the mode permits
    "reviewer": READ_CAPS,
    "explorer": READ_CAPS,
    "verifier": READ_CAPS,
    "human":    frozenset(), # no tools — waits for user input
    "custom":   None,
    "auto":     None,
}
```

---

## `workflow/plugin.py` changes

### `PhaseSpec` — replace role/tool_access with agent_type/allowed_capabilities

```python
@dataclass(frozen=True)
class PhaseSpec:
    name:                          str
    agent_type:                    str                             = "auto"
    allowed_capabilities:          frozenset[ToolCapability] | None = None
    # None → use ROLE_DEFAULT_ALLOWED[agent_type], then mode ceiling
    allowed_capabilities_override: frozenset[ToolCapability] | None = None
    max_turns:                     int                             = 20
    output_schema:                 str | None                      = None
    next:                          str | None                      = None
    on_reject:                     str | None                      = None
    on_error:                      str | None                      = None
    max_iterations:                int                             = 3
    parallel_with:                 tuple[str, ...]                 = ()

    @property
    def resolved_allowed_caps(self) -> frozenset[ToolCapability] | None:
        """Effective allowed capabilities for this phase's agent."""
        if self.allowed_capabilities_override is not None:
            return self.allowed_capabilities_override
        if self.allowed_capabilities is not None:
            return self.allowed_capabilities
        from agenthicc.agents.plugin import ROLE_DEFAULT_ALLOWED
        return ROLE_DEFAULT_ALLOWED.get(self.agent_type)
```

### Removed from `workflow/plugin.py`

- `PhaseRole` enum (keep as string constants alias — see below)
- `ROLE_SYSTEM_PROMPTS`
- `ROLE_TOOL_ACCESS`
- `BLOCKED_CAPS_BY_ACCESS`
- `PhaseSpec.role`, `tool_access`, `effective_tool_access`, `system_prompt`,
  `blocked_capabilities` properties

### `PhaseRole` — kept as typed string constants

```python
class PhaseRole(str):
    """String constants matching builtin agent type names."""
    PLANNER  = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    EXPLORER = "explorer"
    VERIFIER = "verifier"
    HUMAN    = "human"
    CUSTOM   = "custom"
    AUTO     = "auto"
```

`agent_type=PhaseRole.PLANNER` == `agent_type="planner"` — fully compatible.

---

## `workflow/builtins.py` — Python-only

```python
class PlanOnly(WorkflowPlugin):
    name          = "plan_only"
    description   = "Read-only planning pass."
    mode_bindings = ["Plan", "Review"]
    phases = [
        PhaseSpec(name="plan", agent_type=PhaseRole.PLANNER,
                  max_turns=8, output_schema="plan"),
    ]

class Supervised(WorkflowPlugin):
    name          = "supervised"
    description   = "Plan → Human Review → Execute."
    mode_bindings = []
    phases = [
        PhaseSpec(name="plan",         agent_type=PhaseRole.PLANNER,
                  max_turns=8, output_schema="plan", next="human_review"),
        PhaseSpec(name="human_review", agent_type=PhaseRole.HUMAN,
                  max_turns=1, next="execute", on_reject="plan",
                  max_iterations=5),
        PhaseSpec(name="execute",      agent_type=PhaseRole.EXECUTOR,
                  max_turns=30),
    ]

class Architect(WorkflowPlugin): ...   # explore→plan→execute→verify
class ReviewOnly(WorkflowPlugin): ...
```

---

## `workflow/loader.py` — TOML removed

`load_toml_workflow`, `_parse_phase`, `_null_str_to_none`, `_get_tomllib`
are deleted.  `load_builtin_workflows()` imports Python classes directly:

```python
def load_builtin_workflows() -> list[WorkflowDefinition]:
    from agenthicc.workflow.builtins import (
        PlanOnly, ReviewOnly, Supervised, Architect,
    )
    return [cls().to_definition(source="builtin")
            for cls in (PlanOnly, ReviewOnly, Supervised, Architect)]
```

---

## `workflow/registry.py` additions

```python
class WorkflowRegistry:
    def mode_default_map(self) -> dict[str, str]:
        """mode_name → first-registered default workflow name."""
        result: dict[str, str] = {}
        for defn in self._defs.values():
            for mode_name in defn.mode_bindings:
                result.setdefault(mode_name, defn.name)
        return result

    def mode_available_map(self) -> dict[str, list[str]]:
        """mode_name → all workflow names available in that mode."""
        result: dict[str, list[str]] = {}
        for defn in self._defs.values():
            for mode_name in defn.mode_bindings:
                result.setdefault(mode_name, []).append(defn.name)
        return result
```

TOML scanning is removed from `_scan_workflow_dir`.

---

## `workflow/runner.py` changes

`WorkflowRunner` gains `agents_registry: AgentsRegistry` and `model_id: str`.

`_run_phase` replaces the inline agent construction:

```python
async def _run_phase(self, spec, intent, context):
    # 1. Filter tools
    filtered = self._filter_tools(spec)

    # 2. Instantiate agent from registry
    agent_class, agent_instance = self._agents_registry.make_instance(
        agent_type=spec.agent_type,
        filtered_tools=filtered,
        model_id=self._model_id,
    )

    # 3. Stream via AgentRunnerBase directly
    _active_runner = AgentRunnerBase(
        transport=self._runner._transport,
        signals=getattr(self._runner, "_signals", None),
        global_hooks=[ToolCapabilityGate(self._app_state)],
    )
    output_buf: list[str] = []
    _stream = await _active_runner.run_stream(
        agent_instance, phase_text,
        memory=ShortTermMemory(max_tokens=16_000),
        config_override=AgentConfig(max_turns=spec.max_turns),
    )
    async for chunk in _stream:
        if chunk.delta:
            output_buf.append(chunk.delta)
    ...

def _filter_tools(self, spec: PhaseSpec) -> list:
    mode_blocked  = self._app_state.active_mode().blocked_capabilities
    phase_allowed = spec.resolved_allowed_caps   # frozenset | None
    from agenthicc.tools.capabilities import CAPABILITIES_KEY
    result = []
    for tool in self._all_tools:
        from lauren_ai._tools import TOOL_META
        meta = getattr(tool, TOOL_META, None)
        caps = (meta.get_metadata(CAPABILITIES_KEY)
                if meta else None) or frozenset()
        if caps & mode_blocked:
            continue
        if phase_allowed is not None and not (caps <= phase_allowed):
            continue
        result.append(tool)
    return result
```

`_run_agent_turn` is no longer called inside workflow phases — the runner
uses `AgentRunnerBase.run_stream` directly, which is simpler and avoids
the mention-injection and skill-injection overhead that belongs only to
the primary user-facing turn.

---

## `tui/runtime/mode_manager.py` changes

```python
@dataclass(frozen=True)
class RuntimeMode:
    ...
    blocked_capabilities: frozenset[str] = field(default_factory=frozenset)
    approval_required:    frozenset[str] = field(default_factory=frozenset)
    default_workflow:     str | None     = None   # replaces workflow_name
    workflows:            tuple[str,...] = ()      # all available in this mode

def build_default_registry(
    default_map:   dict[str, str]        | None = None,
    available_map: dict[str, list[str]]  | None = None,
) -> ModeRegistry:
    _default   = default_map   or {}
    _available = available_map or {}
    ...
    reg.register(RuntimeMode(
        ...
        default_workflow = _default.get(mode.name),
        workflows        = tuple(_available.get(mode.name, [])),
    ))
```

`_WORKFLOW` dict deleted.

---

## `runners/tui_session.py` changes

```python
# Build both registries
_workflow_registry = build_workflow_registry(...)
_agents_registry   = build_agents_registry(...)

# Pass mode maps to ModeManager
mode_manager = ModeManager(
    app_state      = app_state,
    default_map    = _workflow_registry.mode_default_map(),
    available_map  = _workflow_registry.mode_available_map(),
)

# In _run_turn:
_wf_name = app_state.active_mode().default_workflow
_wf_defn = _workflow_registry.get(_wf_name) if _wf_name else None

if _wf_defn is not None:
    _wf_runner = WorkflowRunner(
        definition      = _wf_defn,
        agents_registry = _agents_registry,
        model_id        = model_label,
        ...
    )
    await _wf_runner.run(text)
else:
    await _run_agent_turn(text, agent_runner, ...)
```

---

## File changes

| File | Change |
|---|---|
| `agents/__init__.py` | **New** — re-exports |
| `agents/plugin.py` | **New** — `AgentDefinition`, `AgentPlugin`, `READ_CAPS`, `WRITE_CAPS`, `ROLE_DEFAULT_ALLOWED` |
| `agents/builtin.py` | **New** — `PlannerAgent`, `ExecutorAgent`, `ReviewerAgent`, `ExplorerAgent`, `VerifierAgent` decorated with `@agent` |
| `agents/registry.py` | **New** — `AgentsRegistry`, `build_agents_registry()` |
| `workflow/builtins.py` | **New** — Python `WorkflowPlugin` subclasses |
| `workflow/builtins/*.toml` | **Delete** |
| `workflow/plugin.py` | Replace `role/tool_access` with `agent_type/allowed_capabilities`; remove dicts; keep `PhaseRole` as string constants; keep all other dataclasses |
| `workflow/loader.py` | Remove TOML machinery; `load_builtin_workflows()` imports Python classes |
| `workflow/registry.py` | Add `mode_default_map()`, `mode_available_map()`; remove TOML branch |
| `workflow/__init__.py` | Remove `load_toml_workflow` export; add agents exports |
| `workflow/runner.py` | Accept `agents_registry`, `model_id`; use `make_instance()`; use direct `AgentRunnerBase` instead of `_run_agent_turn`; add `_filter_tools()` |
| `tools/capability_gate.py` | Remove `PhaseCapabilityGate` |
| `runners/agent_turn.py` | Remove `phase_blocked_caps` param and `PhaseCapabilityGate` branch |
| `tui/runtime/mode_manager.py` | Replace `workflow_name` with `default_workflow`+`workflows`; delete `_WORKFLOW`; `build_default_registry` accepts maps; `ModeManager` accepts and forwards maps |
| `runners/tui_session.py` | Build `AgentsRegistry`; pass maps to `ModeManager`; pass `agents_registry` + `model_id` to `WorkflowRunner`; use `mode.default_workflow` |

---

## Acceptance criteria

- [ ] `AgentsRegistry.make_instance("planner", tools, model)` returns an
      agent whose system prompt matches `PlannerAgent`'s `@agent(system=…)`.
- [ ] Plan mode submitting a message dispatches through `plan_only` workflow
      without any hardcoded `_WORKFLOW` dict.
- [ ] A user workflow TOML in `~/.agenthicc/workflows/` still fails to load
      cleanly (TOML is removed; only Python plugins supported).
- [ ] A user Python agent in `~/.agenthicc/agents/my_agent.py` exporting an
      `AgentPlugin` subclass is discovered and registered.
- [ ] `PhaseSpec(name="plan", agent_type="planner")` uses `READ_CAPS` as its
      resolved tool filter with no `tool_access` string.
- [ ] `PhaseCapabilityGate` no longer exists in `capability_gate.py`.
- [ ] All existing unit, integration, and e2e tests pass.
