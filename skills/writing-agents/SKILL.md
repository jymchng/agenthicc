---
skill: writing-agents
version: 1.0.0
tags: [agents, comm-tools, signalbus, kernel, asyncio]
summary: How to write agents integrated with the Agenthicc kernel using CommunicationTools, the SignalBus bridge, and AgentRunnerBase.
---

# Skill: Writing Agents

## When to use this skill

Use this skill when you need to:
- Write a new agent class that integrates with the Agenthicc kernel
- Use `CommunicationTools` to spawn sub-agents, create tasks, or send messages
- Bridge the lauren-ai `AgentRunnerBase` to the Agenthicc event bus
- Read results from sub-agents via `application_log`
- Understand the tool-only communication rule

---

## The tool-only rule

**Agents must never hold direct references to other agent objects or to `AppState`.**
All communication and state mutation must flow through `CommunicationTools`.
This ensures every interaction is a first-class event in the append-only log,
enabling crash recovery, audit, and replay.

```python
# WRONG — direct object reference
other_agent.do_work(task)           # no audit trail, no replay

# CORRECT — tool-only
await tools.agent_send_message(
    to_agent_id=other_agent_id,
    message={"task": task},
    from_agent_id=self.agent_id,
)
```

---

## CommunicationTools usage

All nine methods are async and return a plain `dict`. Import from the runtime:

```python
from agenthicc.runtime.comm_tools import CommunicationTools
```

### agent_spawn — spawn a sub-agent

```python
result = await tools.agent_spawn(
    agent_type="worker",
    config={"model": "claude-3-5-haiku", "max_tokens": 4096},
    parent_agent_id=self.agent_id,
)
sub_agent_id = result["agent_id"]
# result keys: agent_id (str), agent_type (str)
```

### agent_send_message — send a message

```python
result = await tools.agent_send_message(
    to_agent_id=sub_agent_id,
    message={"command": "run_tests", "path": "tests/unit"},
    from_agent_id=self.agent_id,
)
# result keys: message_id (str), to_agent_id (str), delivered (bool)
```

### task_create — create a task in the DAG

```python
result = await tools.task_create(
    description="Write unit tests for AuthService",
    workflow_id=workflow_id,
    dependencies=[],          # list of node_ids this task depends on
)
task_id = result["task_id"]
node_id = result["node_id"]
# result keys: task_id, node_id, workflow_id, status ("pending")
```

### task_assign — assign a task to an agent

```python
result = await tools.task_assign(
    task_id=task_id,
    agent_id=sub_agent_id,
)
# result keys: task_id, agent_id, assigned (True)
```

### workflow_modify — add/remove DAG nodes

```python
# Add a node (cycle check is automatic)
result = await tools.workflow_modify(
    workflow_id=workflow_id,
    action="add_node",
    node_id="node-review",
    label="Review changes",
    dependencies=["node-test", "node-refactor"],
)
# result keys: workflow_id, action, node_id, applied (True)
# Raises ValueError if the addition would create a cycle

# Remove a node (only pending/failed nodes can be removed)
result = await tools.workflow_modify(
    workflow_id=workflow_id,
    action="remove_node",
    node_id="node-obsolete",
)
```

### application_log — structured logging

```python
result = await tools.application_log(
    level="INFO",             # DEBUG | INFO | WARNING | ERROR | CRITICAL
    message="Starting auth refactor",
    data={"intent_id": intent_id, "workflow_id": workflow_id},
)
log_id = result["log_id"]
# result keys: log_id, level, accepted (True)
```

### application_ui_update — push a TUI update

```python
result = await tools.application_ui_update(
    content="Refactoring complete — 4 files changed",
    ui_type="message",
)
# result keys: update_id, ui_type, queued (True)
```

### tool_define — register a dynamic tool

```python
source = """
async def fetch_pr_comments(pr_number: int) -> list:
    import httpx
    r = httpx.get(f"https://api.github.com/repos/org/repo/pulls/{pr_number}/comments")
    return r.json()
"""
result = await tools.tool_define(
    name="fetch_pr_comments",
    description="Fetch comments from a GitHub pull request",
    source_code=source,
    parameters_schema={
        "type": "object",
        "properties": {"pr_number": {"type": "integer"}},
        "required": ["pr_number"],
    },
)
tool_id = result["tool_id"]
# result keys: tool_id, name, registered (True)
# Raises ValueError on invalid identifier or SyntaxError in source_code
```

### hook_register — register a lifecycle hook

```python
result = await tools.hook_register(
    entity_type="file_write",   # tool name or "*" for all tools
    stage="pre_execute",        # pre_execute | post_execute | on_error
    handler_dotpath="myapp.hooks.FileWriteAuditHook",
)
hook_id = result["hook_id"]
# result keys: hook_id, entity_type, stage, registered (True)
```

---

## SignalBus bridge pattern

The `SignalBus` bridge forwards events bidirectionally between the lauren-ai
`AgentMessageBus` and the Agenthicc kernel `EventProcessor`.

```python
from agenthicc.kernel import AppState, EventProcessor, Event
from agenthicc.runtime.comm_tools import CommunicationTools
from agenthicc.runtime.pool import AgentPool

# Build the kernel
state = AppState.create()
processor = EventProcessor(initial_state=state, persist=True)
pool = AgentPool(maxsize=16)

# Build comm tools (with optional lauren-ai message bus)
from lauren_ai._messaging import AgentMessageBus
bus = AgentMessageBus()
tools = CommunicationTools(processor=processor, pool=pool, message_bus=bus)

# Messages sent via agent_send_message will be delivered to the bus
# AND recorded as AgentMessageSent kernel events
```

### Building a runner for an agent

`_build_runner_for_agent` wires a `AgentRunnerBase` to the tools and kernel:

```python
from agenthicc.runtime._build import _build_runner_for_agent

runner = _build_runner_for_agent(
    agent_id="a1b2c3",
    agent_type="worker",
    tools=tools,
    processor=processor,
    model="claude-3-5-haiku-20241022",
)
result = await runner.run_until_done(
    "Write unit tests for AuthService in tests/unit/test_auth.py"
)
```

---

## Complete working example: planner that spawns a sub-agent and reads results

```python
"""planner_agent.py — A planner that spawns a worker and reads its logs."""
from __future__ import annotations

import asyncio
from agenthicc.kernel import AppState, Event, EventProcessor
from agenthicc.runtime.comm_tools import CommunicationTools
from agenthicc.runtime.pool import AgentPool, AgentRecord


class PlannerAgent:
    """Orchestrator that spawns a worker, assigns a task, and reads results."""

    def __init__(
        self,
        agent_id: str,
        tools: CommunicationTools,
        processor: EventProcessor,
    ) -> None:
        self.agent_id = agent_id
        self.tools = tools
        self.processor = processor

    async def run(self, intent_text: str) -> dict:
        # 1. Log that we're starting
        await self.tools.application_log(
            level="INFO",
            message=f"Planner starting for intent: {intent_text!r}",
            data={"agent_id": self.agent_id},
        )

        # 2. Create a workflow (in a real system, workflow_id comes from IntentCreated)
        workflow_id = "wf-demo-001"

        # 3. Create a task in the workflow
        task = await self.tools.task_create(
            description=f"Execute: {intent_text}",
            workflow_id=workflow_id,
            dependencies=[],
        )
        task_id = task["task_id"]
        node_id = task["node_id"]

        # 4. Spawn a worker agent
        worker = await self.tools.agent_spawn(
            agent_type="worker",
            config={"intent": intent_text},
            parent_agent_id=self.agent_id,
        )
        worker_id = worker["agent_id"]

        # 5. Assign the task to the worker
        await self.tools.task_assign(
            task_id=task_id,
            agent_id=worker_id,
        )

        # 6. Send the worker a message with the task details
        await self.tools.agent_send_message(
            to_agent_id=worker_id,
            message={
                "task_id": task_id,
                "node_id": node_id,
                "description": intent_text,
            },
            from_agent_id=self.agent_id,
        )

        # 7. Wait for the worker to complete (poll via drain + state read)
        for _ in range(50):   # max 5 seconds at 100ms intervals
            await self.processor.drain()
            state = self.processor.get_state()
            task_record = state.tasks.get(task_id)
            if task_record and task_record.status.value in ("complete", "failed"):
                break
            await asyncio.sleep(0.1)

        # 8. Read results from the event log
        log_events = [
            e for e in self.processor.event_log
            if e.event_type == "ApplicationLog"
            and e.payload.get("data", {}).get("task_id") == task_id
        ]

        await self.tools.application_ui_update(
            content=f"Task {task_id} done. {len(log_events)} log entries.",
            ui_type="message",
        )

        return {
            "task_id": task_id,
            "worker_id": worker_id,
            "log_count": len(log_events),
        }


async def main():
    state = AppState.create()
    processor = EventProcessor(initial_state=state, persist=False)
    pool = AgentPool(maxsize=8)
    tools = CommunicationTools(processor=processor, pool=pool)

    # Register the planner's agent_id in the pool
    pool.add(AgentRecord(agent_id="planner-001", agent_type="planner"))

    task = asyncio.create_task(processor.run())
    try:
        planner = PlannerAgent(
            agent_id="planner-001",
            tools=tools,
            processor=processor,
        )
        result = await planner.run("refactor the auth module")
        print("Result:", result)
    finally:
        await processor.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `ValueError: unknown workflow <id>` | `workflow_modify` called before `WorkflowCreated` event | Emit `WorkflowCreated` first or use `task_create` which creates the node |
| `ValueError: adding node would create a cycle` | `workflow_modify` add_node cycle | Check `dependencies` — do not create circular chains |
| `ValueError: cannot remove node: status is running` | Removing a running node | Wait for the node to complete or fail before removing |
| `ValueError: tool name is not a valid identifier` | `tool_define` with spaces or special chars in `name` | Use snake_case identifiers |
| `KeyError` in pool.release | `task_assign` to an agent_id not in the busy set | Ensure the agent was acquired from the pool before assigning |

---

## Key points

- **Always use `CommunicationTools`** — never write to `AppState` directly.
- **Call `await processor.drain()`** before reading state after emitting events.
- **All 9 tools return dicts** — check the `"applied"`, `"registered"`, or
  `"accepted"` boolean key to verify success.
- **`workflow_modify` is cycle-guarded** — raises `ValueError` before emitting
  the event if the operation would create a cycle.
- **`application_log` is the read path for sub-agent results** — filter
  `processor.event_log` by `event_type == "ApplicationLog"` and your `task_id`.
- **`agent_spawn` registers in the pool** — the spawned `agent_id` is immediately
  available for `task_assign`.
