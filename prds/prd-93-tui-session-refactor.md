# PRD-93 — TUISession: Replace the 557-line Mega-Closure

## Problem

`_run_tui_session()` is a single 557-line `async def` that holds every
session-scoped singleton in local variables and every session-scoped behaviour
in nested closures.  Adding workflow dispatch (PRD-87), agent registry
(PRD-87), approval wiring (PRD-78), and mode-reset (PRD-89) each made it
longer.

Local variables include: `processor`, `app_state`, `console`, `workspace`,
`command_bus`, `mode_manager`, `approval_svc`, `_workflow_registry`,
`_agents_registry`, `_skills`, `_project_plugins`, `_mcp_registry`,
`_mention_cache`, `_session_memory`, `_cmd_registry`, `_trigger_registry`,
`agent_runner`, `input_session`, `_pending_skill_body`, `_msg_queue`,
`_agent_task`, `_turn_count`.

Nested closures include: `_dispatch_slash`, `_run_turn`, `_route`, `_advance`,
`_handle_send`, `_handle_interrupt`, `_agent_task_body`, `_on_approval_change`,
`_on_approval_change`, `_on_agent_run_complete`, `_on_approval_change`,
`tick_task`, `proc_task`.

None of these are testable in isolation.  Adding a new workflow feature
requires reading 557 lines to find the right insertion point.

## Goals

- Extract a `SessionContext` dataclass holding all session-scoped singletons.
- Extract a `TUISession` class whose public methods correspond to the nested
  closures.
- `_run_tui_session` becomes a thin factory + `session.run()` call.
- Individual methods are unit-testable.

## Design

### `SessionContext` — all singletons, no logic

```python
@dataclass
class SessionContext:
    # Kernel
    processor:       EventProcessor
    app_state:       AppState
    # Services
    approval_svc:    ApprovalService
    mode_manager:    ModeManager
    command_bus:     CommandBus
    # Registries
    workflow_registry: WorkflowRegistry
    agents_registry:   AgentsRegistry
    cmd_registry:      UnifiedCommandRegistry
    trigger_registry:  TriggerManager
    # Resources
    agent_runner:    AgentRunnerBase
    session_memory:  ShortTermMemory
    mention_cache:   MentionCache
    skills:          dict
    project_plugins: ProjectPlugins
    mcp_registry:    McpToolRegistry | None
    # Config
    cfg:             AgenthiccConfig
    session_id:      str
    model_label:     str
```

### `TUISession` — all behaviour

```python
class TUISession:
    def __init__(self, ctx: SessionContext, workspace: Workspace,
                 input_session: UnifiedInputSession) -> None: ...

    async def run(self) -> None:
        """Main event loop — starts tasks, runs input session, tears down."""

    async def handle_send(self, cmd: SendMessageCommand) -> None:
        """Route user message: slash → command dispatcher, text → agent."""

    async def run_turn(self, text: str) -> None:
        """Dispatch one user message: workflow or direct agent turn."""

    async def agent_task_body(self, text: str) -> None:
        """Wraps run_turn in error handling; advances queue on completion."""

    def advance(self) -> None:
        """Drain _msg_queue: dispatch slash commands, start next agent task."""

    def route(self, msg: str) -> bool:
        """Return True if msg is a slash command and was dispatched."""
```

### `_run_tui_session` becomes thin

```python
async def _run_tui_session(resume_id=None, cli_overrides=None):
    ctx       = await _build_session_context(resume_id, cli_overrides)
    workspace = _build_workspace(ctx)
    session   = TUISession(ctx, workspace, _build_input_session(ctx, workspace))
    await session.run()
```

## File changes

| File | Change |
|---|---|
| `runners/tui_session.py` | Extract `SessionContext`, `TUISession`; reduce to thin factory |
| `runners/session_context.py` | **New** — `SessionContext` dataclass |

## Acceptance criteria

- [ ] `TUISession.handle_send`, `run_turn`, and `advance` are independently unit-testable with a mock `SessionContext`.
- [ ] `_run_tui_session` is ≤ 60 lines.
- [ ] All existing integration and e2e tests pass unchanged.
