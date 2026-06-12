---
id: PRD-03
title: "Agent Runtime and Communication Tools"
status: draft
version: 0.1.0
created: 2025-06-01
authors:
  - platform-ai-team
reviewers:
  - backend-lead
  - infra-lead
related_prds:
  - PRD-01  # Application State and Event Bus
  - PRD-02  # Workflow DAG and Scheduler
supersedes: []
tags:
  - agent-runtime
  - communication
  - tools
  - concurrency
---

# PRD-03: Agent Runtime and Communication Tools

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Architecture and Design](#3-architecture-and-design)
4. [Data Structures and Interfaces](#4-data-structures-and-interfaces)
5. [Communication Tool Catalog](#5-communication-tool-catalog)
6. [Implementation Plan](#6-implementation-plan)
7. [Tests](#7-tests)
8. [Configuration Reference](#8-configuration-reference)
9. [Open Questions](#9-open-questions)

---

## 1. Executive Summary

PRD-03 specifies the **Agent Runtime and Communication Layer** — the subsystem that governs how individual agents are created, scheduled, and how they interact with each other and with shared application state. This document covers:

- The `AgentInstance` dataclass and lifecycle states
- The `AgentPool` with idle/busy queue management
- The `Scheduler` that assigns tasks to available agents
- The **reasoning loop** that runs inside each agent and restricts all side-effects to tool calls
- A **built-in communication tool catalog** — twelve tools that agents use as the exclusive mechanism to spawn peers, exchange messages, manage tasks and workflows, read/write memory, publish artifacts, and interact with the user interface

The design enforces a strict principle: **agents never call each other directly and never mutate `AppState` directly**. Every side-effect is expressed as an event emitted on the central event bus and processed by a pure reducer. This makes the system auditable, replayable, and safe under concurrent execution.

Lauren-AI integration points are identified throughout: `AgentRunnerBase`, `SubagentPool`, `AgentMessageBus`, `SignalBus`, `AgentContext`, and the suite of briefing compilers (`BriefCompiler`, `LlmCompiler`, `PassThroughCompiler`, `TemplateCompiler`).

---

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal |
|---|------|
| G-01 | Define a deterministic agent lifecycle: `pending -> starting -> idle -> busy -> terminated` |
| G-02 | Provide an `AgentPool` that tracks idle and busy agents and exposes `acquire` / `release` semantics |
| G-03 | Provide a `Scheduler` that matches pending tasks to idle agents in O(1) amortized time |
| G-04 | Implement the agent reasoning loop as a pure async coroutine that communicates exclusively via tool calls |
| G-05 | Catalog and fully specify all twelve built-in communication tools with parameter tables, permission checks, emitted events, and reducer effects |
| G-06 | Provide runnable pytest unit, integration, and E2E tests |
| G-07 | Document the TOML configuration surface for agent runtime tuning |
| G-08 | Map every component to its lauren-ai counterpart for straightforward implementation |

### 2.2 Non-Goals

| # | Non-Goal |
|---|----------|
| NG-01 | Distributed multi-process scheduling (single-process asyncio model only in v1) |
| NG-02 | Persistent agent memory across full application restarts (covered by PRD-05) |
| NG-03 | UI rendering of agent activity panels (covered by PRD-04) |
| NG-04 | LLM provider abstraction or prompt engineering (uses existing `AgentRunnerBase`) |
| NG-05 | Security sandboxing for `tool_define` dynamically compiled tools (deferred) |

---

## 3. Architecture and Design

### 3.1 High-Level Component Diagram

```
+---------------------------------------------------------------------+
|                         APPLICATION LAYER                           |
|  +--------------+   +-----------------+   +----------------------+ |
|  |   Scheduler  |   |   AgentPool     |   |    AppState (Redux)  | |
|  |              |-->|  idle_queue     |   |  agents: dict        | |
|  |  task_queue  |   |  busy_set       |   |  tasks: dict         | |
|  |              |<--|                 |   |  workflows: dict     | |
|  +--------------+   +--------+--------+   |  memory: MemoryStore | |
|         |                    |            +----------+-----------+ |
|         | assign             | create/               | reduce      |
|         v                    v release               v             |
|  +---------------------------------------------------------------+ |
|  |                      EventBus                                 | |
|  |  AgentSpawnRequest  AgentMessageSent  TaskCreated             | |
|  |  TaskAssigned  WorkflowModified  MemoryWritten                | |
|  |  ArtifactPublished  LogEmitted  UIUpdatePushed                | |
|  |  ToolDefined  HookRegistered                                  | |
|  +---------------------------------------------------------------+ |
|         ^                                                           |
+---------+-----------------------------------------------------------+
          | tool_call results / events
          |
+---------+-----------------------------------------------------------+
|                        AGENT RUNTIME LAYER                          |
|                                                                     |
|  +--------------------------------------------------------------+  |
|  |                    AgentInstance                             |  |
|  |  id  state  config  context  task_queue  result_future      |  |
|  |                                                              |  |
|  |  +--------------------------------------------------------+ |  |
|  |  |               AgentRunnerBase (lauren-ai)              | |  |
|  |  |                                                        | |  |
|  |  |  REASONING LOOP                                        | |  |
|  |  |  +--------------------------------------------------+ | |  |
|  |  |  |  while not done:                                 | | |  |
|  |  |  |    llm_response = await llm.complete(context)    | | |  |
|  |  |  |    for tool_call in llm_response.tool_calls:     | | |  |
|  |  |  |      result = await dispatch_tool(tool_call)     | | |  |
|  |  |  |      context.append(tool_result(result))         | | |  |
|  |  |  |    if llm_response.is_final: done = True         | | |  |
|  |  |  +--------------------------------------------------+ | |  |
|  |  +--------------------------------------------------------+ |  |
|  +--------------------------------------------------------------+  |
|                                                                     |
|  +-------------------------------------------------------------+   |
|  |               SubagentPool  (lauren-ai)                     |   |
|  |  spawns child AgentInstances, routes messages               |   |
|  +-------------------------------------------------------------+   |
+---------------------------------------------------------------------+
```

### 3.2 Agent Lifecycle State Machine

```
                         +----------+
                         | PENDING  |<--------------------------+
                         +----+-----+                           |
                              | Scheduler.assign()              |
                              v                                 |
                         +----------+                           |
                         | STARTING |                           |
                         +----+-----+                           |
                              | asyncio.Task launched           |
                              v                                 |
                 +------------------------+                     |
            +--->|         IDLE           |                     |
            |    +------+-----------------+                     |
            |           | task received                         |
            |           v                                       |
            |    +----------------------+                       |
            |    |         BUSY         |                       |
  task done |    |  (reasoning loop)    |                       |
            +----+------+---------------+                       |
                        | fatal error / max_tasks               |
                        v                                       |
                 +----------------------+                       |
                 |      DRAINING        |-----------------------+
                 +------+---------------+  new tasks queued
                        | queue empty
                        v
                 +----------------------+
                 |     TERMINATED       |
                 +----------------------+
```

### 3.3 Scheduler Assignment Flow

```
Scheduler.tick()
     |
     +- peek task_queue (priority heap by urgency + deadline)
     |
     +- [no tasks] -> sleep(scheduler.poll_interval_ms)
     |
     +- [tasks available, no idle agents]
     |    +- if auto_spawn enabled -> emit AgentSpawnRequest
     |
     +- [tasks + idle agent available]
          +- dequeue task
          +- acquire agent from idle_queue
          +- emit TaskAssigned(task_id, agent_id)
          +- agent.enqueue(task)
```

### 3.4 Tool Dispatch Architecture

```
Agent reasoning loop calls tool "X"
          |
          v
   ToolRegistry.lookup("X")
          |
   +------+---------+
   | Not found      | -> ToolNotFoundError -> tool_result(error=...)
   +------+---------+
          | Found
          v
   PermissionChecker.check(agent.config.permissions, tool.required_permissions)
          |
   +------+---------+
   | Denied         | -> PermissionDeniedError -> tool_result(error=...)
   +------+---------+
          | Allowed
          v
   tool.execute(params, agent_context)
          |
          +- validate params (pydantic model)
          +- build Event(...)
          +- await event_bus.emit(event)   <- reducer updates AppState
          +- return ToolResult(...)
```

---

## 4. Data Structures and Interfaces

### 4.1 AgentInstance

```python
# lauren_ai/_agents/_instance.py

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
from uuid import UUID, uuid4

from lauren_ai._agents._context import AgentContext
from lauren_ai._agents._config import AgentConfig


class AgentState(Enum):
    PENDING    = auto()
    STARTING   = auto()
    IDLE       = auto()
    BUSY       = auto()
    DRAINING   = auto()
    TERMINATED = auto()


@dataclass
class AgentInstance:
    """A single running (or pending) agent within the pool.

    All mutations go through the event bus / reducer; this object is the
    *read* projection of the agent's current state held in AppState.
    """

    id: UUID = field(default_factory=uuid4)
    parent_id: UUID | None = None

    # Runtime state
    state: AgentState = AgentState.PENDING
    config: AgentConfig = field(default_factory=AgentConfig)
    context: AgentContext | None = None

    # Task tracking
    current_task_id: UUID | None = None
    tasks_completed: int = 0
    tasks_failed: int = 0

    # Asyncio handles -- not serialised
    _loop_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _result_future: asyncio.Future[Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def is_available(self) -> bool:
        return self.state == AgentState.IDLE

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass
class AgentConfig:
    """Static configuration for one agent instance."""

    name: str = "unnamed-agent"
    model: str = "claude-opus-4"
    max_tasks: int = -1            # -1 = unlimited
    max_retries: int = 3
    timeout_seconds: float = 300.0
    permissions: set[str] = field(default_factory=set)
    memory_namespaces: list[str] = field(default_factory=list)
    tool_allowlist: list[str] | None = None   # None = all built-ins allowed
    brief_compiler: str = "passthrough"       # passthrough | template | llm
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 4.2 AgentPool

```python
# lauren_ai/_agents/_pool.py

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from lauren_ai._agents._instance import AgentInstance, AgentState


@dataclass
class AgentPool:
    """Manages the lifecycle of all AgentInstance objects.

    Internally maintains:
      - idle_queue: asyncio.Queue  (FIFO for fair scheduling)
      - busy_set:   set[UUID]      (O(1) membership check)
      - all_agents: dict[UUID, AgentInstance]
    """

    _idle_queue: asyncio.Queue[AgentInstance] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _busy_set: set[UUID] = field(default_factory=set, init=False)
    _all_agents: dict[UUID, AgentInstance] = field(
        default_factory=dict, init=False
    )

    async def register(self, agent: AgentInstance) -> None:
        """Add a newly created agent; place it in idle_queue."""
        self._all_agents[agent.id] = agent
        if agent.state == AgentState.IDLE:
            await self._idle_queue.put(agent)

    async def acquire(self) -> AgentInstance:
        """Block until an idle agent is available, then mark it busy."""
        agent = await self._idle_queue.get()
        agent.state = AgentState.BUSY
        self._busy_set.add(agent.id)
        return agent

    async def release(self, agent_id: UUID) -> None:
        """Return agent to idle queue after completing a task."""
        agent = self._all_agents[agent_id]
        if agent.config.max_tasks != -1 and (
            agent.tasks_completed + agent.tasks_failed >= agent.config.max_tasks
        ):
            agent.state = AgentState.DRAINING
        else:
            agent.state = AgentState.IDLE
            self._busy_set.discard(agent_id)
            await self._idle_queue.put(agent)

    def get_agent(self, agent_id: UUID) -> AgentInstance | None:
        return self._all_agents.get(agent_id)

    @property
    def idle_count(self) -> int:
        return self._idle_queue.qsize()

    @property
    def busy_count(self) -> int:
        return len(self._busy_set)

    @property
    def total_count(self) -> int:
        return len(self._all_agents)
```

### 4.3 Scheduler

```python
# lauren_ai/_agents/_scheduler.py

from __future__ import annotations

import asyncio
import heapq
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lauren_ai._agents._pool import AgentPool
from lauren_ai._events import EventBus, TaskAssigned, AgentSpawnRequest


@dataclass(order=True)
class PrioritisedTask:
    priority: int          # lower = higher urgency
    task_id: UUID = field(compare=False)
    payload: Any  = field(compare=False)


@dataclass
class Scheduler:
    pool: AgentPool
    event_bus: EventBus
    auto_spawn: bool = True
    max_agents: int = 8
    poll_interval_ms: int = 50

    _heap: list[PrioritisedTask] = field(default_factory=list, init=False)

    def enqueue_task(self, task_id: UUID, payload: Any, priority: int = 50) -> None:
        heapq.heappush(self._heap, PrioritisedTask(priority, task_id, payload))

    async def tick(self) -> None:
        """Single scheduling iteration -- called in a loop by the runtime."""
        if not self._heap:
            await asyncio.sleep(self.poll_interval_ms / 1000)
            return

        if self.pool.idle_count == 0:
            if self.auto_spawn and self.pool.total_count < self.max_agents:
                await self.event_bus.emit(AgentSpawnRequest())
            await asyncio.sleep(self.poll_interval_ms / 1000)
            return

        pt = heapq.heappop(self._heap)
        agent = await self.pool.acquire()
        await self.event_bus.emit(
            TaskAssigned(task_id=pt.task_id, agent_id=agent.id)
        )
        agent.context.task_queue.put_nowait(pt.payload)

    async def run_forever(self) -> None:
        while True:
            await self.tick()
```

### 4.4 Event Types

```python
# lauren_ai/_events/_agent_events.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AgentSpawnRequest:
    event_id: UUID = field(default_factory=uuid4)
    parent_agent_id: UUID | None = None
    config_override: dict[str, Any] = field(default_factory=dict)
    brief: str = ""
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class AgentMessageSent:
    event_id: UUID = field(default_factory=uuid4)
    sender_id: UUID = field(default=None)
    recipient_id: UUID = field(default=None)
    message_type: str = "generic"
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class TaskCreated:
    event_id: UUID = field(default_factory=uuid4)
    task_id: UUID = field(default_factory=uuid4)
    workflow_id: UUID = field(default=None)
    created_by: UUID = field(default=None)
    description: str = ""
    priority: int = 50
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class TaskAssigned:
    event_id: UUID = field(default_factory=uuid4)
    task_id: UUID = field(default=None)
    agent_id: UUID = field(default=None)
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class WorkflowModified:
    event_id: UUID = field(default_factory=uuid4)
    workflow_id: UUID = field(default=None)
    operation: str = ""       # "add_node" | "remove_node" | "add_edge" | "remove_edge"
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class MemoryWritten:
    event_id: UUID = field(default_factory=uuid4)
    scope: str = "session"    # session | project | global
    key: str = ""
    value: Any = None
    written_by: UUID = field(default=None)
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class ArtifactPublished:
    event_id: UUID = field(default_factory=uuid4)
    artifact_id: UUID = field(default_factory=uuid4)
    task_id: UUID = field(default=None)
    artifact_type: str = "generic"
    uri: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class LogEmitted:
    event_id: UUID = field(default_factory=uuid4)
    level: str = "INFO"
    message: str = ""
    agent_id: UUID | None = None
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class UIUpdatePushed:
    event_id: UUID = field(default_factory=uuid4)
    update_type: str = "message"
    payload: dict[str, Any] = field(default_factory=dict)
    target: str = "broadcast"   # broadcast | specific panel id
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class ToolDefined:
    event_id: UUID = field(default_factory=uuid4)
    tool_name: str = ""
    source_code: str = ""
    defined_by: UUID = field(default=None)
    scope: str = "agent"    # agent | workflow | global
    timestamp: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class HookRegistered:
    event_id: UUID = field(default_factory=uuid4)
    lifecycle_point: str = ""
    handler_ref: str = ""
    registered_by: UUID = field(default=None)
    timestamp: datetime = field(default_factory=_now)
```

### 4.5 Protocol Interfaces

```python
# lauren_ai/_agents/_protocols.py

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class ITool(Protocol):
    name: str
    description: str
    required_permissions: frozenset[str]

    async def execute(
        self,
        params: dict[str, Any],
        context: "IAgentContext",
    ) -> "ToolResult": ...


@runtime_checkable
class IAgentContext(Protocol):
    agent_id: UUID
    agent_run_id: UUID
    permissions: frozenset[str]

    async def emit_event(self, event: Any) -> None: ...
    async def read_memory(self, scope: str, key: str) -> Any: ...


@runtime_checkable
class IScheduler(Protocol):
    async def enqueue_task(self, task_id: UUID, payload: Any, priority: int) -> None: ...
    async def tick(self) -> None: ...


@runtime_checkable
class IAgentPool(Protocol):
    async def acquire(self) -> Any: ...
    async def release(self, agent_id: UUID) -> None: ...
    async def register(self, agent: Any) -> None: ...
```

---

## 5. Communication Tool Catalog

### 5.1 Summary Table

| Tool Name | Category | Required Permissions | Event Emitted | AppState Effect |
|---|---|---|---|---|
| `agent_spawn` | Lifecycle | `agents:spawn` | `AgentSpawnRequest` | Creates `AgentInstance`, starts asyncio.Task |
| `agent_send_message` | Messaging | `agents:message` | `AgentMessageSent` | Appends message to recipient's inbox queue |
| `task_create` | Task Mgmt | `tasks:write` | `TaskCreated` | Adds `Task` to workflow's task map |
| `task_assign` | Task Mgmt | `tasks:assign` | `TaskAssigned` | Sets `task.assigned_agent_id`; pool.acquire() |
| `workflow_modify` | Workflow | `workflows:write` | `WorkflowModified` | Mutates workflow DAG nodes/edges |
| `memory_write` | Memory | `memory:write:<scope>` | `MemoryWritten` | Upserts key in `MemoryStore[scope]` |
| `memory_read` | Memory | `memory:read:<scope>` | *(none)* | Read-only, no state mutation |
| `publish_artifact` | Artifacts | `artifacts:publish` | `ArtifactPublished` | Registers artifact in `ArtifactRegistry` |
| `application_log` | Observability | *(none -- always allowed)* | `LogEmitted` | Appends entry to `LogBuffer` |
| `application_ui_update` | UI | `ui:update` | `UIUpdatePushed` | Pushes message onto `UIUpdateQueue` |
| `tool_define` | Meta | `tools:define` | `ToolDefined` | Compiles + registers tool in `ToolRegistry` |
| `hook_register` | Meta | `hooks:register` | `HookRegistered` | Registers handler in `HookRegistry` |

---

### 5.2 agent_spawn

**Purpose**: Spawn a new child agent, optionally with a different model, config, and initial brief. Returns the new agent's UUID.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `brief` | `str` | Yes | -- | Initial prompt / task description for the child agent |
| `name` | `str` | No | `"child-agent"` | Human-readable name for the agent |
| `model` | `str` | No | inherits from parent | LLM model identifier |
| `permissions` | `list[str]` | No | `[]` | Explicit permission grants for child |
| `tool_allowlist` | `list[str] \| null` | No | `null` | Restrict which tools the child may call |
| `max_tasks` | `int` | No | `-1` | Max tasks before the agent self-terminates |
| `brief_compiler` | `"passthrough" \| "template" \| "llm"` | No | `"passthrough"` | How to compile the brief string |
| `memory_namespaces` | `list[str]` | No | `[]` | Memory scopes accessible to child |
| `metadata` | `dict` | No | `{}` | Arbitrary key-value for telemetry/tooling |

#### Permission Check

```
SpawnPermissions.check(caller_permissions):
    REQUIRE "agents:spawn" in caller_permissions
    IF permissions param supplied:
        REQUIRE each requested_perm is a subset of caller_permissions
        (child cannot escalate beyond parent)
```

#### Event Emitted

```python
AgentSpawnRequest(
    parent_agent_id = context.agent_id,
    config_override = {
        "name": params["name"],
        "model": params.get("model"),
        "permissions": params.get("permissions", []),
        ...
    },
    brief = params["brief"],
)
```

#### Reducer Effect

```python
# lauren_ai/_state/_reducers.py  (agent_spawn handler)

def handle_agent_spawn_request(state: AppState, event: AgentSpawnRequest) -> AppState:
    config = AgentConfig(**event.config_override)
    instance = AgentInstance(
        parent_id=event.parent_agent_id,
        config=config,
        state=AgentState.STARTING,
    )
    new_state = state.with_agent(instance)
    return new_state

# Side-effect (run in effect handler, NOT reducer):
async def effect_agent_spawn_request(event: AgentSpawnRequest, pool: AgentPool) -> None:
    instance = pool.get_agent_by_parent_and_brief(event)  # freshly created above
    loop_task = asyncio.create_task(
        AgentRunnerBase(instance).run_loop(),
        name=f"agent-loop-{instance.id}",
    )
    instance._loop_task = loop_task
    instance.state = AgentState.IDLE
    await pool.register(instance)
```

#### Return Value

```json
{ "agent_id": "<uuid4-string>" }
```

---

### 5.3 agent_send_message

**Purpose**: Deliver a structured message to another agent's inbox. The recipient processes the message on its next reasoning iteration.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `recipient_id` | `str (UUID)` | Yes | -- | Target agent UUID |
| `message_type` | `str` | Yes | -- | Semantic tag, e.g. `"task_result"`, `"clarification_request"` |
| `payload` | `dict` | Yes | -- | Arbitrary JSON-serialisable message body |
| `reply_to` | `str (UUID) \| null` | No | `null` | Message ID this is a reply to |
| `ttl_seconds` | `float \| null` | No | `null` | Drop message if not consumed within TTL |

#### Permission Check

```
REQUIRE "agents:message" in caller_permissions
REQUIRE recipient agent exists and is not TERMINATED
```

#### Event Emitted

```python
AgentMessageSent(
    sender_id    = context.agent_id,
    recipient_id = UUID(params["recipient_id"]),
    message_type = params["message_type"],
    payload      = params["payload"],
)
```

#### Reducer Effect

Appends `AgentMessage` to `AppState.agent_inboxes[recipient_id]`. The recipient's reasoning loop drains its inbox at the start of each iteration.

#### Return Value

```json
{ "message_id": "<uuid4-string>", "delivered": true }
```

---

### 5.4 task_create

**Purpose**: Create a new task node within the current workflow. The task is placed in PENDING state and picked up by the Scheduler on next tick.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `description` | `str` | Yes | -- | Human-readable task description / prompt |
| `workflow_id` | `str (UUID) \| null` | No | current workflow | Workflow to attach the task to |
| `priority` | `int` | No | `50` | 0 = highest, 100 = lowest |
| `depends_on` | `list[str]` | No | `[]` | Task UUIDs that must complete first |
| `tool_context` | `dict` | No | `{}` | Extra context passed into the task's agent |
| `deadline_iso` | `str (ISO 8601) \| null` | No | `null` | Optional deadline for urgency calculation |

#### Permission Check

```
REQUIRE "tasks:write" in caller_permissions
REQUIRE workflow_id resolves to a non-terminated workflow
```

#### Event Emitted

```python
TaskCreated(
    workflow_id  = resolved_workflow_id,
    created_by   = context.agent_id,
    description  = params["description"],
    priority     = params.get("priority", 50),
)
```

#### Reducer Effect

```python
def handle_task_created(state: AppState, event: TaskCreated) -> AppState:
    task = Task(
        id           = event.task_id,
        workflow_id  = event.workflow_id,
        description  = event.description,
        priority     = event.priority,
        state        = TaskState.PENDING,
    )
    return state.with_task(task)
```

`Scheduler.enqueue_task()` is called as a side-effect.

#### Return Value

```json
{ "task_id": "<uuid4-string>", "state": "pending" }
```

---

### 5.5 task_assign

**Purpose**: Explicitly assign an existing PENDING task to a specific agent. Overrides the Scheduler's automatic assignment.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `task_id` | `str (UUID)` | Yes | -- | ID of the task to assign |
| `agent_id` | `str (UUID)` | Yes | -- | ID of the target agent |
| `force` | `bool` | No | `false` | If true, preempt an ongoing task (BUSY state) |

#### Permission Check

```
REQUIRE "tasks:assign" in caller_permissions
REQUIRE task is PENDING (or force=true)
REQUIRE agent exists and is IDLE (or force=true)
```

#### Event Emitted

```python
TaskAssigned(task_id=UUID(params["task_id"]), agent_id=UUID(params["agent_id"]))
```

#### Reducer Effect

Sets `task.assigned_agent_id`, transitions task to `ASSIGNED`. If `force=true`, emits a `TaskPreempted` event first so the current task can checkpoint.

#### Return Value

```json
{ "task_id": "<uuid4>", "agent_id": "<uuid4>", "preempted": false }
```

---

### 5.6 workflow_modify

**Purpose**: Add or remove nodes and edges in the current workflow's DAG at runtime.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `workflow_id` | `str (UUID)` | Yes | -- | Target workflow |
| `operation` | `"add_node" \| "remove_node" \| "add_edge" \| "remove_edge"` | Yes | -- | DAG mutation type |
| `node_id` | `str (UUID) \| null` | Conditional | -- | Required for node operations |
| `node_type` | `str \| null` | Conditional | -- | Required for `add_node` |
| `node_config` | `dict` | No | `{}` | Config for the new node |
| `source_id` | `str (UUID) \| null` | Conditional | -- | Required for edge operations |
| `target_id` | `str (UUID) \| null` | Conditional | -- | Required for edge operations |
| `edge_label` | `str \| null` | No | `null` | Optional semantic label |

#### Permission Check

```
REQUIRE "workflows:write" in caller_permissions
REQUIRE workflow exists and is ACTIVE
REQUIRE operation does not create a cycle (DAG invariant)
```

#### Event Emitted

```python
WorkflowModified(
    workflow_id = UUID(params["workflow_id"]),
    operation   = params["operation"],
    payload     = { ... operation-specific fields ... },
)
```

#### Reducer Effect

```python
def handle_workflow_modified(state: AppState, event: WorkflowModified) -> AppState:
    wf = state.workflows[event.workflow_id]
    updated_wf = wf.apply_operation(event.operation, event.payload)
    return state.with_workflow(updated_wf)
```

Cycle detection runs inside `wf.apply_operation`; raises `WorkflowCycleError` if a cycle would be introduced.

#### Return Value

```json
{ "workflow_id": "<uuid4>", "operation": "add_node", "applied": true }
```

---

### 5.7 memory_write

**Purpose**: Write a value to one of the three memory scopes: `session`, `project`, or `global`.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `scope` | `"session" \| "project" \| "global"` | Yes | -- | Target memory layer |
| `key` | `str` | Yes | -- | Dot-separated namespace key, e.g. `"refactor.plan"` |
| `value` | `any` | Yes | -- | JSON-serialisable value |
| `ttl_seconds` | `float \| null` | No | `null` | Expiry for session-scoped entries |
| `merge` | `bool` | No | `false` | If true and value is a dict, deep-merge instead of overwrite |

#### Permission Check

```
REQUIRE f"memory:write:{scope}" in caller_permissions
global scope additionally REQUIRES "memory:write:global" (elevated)
```

#### Event Emitted

```python
MemoryWritten(
    scope      = params["scope"],
    key        = params["key"],
    value      = params["value"],
    written_by = context.agent_id,
)
```

#### Reducer Effect

Upserts `AppState.memory[scope][key]`. If `merge=True` and both old/new values are dicts, performs recursive merge.

#### Return Value

```json
{ "scope": "session", "key": "refactor.plan", "written": true }
```

---

### 5.8 memory_read

**Purpose**: Read one or more keys from memory. Does not emit an event (read-only).

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `scope` | `"session" \| "project" \| "global"` | Yes | -- | Source memory layer |
| `key` | `str \| null` | No | `null` | Specific key; if null returns all keys in scope |
| `keys` | `list[str] \| null` | No | `null` | Batch read multiple keys |
| `default` | `any` | No | `null` | Value to return if key not found |

#### Permission Check

```
REQUIRE f"memory:read:{scope}" in caller_permissions
```

#### Event Emitted

None. Read operations are not recorded on the event bus to avoid log noise. Observability hooks may intercept at the tool dispatch layer if needed.

#### Reducer Effect

None.

#### Return Value

```json
{
  "scope": "session",
  "results": {
    "refactor.plan": { "...": "..." },
    "another.key": null
  }
}
```

---

### 5.9 publish_artifact

**Purpose**: Register a named artifact (file path, in-memory blob, URL) so other tasks and agents in the same workflow can discover and consume it.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `artifact_type` | `str` | Yes | -- | e.g. `"code"`, `"test_report"`, `"diff"`, `"document"` |
| `uri` | `str` | Yes | -- | File path, `data:` URI, or HTTPS URL |
| `name` | `str` | No | derived from URI | Human-readable artifact name |
| `task_id` | `str (UUID) \| null` | No | current task | Producing task |
| `content_hash` | `str \| null` | No | `null` | SHA-256 for integrity verification |
| `metadata` | `dict` | No | `{}` | Arbitrary key-value pairs for filtering |
| `visibility` | `"task" \| "workflow" \| "global"` | No | `"workflow"` | Scope of availability |

#### Permission Check

```
REQUIRE "artifacts:publish" in caller_permissions
```

#### Event Emitted

```python
ArtifactPublished(
    task_id       = resolved_task_id,
    artifact_type = params["artifact_type"],
    uri           = params["uri"],
    metadata      = params.get("metadata", {}),
)
```

#### Reducer Effect

Inserts `Artifact` record into `AppState.artifact_registry`. Agents can subsequently call `memory_read` or a future `artifact_fetch` tool to retrieve it.

#### Return Value

```json
{ "artifact_id": "<uuid4>", "uri": "...", "visibility": "workflow" }
```

---

### 5.10 application_log

**Purpose**: Emit a structured log entry visible in the TUI log panel and any configured log sinks. Always permitted -- no permission gate.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `level` | `"DEBUG" \| "INFO" \| "WARNING" \| "ERROR" \| "CRITICAL"` | No | `"INFO"` | Log severity |
| `message` | `str` | Yes | -- | Human-readable log message |
| `data` | `dict \| null` | No | `null` | Structured payload attached to the log entry |
| `tags` | `list[str]` | No | `[]` | Free-form tags for filtering |

#### Permission Check

No permission required. Every agent can always log.

#### Event Emitted

```python
LogEmitted(
    level    = params.get("level", "INFO"),
    message  = params["message"],
    agent_id = context.agent_id,
)
```

#### Reducer Effect

Appends `LogEntry` to `AppState.log_buffer` (ring buffer, default capacity 10,000 entries). TUI subscribes to `LogEmitted` for live rendering.

#### Return Value

```json
{ "log_id": "<uuid4>", "level": "INFO", "accepted": true }
```

---

### 5.11 application_ui_update

**Purpose**: Push a custom update message to one or more panels in the TUI. Enables agents to display progress spinners, partial results, and interactive prompts.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `update_type` | `"message" \| "progress" \| "prompt" \| "clear" \| "custom"` | Yes | -- | Nature of the UI update |
| `content` | `str \| dict` | Yes | -- | Human-readable string or rich content dict |
| `target` | `str` | No | `"broadcast"` | Panel ID or `"broadcast"` |
| `priority` | `int` | No | `50` | Higher priority updates may preempt lower ones |
| `ttl_ms` | `int \| null` | No | `null` | Auto-clear after this many milliseconds |

#### Permission Check

```
REQUIRE "ui:update" in caller_permissions
```

#### Event Emitted

```python
UIUpdatePushed(
    update_type = params["update_type"],
    payload     = { "content": params["content"], **extra },
    target      = params.get("target", "broadcast"),
)
```

#### Reducer Effect

Enqueues `UIUpdate` into `AppState.ui_update_queue`. The TUI's render loop drains this queue on each frame tick.

#### Return Value

```json
{ "update_id": "<uuid4>", "target": "broadcast", "queued": true }
```

---

### 5.12 tool_define

**Purpose**: Dynamically compile and register a new tool from Python source at runtime. The new tool becomes available to the defining agent (or wider scope) immediately.

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `tool_name` | `str` | Yes | -- | Unique name for the tool (must be valid Python identifier) |
| `source_code` | `str` | Yes | -- | Python source defining an async `execute(params, context)` function |
| `description` | `str` | Yes | -- | Natural-language description for the LLM |
| `parameters_schema` | `dict` | Yes | -- | JSON Schema for the `params` argument |
| `required_permissions` | `list[str]` | No | `[]` | Permissions this tool will require when called |
| `scope` | `"agent" \| "workflow" \| "global"` | No | `"agent"` | Visibility of the registered tool |

#### Permission Check

```
REQUIRE "tools:define" in caller_permissions
source_code is statically analysed for prohibited imports (os.system, subprocess, etc.)
scope="global" REQUIRES "tools:define:global" (elevated)
```

#### Event Emitted

```python
ToolDefined(
    tool_name   = params["tool_name"],
    source_code = params["source_code"],
    defined_by  = context.agent_id,
    scope       = params.get("scope", "agent"),
)
```

#### Reducer Effect

Compiles source via `compile()` + `exec()` in a restricted namespace, wraps result in `DynamicTool`, registers in `ToolRegistry` under the resolved scope.

#### Return Value

```json
{ "tool_name": "my_custom_tool", "scope": "agent", "registered": true }
```

---

### 5.13 hook_register

**Purpose**: Register a lifecycle hook handler at runtime. Hooks fire at defined lifecycle points (e.g., `before_tool_call`, `after_task_complete`).

#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `lifecycle_point` | `str` | Yes | -- | One of the defined hook points (see below) |
| `handler_ref` | `str` | Yes | -- | Fully-qualified Python callable or previously `tool_define`-d tool name |
| `priority` | `int` | No | `50` | Execution order among multiple hooks at same point |
| `filter_event_types` | `list[str]` | No | `[]` | If non-empty, only fire for matching event types |
| `scope` | `"agent" \| "workflow" \| "global"` | No | `"agent"` | Hook visibility scope |

#### Defined Lifecycle Points

| Point | Fires When |
|---|---|
| `before_tool_call` | Before any tool executes |
| `after_tool_call` | After any tool returns |
| `before_llm_call` | Before LLM completion request |
| `after_llm_call` | After LLM completion response |
| `on_task_start` | When agent picks up a task |
| `on_task_complete` | When agent finishes a task successfully |
| `on_task_error` | When a task throws an unhandled error |
| `on_agent_idle` | When agent transitions to IDLE |
| `on_agent_terminate` | When agent is shutting down |

#### Permission Check

```
REQUIRE "hooks:register" in caller_permissions
scope="global" REQUIRES "hooks:register:global"
handler_ref must resolve to a known callable or registered tool
```

#### Event Emitted

```python
HookRegistered(
    lifecycle_point = params["lifecycle_point"],
    handler_ref     = params["handler_ref"],
    registered_by   = context.agent_id,
)
```

#### Reducer Effect

Inserts `HookEntry` into `AppState.hook_registry[lifecycle_point]`, sorted by priority.

#### Return Value

```json
{ "hook_id": "<uuid4>", "lifecycle_point": "after_tool_call", "registered": true }
```

---

## 6. Implementation Plan

### 6.1 Phase 1 -- Core Runtime (Week 1-2)

| Task | File(s) | Lauren-AI Type | Notes |
|---|---|---|---|
| Implement `AgentInstance` dataclass | `lauren_ai/_agents/_instance.py` | -- | Use `AgentRunnerBase` as execution engine |
| Implement `AgentConfig` dataclass | `lauren_ai/_agents/_config.py` | `SubagentConfig` | Extend `SubagentConfig` with pool-relevant fields |
| Implement `AgentPool` | `lauren_ai/_agents/_pool.py` | `SubagentPool` | Wrap `SubagentPool`, expose `acquire`/`release` |
| Implement `Scheduler` | `lauren_ai/_agents/_scheduler.py` | -- | New component; integrate with `SignalBus` |
| Wire `AgentRunnerBase` into reasoning loop | `lauren_ai/_agents/_runner.py` | `AgentRunnerBase` | Ensure tool dispatch uses `ToolRegistry` |

### 6.2 Phase 2 -- Event Infrastructure (Week 2)

| Task | File(s) | Lauren-AI Type | Notes |
|---|---|---|---|
| Define all 12 event dataclasses | `lauren_ai/_events/_agent_events.py` | -- | Frozen dataclasses; use `uuid4` for IDs |
| Implement event reducers | `lauren_ai/_state/_reducers.py` | -- | Pure functions, no I/O |
| Implement effect handlers | `lauren_ai/_state/_effects.py` | `AgentMessageBus` | Side-effects decouple from reducer |
| Wire `SignalBus` to event bus | `lauren_ai/_signals/__init__.py` | `SignalBus`, `SubagentStarted`, `SubagentCompleted` | Translate signals to events |

### 6.3 Phase 3 -- Tool Catalog (Week 3)

| Task | File(s) | Depends On |
|---|---|---|
| `agent_spawn` + `SpawnPermissions` | `lauren_ai/_tools/agent_spawn.py` | Phase 1, Phase 2 |
| `agent_send_message` | `lauren_ai/_tools/agent_send_message.py` | `AgentMessageBus`, `InMemoryAgentMessageTransport` |
| `task_create`, `task_assign` | `lauren_ai/_tools/task_tools.py` | Phase 2 reducers |
| `workflow_modify` | `lauren_ai/_tools/workflow_modify.py` | Workflow DAG (PRD-02) |
| `memory_write`, `memory_read` | `lauren_ai/_tools/memory_tools.py` | `AgentContext.memory` |
| `publish_artifact` | `lauren_ai/_tools/artifact_tools.py` | Phase 2 events |
| `application_log`, `application_ui_update` | `lauren_ai/_tools/ui_tools.py` | `LogEmitted`, `UIUpdatePushed` |
| `tool_define`, `hook_register` | `lauren_ai/_tools/meta_tools.py` | `ToolRegistry`, `HookRegistry` |

### 6.4 Phase 4 -- Brief Compilers Integration (Week 3-4)

The `agent_spawn` tool delegates brief preparation to one of four compilers, selected by `AgentConfig.brief_compiler`:

| Compiler | Class | When to Use |
|---|---|---|
| `passthrough` | `PassThroughCompiler` | Brief is already a ready prompt string |
| `template` | `TemplateCompiler` | Brief contains `{variable}` placeholders, values from `tool_context` |
| `llm` | `LlmCompiler` | Brief is a high-level intent; LLM expands it into a full agent prompt |
| `structured` | `BriefCompiler` (base) | Structured `BriefSpec` object with sections |

### 6.5 Phase 5 -- ReturnMode and SubagentTool (Week 4)

`ReturnMode` governs how a spawned agent signals completion:

| Mode | Behaviour |
|---|---|
| `SYNC` | `agent_spawn` blocks until child completes; result returned inline |
| `ASYNC` | `agent_spawn` returns `agent_id` immediately; parent polls via `memory_read` or waits for `AgentMessageSent` |
| `FIRE_AND_FORGET` | Child runs independently; no result propagation |

`SubagentTool` from `lauren_ai._subagent` wraps this logic and is used as the backend for the `agent_spawn` tool implementation.

---

## 7. Tests

### 7.1 Unit Tests -- Each Tool with Mock Event Bus

```python
# tests/unit/tools/test_agent_spawn.py

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from lauren_ai._tools.agent_spawn import AgentSpawnTool
from lauren_ai._agents._context import AgentContext
from lauren_ai._events._agent_events import AgentSpawnRequest


class FakeEventBus:
    def __init__(self):
        self.emitted: list = []

    async def emit(self, event) -> None:
        self.emitted.append(event)


@pytest.fixture
def event_bus():
    return FakeEventBus()


@pytest.fixture
def agent_context(event_bus):
    ctx = MagicMock(spec=AgentContext)
    ctx.agent_id = uuid4()
    ctx.permissions = frozenset({"agents:spawn", "agents:message"})
    ctx.emit_event = event_bus.emit
    return ctx


@pytest.mark.asyncio
async def test_agent_spawn_emits_spawn_request(agent_context, event_bus):
    tool = AgentSpawnTool()
    result = await tool.execute(
        params={
            "brief": "Analyse the codebase and report dead functions.",
            "name": "analysis-agent",
            "model": "claude-opus-4",
            "permissions": ["memory:read:session"],
        },
        context=agent_context,
    )

    assert result["agent_id"] is not None
    assert len(event_bus.emitted) == 1
    event = event_bus.emitted[0]
    assert isinstance(event, AgentSpawnRequest)
    assert event.parent_agent_id == agent_context.agent_id
    assert event.brief == "Analyse the codebase and report dead functions."


@pytest.mark.asyncio
async def test_agent_spawn_permission_denied(agent_context, event_bus):
    agent_context.permissions = frozenset()   # no permissions
    tool = AgentSpawnTool()

    with pytest.raises(PermissionError, match="agents:spawn"):
        await tool.execute(
            params={"brief": "Do something."},
            context=agent_context,
        )
    assert len(event_bus.emitted) == 0


@pytest.mark.asyncio
async def test_agent_spawn_child_cannot_escalate_permissions(agent_context, event_bus):
    """Child permissions must be a subset of parent permissions."""
    agent_context.permissions = frozenset({"agents:spawn", "memory:read:session"})
    tool = AgentSpawnTool()

    with pytest.raises(PermissionError, match="escalation"):
        await tool.execute(
            params={
                "brief": "Do something.",
                "permissions": ["memory:write:global"],  # not in parent perms
            },
            context=agent_context,
        )
```

```python
# tests/unit/tools/test_agent_send_message.py

import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from lauren_ai._tools.agent_send_message import AgentSendMessageTool
from lauren_ai._events._agent_events import AgentMessageSent


class FakeEventBus:
    def __init__(self):
        self.emitted = []

    async def emit(self, event):
        self.emitted.append(event)


@pytest.fixture
def ctx_with_perm():
    ctx = MagicMock()
    ctx.agent_id = uuid4()
    ctx.permissions = frozenset({"agents:message"})
    bus = FakeEventBus()
    ctx.emit_event = bus.emit
    return ctx, bus


@pytest.mark.asyncio
async def test_send_message_emits_event(ctx_with_perm):
    ctx, bus = ctx_with_perm
    recipient = uuid4()
    tool = AgentSendMessageTool()

    result = await tool.execute(
        params={
            "recipient_id": str(recipient),
            "message_type": "task_result",
            "payload": {"status": "done", "summary": "Found 3 dead functions."},
        },
        context=ctx,
    )

    assert result["delivered"] is True
    event = bus.emitted[0]
    assert isinstance(event, AgentMessageSent)
    assert event.sender_id == ctx.agent_id
    assert event.recipient_id == recipient
    assert event.message_type == "task_result"


@pytest.mark.asyncio
async def test_send_message_no_permission(ctx_with_perm):
    ctx, _ = ctx_with_perm
    ctx.permissions = frozenset()
    tool = AgentSendMessageTool()

    with pytest.raises(PermissionError):
        await tool.execute(
            params={
                "recipient_id": str(uuid4()),
                "message_type": "ping",
                "payload": {},
            },
            context=ctx,
        )
```

```python
# tests/unit/tools/test_memory_tools.py

import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from lauren_ai._tools.memory_tools import MemoryWriteTool, MemoryReadTool
from lauren_ai._events._agent_events import MemoryWritten


class FakeMemoryStore:
    def __init__(self):
        self._store: dict = {"session": {}, "project": {}, "global": {}}

    def read(self, scope: str, key: str):
        return self._store[scope].get(key)

    def write(self, scope: str, key: str, value):
        self._store[scope][key] = value


class FakeEventBus:
    def __init__(self):
        self.emitted = []

    async def emit(self, event):
        self.emitted.append(event)


@pytest.fixture
def memory_ctx():
    ctx = MagicMock()
    ctx.agent_id = uuid4()
    ctx.permissions = frozenset(
        {"memory:write:session", "memory:read:session", "memory:read:project"}
    )
    store = FakeMemoryStore()
    bus = FakeEventBus()
    ctx.emit_event = bus.emit
    ctx.memory_store = store
    return ctx, store, bus


@pytest.mark.asyncio
async def test_memory_write_session(memory_ctx):
    ctx, store, bus = memory_ctx
    tool = MemoryWriteTool()

    result = await tool.execute(
        params={"scope": "session", "key": "refactor.plan", "value": {"steps": [1, 2]}},
        context=ctx,
    )

    assert result["written"] is True
    assert store.read("session", "refactor.plan") == {"steps": [1, 2]}
    event = bus.emitted[0]
    assert isinstance(event, MemoryWritten)
    assert event.scope == "session"
    assert event.key == "refactor.plan"


@pytest.mark.asyncio
async def test_memory_read_returns_value(memory_ctx):
    ctx, store, bus = memory_ctx
    store.write("session", "my.key", "hello")
    tool = MemoryReadTool()

    result = await tool.execute(
        params={"scope": "session", "key": "my.key"},
        context=ctx,
    )

    assert result["results"]["my.key"] == "hello"
    assert len(bus.emitted) == 0   # reads do NOT emit events


@pytest.mark.asyncio
async def test_memory_write_denied_without_permission(memory_ctx):
    ctx, _, _ = memory_ctx
    ctx.permissions = frozenset()
    tool = MemoryWriteTool()

    with pytest.raises(PermissionError, match="memory:write:session"):
        await tool.execute(
            params={"scope": "session", "key": "x", "value": 1},
            context=ctx,
        )
```

```python
# tests/unit/tools/test_task_tools.py

import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from lauren_ai._tools.task_tools import TaskCreateTool, TaskAssignTool
from lauren_ai._events._agent_events import TaskCreated, TaskAssigned


class FakeEventBus:
    def __init__(self):
        self.emitted = []

    async def emit(self, event):
        self.emitted.append(event)


@pytest.fixture
def task_ctx():
    ctx = MagicMock()
    ctx.agent_id = uuid4()
    ctx.permissions = frozenset({"tasks:write", "tasks:assign"})
    bus = FakeEventBus()
    ctx.emit_event = bus.emit
    ctx.current_workflow_id = uuid4()
    return ctx, bus


@pytest.mark.asyncio
async def test_task_create_emits_task_created(task_ctx):
    ctx, bus = task_ctx
    tool = TaskCreateTool()

    result = await tool.execute(
        params={"description": "Write unit tests for auth module", "priority": 30},
        context=ctx,
    )

    assert result["state"] == "pending"
    assert result["task_id"] is not None
    event = bus.emitted[0]
    assert isinstance(event, TaskCreated)
    assert event.description == "Write unit tests for auth module"
    assert event.priority == 30
    assert event.created_by == ctx.agent_id


@pytest.mark.asyncio
async def test_task_create_default_priority(task_ctx):
    ctx, bus = task_ctx
    tool = TaskCreateTool()

    result = await tool.execute(
        params={"description": "Generic task"},
        context=ctx,
    )

    event = bus.emitted[0]
    assert event.priority == 50


@pytest.mark.asyncio
async def test_task_assign_emits_task_assigned(task_ctx):
    ctx, bus = task_ctx
    task_id = uuid4()
    agent_id = uuid4()
    tool = TaskAssignTool()

    result = await tool.execute(
        params={"task_id": str(task_id), "agent_id": str(agent_id)},
        context=ctx,
    )

    assert result["preempted"] is False
    event = bus.emitted[0]
    assert isinstance(event, TaskAssigned)
    assert event.task_id == task_id
    assert event.agent_id == agent_id
```

```python
# tests/unit/tools/test_meta_tools.py

import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from lauren_ai._tools.meta_tools import ToolDefineTool, HookRegisterTool
from lauren_ai._events._agent_events import ToolDefined, HookRegistered


class FakeEventBus:
    def __init__(self):
        self.emitted = []

    async def emit(self, event):
        self.emitted.append(event)


@pytest.fixture
def meta_ctx():
    ctx = MagicMock()
    ctx.agent_id = uuid4()
    ctx.permissions = frozenset({"tools:define", "hooks:register"})
    bus = FakeEventBus()
    ctx.emit_event = bus.emit
    return ctx, bus


SAFE_TOOL_SOURCE = '''
async def execute(params, context):
    return {"result": params.get("x", 0) * 2}
'''

UNSAFE_TOOL_SOURCE = '''
import subprocess
async def execute(params, context):
    return subprocess.run(["ls"])
'''


@pytest.mark.asyncio
async def test_tool_define_emits_tool_defined(meta_ctx):
    ctx, bus = meta_ctx
    tool = ToolDefineTool()

    result = await tool.execute(
        params={
            "tool_name": "double_x",
            "source_code": SAFE_TOOL_SOURCE,
            "description": "Doubles the value of x",
            "parameters_schema": {
                "type": "object",
                "properties": {"x": {"type": "number"}},
            },
        },
        context=ctx,
    )

    assert result["registered"] is True
    event = bus.emitted[0]
    assert isinstance(event, ToolDefined)
    assert event.tool_name == "double_x"


@pytest.mark.asyncio
async def test_tool_define_rejects_prohibited_imports(meta_ctx):
    ctx, bus = meta_ctx
    tool = ToolDefineTool()

    with pytest.raises(ValueError, match="prohibited"):
        await tool.execute(
            params={
                "tool_name": "evil_tool",
                "source_code": UNSAFE_TOOL_SOURCE,
                "description": "Should be rejected",
                "parameters_schema": {},
            },
            context=ctx,
        )
    assert len(bus.emitted) == 0


@pytest.mark.asyncio
async def test_hook_register_emits_hook_registered(meta_ctx):
    ctx, bus = meta_ctx
    tool = HookRegisterTool()

    result = await tool.execute(
        params={
            "lifecycle_point": "after_tool_call",
            "handler_ref": "my_module:audit_handler",
        },
        context=ctx,
    )

    assert result["registered"] is True
    event = bus.emitted[0]
    assert isinstance(event, HookRegistered)
    assert event.lifecycle_point == "after_tool_call"
    assert event.handler_ref == "my_module:audit_handler"
```

### 7.2 Integration Test -- Parent Spawns Child, Exchanges Message

```python
# tests/integration/test_spawn_and_message_round_trip.py

import asyncio
import pytest
from uuid import uuid4, UUID

from lauren_ai._agents._pool import AgentPool
from lauren_ai._agents._instance import AgentInstance, AgentState, AgentConfig
from lauren_ai._agents._scheduler import Scheduler
from lauren_ai._events import InMemoryEventBus
from lauren_ai._tools.agent_spawn import AgentSpawnTool
from lauren_ai._tools.agent_send_message import AgentSendMessageTool
from lauren_ai._events._agent_events import AgentSpawnRequest, AgentMessageSent
from unittest.mock import MagicMock


@pytest.fixture
def event_bus():
    return InMemoryEventBus()


@pytest.fixture
async def pool():
    return AgentPool()


@pytest.fixture
async def scheduler(pool, event_bus):
    return Scheduler(pool=pool, event_bus=event_bus, auto_spawn=False)


def _make_context(agent_id, permissions, event_bus):
    ctx = MagicMock()
    ctx.agent_id = agent_id
    ctx.permissions = frozenset(permissions)
    ctx.emit_event = event_bus.emit
    return ctx


@pytest.mark.asyncio
async def test_parent_spawns_child_and_sends_message(event_bus, pool, scheduler):
    """
    Scenario:
      1. Parent agent calls agent_spawn
      2. Reducer creates child AgentInstance
      3. Parent calls agent_send_message targeting child
      4. Child's inbox contains the message
    """
    parent_id = uuid4()
    parent_config = AgentConfig(
        name="parent",
        permissions={"agents:spawn", "agents:message", "memory:read:session"},
    )
    parent = AgentInstance(id=parent_id, config=parent_config, state=AgentState.IDLE)
    await pool.register(parent)

    # Step 1: Spawn child
    spawn_tool = AgentSpawnTool(event_bus=event_bus, pool=pool)
    spawn_ctx = _make_context(parent_id, parent_config.permissions, event_bus)

    spawn_result = await spawn_tool.execute(
        params={
            "brief": "Wait for instructions and return result.",
            "name": "child-agent",
            "permissions": ["memory:read:session"],
        },
        context=spawn_ctx,
    )
    child_id_str = spawn_result["agent_id"]

    # Verify AgentSpawnRequest was emitted
    spawn_events = [e for e in event_bus.all_events if isinstance(e, AgentSpawnRequest)]
    assert len(spawn_events) == 1
    assert str(spawn_events[0].parent_agent_id) == str(parent_id)

    # Simulate reducer creating child instance
    child = AgentInstance(
        id=UUID(child_id_str),
        parent_id=parent_id,
        config=AgentConfig(name="child-agent", permissions={"memory:read:session"}),
        state=AgentState.IDLE,
    )
    await pool.register(child)

    # Step 2: Parent sends message to child
    msg_tool = AgentSendMessageTool(event_bus=event_bus, pool=pool)
    msg_result = await msg_tool.execute(
        params={
            "recipient_id": child_id_str,
            "message_type": "instruction",
            "payload": {"action": "analyse_imports", "file": "main.py"},
        },
        context=spawn_ctx,
    )

    assert msg_result["delivered"] is True

    msg_events = [e for e in event_bus.all_events if isinstance(e, AgentMessageSent)]
    assert len(msg_events) == 1
    assert msg_events[0].message_type == "instruction"
    assert msg_events[0].payload["action"] == "analyse_imports"


@pytest.mark.asyncio
async def test_scheduler_assigns_task_to_idle_agent(event_bus, pool, scheduler):
    """Scheduler.tick() should dequeue a task and assign it to the idle agent."""
    agent_id = uuid4()
    task_id = uuid4()
    config = AgentConfig(name="worker", permissions=set())
    agent = AgentInstance(id=agent_id, config=config, state=AgentState.IDLE)
    agent.context = MagicMock()
    agent.context.task_queue = asyncio.Queue()
    await pool.register(agent)

    scheduler.enqueue_task(task_id, {"description": "do something"}, priority=10)
    await scheduler.tick()

    # Agent should be BUSY now
    assert agent.state == AgentState.BUSY

    # TaskAssigned event should be emitted
    from lauren_ai._events._agent_events import TaskAssigned
    assigned_events = [e for e in event_bus.all_events if isinstance(e, TaskAssigned)]
    assert len(assigned_events) == 1
    assert assigned_events[0].task_id == task_id
    assert assigned_events[0].agent_id == agent_id
```

### 7.3 E2E Test -- Delegated Refactor Sub-Agent

```python
# tests/e2e/test_delegated_refactor_subagent.py

"""
End-to-end test: a coordinator agent receives a refactor task, spawns a
child sub-agent with the `agent_spawn` tool, the child produces a diff
artifact via `publish_artifact`, and the coordinator reads it via
`memory_read` after receiving an `agent_send_message` notification.

This test uses a stubbed LLM to avoid real API calls.
"""

import asyncio
import pytest
from uuid import uuid4

from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._agents._instance import AgentInstance, AgentConfig, AgentState
from lauren_ai._agents._pool import AgentPool
from lauren_ai._agents._scheduler import Scheduler
from lauren_ai._events import InMemoryEventBus
from lauren_ai._state import create_initial_state
from lauren_ai._tools import build_default_registry
from lauren_ai._events._agent_events import ArtifactPublished, AgentMessageSent


class StubLLMResponse:
    def __init__(self, tool_calls=None, is_final=False):
        self.tool_calls = tool_calls or []
        self.is_final = is_final or len(self.tool_calls) == 0


class StubLLM:
    """Deterministic LLM stub: returns pre-canned tool call sequences."""

    def __init__(self, call_sequences: list[list[dict]]):
        self._sequences = iter(call_sequences)

    async def complete(self, messages, tools):
        try:
            calls = next(self._sequences)
            return StubLLMResponse(tool_calls=calls)
        except StopIteration:
            return StubLLMResponse(tool_calls=[], is_final=True)


@pytest.fixture
def event_bus():
    return InMemoryEventBus()


@pytest.fixture
def app_state():
    return create_initial_state()


@pytest.mark.asyncio
async def test_coordinator_delegates_refactor(event_bus, app_state):
    """
    Coordinator call sequence:
      turn 1: agent_spawn(brief="refactor main.py")
      turn 2: memory_read(scope="session", key="refactor.diff_uri")
              application_log(message="Refactor complete")

    Child call sequence:
      turn 1: publish_artifact(artifact_type="diff", uri="...", ...)
              memory_write(scope="session", key="refactor.diff_uri", ...)
              agent_send_message(recipient_id=<coordinator>, message_type="task_result")
    """
    pool = AgentPool()
    registry = build_default_registry(
        event_bus=event_bus, pool=pool, app_state=app_state
    )

    coordinator_id = uuid4()
    coordinator_config = AgentConfig(
        name="coordinator",
        permissions={
            "agents:spawn", "agents:message",
            "memory:read:session", "memory:write:session",
            "artifacts:publish", "ui:update",
        },
    )

    child_id = uuid4()
    child_config = AgentConfig(
        name="refactor-agent",
        permissions={
            "agents:message", "artifacts:publish",
            "memory:write:session",
        },
    )

    coordinator_llm = StubLLM([
        # Turn 1: spawn child
        [{"name": "agent_spawn", "params": {
            "brief": "Refactor main.py to use new_code()",
            "name": "refactor-agent",
            "permissions": [
                "agents:message", "artifacts:publish", "memory:write:session"
            ],
        }}],
        # Turn 2: read artifact uri from memory, log completion
        [
            {"name": "memory_read", "params": {
                "scope": "session", "key": "refactor.diff_uri",
            }},
            {"name": "application_log", "params": {
                "level": "INFO",
                "message": "Refactor sub-agent completed.",
            }},
        ],
        # Turn 3: done
        [],
    ])

    child_llm = StubLLM([
        # Turn 1: publish artifact, write memory, notify coordinator
        [
            {"name": "publish_artifact", "params": {
                "artifact_type": "diff",
                "uri": "data:text/plain;base64,LS0t",
                "name": "refactor-main.patch",
                "metadata": {"file": "main.py"},
            }},
            {"name": "memory_write", "params": {
                "scope": "session",
                "key": "refactor.diff_uri",
                "value": "data:text/plain;base64,LS0t",
            }},
            {"name": "agent_send_message", "params": {
                "recipient_id": str(coordinator_id),
                "message_type": "task_result",
                "payload": {"status": "done", "artifact_key": "refactor.diff_uri"},
            }},
        ],
        [],
    ])

    coordinator = AgentInstance(
        id=coordinator_id, config=coordinator_config, state=AgentState.IDLE
    )
    child = AgentInstance(
        id=child_id, parent_id=coordinator_id,
        config=child_config, state=AgentState.IDLE,
    )

    coordinator_runner = AgentRunnerBase(
        instance=coordinator, llm=coordinator_llm,
        tool_registry=registry, event_bus=event_bus,
    )
    child_runner = AgentRunnerBase(
        instance=child, llm=child_llm,
        tool_registry=registry, event_bus=event_bus,
    )

    await pool.register(coordinator)
    await pool.register(child)

    # Run both agents concurrently
    await asyncio.gather(
        coordinator_runner.run_once(task="Refactor main.py"),
        child_runner.run_once(task="Refactor main.py -- child"),
    )

    # Assertions: artifact published
    artifact_events = [
        e for e in event_bus.all_events if isinstance(e, ArtifactPublished)
    ]
    assert len(artifact_events) >= 1
    assert artifact_events[0].artifact_type == "diff"
    assert "refactor-main.patch" in artifact_events[0].metadata.get("name", "")

    # Assertions: task_result message delivered to coordinator
    msg_events = [
        e for e in event_bus.all_events if isinstance(e, AgentMessageSent)
    ]
    result_msgs = [m for m in msg_events if m.message_type == "task_result"]
    assert len(result_msgs) >= 1
    assert result_msgs[0].payload["status"] == "done"
    assert result_msgs[0].recipient_id == coordinator_id
```

---

## 8. Configuration Reference (TOML)

```toml
# lauren_ai.toml  -- Agent Runtime section

[agent_runtime]
# Default model for all agents unless overridden per-instance
default_model = "claude-opus-4"

# Maximum number of concurrent agents in the pool
max_agents = 8

# Automatically spawn new agents when task queue is non-empty and pool is exhausted
auto_spawn = true

# Seconds before a running agent task is cancelled with TimeoutError
agent_timeout_seconds = 300.0

# Maximum tasks an auto-spawned agent will process before self-terminating
# -1 means unlimited
auto_spawn_max_tasks = -1


[agent_runtime.scheduler]
# Scheduling poll interval in milliseconds
poll_interval_ms = 50

# Default task priority for tasks without an explicit priority
default_priority = 50

# Enable deadline-aware priority boosting
deadline_boost = true

# Amount to increase priority per second past deadline (lower = higher urgency)
deadline_boost_rate = 1


[agent_runtime.permissions]
# Permissions granted to all agents by default (cannot be revoked per-agent)
default_permissions = [
  "memory:read:session",
  "memory:write:session",
  "artifacts:publish",
  "tasks:write",
]

# Permissions that require explicit grant (not in default set)
elevated_permissions = [
  "agents:spawn",
  "agents:message",
  "memory:read:project",
  "memory:write:project",
  "memory:read:global",
  "memory:write:global",
  "workflows:write",
  "tools:define",
  "hooks:register",
  "ui:update",
]


[agent_runtime.memory]
# Session memory TTL in seconds (0 = no expiry)
session_ttl_seconds = 0

# Maximum size of session memory store in bytes
session_max_bytes = 10_485_760    # 10 MiB

# Project memory persistence backend: "in_memory" | "sqlite" | "redis"
project_backend = "sqlite"

# Path for SQLite project memory (relative to project root)
project_sqlite_path = ".lauren_ai/memory.db"


[agent_runtime.log_buffer]
# Maximum log entries held in the ring buffer
capacity = 10_000

# Minimum log level written to buffer: DEBUG | INFO | WARNING | ERROR | CRITICAL
min_level = "DEBUG"

# Also write logs to file
file_enabled = true
file_path = ".lauren_ai/logs/agent.log"
file_rotation_mb = 50
file_backup_count = 5


[agent_runtime.tools]
# Source code analysis deny-list for tool_define
# Any import matching these patterns causes ToolDefinitionError
prohibited_imports = [
  "os.system",
  "subprocess",
  "pty",
  "ctypes",
  "cffi",
]

# Maximum size of source code accepted by tool_define (bytes)
tool_define_max_source_bytes = 65_536


[agent_runtime.ui]
# Maximum pending UI updates before oldest are dropped
ui_update_queue_capacity = 256

# TUI refresh rate in Hz
tui_refresh_hz = 10


[agent_runtime.brief_compilers]
# LLM used by LlmCompiler when brief_compiler = "llm"
llm_compiler_model = "claude-haiku-3-5"

# Maximum tokens the LLM compiler can produce for a brief
llm_compiler_max_tokens = 2_048

# Template directory for TemplateCompiler
template_dir = ".lauren_ai/templates/briefs"
```

---

## 9. Open Questions

| # | Question | Owner | Priority | Status |
|---|---|---|---|---|
| OQ-01 | Should `tool_define` run compiled code in a subprocess sandbox (e.g., `restrictedpython` or a `seccomp`-confined subprocess) in v1, or is the import deny-list sufficient for the initial release? | Security lead | High | Open |
| OQ-02 | The `ReturnMode.SYNC` path in `agent_spawn` blocks the caller's reasoning loop. For long-running children this could cause the parent to exceed `agent_timeout_seconds`. Should we enforce a separate `spawn_timeout_seconds`? | Platform | Medium | Open |
| OQ-03 | `memory_read` intentionally does not emit events to reduce bus noise. If we need a full audit trail (e.g., for compliance), should we add an opt-in `audit_reads` config flag? | Compliance | Low | Open |
| OQ-04 | `workflow_modify` can remove nodes that have in-flight tasks. The current spec says the reducer raises an error, but should we instead soft-delete nodes and let in-flight tasks complete? | Product | Medium | Open |
| OQ-05 | The `hook_register` tool references `handler_ref` as a string. For tools defined via `tool_define`, this is unambiguous. For external callables, we need a loading convention (e.g., `module:callable`). Finalise the loading protocol. | Runtime team | Medium | Open |
| OQ-06 | Should the `AgentPool` support agent affinity -- the ability to route tasks to a specific agent by name/tag without going through the Scheduler? | Platform | Low | Open |
| OQ-07 | `application_ui_update` targets a panel by ID string. These IDs are not yet stabilised in PRD-04. Coordinate with the TUI team to lock down the panel ID vocabulary. | TUI team | High | Blocked on PRD-04 |
| OQ-08 | The `Scheduler` is purely FIFO within a priority level. Should we add weighted-fair-queue (WFQ) semantics so that agents with many small tasks do not starve agents with large tasks? | Platform | Low | Open |
| OQ-09 | `publish_artifact` accepts a `data:` URI for in-memory blobs. For large artifacts (e.g., 100 MB diffs) this is impractical. Define a maximum `data:` URI size and a spill-to-disk strategy. | Platform | Medium | Open |
| OQ-10 | Do we need a `agent_cancel` built-in tool to allow a parent to terminate a running child? The current model relies on the child's `max_tasks` limit or `DRAINING` state, which may not be fast enough for interactive workflows. | Product | Medium | Open |
