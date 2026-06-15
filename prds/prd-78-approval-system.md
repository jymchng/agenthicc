# PRD-78 — Tool Approval System

## Background

agenthicc already has two levels of mode-based tool control:

1. **Hard block** (`blocked_capabilities` on `RuntimeMode`) — `ToolCapabilityGate`
   aborts the tool call with a structured error; the model never executes it.
2. **No restriction** — the tool executes immediately.

There is no middle ground: a mode where side-effecting tools are *allowed but
require explicit human approval before each execution*.  This PRD introduces
that middle tier.

---

## Why not use lauren-ai's `requires_confirmation=True`?

`@tool(requires_confirmation=True)` raises `ToolPendingApprovalSignal` inside
`ToolExecutor.execute()` **before** hooks run.  In the Managed Agents runner
this is caught and converted to a session-pause SSE.  In the direct
`AgentRunnerBase.run_stream()` path that agenthicc uses, the signal falls into
the generic `except Exception` handler in `_execute_single_tool` and becomes
a plain tool-error result — the approval pathway is dead.

`runner.approve_tool()` / `runner.reject_tool()` similarly exist only for the
Managed Agents out-of-band approval flow, not for direct streaming.

**Conclusion:** agenthicc's approval system must be built entirely at the
`ToolHook.before_tool_call` layer.  `ToolHook.before_tool_call` is `async`,
so the hook can `await` an `asyncio.Event` without blocking the event loop,
giving the TUI time to render and the user time to respond.

---

## Goals

- A new **Guard** runtime mode where all side-effecting tools pause and ask
  the user before executing.  The user can allow once, allow all of that
  capability for the turn, allow for the session, or deny.
- The `ApprovalOverlay` is shown in the TUI while the agent is paused.  The
  user reads the tool name, capability tag, and truncated args and presses a
  single key.
- After the user responds, the agent resumes in the same asyncio event loop
  tick — no new turns, no re-sending the message.
- `ToolCapabilityGate` (hard block) and `ApprovalGate` (soft block) are
  independent hooks with a defined ordering.  A mode can use either, both, or
  neither for each capability.
- Adding approval to any tool requires only annotating it with
  `@tool_write` (etc.) and setting `approval_required` on the mode — no
  changes to the tool implementation.

---

## Non-goals

- Do not integrate with `requires_confirmation=True` or the Managed Agents
  `approve_tool()` / `reject_tool()` API — those paths are for a different
  runner context.
- Do not add approval UI to the headless runner.
- Do not change `ToolExecutor`, `ToolMeta`, or `@tool()` decorator.
- Do not add approval to commands (slash commands), only to `@tool()`-decorated
  tool functions.

---

## Data model

### `ApprovalRequest`

```python
@dataclass(frozen=True)
class ApprovalRequest:
    tool_name:    str
    tool_use_id:  str
    tool_input:   dict[str, Any]
    capabilities: frozenset[str]   # which ToolCapability values are present
    event:        asyncio.Event    # set by ApprovalService.respond()
```

Carried on `AppState.pending_approval: Signal[ApprovalRequest | None]`.  When
`None`, no approval is pending.  When non-`None`, `Workspace._build()` replaces
the composer with `ApprovalOverlay`.

### `ApprovalResponse`

```python
@dataclass(frozen=True)
class ApprovalResponse:
    allowed:      bool
    remember:     bool = False     # "allow all remaining calls of this capability in this turn"
    remember_all: bool = False     # "allow all remaining calls of this capability in this session"
```

### `RuntimeMode.approval_required`

```python
@dataclass(frozen=True)
class RuntimeMode:
    name:                 str
    badge:                str             = "⏵⏵"
    description:          str             = ""
    system_prompt_suffix: str             = ""
    blocked_capabilities: frozenset[str]  = field(default_factory=frozenset)
    approval_required:    frozenset[str]  = field(default_factory=frozenset)  # NEW
```

### Three-tier capability control per mode

| Tier | Field | Mechanism | User interaction |
|---|---|---|---|
| **Blocked** | `blocked_capabilities` | `ToolCapabilityGate.abort()` | None — model gets error |
| **Approval required** | `approval_required` | `ApprovalGate` + overlay | User presses y/n/a/A |
| **Free** | neither | No hook action | None — tool runs immediately |

### Built-in modes — capability matrix

| Mode | `blocked_capabilities` | `approval_required` | Effective behaviour |
|---|---|---|---|
| Auto | ∅ | ∅ | All tools run immediately. No restrictions. |
| Plan | `{WRITE,GIT_WRITE,EXECUTE,NETWORK}` | ∅ | Side-effecting tools are hard-blocked. Model receives a structured error. No user prompt. Read-only tools run freely. |
| Ask | `{WRITE,GIT_WRITE,EXECUTE,NETWORK}` | ∅ | Same hard-block as Plan. System prompt nudges the model to ask clarifying questions rather than act. Tool blocking is the enforcement layer. |
| Review | `{WRITE,GIT_WRITE,EXECUTE,NETWORK}` | ∅ | Same hard-block as Plan/Ask. System prompt instructs the model to inspect and comment only. Prevents accidental writes during code review. |
| Safe | `{WRITE,GIT_WRITE,EXECUTE,NETWORK}` | ∅ | Most restrictive preset. Hard-blocks all write, execution, and network capabilities. Read-only filesystem and git operations only. |
| Debug | ∅ | ∅ | Full access, identical to Auto for tool execution. System prompt appends a diagnostic footer to every response. |
| **Guard** (new) | ∅ | `{WRITE,GIT_WRITE,EXECUTE,NETWORK}` | All tools are allowed in principle, but every call to a side-effecting tool pauses the agent and shows `ApprovalOverlay`. The user decides per-call, per-capability-class-for-the-turn, or per-capability-class-for-the-session. |

### How each mode uses the approval system

#### Auto and Debug — approval system inactive

`blocked_capabilities = ∅` and `approval_required = ∅`.  Both hooks
(`ToolCapabilityGate` and `ApprovalGate`) return `proceed()` on the first
check without any further work.  Every tool runs immediately.

#### Plan, Ask, Review, Safe — hard-block only, no approval prompt

`blocked_capabilities` covers all side-effecting capabilities.
`ToolCapabilityGate` aborts the call before `ApprovalGate` even runs.
The model receives a machine-readable error:

```json
{"ok": false, "error": "Tool 'write_file' requires write — blocked in Plan mode. Switch to Auto or Guard mode to use this tool."}
```

No `ApprovalOverlay` is shown.  The user is not interrupted.  The hard-block
is the right choice here because these modes are explicitly "read-only" — the
intent is never to allow writes, so asking the user each time would defeat the
purpose.

#### Guard — soft-block with approval overlay

`blocked_capabilities = ∅` (nothing is pre-blocked) and
`approval_required = {WRITE,GIT_WRITE,EXECUTE,NETWORK}`.

Flow for a `write_file` call in Guard mode:

1. `ToolCapabilityGate` checks `write_file`'s capability (`WRITE`) against
   `mode.blocked_capabilities` (empty) → **proceeds**.
2. `ApprovalGate` checks `WRITE` against `mode.approval_required`
   (contains `WRITE`) → **must ask**.
3. Agent task suspends via `await req.event.wait()`.
4. `ApprovalOverlay` appears; user presses `y`, `a`, `A`, or `n`.
5. `ApprovalService.respond()` fires the event; agent resumes.
6. Hook returns `proceed()` or `abort()` depending on the response.

Guard is appropriate when the user wants full agent capability but maintains
explicit oversight over every state-changing action — e.g. supervised code
editing, production deployments, or learning what an agent does step-by-step.

#### Mixing tiers within Guard

Guard can be further customised at runtime.  For example, if the user presses
`a` ("allow all WRITE this turn"), `ApprovalService` records
`WRITE ⊆ _remembered_turn`.  Subsequent `write_file` calls in the same turn
skip the overlay entirely (fast path in `request_approval`).  On the next turn,
`reset_turn_memory()` clears this, and prompting resumes.

If the user presses `A` ("allow all WRITE this session"), the capability is
added to `_remembered_all` and never prompted again for the life of the process.

#### Switching modes mid-session

Because `ApprovalGate` reads `app_state.active_mode()` **at call time** (not
at turn start), a Shift+Tab switch takes effect on the very next tool call:

| Switch | Effect on next tool call |
|---|---|
| Guard → Auto | `approval_required` becomes ∅; `ApprovalGate.proceed()` immediately |
| Auto → Guard | `approval_required` gains `{WRITE,…}`; overlay shown for next write |
| Plan → Guard | `blocked_capabilities` cleared; writes now prompt instead of hard-block |
| Guard → Plan | Writes are now hard-blocked; no overlay shown |

This live responsiveness means the user can downgrade permissions (Auto → Guard)
or upgrade them (Plan → Auto) at any point in a streaming turn, and the change
takes effect before the agent's next tool invocation.

---

## Architecture

### Asyncio rendezvous — why it works without threads

The agent task (`_agent_task_body` → `_run_turn` → `runner.run_stream()`) is a
standard `asyncio.Task`.  `ApprovalGate.before_tool_call()` is `async` and
calls `await req.event.wait()`.  This suspends the coroutine but **does not
block the event loop** — the loop is free to run other tasks.

`UnifiedInputSession.run()` uses `loop.run_in_executor(None, read_key, fd)` to
read keystrokes without blocking.  When the user presses a key the executor
completes, `OverlayHost.handle_key` is called on `ApprovalOverlay`,
`ApprovalService.respond()` sets `req.event`, and the agent task resumes — all
within the same thread and event loop.

```
  asyncio event loop
  ──────────────────────────────────────────────────────
  Agent task                        Input task
  ─────────────────────────────     ──────────────────
  runner.run_stream()
    └─ _execute_single_tool()
         └─ ApprovalGate
              .before_tool_call()
              ┌─ AppState.pending_approval.set(req)
              │    → _redraw() → ApprovalOverlay shown
              └─ await req.event.wait()  ←─────────── user presses y/n
                                                       service.respond()
                                                       req.event.set()
              └─ reads response
              return proceed() or abort()
```

### `ApprovalService`

Single instance per session.  Owned by `tui_session.py`, passed to
`ApprovalGate` and wired to `AppState`.

```python
class ApprovalService:
    def __init__(self, app_state: AppState) -> None: ...

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        """Agent-side: suspend until user responds."""
        # Fast path: already blanket-approved this capability
        if req.capabilities <= self._remembered_all:
            return ApprovalResponse(allowed=True)
        if req.capabilities <= self._remembered_turn:
            return ApprovalResponse(allowed=True)
        # Slow path: show overlay, wait
        self._app_state.pending_approval.set(req)
        await req.event.wait()
        self._app_state.pending_approval.set(None)
        response = self._response
        if response.remember_all:
            self._remembered_all |= req.capabilities
        elif response.remember:
            self._remembered_turn |= req.capabilities
        return response

    def respond(self, allowed: bool, *, remember: bool = False, remember_all: bool = False) -> None:
        """TUI-side (sync): called from ApprovalOverlay.handle_key()."""
        self._response = ApprovalResponse(allowed=allowed, remember=remember, remember_all=remember_all)
        pending = self._app_state.pending_approval()
        if pending is not None:
            pending.event.set()

    def reset_turn_memory(self) -> None:
        """Clear per-turn approvals at the start of each new agent turn."""
        self._remembered_turn = frozenset()
```

### `ApprovalGate` (ToolHook)

```python
class ApprovalGate(ToolHook):
    """Soft-block: pauses tool execution and asks the user.

    Runs after ToolCapabilityGate in the global hook chain.
    If ToolCapabilityGate has already aborted, this hook never fires.
    """

    def __init__(self, app_state: AppState, service: ApprovalService) -> None: ...

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        mode      = self._app_state.active_mode()
        tool_caps = ctx.get_metadata(CAPABILITIES_KEY) or frozenset()
        if not (tool_caps & mode.approval_required):
            return BeforeToolHookDecision.proceed()

        req = ApprovalRequest(
            tool_name=ctx.tool_name,
            tool_use_id=ctx.tool_use_id,
            tool_input=ctx.tool_input,
            capabilities=tool_caps & mode.approval_required,
            event=asyncio.Event(),
        )
        response = await self._service.request_approval(req)
        if response.allowed:
            return BeforeToolHookDecision.proceed()
        return BeforeToolHookDecision.abort({
            "ok":    False,
            "error": f"User denied permission to run '{ctx.tool_name}'.",
        })
```

### Hook ordering in `agent_turn.py`

```python
_global_hooks = [
    ToolCapabilityGate(app_state),          # 1. hard block (no user interaction)
    ApprovalGate(app_state, approval_svc),  # 2. soft block (user decides)
]
_active_runner = AgentRunnerBase(
    transport=runner._transport,
    signals=getattr(runner, "_signals", None),
    global_hooks=_global_hooks,
)
```

`ToolCapabilityGate` fires first.  If it aborts, `ApprovalGate` is never called.

### `ApprovalOverlay`

Replaces the composer in `Workspace._build()` when
`app_state.pending_approval()` is non-`None`.  Same slot as other overlays —
no changes to `OverlayHost`.

```
╔══════════════════════════════════════════════════════════════════╗
║  ⚠  Tool Approval Required                             [WRITE]  ║
║                                                                  ║
║  write_file                                                      ║
║  path: "src/agenthicc/runners/agent_turn.py"                     ║
║  content: "…async def _run_agent_turn(text: str, run…" (432 B)  ║
║                                                                  ║
║  [y] Allow once                                                  ║
║  [a] Allow all WRITE this turn                                   ║
║  [A] Allow all WRITE this session                                ║
║  [n / Esc] Deny                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

Key bindings:
- `y` → `service.respond(True)`
- `a` → `service.respond(True, remember=True)`
- `A` → `service.respond(True, remember_all=True)`
- `n` or Esc → `service.respond(False)`

`ApprovalOverlay` is not a trigger picker; it does not route through
`OverlayHost.handle_key`.  It is managed directly via
`AppState.pending_approval` signal changes.  `Workspace._build()` renders it
whenever the signal is non-`None`, exactly like any other overlay.

---

## `AppState` change

```python
class AppState:
    conversation:       ConversationStore
    input:              InputState
    active_mode:        Signal[RuntimeMode]
    overlay:            Signal[str]
    modal_open:         Signal[bool]
    pending_approval:   Signal[ApprovalRequest | None]   # NEW
```

`Workspace.start()` subscribes `pending_approval` to `_redraw` alongside the
other signals.

---

## File changes

| File | Change |
|---|---|
| `tools/approval.py` | **New** — `ApprovalRequest`, `ApprovalResponse`, `ApprovalService`, `ApprovalGate` |
| `tui/workspace/overlays/approval.py` | **New** — `ApprovalOverlay(Overlay)` |
| `tui/conversation_store.py` | Add `pending_approval: Signal[ApprovalRequest | None]` to `AppState` |
| `tui/runtime/mode_manager.py` | Add `approval_required: frozenset[str]` to `RuntimeMode`; add Guard mode to `build_default_registry()` |
| `tui/workspace/workspace.py` | Subscribe `pending_approval` to `_redraw`; render `ApprovalOverlay` when non-`None` |
| `runners/agent_turn.py` | Add `app_state` and `approval_svc` params; add `ApprovalGate` to `_global_hooks` |
| `runners/tui_session.py` | Instantiate `ApprovalService(app_state)`; pass to `_run_agent_turn`; call `reset_turn_memory()` at turn start |

---

## Acceptance criteria

- [ ] In Guard mode, calling `write_file` pauses the agent and shows `ApprovalOverlay`.
- [ ] `y` allows the call; tool executes; agent continues.
- [ ] `n` / Esc denies; model receives `{"ok": false, "error": "User denied…"}`.
- [ ] `a` allows the call and all subsequent `WRITE` calls in the same turn without prompting.
- [ ] `A` allows all `WRITE` calls for the rest of the session.
- [ ] In Auto mode, `write_file` executes immediately (no overlay, no prompt).
- [ ] In Plan mode, `write_file` is hard-blocked (no overlay — `ToolCapabilityGate` aborts before `ApprovalGate` runs).
- [ ] Shift+Tab from Guard → Auto during streaming: next tool call executes immediately without prompting.
- [ ] The TUI remains fully interactive while waiting for approval (overlay visible, ESC/y/n keys work, agent is paused but not hanging).
- [ ] All existing tests pass.
- [ ] `ApprovalService.reset_turn_memory()` is called at the start of each new agent turn so per-turn blanket approvals do not carry over.
