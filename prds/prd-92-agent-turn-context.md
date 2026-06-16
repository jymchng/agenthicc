# PRD-92 — AgentTurnContext: Decompose the `_run_agent_turn` God Function

## Problem

`_run_agent_turn` is a 290-line async function with 14 parameters all typed
`Any`.  It handles: tool-registry construction, agent-class creation,
signal-handler registration, `@mention` injection, skill injection, streaming,
token accounting, `output_collector` capture, and error recovery.

Every new PRD adds another parameter.  The function is impossible to test in
isolation, impossible to extend without touching all call sites, and impossible
to subclass or override any single concern.

## Goals

- Replace the monolithic function with a `AgentTurnContext` dataclass (static
  configuration) and an `AgentTurnRunner` class whose `run()` method drives
  the turn.
- Every concern becomes a composable method, testable in isolation.
- `_run_agent_turn` is kept as a thin compatibility shim during migration.
- No behaviour change — identical output to callers.

## Design

### `AgentTurnContext` — pure configuration, no I/O

```python
@dataclass(frozen=True)
class AgentTurnContext:
    text:                str
    runner:              AgentRunnerBase
    processor:           EventProcessor
    session_memory:      ShortTermMemory | None  = None
    max_agent_turns:     int                     = 200
    conv_store:          ConversationStore | None = None
    app_state:           AppState | None         = None
    exec_cfg:            ExecutionSettings | None = None
    skills:              dict                    = field(default_factory=dict)
    mention_cache:       MentionCache | None     = None
    project_plugin_tools: list                  = field(default_factory=list)
    mcp_registry:        Any | None             = None
    active_agent:        str                    = "default"
    completed_turns:     int                    = 0
    approval_svc:        ApprovalService | None = None
    output_collector:    list[str] | None       = None
    system_prompt_suffix: str                  = ""
```

All fields are typed with real types (no `Any`).

### `AgentTurnRunner` — composable execution

```python
class AgentTurnRunner:
    def __init__(self, ctx: AgentTurnContext) -> None: ...

    async def run(self) -> None:
        self._emit_intent_created()
        self._begin_conv_turn()
        self._register_signal_handlers()
        agent_text = await self._inject_mentions()
        await self._inject_skills(agent_text)
        agent_instance = self._build_agent(agent_text)
        await self._stream(agent_instance, agent_text)
        self._emit_intent_complete()

    # Private composable steps — each independently testable
    def _emit_intent_created(self) -> None: ...
    def _begin_conv_turn(self) -> None: ...
    def _register_signal_handlers(self) -> None: ...
    async def _inject_mentions(self) -> str: ...
    async def _inject_skills(self, text: str) -> None: ...
    def _build_agent(self, text: str) -> Any: ...
    async def _stream(self, agent: Any, text: str) -> None: ...
    def _emit_intent_complete(self) -> None: ...
```

### Compatibility shim

```python
async def _run_agent_turn(text, runner, processor, **kwargs) -> None:
    ctx = AgentTurnContext(text=text, runner=runner, processor=processor, **kwargs)
    await AgentTurnRunner(ctx).run()
```

All existing call sites (`tui_session.py`, `workflow/runner.py`) work
unchanged.

## File changes

| File | Change |
|---|---|
| `runners/agent_turn.py` | Introduce `AgentTurnContext` dataclass + `AgentTurnRunner`; `_run_agent_turn` becomes shim |
| `runners/agent_turn_context.py` | **New** — `AgentTurnContext` dataclass (importable separately) |

## Acceptance criteria

- [ ] `AgentTurnContext` carries all 14 parameters with real types (no `Any`).
- [ ] `AgentTurnRunner.run()` produces identical streaming output to the old function.
- [ ] Each private method (`_inject_mentions`, `_build_agent`, etc.) is unit-testable with a mock context.
- [ ] All existing tests pass without modification.
