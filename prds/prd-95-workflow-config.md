# PRD-95 — WorkflowConfig: Simplify WorkflowRunner Construction

## Problem

`WorkflowRunner.__init__` accepts 15 parameters.  12 of them are session-scoped
singletons that never change between phases.  Every call site in `tui_session.py`
must pass all 15 explicitly, and every new feature (PRD-87, PRD-90, PRD-91)
added another parameter.

`_build_runner_for_agent` from `lauren_ai.testing` is imported in production
`agent_turn.py` to populate `meta.tools` — a testing utility used as production
infrastructure because no clean public API exists.

## Goals

- Introduce `WorkflowConfig` holding all session-scoped singletons.
- `WorkflowRunner.__init__` takes `(definition, config, mode_manager=None)`.
- `tui_session.py` constructs `WorkflowConfig` once and reuses it for every
  workflow run in the session.
- Replace `_build_runner_for_agent` (testing import) with a proper
  `ToolPopulator` utility in `agenthicc`.

## Design

### `WorkflowConfig`

```python
@dataclass(frozen=True)
class WorkflowConfig:
    conv_store:      ConversationStore
    app_state:       AppState
    processor:       EventProcessor
    agent_runner:    AgentRunnerBase
    approval_svc:    ApprovalService | None
    cfg:             AgenthiccConfig
    skills:          dict
    plugin_tools:    list
    mcp_registry:    Any | None
    mention_cache:   MentionCache
    agents_registry: AgentsRegistry
```

### `WorkflowRunner.__init__`

```python
def __init__(
    self,
    definition:   WorkflowDefinition,
    config:       WorkflowConfig,
    mode_manager: ModeManager | None = None,
) -> None:
    self._def          = definition
    self._cfg          = config
    self._mode_manager = mode_manager
    # All singletons accessed via self._cfg.*
```

### `tui_session.py`

```python
_wf_config = WorkflowConfig(
    conv_store=app_state.conversation,
    app_state=app_state,
    processor=processor,
    agent_runner=agent_runner,
    approval_svc=approval_svc,
    cfg=cfg,
    skills=_skills,
    plugin_tools=_project_plugins.all_tools,
    mcp_registry=_mcp_registry,
    mention_cache=_mention_cache,
    agents_registry=_agents_registry,
)
# Per-turn:
_wf_runner = WorkflowRunner(_wf_defn, _wf_config, mode_manager)
```

### Replace `_build_runner_for_agent`

```python
# runners/tool_populator.py
def populate_agent_tools(agent_instance: Any, tools: list) -> None:
    """Populate meta.tools from a filtered tool list without using lauren_ai.testing."""
    from lauren_ai._agents import AGENT_META
    from lauren_ai._tools import TOOL_META, _add_to_tool_map
    meta = getattr(type(agent_instance), AGENT_META, None)
    if meta is None:
        return
    tool_map = {}
    for t in tools:
        if getattr(t, TOOL_META, None) is not None:
            _add_to_tool_map(tool_map, t)
    meta.tools = tool_map
```

## File changes

| File | Change |
|---|---|
| `workflow/config.py` | **New** — `WorkflowConfig` dataclass |
| `workflow/runner.py` | `__init__(definition, config, mode_manager)`; all `self._*` accessed via `self._cfg` |
| `runners/tui_session.py` | Construct `_wf_config` once; pass to each `WorkflowRunner` |
| `runners/tool_populator.py` | **New** — `populate_agent_tools` replaces `_build_runner_for_agent` import |
| `runners/agent_turn.py` | Import from `tool_populator` instead of `lauren_ai.testing` |

## Acceptance criteria

- [ ] `WorkflowRunner.__init__` takes exactly 3 parameters.
- [ ] `lauren_ai.testing` is not imported in any production module.
- [ ] `WorkflowConfig` is constructed once per session and reused across all workflow runs.
- [ ] All existing tests pass.
