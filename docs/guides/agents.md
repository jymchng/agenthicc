# Writing Agents

This guide explains how to write agents for agenthicc, how they communicate
with the kernel, and how to coordinate multi-agent workflows using the nine
built-in communication tools.

---

## Core principle: tool-only communication

Agents in agenthicc never touch `AppState` directly.  Every side-effect —
spawning a child agent, creating a task, sending a message, logging output —
is expressed as an event emitted through the kernel's `EventProcessor`.

The communication layer is `CommunicationTools`, a plain Python class whose
async methods are the only write path available to agents.  This design means:

- State mutations are serialised through a single event queue (no races).
- Every action is recorded in the append-only event log (full auditability).
- The same implementations can be wrapped by any tool framework (lauren-ai,
  LangChain, etc.) without changes.

---

## Agent lifecycle

An agent is any Python object that holds a reference to a
`CommunicationTools` instance.  The minimal skeleton:

```python
from agenthicc.runtime.comm_tools import CommunicationTools
from agenthicc.runtime.pool import AgentPool
from agenthicc.kernel import EventProcessor, AppState

class MyAgent:
    def __init__(self, agent_id: str, tools: CommunicationTools) -> None:
        self.agent_id = agent_id
        self.tools = tools

    async def run(self, intent: str) -> None:
        await self.tools.application_log(
            "INFO", f"received intent: {intent}", {"agent_id": self.agent_id}
        )
        # ... do work ...
```

`CommunicationTools` is constructed by the runtime and injected into your
agent.  You do not instantiate it yourself in normal usage.

### How agents connect to the kernel

```
Agent.tools.application_log(...)
    └─> CommunicationTools._emit("ApplicationLog", payload)
            └─> EventProcessor.emit(event)
                    └─> asyncio.Queue  (MPSC)
                            └─> root_reducer(state, event) -> (new_state, effects)
                                    └─> SignalBus.broadcast(new_state)
```

The `SignalBus` bridge notifies any connected lauren-ai `AgentMessageBus`,
enabling real point-to-point delivery in addition to the event log record.

---

## The nine communication tools

### 1. `agent_spawn` — start a child agent

```python
result = await tools.agent_spawn(
    agent_type="researcher",
    config={"model": "claude-opus-4", "max_tokens": 4096},
    parent_agent_id=self.agent_id,
)
child_id = result["agent_id"]
# result: {"agent_id": "<hex>", "agent_type": "researcher"}
```

`agent_type` must match a key registered under `[agents]` in `agenthicc.toml`
or previously registered via `tool_define`.  The new agent's `AgentRecord` is
added to the pool immediately; the `AgentSpawnRequest` event triggers the
runtime's effect executor to actually start the agent coroutine.

### 2. `agent_send_message` — point-to-point messaging

```python
result = await tools.agent_send_message(
    to_agent_id=child_id,
    message={"directive": "search for X"},
    from_agent_id=self.agent_id,
)
# result: {"message_id": "...", "to_agent_id": "...", "delivered": bool}
```

When a `message_bus` is wired into `CommunicationTools`, the message is
delivered via `AgentMessage`; the `AgentMessageSent` event is always written
to the log regardless.  `delivered: False` means the bus had no subscriber for
that agent — the event is still recorded.

### 3. `task_create` — create a workflow task

```python
result = await tools.task_create(
    description="Extract key findings from document.pdf",
    workflow_id=self.workflow_id,
    dependencies=[upstream_node_id],   # optional
    node_id="extract-findings",        # optional; auto-generated if omitted
)
task_id = result["task_id"]
node_id = result["node_id"]
# result: {"task_id": "...", "node_id": "...", "workflow_id": "...", "status": "pending"}
```

This emits two events: `WorkflowNodeAdded` (registers the DAG node) and
`TaskCreated` (registers the task).  Dependency ordering is enforced by the
workflow executor.

### 4. `task_assign` — assign a task to an agent

```python
result = await tools.task_assign(task_id=task_id, agent_id=child_id)
# result: {"task_id": "...", "agent_id": "...", "assigned": True}
```

Emits `TaskAssigned`.  The workflow scheduler uses this assignment to route
results back to the correct agent.

### 5. `workflow_modify` — mutate a running DAG

```python
# Add a node (rejected if it would create a cycle)
result = await tools.workflow_modify(
    workflow_id=self.workflow_id,
    action="add_node",
    node_id="validate-output",
    label="Validate output format",
    dependencies=["extract-findings"],
)

# Remove a pending node
result = await tools.workflow_modify(
    workflow_id=self.workflow_id,
    action="remove_node",
    node_id="optional-step",
)
# result: {"workflow_id": "...", "action": "...", "node_id": "...", "applied": True}
```

`add_node` performs a full cycle-check before emitting `WorkflowNodeAdded`.
`remove_node` is rejected for nodes whose status is `running` or `complete`.

### 6. `application_log` — structured logging

```python
result = await tools.application_log(
    level="INFO",              # DEBUG | INFO | WARNING | ERROR | CRITICAL
    message="Processed 42 records",
    data={"records": 42, "elapsed_ms": 137},
)
# result: {"log_id": "...", "level": "INFO", "accepted": True}
```

Log entries appear in the TUI transcript and are written to the event log.
Use `data` for structured fields that downstream hooks or observability tools
can parse.

### 7. `application_ui_update` — push a TUI update

```python
result = await tools.application_ui_update(
    content={"progress": 0.75, "label": "analysing..."},
    ui_type="progress",
)
# result: {"update_id": "...", "ui_type": "progress", "queued": True}
```

The `UIUpdate` event is consumed by the TUI's transcript model and re-rendered
on the next frame.  `ui_type` is a free-form string; the TUI renders a
`"message"` type as plain text and ignores unknown types gracefully.

### 8. `tool_define` — register a dynamic tool

```python
result = await tools.tool_define(
    name="summarise_file",
    description="Return a 3-bullet summary of a text file.",
    source_code="""
async def summarise_file(path: str) -> dict:
    import pathlib
    text = pathlib.Path(path).read_text()
    # ... call model ...
    return {"summary": ["...", "...", "..."]}
""",
    parameters_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)
# result: {"tool_id": "...", "name": "summarise_file", "registered": True}
```

The source code is compiled (syntax-checked) before the `ToolRegistered`
event is emitted.  The tool becomes available to other agents immediately.

### 9. `hook_register` — register a lifecycle hook at runtime

```python
result = await tools.hook_register(
    entity_type="tool_call",
    stage="before",
    handler_dotpath="myproject.hooks:AuditHook",
)
# result: {"hook_id": "...", "entity_type": "tool_call", "stage": "before", "registered": True}
```

See the [Lifecycle hooks guide](hooks.md) for the full hook ABC and examples.

---

## Spawning sub-agents

A common pattern is an orchestrator that fans out work to parallel workers:

```python
async def fan_out(tools, workflow_id, items):
    worker_ids = []
    for item in items:
        r = await tools.agent_spawn("worker", config={"item": item})
        worker_ids.append(r["agent_id"])

    for wid in worker_ids:
        await tools.agent_send_message(
            to_agent_id=wid,
            message={"action": "process"},
        )

    return worker_ids
```

---

## Sharing data via `application_log` and memory

Agents share structured results through two channels:

**Event log** — call `application_log` with a structured `data` dict.  Any
agent subscribed to the `EventProcessor` via `subscribe()` receives the
`AppState` snapshot after each event.

**Memory router** — use `MemoryRouter.write()` / `MemoryRouter.read()` for
larger payloads or cross-session persistence.  See the [memory guide](memory.md).

---

## Complete worked example: Argon2 refactor orchestrator

This example mirrors the `test_argon2_scenario.py` end-to-end test.  The
orchestrator spawns three parallel agents — a planner, a coder, and a reviewer
— coordinated through a shared workflow.

```python
"""argon2_orchestrator.py — spawn three parallel agents for a refactor task."""

import asyncio
from agenthicc.runtime.comm_tools import CommunicationTools
from agenthicc.kernel import EventProcessor, AppState
from agenthicc.runtime.pool import AgentPool


class Argon2OrchestratorAgent:
    """Orchestrate: plan -> code -> review in parallel where possible."""

    def __init__(self, agent_id: str, tools: CommunicationTools) -> None:
        self.agent_id = agent_id
        self.tools = tools

    async def run(self, workflow_id: str) -> None:
        t = self.tools

        await t.application_log(
            "INFO", "Argon2 refactor orchestration started",
            {"workflow_id": workflow_id},
        )

        # ── Step 1: spawn three specialist agents ─────────────────────────
        planner_r  = await t.agent_spawn("planner",  parent_agent_id=self.agent_id)
        coder_r    = await t.agent_spawn("coder",    parent_agent_id=self.agent_id)
        reviewer_r = await t.agent_spawn("reviewer", parent_agent_id=self.agent_id)

        planner_id  = planner_r["agent_id"]
        coder_id    = coder_r["agent_id"]
        reviewer_id = reviewer_r["agent_id"]

        # ── Step 2: create workflow tasks with dependency ordering ─────────
        plan_task = await t.task_create(
            description="Analyse current password hashing and produce a migration plan",
            workflow_id=workflow_id,
            node_id="plan",
        )
        code_task = await t.task_create(
            description="Implement Argon2id wrapper with backward-compat verify",
            workflow_id=workflow_id,
            dependencies=["plan"],
            node_id="code",
        )
        review_task = await t.task_create(
            description="Security review: timing-safe comparison, param tuning",
            workflow_id=workflow_id,
            dependencies=["code"],
            node_id="review",
        )

        # ── Step 3: assign tasks ───────────────────────────────────────────
        await t.task_assign(plan_task["task_id"],   planner_id)
        await t.task_assign(code_task["task_id"],   coder_id)
        await t.task_assign(review_task["task_id"], reviewer_id)

        # ── Step 4: brief each agent ───────────────────────────────────────
        await t.agent_send_message(
            to_agent_id=planner_id,
            message={"directive": "analyse", "target": "auth/hashing.py"},
            from_agent_id=self.agent_id,
        )
        await t.agent_send_message(
            to_agent_id=coder_id,
            message={"directive": "implement", "spec": "argon2id", "compat": True},
            from_agent_id=self.agent_id,
        )
        await t.agent_send_message(
            to_agent_id=reviewer_id,
            message={"directive": "review", "focus": "security"},
            from_agent_id=self.agent_id,
        )

        await t.application_log(
            "INFO", "All three agents briefed; workflow running",
            {"planner": planner_id, "coder": coder_id, "reviewer": reviewer_id},
        )

        # ── Step 5: dynamically add a docs node once review is in flight ───
        await t.workflow_modify(
            workflow_id=workflow_id,
            action="add_node",
            node_id="docs",
            label="Update CHANGELOG and migration guide",
            dependencies=["review"],
        )

        await t.application_ui_update(
            content="Argon2 refactor workflow fully scheduled.",
            ui_type="message",
        )
```

### Running the orchestrator in a test harness

```python
import asyncio
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.kernel.reducer import root_reducer
from agenthicc.runtime.pool import AgentPool
from agenthicc.runtime.comm_tools import CommunicationTools

async def main():
    state = AppState.create()
    processor = EventProcessor(state, root_reducer, persist=False)
    pool = AgentPool()
    tools = CommunicationTools(processor, pool)

    # Start the event loop task
    runner = asyncio.create_task(processor.run())

    # Emit a workflow creation event first (normally done by the intent planner)
    from agenthicc.kernel.events import Event
    wf_event = Event.create("WorkflowCreated", {
        "workflow_id": "wf-argon2",
        "intent_id": "intent-001",
    })
    await processor.emit(wf_event)
    await processor.drain()

    agent = Argon2OrchestratorAgent("orch-001", tools)
    await agent.run("wf-argon2")
    await processor.drain()

    final_state = processor.get_state()
    print(f"Agents spawned: {len(final_state.agents)}")
    print(f"Tasks created: {len(final_state.tasks)}")

    await processor.stop()
    await runner

asyncio.run(main())
```

---

## Next steps

- [Memory guide](memory.md) — share large payloads across agents and sessions
- [Lifecycle hooks](hooks.md) — intercept spawns, task assignments, and tool calls
- [Kernel reference](../reference/kernel.md) — full event taxonomy
