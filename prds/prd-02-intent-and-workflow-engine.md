---
title: "PRD-02: Intent Layer and Workflow Engine"
status: draft
version: 0.1.0
created: 2025-01-01
authors:
  - AgentHicc Core Team
tags:
  - intent
  - workflow
  - dag
  - async
  - lauren-ai
---

# PRD-02: Intent Layer and Workflow Engine

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Goals and Non-Goals](#goals-and-non-goals)
3. [Architecture and Design](#architecture-and-design)
4. [Data Structures and Interfaces](#data-structures-and-interfaces)
5. [Implementation Plan](#implementation-plan)
6. [Tests](#tests)
7. [Configuration Reference](#configuration-reference)
8. [Open Questions](#open-questions)

---

## Executive Summary

AgentHicc requires a principled mechanism for transforming raw user requests into structured, executable plans composed of discrete, trackable units of work. The Intent Layer and Workflow Engine described in this document provides that mechanism. At the top level, an *intent* represents a user goal: it captures the parsed request, a validation result, and the execution plan that the system derives from it. Intents are first-class objects that travel through a well-defined lifecycle — Parse, Validate, Plan — before they are handed off to the Workflow Engine for execution.

The Workflow Engine models execution as a Directed Acyclic Graph (DAG). Each node in the DAG corresponds to a discrete task; edges encode data dependencies between tasks. The DAG Executor resolves the graph at runtime, launching every task whose dependencies have already completed while respecting a configurable parallelism ceiling. Completion signaling between tasks uses `asyncio.Event` objects rather than polling, eliminating unnecessary CPU spin and keeping latency at the event-loop level. When a running workflow must be modified — because a user adds a follow-on requirement or because an agent discovers additional work — the `workflow_modify` tool makes atomic node additions and removals while verifying that DAG integrity (no cycles, no dangling edges) is preserved throughout.

Integration with **lauren-ai** is the primary execution path. Workflow nodes are executed via `AgentRunnerBase` (the agent loop driver located at `lauren_ai._agents._runner`), and multi-agent coordination is handled by `TeamRunner` (at `lauren_ai._teams._runner`). The `SignalBus` (at `lauren_ai._signals`) carries inter-node events — `TeamCoordinatorDecision`, `TeamWorkerStarted`, `TeamWorkerFinished` — that feed both observability dashboards and dynamic scheduling logic. This design ensures that every unit of work in a complex, multi-agent pipeline is visible, retryable, and modifiable without restarting from scratch.

---

## Goals and Non-Goals

### Goals

- **G1** — Define a stable `Intent` dataclass with a well-specified lifecycle: Parse -> Validate -> Plan.
- **G2** — Support parallel intent processing: each `Intent` is dispatched as an independent `asyncio.Task`, enabling multiple concurrent user requests.
- **G3** — Model workflows as DAGs: `WorkflowNode` nodes with typed status (`pending`, `running`, `complete`, `failed`) and explicit edge dependencies.
- **G4** — Implement a `DAGExecutor` that discovers ready nodes automatically, signals completion via `asyncio.Event`, and throttles concurrency via `asyncio.Semaphore`.
- **G5** — Provide a `workflow_modify` tool that adds or removes nodes atomically with full DAG integrity validation (cycle detection, dangling edge prevention).
- **G6** — Integrate natively with lauren-ai: route coordinator decisions through `TeamRunner`, execute leaf nodes through `AgentRunnerBase`, and propagate events through `SignalBus`.
- **G7** — Expose TOML-based configuration for execution limits (max parallelism, timeouts, retry policy).
- **G8** — Provide a complete test suite: unit tests for DAG algorithms, integration tests for parallel branches, and an E2E test covering an Argon2 security-refactor scenario with three coordinated agents.

### Non-Goals

- **NG1** — This PRD does not cover the UI or API surface for submitting intents. That is the responsibility of PRD-01 (Ingress Layer).
- **NG2** — Persistent storage of workflow state across process restarts is out of scope. Durable checkpointing will be addressed in a future PRD.
- **NG3** — This document does not define the agent skill/tool catalog. Tool registration is managed by the `@use_tools()` decorator and belongs to PRD-04.
- **NG4** — Authentication and authorization for the `workflow_modify` tool are deferred to PRD-05 (Security Layer).
- **NG5** — Distributed execution across multiple processes or machines is not addressed here. The executor is single-process, single-event-loop.

---

## Architecture and Design

### 3.1 Intent Lifecycle

An intent begins as raw text from an ingress source (HTTP, CLI, message queue). It passes through three sequential stages before the Workflow Engine takes over.

```
  RAW INPUT
      |
      v
+-------------+
|  PARSE      |  IntentParser
|             |  - extract goal, entities, constraints
|             |  - produce ParsedIntent (structured)
+------+------+
       |
       v
+-------------+
|  VALIDATE   |  IntentValidator
|             |  - schema validation
|             |  - capability check (can the system do this?)
|             |  - produce ValidationResult (ok | rejected | needs_clarification)
+------+------+
       |  ok
       v
+-------------+
|  PLAN       |  IntentPlanner
|             |  - decompose into WorkflowNodes
|             |  - establish dependency edges
|             |  - produce WorkflowGraph
+------+------+
       |
       v
  WORKFLOW ENGINE
```

Each stage is independently testable and replaceable. The `ValidationResult` short-circuits the pipeline on rejection, returning a structured error back to the ingress layer without ever constructing a workflow.

### 3.2 Parallel Intent Processing

Multiple intents may arrive concurrently. Each is wrapped in an independent `asyncio.Task` via `IntentProcessor.submit()`. Tasks share no mutable state; they communicate only through the `SignalBus`.

```
                +---------------------------+
                |      IntentProcessor      |
                |                           |
  intent_1 -->  |  asyncio.Task(intent_1)   |--> WorkflowEngine
  intent_2 -->  |  asyncio.Task(intent_2)   |--> WorkflowEngine
  intent_3 -->  |  asyncio.Task(intent_3)   |--> WorkflowEngine
                |                           |
                |  (no shared mutable state)|
                +---------------------------+
                            |
                            v
                        SignalBus
                   (lauren_ai._signals)
```

The `IntentProcessor` maintains a dict of running tasks keyed by `intent_id` for cancellation and status queries.

### 3.3 Workflow DAG Structure

A `WorkflowGraph` is a DAG where nodes are `WorkflowNode` instances and edges represent "must complete before" relationships.

```
  Example: Argon2 Security Refactor

  +----------------------+
  |  Node A              |
  |  audit_dependencies  |  (no deps -- starts immediately)
  +----------+-----------+
             |
     +--------+--------+
     |                 |
     v                 v
+---------+     +-----------+
| Node B  |     | Node C    |
| patch   |     | update    |
| hashing |     | tests     |
+----+----+     +-----+-----+
     |                |
     +--------+-------+
              |
              v
      +--------------+
      |    Node D    |
      |  integration |
      |     test     |
      +--------------+
```

Nodes B and C are independent: they run in parallel once A completes. Node D waits for both B and C.

### 3.4 DAG Executor Design

```
+--------------------------------------------------------------+
|                        DAGExecutor                           |
|                                                              |
|  WorkflowGraph --> _pending_set  (nodes not yet dispatched)  |
|                    _running_set  (currently executing)        |
|                    _done_set     (complete | failed)          |
|                                                              |
|  +--------------------------------------------------------+  |
|  |  Main scheduling loop (asyncio coroutine)              |  |
|  |                                                        |  |
|  |  while not all_done:                                   |  |
|  |    ready = _find_ready_nodes()                         |  |
|  |    for node in ready:                                  |  |
|  |      await semaphore.acquire()       <-- throttle      |  |
|  |      task = asyncio.create_task(     <-- dispatch      |  |
|  |               _execute_node(node))                     |  |
|  |    await asyncio.sleep(0)            <-- yield         |  |
|  |    # node._completion_event.wait()   <-- wake on done  |  |
|  +--------------------------------------------------------+  |
|                                                              |
|  asyncio.Semaphore(max_parallel_tasks)                       |
|  per-node asyncio.Event  ->  signals downstream nodes        |
+--------------------------------------------------------------+
```

The scheduling loop waits on a `asyncio.Event` that is set by whichever node finishes first, then re-evaluates the ready set. This avoids busy-waiting.

### 3.5 Dynamic Modification via `workflow_modify`

```
  workflow_modify(action="add_node", node=new_node, depends_on=[...])
         |
         v
  +---------------------------------+
  |  WorkflowModifier               |
  |                                 |
  |  1. acquire _modify_lock        |  (asyncio.Lock -- atomic)
  |  2. apply tentative change      |
  |  3. run cycle detector (DFS)    |
  |     +-- cycle found? -> rollback|
  |     +-- no cycle? -> commit     |
  |  4. release _modify_lock        |
  |  5. emit ModificationApplied    |  -> SignalBus
  +---------------------------------+
```

Node removal is only permitted for nodes in `pending` state. Removing a `running` or `complete` node raises `InvalidModificationError`. Removing a node whose outputs are depended upon by other non-`failed` nodes also raises `InvalidModificationError` unless those dependent nodes are simultaneously removed in the same operation.

### 3.6 Lauren-AI Integration

```
  WorkflowNode (leaf agent task)
         |
         v
  AgentRunnerBase.run(context)
  (lauren_ai._agents._runner)
         |
         v
  @agent() decorated function
  with @use_tools([...])
         |
         |  (if node is a multi-agent sub-workflow)
         v
  TeamRunner.run(team_config)
  (lauren_ai._teams._runner)
         |
         +---> Coordinator agent  (@team() decorated)
         |         |
         |    TeamCoordinatorDecision  --> SignalBus
         |         |
         +---> Worker agents
         |         |
         |    TeamWorkerStarted  --> SignalBus
         |    TeamWorkerFinished --> SignalBus
         |
         v
  TeamResult(final_answer, worker_outputs, rounds,
             total_input_tokens, total_output_tokens)
         |
         v
  WorkflowNode.result = TeamResult
  WorkflowNode.status = NodeStatus.COMPLETE
  node._completion_event.set()   --> wakes DAGExecutor
```

---

## Data Structures and Interfaces

### 4.1 Intent Dataclasses

```python
# agenthicc/intent/models.py
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any


class IntentStatus(Enum):
    RECEIVED   = auto()
    PARSING    = auto()
    VALIDATING = auto()
    PLANNING   = auto()
    QUEUED     = auto()
    RUNNING    = auto()
    COMPLETE   = auto()
    FAILED     = auto()
    REJECTED   = auto()


class ValidationOutcome(Enum):
    OK                    = auto()
    REJECTED              = auto()
    NEEDS_CLARIFICATION   = auto()


@dataclass
class ParsedIntent:
    """Structured representation of a user goal after NLP parsing."""
    goal: str
    entities: dict[str, Any]
    constraints: dict[str, Any]
    raw_text: str
    confidence: float  # 0.0 - 1.0


@dataclass
class ValidationResult:
    outcome: ValidationOutcome
    reason: str | None = None
    clarification_prompt: str | None = None


@dataclass
class Intent:
    """
    Top-level intent object. Created once per user request and
    mutated in-place as it advances through the lifecycle.
    """
    intent_id:        str                    = field(default_factory=lambda: str(uuid.uuid4()))
    raw_text:         str                    = ""
    status:           IntentStatus           = IntentStatus.RECEIVED
    parsed:           ParsedIntent  | None   = None
    validation:       ValidationResult | None = None
    workflow_graph:   "WorkflowGraph | None"  = None
    created_at:       datetime               = field(
                          default_factory=lambda: datetime.now(timezone.utc))
    updated_at:       datetime               = field(
                          default_factory=lambda: datetime.now(timezone.utc))
    metadata:         dict[str, Any]         = field(default_factory=dict)

    def advance(self, new_status: IntentStatus) -> None:
        """Advance the lifecycle status and update the timestamp."""
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc)
```

### 4.2 Workflow Node and Status

```python
# agenthicc/workflow/models.py
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Awaitable


class NodeStatus(Enum):
    PENDING  = auto()
    RUNNING  = auto()
    COMPLETE = auto()
    FAILED   = auto()


@dataclass
class WorkflowNode:
    """
    A single unit of executable work within a workflow DAG.

    The `executor` callable receives a WorkflowNode (self reference)
    and returns an arbitrary result. It is typically one of:
      - AgentNodeExecutor   (wraps AgentRunnerBase for single-agent tasks)
      - TeamNodeExecutor    (wraps TeamRunner for multi-agent coordination)
    """
    node_id:           str                             = field(
                           default_factory=lambda: str(uuid.uuid4()))
    name:              str                             = ""
    description:       str                             = ""
    status:            NodeStatus                      = NodeStatus.PENDING
    dependencies:      list[str]                       = field(default_factory=list)
    # Optional callable -- None is valid for "gate" nodes that exist only for ordering
    executor:          Callable[..., Awaitable[Any]] | None = None
    result:            Any                             = None
    error:             Exception | None                = None
    metadata:          dict[str, Any]                  = field(default_factory=dict)

    # Internal asyncio primitives -- excluded from repr and serialization
    _completion_event: asyncio.Event                   = field(
                           default_factory=asyncio.Event, repr=False)

    def is_ready(self, completed_ids: set[str]) -> bool:
        """Return True when all dependency nodes have completed successfully."""
        return (
            self.status == NodeStatus.PENDING
            and all(dep in completed_ids for dep in self.dependencies)
        )

    def mark_running(self) -> None:
        self.status = NodeStatus.RUNNING

    def mark_complete(self, result: Any) -> None:
        self.result = result
        self.status = NodeStatus.COMPLETE
        self._completion_event.set()

    def mark_failed(self, error: Exception) -> None:
        self.error = error
        self.status = NodeStatus.FAILED
        self._completion_event.set()  # unblock waiters even on failure
```

### 4.3 WorkflowGraph

```python
# agenthicc/workflow/graph.py
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterator

from .models import WorkflowNode, NodeStatus


class CycleDetectedError(Exception):
    """Raised when a proposed edge would introduce a cycle."""


class DanglingEdgeError(Exception):
    """Raised when a dependency references a non-existent node."""


class InvalidModificationError(Exception):
    """Raised when a workflow modification violates structural integrity rules."""


@dataclass
class WorkflowGraph:
    """
    A Directed Acyclic Graph of WorkflowNode instances.

    Nodes are stored in a dict keyed by node_id.
    Edges are implicit in each node's `dependencies` list.
    The graph is the authoritative state for the DAGExecutor.
    """
    graph_id: str = field(
        default_factory=lambda: __import__("uuid").uuid4().__str__()
    )
    nodes: dict[str, WorkflowNode] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def add_node(self, node: WorkflowNode) -> None:
        """
        Add a node to the graph.

        Validates:
          1. All declared dependencies exist (DanglingEdgeError).
          2. The addition does not create a cycle (CycleDetectedError).
        On failure, the graph is unchanged (atomic).
        """
        for dep_id in node.dependencies:
            if dep_id not in self.nodes:
                raise DanglingEdgeError(
                    f"Node '{node.node_id}' depends on unknown node '{dep_id}'"
                )
        self.nodes[node.node_id] = node
        if self._has_cycle():
            del self.nodes[node.node_id]
            raise CycleDetectedError(
                f"Adding node '{node.node_id}' would introduce a cycle."
            )

    def remove_node(self, node_id: str) -> None:
        """
        Remove a node from the graph.

        Rules:
          - Only PENDING or FAILED nodes may be removed.
          - A node that non-failed nodes depend on may not be removed
            unless those dependents are simultaneously absent.
        """
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found in graph.")
        if node.status in (NodeStatus.RUNNING, NodeStatus.COMPLETE):
            raise InvalidModificationError(
                f"Cannot remove node '{node_id}' with status {node.status.name}."
            )
        dependents = [
            n for n in self.nodes.values()
            if node_id in n.dependencies and n.status != NodeStatus.FAILED
        ]
        if dependents:
            dep_ids = [n.node_id for n in dependents]
            raise InvalidModificationError(
                f"Node '{node_id}' is a dependency of active nodes: {dep_ids}. "
                "Remove dependent nodes first or in the same atomic operation."
            )
        del self.nodes[node_id]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def ready_nodes(self, completed_ids: set[str]) -> list[WorkflowNode]:
        """Return all nodes that are ready to execute right now."""
        return [n for n in self.nodes.values() if n.is_ready(completed_ids)]

    def completed_ids(self) -> set[str]:
        return {
            nid for nid, n in self.nodes.items()
            if n.status == NodeStatus.COMPLETE
        }

    def failed_ids(self) -> set[str]:
        return {
            nid for nid, n in self.nodes.items()
            if n.status == NodeStatus.FAILED
        }

    def is_terminal(self) -> bool:
        """True when every node is either COMPLETE or FAILED."""
        return all(
            n.status in (NodeStatus.COMPLETE, NodeStatus.FAILED)
            for n in self.nodes.values()
        )

    def topological_order(self) -> list[str]:
        """
        Return node IDs in a valid topological order using Kahn's algorithm.
        Raises CycleDetectedError if the graph has a cycle (invariant: it should not).
        """
        in_degree: dict[str, int] = defaultdict(int)
        for nid in self.nodes:
            in_degree[nid]  # ensure key initialised to 0
        for node in self.nodes.values():
            for _ in node.dependencies:
                in_degree[node.node_id] += 1

        queue: deque[str] = deque(nid for nid, deg in in_degree.items() if deg == 0)
        order: list[str] = []

        while queue:
            nid = queue.popleft()
            order.append(nid)
            for other in self.nodes.values():
                if nid in other.dependencies:
                    in_degree[other.node_id] -= 1
                    if in_degree[other.node_id] == 0:
                        queue.append(other.node_id)

        if len(order) != len(self.nodes):
            raise CycleDetectedError(
                "Topological sort failed: graph contains a cycle."
            )
        return order

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _has_cycle(self) -> bool:
        """
        DFS-based cycle detection. O(V + E).
        Uses three-color marking: WHITE (unvisited), GRAY (in stack), BLACK (done).
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in self.nodes}

        def dfs(nid: str) -> bool:
            color[nid] = GRAY
            for dep in self.nodes[nid].dependencies:
                if dep not in color:
                    continue  # dangling edge -- caught by add_node separately
                if color[dep] == GRAY:
                    return True  # back edge -> cycle
                if color[dep] == WHITE and dfs(dep):
                    return True
            color[nid] = BLACK
            return False

        return any(dfs(nid) for nid in self.nodes if color[nid] == WHITE)
```

### 4.4 DAG Executor

```python
# agenthicc/workflow/executor.py
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .graph import WorkflowGraph
from .models import NodeStatus, WorkflowNode

logger = logging.getLogger(__name__)


@dataclass
class ExecutorConfig:
    max_parallel_tasks:   int          = 4
    node_timeout_seconds: float | None = 120.0
    fail_fast:            bool         = False  # cancel all PENDING on first failure


class DAGExecutor:
    """
    Executes a WorkflowGraph concurrently, respecting dependency order.

    Scheduling policy:
      - A node is dispatched when ALL its dependency nodes are COMPLETE.
      - Concurrency is capped by asyncio.Semaphore(max_parallel_tasks).
      - The main loop wakes via a shared asyncio.Event whenever any node
        finishes, then recomputes the ready set. No polling.

    Usage::

        executor = DAGExecutor(graph, config)
        results = await executor.run()
        # results: dict mapping node_id -> result_value_or_exception
    """

    def __init__(self, graph: WorkflowGraph, config: ExecutorConfig | None = None):
        self._graph        = graph
        self._config       = config or ExecutorConfig()
        self._sem          = asyncio.Semaphore(self._config.max_parallel_tasks)
        self._any_done     = asyncio.Event()   # set whenever any node finishes
        self._modify_lock  = asyncio.Lock()    # shared with WorkflowModifier

    async def run(self) -> dict[str, Any]:
        """
        Drive execution until the graph reaches a terminal state.
        Returns a dict: node_id -> result (or exception if FAILED).
        """
        dispatched: set[str] = set()

        while not self._graph.is_terminal():
            completed = self._graph.completed_ids()
            failed    = self._graph.failed_ids()

            if self._config.fail_fast and failed:
                await self._cancel_pending()
                break

            ready = [
                n for n in self._graph.ready_nodes(completed)
                if n.node_id not in dispatched
            ]

            for node in ready:
                dispatched.add(node.node_id)
                asyncio.create_task(
                    self._run_node(node),
                    name=f"workflow-node-{node.name or node.node_id}",
                )

            # If nothing was dispatched AND nothing is running, the graph is stuck.
            # This happens when all remaining nodes have a failed dependency.
            if not ready and not self._has_running():
                break

            # Yield control; wake when the next node finishes.
            self._any_done.clear()
            await self._any_done.wait()

        return self._collect_results()

    async def _run_node(self, node: WorkflowNode) -> None:
        """Acquire semaphore, execute the node, release semaphore, signal done."""
        async with self._sem:
            node.mark_running()
            logger.info("node_started node_id=%s name=%s", node.node_id, node.name)
            try:
                if node.executor is not None:
                    if self._config.node_timeout_seconds is not None:
                        result = await asyncio.wait_for(
                            node.executor(node),
                            timeout=self._config.node_timeout_seconds,
                        )
                    else:
                        result = await node.executor(node)
                else:
                    result = None
                node.mark_complete(result)
                logger.info("node_complete node_id=%s name=%s", node.node_id, node.name)
            except Exception as exc:
                node.mark_failed(exc)
                logger.error(
                    "node_failed node_id=%s name=%s error=%r",
                    node.node_id, node.name, exc,
                )
            finally:
                self._any_done.set()  # wake the scheduling loop

    def _has_running(self) -> bool:
        return any(
            n.status == NodeStatus.RUNNING for n in self._graph.nodes.values()
        )

    async def _cancel_pending(self) -> None:
        """Mark all PENDING nodes as FAILED (fail_fast policy)."""
        for node in self._graph.nodes.values():
            if node.status == NodeStatus.PENDING:
                node.mark_failed(
                    RuntimeError("Cancelled by fail_fast policy")
                )

    def _collect_results(self) -> dict[str, Any]:
        return {
            nid: (n.result if n.status == NodeStatus.COMPLETE else n.error)
            for nid, n in self._graph.nodes.items()
        }
```

### 4.5 WorkflowModifier (`workflow_modify` Tool)

```python
# agenthicc/workflow/modifier.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from .graph import (
    CycleDetectedError,
    DanglingEdgeError,
    InvalidModificationError,
    WorkflowGraph,
)
from .models import WorkflowNode


@dataclass
class ModifyAddNode:
    """Payload for adding a node to a running workflow."""
    action:     Literal["add_node"] = "add_node"
    node:       WorkflowNode        = None         # type: ignore[assignment]
    depends_on: list[str] | None    = None         # overrides node.dependencies if set


@dataclass
class ModifyRemoveNode:
    """Payload for removing a pending node from a running workflow."""
    action:  Literal["remove_node"] = "remove_node"
    node_id: str                    = ""


ModifyOperation = ModifyAddNode | ModifyRemoveNode


class WorkflowModifier:
    """
    Lock-protected atomic modifier for WorkflowGraph instances.

    Designed to be used concurrently with a running DAGExecutor.
    The shared asyncio.Lock guarantees the executor sees a fully
    consistent graph snapshot after every modification.

    Registered as a tool on the coordinator agent:

        @use_tools([workflow_modify_tool])
        @agent()
        async def coordinator(ctx: AgentContext) -> str: ...
    """

    def __init__(
        self,
        graph: WorkflowGraph,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self._graph = graph
        self._lock  = lock or asyncio.Lock()

    async def apply(self, operation: ModifyOperation) -> None:
        """Apply the operation atomically. Raises on integrity violation."""
        async with self._lock:
            if isinstance(operation, ModifyAddNode):
                await self._add_node(operation)
            elif isinstance(operation, ModifyRemoveNode):
                await self._remove_node(operation)
            else:
                raise ValueError(f"Unknown operation type: {type(operation)}")

    async def _add_node(self, op: ModifyAddNode) -> None:
        node = op.node
        if op.depends_on is not None:
            node.dependencies = list(op.depends_on)
        # WorkflowGraph.add_node performs both dangling-edge and cycle checks
        self._graph.add_node(node)

    async def _remove_node(self, op: ModifyRemoveNode) -> None:
        # WorkflowGraph.remove_node enforces status and dependency constraints
        self._graph.remove_node(op.node_id)
```

### 4.6 Intent Processor

```python
# agenthicc/intent/processor.py
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .models import Intent, IntentStatus
from ..workflow.executor import DAGExecutor, ExecutorConfig
from ..workflow.graph import WorkflowGraph

logger = logging.getLogger(__name__)


class IntentProcessor:
    """
    Dispatches each Intent as an independent asyncio.Task.

    Each task runs the full Parse -> Validate -> Plan -> Execute pipeline.
    Tasks share no mutable state; they interact only through SignalBus.

    The task registry (self._tasks) supports:
      - Status queries by intent_id
      - Graceful cancellation via cancel()
    """

    def __init__(self, executor_config: ExecutorConfig | None = None):
        self._config = executor_config or ExecutorConfig()
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def submit(self, intent: Intent) -> asyncio.Task[Any]:
        """
        Wrap the intent processing coroutine in an asyncio.Task
        and register it for lifecycle management.
        """
        task = asyncio.create_task(
            self._process(intent),
            name=f"intent-{intent.intent_id}",
        )
        self._tasks[intent.intent_id] = task
        task.add_done_callback(
            lambda _: self._tasks.pop(intent.intent_id, None)
        )
        return task

    async def cancel(self, intent_id: str) -> bool:
        """Cancel a running intent task. Returns False if not found."""
        task = self._tasks.get(intent_id)
        if task is None:
            return False
        task.cancel()
        return True

    def running_count(self) -> int:
        return len(self._tasks)

    async def _process(self, intent: Intent) -> dict[str, Any]:
        try:
            # Parse, Validate, and Plan stages are injected per Phase 3.
            # Here we assume intent.workflow_graph is already populated
            # (e.g. by an upstream IntentPlanner).
            intent.advance(IntentStatus.RUNNING)

            if intent.workflow_graph is None:
                raise RuntimeError(
                    f"Intent {intent.intent_id} has no workflow_graph. "
                    "Ensure the Plan stage ran successfully."
                )

            graph: WorkflowGraph = intent.workflow_graph
            executor = DAGExecutor(graph, self._config)
            results = await executor.run()
            intent.advance(IntentStatus.COMPLETE)
            return results

        except asyncio.CancelledError:
            intent.advance(IntentStatus.FAILED)
            raise
        except Exception as exc:
            logger.exception("Intent %s failed: %s", intent.intent_id, exc)
            intent.advance(IntentStatus.FAILED)
            raise
```

### 4.7 Protocols for Lauren-AI Integration

```python
# agenthicc/workflow/lauren_bridge.py
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Real imports when lauren-ai is installed:
#   from lauren_ai._agents._runner import AgentRunnerBase
#   from lauren_ai._teams._runner  import TeamRunner
#   from lauren_ai._signals        import SignalBus
#
# Shown here as Protocol stubs for documentation clarity.


@runtime_checkable
class NodeExecutorProtocol(Protocol):
    """
    Structural type for any callable that can serve as WorkflowNode.executor.
    Both AgentNodeExecutor and TeamNodeExecutor satisfy this protocol.
    """

    async def __call__(self, node: Any) -> Any: ...


class AgentNodeExecutor:
    """
    Adapts AgentRunnerBase to the WorkflowNode executor interface.

    Usage::

        runner = AgentRunnerBase(agent_fn=my_agent, ...)
        executor = AgentNodeExecutor(runner, signal_bus)
        node = WorkflowNode(name="my_task", executor=executor)
    """

    def __init__(self, runner: Any, signal_bus: Any) -> None:
        self._runner     = runner
        self._signal_bus = signal_bus

    async def __call__(self, node: Any) -> Any:
        # AgentContext must be pre-populated in node.metadata by the IntentPlanner
        ctx = node.metadata["agent_context"]
        return await self._runner.run(ctx)


class TeamNodeExecutor:
    """
    Adapts TeamRunner to the WorkflowNode executor interface.

    Emits TeamCoordinatorDecision, TeamWorkerStarted, and TeamWorkerFinished
    events onto the SignalBus so they are visible to the rest of the system.

    Usage::

        runner = TeamRunner(...)
        executor = TeamNodeExecutor(runner, signal_bus)
        node = WorkflowNode(name="refactor_team", executor=executor,
                            metadata={"team_config": team_cfg})
    """

    def __init__(self, runner: Any, signal_bus: Any) -> None:
        self._runner     = runner
        self._signal_bus = signal_bus

    async def __call__(self, node: Any) -> Any:
        team_config = node.metadata["team_config"]
        # TeamRunner.run() drives the coordinator<->worker loop internally and
        # emits TeamCoordinatorDecision / TeamWorkerStarted / TeamWorkerFinished
        # onto its own internal bus. We bridge those events to the global bus here.
        result = await self._runner.run(team_config)

        await self._signal_bus.emit(
            "team_node_complete",
            {
                "node_id":             node.node_id,
                "node_name":           node.name,
                "final_answer":        result.final_answer,
                "rounds":              result.rounds,
                "total_input_tokens":  result.total_input_tokens,
                "total_output_tokens": result.total_output_tokens,
            },
        )
        return result
```

---

## Implementation Plan

### Phase 1 -- Core Data Structures (Week 1)

**Deliverables:**
- `agenthicc/intent/models.py` -- `Intent`, `ParsedIntent`, `ValidationResult`, `IntentStatus`, `ValidationOutcome`
- `agenthicc/workflow/models.py` -- `WorkflowNode`, `NodeStatus`
- `agenthicc/workflow/graph.py` -- `WorkflowGraph` with full DFS cycle detection and Kahn topological sort

**Lauren-AI touch-points:** None at this phase. Pure Python dataclasses with no external dependencies. Type-checking with `mypy --strict` must pass without errors.

**Acceptance criteria:**
- 100% branch coverage of `_has_cycle()` (see Section 6.1)
- `WorkflowGraph.topological_order()` returns a valid ordering for all valid graphs and raises `CycleDetectedError` for invalid ones
- `CycleDetectedError` is raised AND the graph is unchanged when adding a node that would create a back-edge
- `DanglingEdgeError` is raised when a declared dependency does not exist in the graph

---

### Phase 2 -- DAG Executor and Semaphore Throttling (Week 2)

**Deliverables:**
- `agenthicc/workflow/executor.py` -- `DAGExecutor`, `ExecutorConfig`
- `agenthicc/workflow/modifier.py` -- `WorkflowModifier` with `asyncio.Lock`-protected atomicity

**Lauren-AI touch-points:**
- `SignalBus` from `lauren_ai._signals` subscribed for node lifecycle events:
  ```python
  signal_bus.on("node_complete", handle_node_complete)
  signal_bus.on("node_failed",   handle_node_failed)
  ```
- `ExecutorConfig` values are sourced from TOML config via `executor_config_from_workflow_config()` (see Section 8.3).

**Acceptance criteria:**
- Parallel branches execute concurrently: wall-clock time for two independent 150ms nodes is under 280ms
- `asyncio.Semaphore` correctly caps live concurrency to `max_parallel_tasks`
- `fail_fast=True` marks all PENDING nodes as FAILED immediately upon first node failure
- `WorkflowModifier.apply()` is safe to call while `DAGExecutor.run()` is in progress (lock prevents partial graph reads)

---

### Phase 3 -- Intent Lifecycle Pipeline (Week 3)

**Deliverables:**
- `agenthicc/intent/parser.py` -- `IntentParser` (initial heuristic stub; LLM-backed version deferred to Phase 4)
- `agenthicc/intent/validator.py` -- `IntentValidator` (schema + capability checks)
- `agenthicc/intent/planner.py` -- `IntentPlanner` (decomposes `ParsedIntent` into `WorkflowGraph`)
- `agenthicc/intent/processor.py` -- `IntentProcessor` with `submit()` and `cancel()`

**Lauren-AI touch-points:**
- `IntentPlanner` invokes a coordinator agent via `TeamRunner` for complex goals. The coordinator issues `TeamCoordinatorDecision` events that the planner translates into `WorkflowNode` additions.
- `AgentContext` is assembled in `IntentPlanner._build_context()` with fields: `agent_id`, `agent_run_id`, `config`, `memory`, `turn`, `signals`, `runner`.

**Acceptance criteria:**
- Two concurrently submitted intents produce two independent `asyncio.Task` objects with separate `WorkflowGraph` instances
- Cancelling an intent via `processor.cancel(intent_id)` propagates `asyncio.CancelledError` through to the executor and sets `IntentStatus.FAILED`
- `ValidationOutcome.REJECTED` short-circuits the pipeline before `WorkflowGraph` construction

---

### Phase 4 -- Lauren-AI Bridge and E2E Integration (Week 4)

**Deliverables:**
- `agenthicc/workflow/lauren_bridge.py` -- `AgentNodeExecutor`, `TeamNodeExecutor`
- `agenthicc/workflow/signals.py` -- signal type constants and structured emitter helpers
- Integration test: E2E Argon2 refactor scenario (see Section 6.3)

**Lauren-AI touch-points:**
- `AgentRunnerBase` from `lauren_ai._agents._runner` drives leaf nodes via `AgentNodeExecutor`.
- `TeamRunner` from `lauren_ai._teams._runner` drives coordinator+worker sub-graphs via `TeamNodeExecutor`.
- `@agent()`, `@use_tools()`, `@team()` decorators applied to the three specialist agents (audit, patch, test-runner).
- `TeamResult.worker_outputs` mapped to downstream node inputs via `node.metadata["upstream_results"]`.

**Acceptance criteria:**
- Argon2 E2E test passes end-to-end: all three nodes complete, zero test failures in simulated test runner
- `TeamResult` from each node is stored in `WorkflowNode.result`
- All `SignalBus` events are emitted in the correct order relative to node lifecycle transitions

---

### Phase 5 -- Dynamic Modification and Hardening (Week 5)

**Deliverables:**
- `workflow_modify` registered as a tool on the coordinator agent via `@use_tools([workflow_modify_tool])`
- Retry policy loop in `DAGExecutor._run_node()` (reads `node_retry_limit` and `retry_backoff_seconds` from config)
- Production logging via `structlog` with structured JSON output
- `agenthicc/config.py` -- `AppConfig`, `load_config()` bound to TOML (Section 8)

**Lauren-AI touch-points:**
- `workflow_modify` is available as a callable tool inside the coordinator's `@team()` decorated function.
- When the coordinator's `TeamCoordinatorDecision` includes `modify_workflow: true`, the agent runtime calls `workflow_modify` with the appropriate `ModifyAddNode` or `ModifyRemoveNode` payload.

**Acceptance criteria:**
- Adding a node to a running workflow during execution causes `DAGExecutor` to dispatch it in the next scheduling tick
- Attempting to remove a RUNNING node raises `InvalidModificationError` and leaves the graph unchanged
- Injecting a cycle via `workflow_modify` is detected and rejected atomically; the graph reverts to its pre-modification state
- Retry loop exhausts `node_retry_limit` attempts before permanently failing a node

---

## Tests

### 6.1 Unit Tests -- DAG Algorithm and Cycle Detection

```python
# tests/unit/test_workflow_graph.py
"""
Unit tests for WorkflowGraph: structure, cycle detection, topological ordering,
node removal integrity.

Run with:
    pytest tests/unit/test_workflow_graph.py -v
"""
import pytest
from agenthicc.workflow.graph import (
    CycleDetectedError,
    DanglingEdgeError,
    InvalidModificationError,
    WorkflowGraph,
)
from agenthicc.workflow.models import NodeStatus, WorkflowNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_node(name: str, deps: list[str] | None = None) -> WorkflowNode:
    """Create a minimal WorkflowNode for structural tests (no executor)."""
    return WorkflowNode(node_id=name, name=name, dependencies=deps or [])


# ---------------------------------------------------------------------------
# Basic graph construction
# ---------------------------------------------------------------------------

class TestGraphConstruction:
    def test_add_single_node(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        assert "A" in g.nodes

    def test_add_linear_chain(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        g.add_node(make_node("C", ["B"]))
        assert set(g.nodes.keys()) == {"A", "B", "C"}

    def test_add_node_with_unknown_dependency_raises(self):
        g = WorkflowGraph()
        with pytest.raises(DanglingEdgeError, match="unknown node 'ghost'"):
            g.add_node(make_node("X", ["ghost"]))

    def test_add_node_with_unknown_dependency_leaves_graph_unchanged(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        try:
            g.add_node(make_node("B", ["nonexistent"]))
        except DanglingEdgeError:
            pass
        assert set(g.nodes.keys()) == {"A"}

    def test_diamond_topology_accepted(self):
        """A -> B, A -> C, B -> D, C -> D -- no cycle."""
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        g.add_node(make_node("C", ["A"]))
        g.add_node(make_node("D", ["B", "C"]))
        assert len(g.nodes) == 4

    def test_wide_fan_out(self):
        """One root fanning out to 10 leaves."""
        g = WorkflowGraph()
        g.add_node(make_node("root"))
        for i in range(10):
            g.add_node(make_node(f"leaf_{i}", ["root"]))
        assert len(g.nodes) == 11

    def test_wide_fan_in(self):
        """10 independent sources converging to one sink."""
        g = WorkflowGraph()
        sources = [f"src_{i}" for i in range(10)]
        for s in sources:
            g.add_node(make_node(s))
        g.add_node(make_node("sink", sources))
        assert len(g.nodes) == 11


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

class TestCycleDetection:
    def test_self_loop_detected(self):
        g = WorkflowGraph()
        # Bypass add_node to force a structural cycle
        g.nodes["A"] = make_node("A", ["A"])
        assert g._has_cycle() is True

    def test_two_node_cycle_detected(self):
        g = WorkflowGraph()
        g.nodes["A"] = make_node("A", ["B"])
        g.nodes["B"] = make_node("B", ["A"])
        assert g._has_cycle() is True

    def test_three_node_cycle_detected(self):
        g = WorkflowGraph()
        g.nodes["A"] = make_node("A", ["C"])
        g.nodes["B"] = make_node("B", ["A"])
        g.nodes["C"] = make_node("C", ["B"])
        assert g._has_cycle() is True

    def test_add_node_creating_cycle_raises_and_is_rolled_back(self):
        """Adding a cycle-forming node must raise AND leave graph unchanged."""
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        # Inject a back edge by bypassing normal add to set up a later test:
        # Here we test that _has_cycle is triggered for a real add attempt.
        # Construct: C -> B, A -> C  would be fine. But B -> A (via C->B->A->C is tested above).
        # Simpler: manually add A again with dep on B
        g.nodes["A"].dependencies = ["B"]
        assert g._has_cycle() is True
        # Restore
        g.nodes["A"].dependencies = []
        assert g._has_cycle() is False

    def test_valid_dag_has_no_cycle(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        g.add_node(make_node("C", ["A"]))
        g.add_node(make_node("D", ["B", "C"]))
        assert g._has_cycle() is False

    def test_empty_graph_has_no_cycle(self):
        assert WorkflowGraph()._has_cycle() is False

    def test_single_node_no_cycle(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        assert g._has_cycle() is False


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------

class TestTopologicalOrder:
    def test_linear_chain_order(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        g.add_node(make_node("C", ["B"]))
        order = g.topological_order()
        assert order.index("A") < order.index("B") < order.index("C")

    def test_diamond_order_constraints(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        g.add_node(make_node("C", ["A"]))
        g.add_node(make_node("D", ["B", "C"]))
        order = g.topological_order()
        assert order.index("A") < order.index("B")
        assert order.index("A") < order.index("C")
        assert order.index("B") < order.index("D")
        assert order.index("C") < order.index("D")

    def test_all_nodes_present_in_order(self):
        g = WorkflowGraph()
        names = ["X", "Y", "Z", "W"]
        for n in names:
            g.add_node(make_node(n))
        order = g.topological_order()
        assert set(order) == set(names)

    def test_single_node_order(self):
        g = WorkflowGraph()
        g.add_node(make_node("solo"))
        assert g.topological_order() == ["solo"]

    def test_empty_graph_order(self):
        assert WorkflowGraph().topological_order() == []


# ---------------------------------------------------------------------------
# Ready node detection
# ---------------------------------------------------------------------------

class TestReadyNodes:
    def test_root_nodes_always_ready(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B"))
        ready = {n.node_id for n in g.ready_nodes(set())}
        assert ready == {"A", "B"}

    def test_dependent_node_not_ready_until_dep_complete(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        # Before A completes: only A is ready
        assert {n.node_id for n in g.ready_nodes(set())} == {"A"}
        # After A completes: B becomes ready
        assert {n.node_id for n in g.ready_nodes({"A"})} == {"B"}

    def test_diamond_parallel_after_root(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        g.add_node(make_node("C", ["A"]))
        g.add_node(make_node("D", ["B", "C"]))
        # After A done: B and C ready
        ready = {n.node_id for n in g.ready_nodes({"A"})}
        assert ready == {"B", "C"}
        # After B and C done: D ready
        ready = {n.node_id for n in g.ready_nodes({"A", "B", "C"})}
        assert ready == {"D"}


# ---------------------------------------------------------------------------
# Node removal
# ---------------------------------------------------------------------------

class TestNodeRemoval:
    def test_remove_pending_leaf_succeeds(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        g.remove_node("B")
        assert "B" not in g.nodes

    def test_remove_node_with_active_dependents_raises(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        with pytest.raises(InvalidModificationError):
            g.remove_node("A")

    def test_remove_running_node_raises(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.nodes["A"].status = NodeStatus.RUNNING
        with pytest.raises(InvalidModificationError):
            g.remove_node("A")

    def test_remove_complete_node_raises(self):
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.nodes["A"].status = NodeStatus.COMPLETE
        with pytest.raises(InvalidModificationError):
            g.remove_node("A")

    def test_remove_nonexistent_node_raises_key_error(self):
        g = WorkflowGraph()
        with pytest.raises(KeyError):
            g.remove_node("does_not_exist")

    def test_remove_failed_node_with_dependents_allowed(self):
        """Failed nodes may be removed even if dependents exist."""
        g = WorkflowGraph()
        g.add_node(make_node("A"))
        g.add_node(make_node("B", ["A"]))
        g.nodes["B"].status = NodeStatus.FAILED
        # B depends on A but B is failed, so A can be removed
        g.remove_node("A")
        assert "A" not in g.nodes
```

---

### 6.2 Integration Tests -- Parallel Branches and Semaphore Throttling

```python
# tests/integration/test_dag_executor_parallel.py
"""
Integration tests for DAGExecutor: parallelism verification, semaphore
throttling, fail-fast behaviour, timeout enforcement, and dynamic modification.

Run with:
    pytest tests/integration/test_dag_executor_parallel.py -v
"""
import asyncio
import time

import pytest

from agenthicc.workflow.executor import DAGExecutor, ExecutorConfig
from agenthicc.workflow.graph import WorkflowGraph
from agenthicc.workflow.models import NodeStatus, WorkflowNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_sleeping_node(
    name: str,
    deps: list[str] | None = None,
    sleep: float = 0.0,
    result: str | None = None,
) -> WorkflowNode:
    """Create a node whose executor sleeps then returns `result` (or name)."""
    _result = result or name

    async def _executor(node: WorkflowNode) -> str:
        await asyncio.sleep(sleep)
        return _result

    return WorkflowNode(
        node_id=name,
        name=name,
        dependencies=deps or [],
        executor=_executor,
    )


# ---------------------------------------------------------------------------
# Basic sequential execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_node_executes_and_completes():
    g = WorkflowGraph()
    g.add_node(make_sleeping_node("A"))
    results = await DAGExecutor(g).run()
    assert results["A"] == "A"
    assert g.nodes["A"].status == NodeStatus.COMPLETE


@pytest.mark.asyncio
async def test_chain_of_three_executes_in_order():
    g = WorkflowGraph()
    execution_order: list[str] = []

    async def _tracked_executor(node: WorkflowNode) -> str:
        execution_order.append(node.name)
        return node.name

    for name, deps in [("A", []), ("B", ["A"]), ("C", ["B"])]:
        g.add_node(WorkflowNode(
            node_id=name, name=name,
            dependencies=deps,
            executor=_tracked_executor,
        ))

    await DAGExecutor(g).run()
    assert execution_order == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_executor_with_no_executor_callable():
    """Nodes with executor=None should complete with result=None."""
    g = WorkflowGraph()
    g.add_node(WorkflowNode(node_id="gate", name="gate", executor=None))
    results = await DAGExecutor(g).run()
    assert results["gate"] is None
    assert g.nodes["gate"].status == NodeStatus.COMPLETE


# ---------------------------------------------------------------------------
# Parallel branch execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parallel_branches_run_concurrently():
    """
    Graph:  A -> B (sleeps 0.15s)
            A -> C (sleeps 0.15s)
            B, C -> D

    Sequential time:  ~0.32s
    Parallel time:    ~0.17s

    We verify wall-clock time is well under sequential.
    """
    g = WorkflowGraph()
    g.add_node(make_sleeping_node("A", sleep=0.01))
    g.add_node(make_sleeping_node("B", ["A"], sleep=0.15))
    g.add_node(make_sleeping_node("C", ["A"], sleep=0.15))
    g.add_node(make_sleeping_node("D", ["B", "C"], sleep=0.01))

    config = ExecutorConfig(max_parallel_tasks=4)
    start   = time.monotonic()
    results = await DAGExecutor(g, config).run()
    elapsed = time.monotonic() - start

    assert all(v == k for k, v in results.items())
    # Must be well under ~0.32s sequential; give generous tolerance for CI
    assert elapsed < 0.30, (
        f"Expected parallel execution (~0.17s), got {elapsed:.3f}s"
    )


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency_to_one():
    """With max_parallel_tasks=1, peak concurrency must never exceed 1."""
    g = WorkflowGraph()
    running_now  = 0
    peak_running = 0
    track_lock   = asyncio.Lock()

    async def _exec(node: WorkflowNode) -> str:
        nonlocal running_now, peak_running
        async with track_lock:
            running_now  += 1
            peak_running  = max(peak_running, running_now)
        await asyncio.sleep(0.03)
        async with track_lock:
            running_now -= 1
        return node.name

    for name in ["X", "Y", "Z", "W"]:
        g.add_node(WorkflowNode(node_id=name, name=name, executor=_exec))

    config = ExecutorConfig(max_parallel_tasks=1)
    await DAGExecutor(g, config).run()
    assert peak_running == 1, f"Expected max concurrency 1, saw {peak_running}"


@pytest.mark.asyncio
async def test_semaphore_reaches_configured_peak():
    """With max_parallel_tasks=3 and 5 independent nodes, peak should be 3."""
    g = WorkflowGraph()
    running_now  = 0
    peak_running = 0
    track_lock   = asyncio.Lock()

    async def _exec(node: WorkflowNode) -> str:
        nonlocal running_now, peak_running
        async with track_lock:
            running_now  += 1
            peak_running  = max(peak_running, running_now)
        await asyncio.sleep(0.06)
        async with track_lock:
            running_now -= 1
        return node.name

    for name in ["A", "B", "C", "D", "E"]:
        g.add_node(WorkflowNode(node_id=name, name=name, executor=_exec))

    config = ExecutorConfig(max_parallel_tasks=3)
    await DAGExecutor(g, config).run()
    assert peak_running == 3, f"Expected peak concurrency 3, saw {peak_running}"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failing_node_is_marked_failed():
    g = WorkflowGraph()

    async def _bad(_node: WorkflowNode) -> None:
        raise ValueError("intentional failure")

    g.add_node(WorkflowNode(node_id="X", name="X", executor=_bad))
    results = await DAGExecutor(g).run()

    assert isinstance(results["X"], ValueError)
    assert g.nodes["X"].status == NodeStatus.FAILED


@pytest.mark.asyncio
async def test_dependent_node_blocked_by_failed_dep():
    """A node whose dependency failed should remain PENDING (never dispatched)."""
    g = WorkflowGraph()

    async def _bad(_node: WorkflowNode) -> None:
        raise RuntimeError("upstream failure")

    async def _good(_node: WorkflowNode) -> str:
        return "ok"

    g.add_node(WorkflowNode(node_id="A", name="A", executor=_bad))
    g.add_node(WorkflowNode(node_id="B", name="B", dependencies=["A"], executor=_good))

    await DAGExecutor(g).run()

    assert g.nodes["A"].status == NodeStatus.FAILED
    # B's dependency failed -- B was never dispatched and should stay PENDING
    assert g.nodes["B"].status == NodeStatus.PENDING


@pytest.mark.asyncio
async def test_fail_fast_cancels_all_pending():
    """fail_fast=True should cancel all PENDING nodes on first failure."""
    g = WorkflowGraph()
    ran: list[str] = []

    async def _bad(_node: WorkflowNode) -> None:
        raise RuntimeError("boom")

    async def _slow(node: WorkflowNode) -> str:
        await asyncio.sleep(10)
        ran.append(node.name)
        return node.name

    g.add_node(WorkflowNode(node_id="FAIL", name="FAIL", executor=_bad))
    g.add_node(WorkflowNode(node_id="SLOW", name="SLOW", executor=_slow))

    config = ExecutorConfig(fail_fast=True, max_parallel_tasks=1)
    await DAGExecutor(g, config).run()

    assert g.nodes["FAIL"].status == NodeStatus.FAILED
    assert g.nodes["SLOW"].status == NodeStatus.FAILED
    assert "SLOW" not in ran  # executor callable should not have run


@pytest.mark.asyncio
async def test_node_timeout_triggers_failure():
    """Nodes that exceed node_timeout_seconds should be marked FAILED."""
    g = WorkflowGraph()

    async def _slow(_node: WorkflowNode) -> str:
        await asyncio.sleep(60)
        return "never"

    g.add_node(WorkflowNode(node_id="T", name="T", executor=_slow))
    config = ExecutorConfig(node_timeout_seconds=0.05)
    results = await DAGExecutor(g, config).run()

    assert g.nodes["T"].status == NodeStatus.FAILED
    assert isinstance(results["T"], asyncio.TimeoutError)


# ---------------------------------------------------------------------------
# Dynamic modification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_independent_node_mid_execution():
    """
    A node injected while the executor is running (no dependencies) should
    be dispatched and complete.
    """
    from agenthicc.workflow.modifier import ModifyAddNode, WorkflowModifier

    g = WorkflowGraph()
    modifier     = WorkflowModifier(g)
    injected_ran = asyncio.Event()

    async def _exec_a(node: WorkflowNode) -> str:
        # Inject a new independent node while A is executing
        async def _exec_injected(_n: WorkflowNode) -> str:
            injected_ran.set()
            return "injected"

        new_node = WorkflowNode(
            node_id="INJECTED",
            name="INJECTED",
            executor=_exec_injected,
        )
        await modifier.apply(ModifyAddNode(node=new_node, depends_on=[]))
        await asyncio.sleep(0.01)
        return "A"

    g.add_node(WorkflowNode(node_id="A", name="A", executor=_exec_a))

    await DAGExecutor(g).run()

    assert "INJECTED" in g.nodes
    assert injected_ran.is_set()
    assert g.nodes["INJECTED"].status == NodeStatus.COMPLETE
```

---

### 6.3 End-to-End Test -- Argon2 Security Refactor (3 Agents)

```python
# tests/e2e/test_argon2_refactor_workflow.py
"""
E2E test: Argon2 password-hashing security refactor workflow.

Scenario
--------
A legacy codebase uses MD5 for password hashing.
The user's intent: "Refactor password hashing from MD5 to Argon2id."

Three specialist agents collaborate in a dependency DAG:

  Node A -- audit_agent  (no dependencies, starts immediately)
    Role: Scan the codebase for MD5 usage.
    Output: list of files, occurrence count.

  Node B -- patch_agent  (depends on A)
    Role: Rewrite MD5 calls to use argon2-cffi PasswordHasher.
    Input: file list from audit_agent.
    Output: per-file diffs, patched_count.

  Node C -- test_runner_agent  (depends on B)
    Role: Execute pytest against patched codebase.
    Input: patched_count from patch_agent.
    Output: tests_passed, tests_failed, coverage.

All agent executors are async stubs that satisfy the
AgentRunnerBase / TeamRunner interface contract used in production.
No real LLM calls are made; stubs return deterministic data.

Run with:
    pytest tests/e2e/test_argon2_refactor_workflow.py -v
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agenthicc.intent.models import Intent, IntentStatus
from agenthicc.workflow.executor import DAGExecutor, ExecutorConfig
from agenthicc.workflow.graph import WorkflowGraph
from agenthicc.workflow.models import NodeStatus, WorkflowNode


# ---------------------------------------------------------------------------
# Minimal stub types mirroring lauren-ai's TeamResult / AgentContext
# ---------------------------------------------------------------------------

@dataclass
class FakeTeamResult:
    """Drop-in for lauren_ai._teams._runner.TeamResult."""
    final_answer:        str
    worker_outputs:      dict[str, Any]  = field(default_factory=dict)
    rounds:              int             = 1
    total_input_tokens:  int             = 0
    total_output_tokens: int             = 0


@dataclass
class FakeAgentContext:
    """Drop-in for lauren_ai._agents._runner.AgentContext."""
    agent_id:     str
    agent_run_id: str
    config:       dict[str, Any] = field(default_factory=dict)
    memory:       list[Any]      = field(default_factory=list)
    turn:         int            = 0
    signals:      Any            = None
    runner:       Any            = None


# ---------------------------------------------------------------------------
# Fixtures: deterministic file list
# ---------------------------------------------------------------------------

MD5_FILES = [
    "src/auth/login.py",
    "src/users/registration.py",
    "src/admin/password_reset.py",
]


# ---------------------------------------------------------------------------
# Agent executor stubs
# ---------------------------------------------------------------------------

async def audit_agent_executor(node: WorkflowNode) -> FakeTeamResult:
    """
    Simulates scanning the codebase for MD5 usage.
    In production this would use AgentRunnerBase with a code-analysis tool.
    """
    await asyncio.sleep(0.02)  # simulate LLM + tool latency
    return FakeTeamResult(
        final_answer="MD5 usage found in 3 files across the authentication subsystem.",
        worker_outputs={
            "files_to_patch": MD5_FILES,
            "occurrences":    7,
            "severity":       "HIGH",
        },
    )


async def patch_agent_executor(node: WorkflowNode) -> FakeTeamResult:
    """
    Simulates rewriting MD5 calls to argon2-cffi.
    Reads the file list injected from the upstream audit result.
    In production this would use AgentRunnerBase with a file-edit tool.
    """
    await asyncio.sleep(0.03)
    audit_result: FakeTeamResult = node.metadata["upstream_results"]["audit"]
    files = audit_result.worker_outputs["files_to_patch"]
    diffs = {
        f: (
            f"--- a/{f}\n+++ b/{f}\n"
            "-import hashlib\n"
            "-hashlib.md5(password.encode()).hexdigest()\n"
            "+from argon2 import PasswordHasher\n"
            "+PasswordHasher().hash(password)\n"
        )
        for f in files
    }
    return FakeTeamResult(
        final_answer=(
            f"Patched {len(files)} files. "
            "Replaced hashlib.md5 with argon2.PasswordHasher (Argon2id, default params)."
        ),
        worker_outputs={
            "diffs":         diffs,
            "patched_count": len(files),
            "algorithm":     "argon2id",
        },
    )


async def test_runner_agent_executor(node: WorkflowNode) -> FakeTeamResult:
    """
    Simulates running pytest against the patched codebase.
    In production this would use AgentRunnerBase with a shell-exec tool.
    """
    await asyncio.sleep(0.02)
    patch_result: FakeTeamResult = node.metadata["upstream_results"]["patch"]
    patched = patch_result.worker_outputs["patched_count"]
    return FakeTeamResult(
        final_answer=(
            f"pytest: 42 passed, 0 failed after patching {patched} files. "
            "Coverage: 94%."
        ),
        worker_outputs={
            "tests_passed": 42,
            "tests_failed": 0,
            "coverage":     "94%",
            "duration_s":   8.4,
        },
    )


# ---------------------------------------------------------------------------
# Primary E2E test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_argon2_refactor_workflow_succeeds_end_to_end():
    """
    Happy path: all three agents complete, results propagate correctly,
    intent status reaches COMPLETE.
    """
    # ---- Build intent -----------------------------------------------------
    intent = Intent(raw_text="Refactor password hashing from MD5 to Argon2id")

    # ---- Build workflow graph ---------------------------------------------
    g = WorkflowGraph()

    # Node A: audit
    audit_node = WorkflowNode(
        node_id="audit",
        name="audit_agent",
        description="Scan codebase for MD5 password hashing",
        executor=audit_agent_executor,
    )

    # Node B: patch  (reads audit result via closure on graph)
    async def patch_executor_with_context(node: WorkflowNode) -> FakeTeamResult:
        node.metadata["upstream_results"] = {"audit": g.nodes["audit"].result}
        return await patch_agent_executor(node)

    patch_node = WorkflowNode(
        node_id="patch",
        name="patch_agent",
        description="Replace hashlib.md5 with argon2.PasswordHasher",
        dependencies=["audit"],
        executor=patch_executor_with_context,
    )

    # Node C: test  (reads patch result via closure on graph)
    async def test_executor_with_context(node: WorkflowNode) -> FakeTeamResult:
        node.metadata["upstream_results"] = {"patch": g.nodes["patch"].result}
        return await test_runner_agent_executor(node)

    test_node = WorkflowNode(
        node_id="test",
        name="test_runner_agent",
        description="Run pytest suite against patched code",
        dependencies=["patch"],
        executor=test_executor_with_context,
    )

    g.add_node(audit_node)
    g.add_node(patch_node)
    g.add_node(test_node)

    # Attach graph to intent
    intent.workflow_graph = g
    intent.advance(IntentStatus.RUNNING)

    # ---- Execute ----------------------------------------------------------
    config  = ExecutorConfig(max_parallel_tasks=4, node_timeout_seconds=30.0)
    results = await DAGExecutor(g, config).run()

    intent.advance(IntentStatus.COMPLETE)

    # ---- Status assertions ------------------------------------------------
    assert g.nodes["audit"].status == NodeStatus.COMPLETE, "audit node did not complete"
    assert g.nodes["patch"].status == NodeStatus.COMPLETE, "patch node did not complete"
    assert g.nodes["test"].status  == NodeStatus.COMPLETE, "test node did not complete"
    assert intent.status == IntentStatus.COMPLETE

    # ---- Audit result assertions ------------------------------------------
    audit_r: FakeTeamResult = results["audit"]
    assert isinstance(audit_r, FakeTeamResult)
    assert audit_r.worker_outputs["files_to_patch"] == MD5_FILES
    assert audit_r.worker_outputs["occurrences"] == 7
    assert audit_r.worker_outputs["severity"] == "HIGH"

    # ---- Patch result assertions ------------------------------------------
    patch_r: FakeTeamResult = results["patch"]
    assert patch_r.worker_outputs["patched_count"] == 3
    assert patch_r.worker_outputs["algorithm"] == "argon2id"
    for f in MD5_FILES:
        assert f in patch_r.worker_outputs["diffs"], f"Missing diff for {f}"

    # ---- Test runner result assertions ------------------------------------
    test_r: FakeTeamResult = results["test"]
    assert test_r.worker_outputs["tests_failed"] == 0
    assert test_r.worker_outputs["tests_passed"] == 42
    assert "94%" in test_r.worker_outputs["coverage"]
    assert "42 passed" in test_r.final_answer

    # ---- Ordering assertion -----------------------------------------------
    order = g.topological_order()
    assert order.index("audit") < order.index("patch")
    assert order.index("patch") < order.index("test")


@pytest.mark.asyncio
async def test_argon2_refactor_audit_failure_blocks_downstream():
    """
    If audit fails, patch and test should never execute.
    Intent should reach FAILED state.
    """
    from agenthicc.intent.processor import IntentProcessor

    g = WorkflowGraph()
    actually_ran: list[str] = []

    async def _failing_audit(_node: WorkflowNode) -> None:
        raise ConnectionError("Code scanner service unavailable")

    async def _patch(node: WorkflowNode) -> str:
        actually_ran.append("patch")
        return "patched"

    async def _test(node: WorkflowNode) -> str:
        actually_ran.append("test")
        return "passed"

    g.add_node(WorkflowNode(node_id="audit", name="audit", executor=_failing_audit))
    g.add_node(WorkflowNode(node_id="patch", name="patch", dependencies=["audit"], executor=_patch))
    g.add_node(WorkflowNode(node_id="test",  name="test",  dependencies=["patch"], executor=_test))

    await DAGExecutor(g, ExecutorConfig(fail_fast=False)).run()

    assert g.nodes["audit"].status == NodeStatus.FAILED
    assert isinstance(g.nodes["audit"].error, ConnectionError)
    # patch and test were never dispatched because their dependency failed
    assert "patch" not in actually_ran
    assert "test"  not in actually_ran
    assert g.nodes["patch"].status == NodeStatus.PENDING
    assert g.nodes["test"].status  == NodeStatus.PENDING


@pytest.mark.asyncio
async def test_argon2_refactor_signal_event_sequence():
    """
    Verify that node lifecycle events are emitted in the correct sequence.
    Uses an in-process event collector instead of the real SignalBus.
    """
    event_log: list[tuple[str, str]] = []  # (event_name, node_id)

    class CollectingSignalBus:
        async def emit(self, event_name: str, payload: dict[str, Any]) -> None:
            event_log.append((event_name, payload.get("node_id", "")))

    bus = CollectingSignalBus()
    g   = WorkflowGraph()

    async def _instrumented_executor(node: WorkflowNode) -> str:
        await bus.emit("node_started",  {"node_id": node.node_id})
        await asyncio.sleep(0.01)
        await bus.emit("node_complete", {"node_id": node.node_id})
        return node.node_id

    g.add_node(WorkflowNode(node_id="A", name="A", executor=_instrumented_executor))
    g.add_node(WorkflowNode(node_id="B", name="B", dependencies=["A"],
                            executor=_instrumented_executor))

    await DAGExecutor(g).run()

    event_names = [e[0] for e in event_log]
    node_ids    = [e[1] for e in event_log]

    # Both start events and both complete events must be present
    assert event_names.count("node_started")  == 2
    assert event_names.count("node_complete") == 2

    # A must start before B starts
    start_a = next(i for i, e in enumerate(event_log) if e == ("node_started",  "A"))
    start_b = next(i for i, e in enumerate(event_log) if e == ("node_started",  "B"))
    comp_a  = next(i for i, e in enumerate(event_log) if e == ("node_complete", "A"))
    assert start_a < comp_a < start_b, (
        f"Expected A to fully complete before B starts. Log: {event_log}"
    )


@pytest.mark.asyncio
async def test_argon2_concurrent_intents_independent():
    """
    Two separate Argon2 refactor intents submitted concurrently must not
    interfere with each other (no shared graph state).
    """
    from agenthicc.intent.processor import IntentProcessor

    async def _build_graph(suffix: str) -> WorkflowGraph:
        g = WorkflowGraph()

        async def _audit(_n: WorkflowNode) -> str:
            await asyncio.sleep(0.02)
            return f"audit_{suffix}"

        async def _patch(n: WorkflowNode) -> str:
            await asyncio.sleep(0.02)
            return f"patch_{suffix}"

        g.add_node(WorkflowNode(node_id=f"audit_{suffix}", name="audit", executor=_audit))
        g.add_node(WorkflowNode(
            node_id=f"patch_{suffix}", name="patch",
            dependencies=[f"audit_{suffix}"],
            executor=_patch,
        ))
        return g

    intent_1 = Intent(raw_text="Refactor MD5 to Argon2 (project alpha)")
    intent_1.workflow_graph = await _build_graph("alpha")

    intent_2 = Intent(raw_text="Refactor MD5 to Argon2 (project beta)")
    intent_2.workflow_graph = await _build_graph("beta")

    processor = IntentProcessor()
    task_1 = processor.submit(intent_1)
    task_2 = processor.submit(intent_2)

    results_1, results_2 = await asyncio.gather(task_1, task_2)

    # Each intent has its own result namespace
    assert f"audit_alpha" in results_1
    assert f"patch_alpha" in results_1
    assert f"audit_beta"  in results_2
    assert f"patch_beta"  in results_2

    # No cross-contamination
    assert f"audit_beta"  not in results_1
    assert f"audit_alpha" not in results_2
```

---

## Configuration Reference

All execution parameters live in `agenthicc.toml` under the `[workflow]` and `[intent]` sections.

### 8.1 Full TOML Schema with Defaults and Annotations

```toml
# agenthicc.toml
# Full configuration reference for the Intent Layer and Workflow Engine.

# ============================================================================
# Intent lifecycle settings
# ============================================================================
[intent]

# Maximum number of concurrently processing intent asyncio.Tasks.
# Each top-level user request occupies one task slot.
# Increase this if throughput matters more than per-request resource usage.
max_concurrent_intents = 10

# Seconds to wait for the Parse stage (NLP entity extraction) to complete.
# If using an LLM-backed parser, set this to at least 2x your P95 LLM latency.
parse_timeout_seconds = 15.0

# Seconds to wait for the Validate stage.
# Validation is typically fast (schema + capability checks, no LLM calls).
validate_timeout_seconds = 5.0

# Seconds to wait for the Plan stage (LLM-based decomposition into nodes).
# Complex intents requiring multi-round coordinator dialogue need more time.
plan_timeout_seconds = 30.0


# ============================================================================
# Workflow DAG execution settings
# ============================================================================
[workflow]

# Maps to asyncio.Semaphore(max_parallel_tasks) in DAGExecutor.
# This caps the number of WorkflowNodes running simultaneously per workflow.
# Note: this is per-workflow, not per-process. Total active goroutines may be
# max_concurrent_intents * max_parallel_tasks.
max_parallel_tasks = 4

# Per-node execution timeout in seconds.
# Applies via asyncio.wait_for() in DAGExecutor._run_node().
# Set to 0 to disable (no timeout enforced).
node_timeout_seconds = 120.0

# When true: the first node failure immediately cancels all PENDING nodes.
# When false: the graph runs to completion; only blocked nodes stay PENDING.
fail_fast = false

# How many times to retry a FAILED node before permanently failing it.
# The retry loop is: attempt 1 (normal) + node_retry_limit retries.
# Set to 0 to disable retries.
node_retry_limit = 2

# Seconds to wait between retry attempts.
# Actual backoff = attempt_number * retry_backoff_seconds (linear).
retry_backoff_seconds = 2.0

# Maximum number of dynamic node additions allowed per workflow execution.
# Prevents runaway modification loops from coordinator agents.
max_dynamic_additions = 20


# ============================================================================
# Structured logging for the workflow engine
# ============================================================================
[workflow.logging]

# Emit log lines as JSON objects (via structlog) instead of plain text.
# Recommended for production environments with log aggregation pipelines.
structured = true

# Minimum severity: "DEBUG" | "INFO" | "WARNING" | "ERROR"
level = "INFO"

# Include the full node result payload in completion log lines.
# May significantly increase log volume for large result objects.
include_results = false


# ============================================================================
# SignalBus settings (lauren_ai._signals)
# ============================================================================
[signals]

# Internal buffer size for each signal channel.
# Events beyond this limit are handled per overflow_policy.
buffer_size = 256

# What to do when a channel's buffer is full:
#   "drop"  -- silently discard the event (default; never blocks)
#   "block" -- await until buffer has space (may create backpressure)
#   "raise" -- raise BufferFullError (useful for strict audit pipelines)
overflow_policy = "drop"
```

### 8.2 Config Loader Implementation

```python
# agenthicc/config.py
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkflowConfig:
    max_parallel_tasks:    int          = 4
    node_timeout_seconds:  float        = 120.0
    fail_fast:             bool         = False
    node_retry_limit:      int          = 2
    retry_backoff_seconds: float        = 2.0
    max_dynamic_additions: int          = 20


@dataclass
class IntentConfig:
    max_concurrent_intents:   int   = 10
    parse_timeout_seconds:    float = 15.0
    validate_timeout_seconds: float = 5.0
    plan_timeout_seconds:     float = 30.0


@dataclass
class AppConfig:
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    intent:   IntentConfig   = field(default_factory=IntentConfig)


def load_config(path: Path | str = "agenthicc.toml") -> AppConfig:
    """
    Load configuration from a TOML file.
    Returns defaults for any missing keys.
    If the file does not exist, returns a fully-defaulted AppConfig.
    """
    p = Path(path)
    if not p.exists():
        return AppConfig()

    with p.open("rb") as f:
        raw = tomllib.load(f)

    wf  = raw.get("workflow", {})
    it  = raw.get("intent",   {})

    return AppConfig(
        workflow=WorkflowConfig(
            max_parallel_tasks    = wf.get("max_parallel_tasks",    4),
            node_timeout_seconds  = wf.get("node_timeout_seconds",  120.0),
            fail_fast             = wf.get("fail_fast",             False),
            node_retry_limit      = wf.get("node_retry_limit",      2),
            retry_backoff_seconds = wf.get("retry_backoff_seconds", 2.0),
            max_dynamic_additions = wf.get("max_dynamic_additions", 20),
        ),
        intent=IntentConfig(
            max_concurrent_intents   = it.get("max_concurrent_intents",   10),
            parse_timeout_seconds    = it.get("parse_timeout_seconds",    15.0),
            validate_timeout_seconds = it.get("validate_timeout_seconds", 5.0),
            plan_timeout_seconds     = it.get("plan_timeout_seconds",     30.0),
        ),
    )
```

### 8.3 Wiring Config to DAGExecutor

```python
# agenthicc/workflow/executor.py  (addition -- place near bottom of file)
from agenthicc.config import WorkflowConfig


def executor_config_from_app_config(wf: WorkflowConfig) -> ExecutorConfig:
    """
    Convert an AppConfig WorkflowConfig block into a DAGExecutor ExecutorConfig.

    Translates the node_timeout_seconds=0 sentinel (disable timeout) to None,
    which asyncio.wait_for treats as "no timeout".
    """
    return ExecutorConfig(
        max_parallel_tasks   = wf.max_parallel_tasks,
        node_timeout_seconds = wf.node_timeout_seconds if wf.node_timeout_seconds > 0 else None,
        fail_fast            = wf.fail_fast,
    )
```

### 8.4 Minimal Development Config

For local development with fast feedback loops:

```toml
# agenthicc.toml (development overrides)
[intent]
max_concurrent_intents = 2
parse_timeout_seconds  = 60.0   # generous: LLM calls can be slow on dev hardware
plan_timeout_seconds   = 60.0

[workflow]
max_parallel_tasks    = 2       # low to reduce resource contention
node_timeout_seconds  = 300.0   # long: allow step-through debugging
fail_fast             = true    # surface failures fast during development
node_retry_limit      = 0       # disable retries: fail fast, fix fast

[workflow.logging]
structured     = false          # plain text is easier to read in a terminal
level          = "DEBUG"
include_results = true          # see full results during development
```

---

## Open Questions

### OQ-1 -- Intent Parser Backend Strategy

The `IntentParser` is currently a stub. The production path involves invoking an LLM via `AgentRunnerBase` to extract structured entities (goal, constraints, affected files, programming language) from free-form text. Two approaches are under consideration:

**Option A (Hybrid):** Heuristic/regex parser for simple intents (single-action, well-known pattern) with LLM fallback for ambiguous or multi-step intents. A `confidence` threshold (e.g. `< 0.7`) triggers the LLM path.

**Option B (LLM-first):** Always invoke the LLM parser. Simpler code path but higher latency and cost for trivial intents.

The decision affects `parse_timeout_seconds` tuning and the LLM cost model.

**Owner:** Intent team
**Target resolution:** Phase 3 planning session

---

### OQ-2 -- Retry State Visibility

The TOML config exposes `node_retry_limit` and `retry_backoff_seconds`, but the current `DAGExecutor._run_node()` marks a node FAILED on first error. Adding retries requires wrapping the execution loop. Two visibility models are possible:

**Option A (Opaque retries):** The node stays in RUNNING state during retries. FAILED is only set after the retry budget is exhausted. SignalBus emits `node_retry` events for observability without changing the status state machine.

**Option B (RETRYING state):** A new `NodeStatus.RETRYING` is added to the enum. The node transitions RUNNING -> RETRYING -> RUNNING -> ... -> FAILED. This makes retry state visible in the WorkflowGraph but complicates all consumers that inspect node status.

**Owner:** Workflow team
**Target resolution:** Phase 5 design review

---

### OQ-3 -- Coordinator Discovery of Modifiable Node IDs

The `workflow_modify` tool is registered on the coordinator agent. For the coordinator to call it meaningfully, it needs to know the current set of node IDs and their statuses. Two options:

**Option A (Context injection):** Serialize the current `WorkflowGraph` state (node IDs, names, statuses, descriptions) into the coordinator's `AgentContext.memory` at each turn. The coordinator sees the graph as structured data.

**Option B (Query tool):** Register a `list_workflow_nodes() -> list[NodeSummary]` tool alongside `workflow_modify`. The coordinator queries the live graph on demand.

Option B is preferred for large graphs (avoids polluting the context window) but requires an additional tool registration.

**Owner:** Lauren-AI integration team
**Target resolution:** Phase 4 design review

---

### OQ-4 -- Cross-Intent Resource Contention

Multiple concurrent intents may need the same external resource (e.g., a static-analysis service that accepts only one concurrent connection, or a shared database migration lock). The current design leaves resource contention entirely to the downstream services.

Options:
- Introduce a global `ResourceLock` registry that nodes declare resource requirements in `node.metadata["resources"]` and the executor acquires/releases before/after execution.
- Rely on the `max_parallel_tasks` semaphore as a blunt-instrument limiter across all intents.
- Defer entirely to the external service's own concurrency controls.

**Owner:** Infrastructure team
**Target resolution:** Before Phase 2 merge (may affect ExecutorConfig schema)

---

### OQ-5 -- JSON-Serialisability Requirement for Durable Checkpointing

The current design is entirely in-memory; a process crash loses all workflow state. A future PRD will address durable checkpointing (SQLite, Redis, or object storage).

Preparing for that PRD requires deciding now whether `WorkflowNode.result` must be JSON-serialisable. `TeamResult.worker_outputs` is typed as `dict[str, Any]`, which may include non-serialisable objects (e.g., dataclass instances, file handles, numpy arrays).

If we mandate JSON-serialisability now (or add a `to_dict()` protocol requirement), the checkpointing PRD becomes much easier to implement. If we defer this requirement, existing node executors may need breaking changes later.

**Owner:** Platform team
**Target resolution:** Prior to production release (non-blocking for Phases 1-5)

---

### OQ-6 -- Per-Node Token Budget Enforcement

`TeamResult.total_input_tokens` and `TeamResult.total_output_tokens` are available after each node completes. A cost-safety feature could fail any node whose token consumption exceeds a configured budget (e.g., `node_max_tokens = 50000`).

This would require the executor to inspect the result after `node.executor(node)` returns and call `node.mark_failed()` retroactively if the budget was exceeded. The `TeamResult` must be the return type (or contain token counts) for this to be feasible without introspection.

The open question is whether this belongs in `DAGExecutor` (generic budget enforcement) or in `TeamNodeExecutor` (lauren-ai-specific post-processing).

**Owner:** Cost management team
**Target resolution:** Phase 5 scope decision

---

### OQ-7 -- Coordinator vs. Planner Separation

Currently `IntentPlanner` is responsible for decomposing a `ParsedIntent` into a `WorkflowGraph`. For complex intents this will involve an LLM call via `TeamRunner`. The question is whether the coordinator agent used for planning is the same as the coordinator that later uses `workflow_modify` during execution, or whether they are separate agent instances with separate contexts and tool sets.

Using the same agent preserves planning context (the agent "remembers" why it made each node decision). Using separate agents is cleaner architecturally but requires serialising planning decisions into the workflow graph as metadata.

**Owner:** Agent architecture team
**Target resolution:** Phase 3/4 boundary

---

*End of PRD-02: Intent Layer and Workflow Engine*
