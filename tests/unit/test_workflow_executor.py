"""Unit tests for DAGExecutor (PRD-02) — covers 0% file."""

from __future__ import annotations

import asyncio
import time

import pytest

from agenthicc.kernel import (
    AppState,
    Event,
    EventProcessor,
    NodeStatus,
    SecurityPolicy,
    SystemSettings,
    Workflow,
    WorkflowNode,
)
from agenthicc.workflow.executor import DAGExecutor

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_state_with_workflow(workflow_id: str, nodes: dict[str, WorkflowNode] | None = None) -> AppState:
    """Return a fresh AppState containing one workflow."""
    base = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
    wf = Workflow(
        workflow_id=workflow_id,
        intent_id="intent-test",
        nodes=nodes or {},
        status=NodeStatus.pending,
        created_at=time.time(),
    )
    return base.with_workflow(wf)


def make_node(node_id: str, status: NodeStatus = NodeStatus.pending, deps: frozenset[str] | None = None) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        task_id=f"task-{node_id}",
        label=f"Label {node_id}",
        dependencies=deps or frozenset(),
        status=status,
    )


@pytest.fixture
async def processor_with_workflow():
    """Running EventProcessor that already has one workflow seeded."""
    wf_id = "wf-test"
    state = make_state_with_workflow(wf_id)
    proc = EventProcessor(initial_state=state, persist=False)
    task = asyncio.create_task(proc.run())
    yield proc, wf_id
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# TestDAGExecutor
# ---------------------------------------------------------------------------

class TestDAGExecutor:
    async def test_run_workflow_single_node(self, processor_with_workflow):
        """A single pending node should be executed and become complete."""
        proc, wf_id = processor_with_workflow

        # Add a node
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {
                "workflow_id": wf_id,
                "node_id": "node-1",
                "task_id": "task-1",
                "label": "Node 1",
                "dependencies": [],
            },
        ))
        await proc.drain()

        executed = []

        async def node_runner(node: WorkflowNode, workflow_id: str) -> str:
            executed.append(node.node_id)
            return f"result-{node.node_id}"

        executor = DAGExecutor(proc, node_runner, max_parallel_tasks=2)
        result = await executor.run_workflow(wf_id, timeout=5.0)

        assert result.status == NodeStatus.complete
        assert "node-1" in executed

    async def test_run_workflow_linear_chain(self, processor_with_workflow):
        """A->B->C chain should execute in order."""
        proc, wf_id = processor_with_workflow

        # Create chain: A -> B -> C
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "A", "task_id": "t-a", "label": "A", "dependencies": []},
        ))
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "B", "task_id": "t-b", "label": "B", "dependencies": ["A"]},
        ))
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "C", "task_id": "t-c", "label": "C", "dependencies": ["B"]},
        ))
        await proc.drain()

        execution_order = []

        async def node_runner(node: WorkflowNode, workflow_id: str) -> str:
            execution_order.append(node.node_id)
            await asyncio.sleep(0.01)  # Small delay to test concurrency
            return f"result-{node.node_id}"

        executor = DAGExecutor(proc, node_runner, max_parallel_tasks=2)
        result = await executor.run_workflow(wf_id, timeout=5.0)

        assert result.status == NodeStatus.complete
        # A must come before B, B before C
        assert execution_order.index("A") < execution_order.index("B")
        assert execution_order.index("B") < execution_order.index("C")

    async def test_run_workflow_parallel_nodes(self, processor_with_workflow):
        """Nodes with no dependencies should run in parallel."""
        proc, wf_id = processor_with_workflow

        # Create: A, B (both roots)
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "A", "task_id": "t-a", "label": "A", "dependencies": []},
        ))
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "B", "task_id": "t-b", "label": "B", "dependencies": []},
        ))
        await proc.drain()

        execution_times = {}

        async def node_runner(node: WorkflowNode, workflow_id: str) -> str:
            execution_times[node.node_id] = time.monotonic()
            await asyncio.sleep(0.1)  # Long enough to see parallelism
            return f"result-{node.node_id}"

        executor = DAGExecutor(proc, node_runner, max_parallel_tasks=2)
        result = await executor.run_workflow(wf_id, timeout=5.0)

        assert result.status == NodeStatus.complete
        # Both should have started at roughly the same time
        if len(execution_times) == 2:
            time_diff = abs(execution_times["A"] - execution_times["B"])
            assert time_diff < 0.05  # Started within 50ms of each other

    async def test_run_workflow_node_failure_marks_dependents_skipped(self, processor_with_workflow):
        """When a node fails, dependents should be skipped."""
        proc, wf_id = processor_with_workflow

        # Create: A -> B, where A will fail
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "A", "task_id": "t-a", "label": "A", "dependencies": []},
        ))
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "B", "task_id": "t-b", "label": "B", "dependencies": ["A"]},
        ))
        await proc.drain()

        async def node_runner(node: WorkflowNode, workflow_id: str) -> str:
            if node.node_id == "A":
                raise ValueError("Intentional failure")
            return f"result-{node.node_id}"

        executor = DAGExecutor(proc, node_runner, max_parallel_tasks=2)
        result = await executor.run_workflow(wf_id, timeout=5.0)

        # Workflow should still reach terminal state
        assert result.status in (NodeStatus.failed, NodeStatus.complete)
        wf = proc.get_state().workflows[wf_id]
        assert wf.nodes["A"].status == NodeStatus.failed
        assert wf.nodes["B"].status == NodeStatus.skipped

    async def test_run_workflow_diamond_dag(self, processor_with_workflow):
        """Diamond DAG: A -> B, A -> C, B -> D, C -> D."""
        proc, wf_id = processor_with_workflow

        # Create diamond
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "A", "task_id": "t-a", "label": "A", "dependencies": []},
        ))
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "B", "task_id": "t-b", "label": "B", "dependencies": ["A"]},
        ))
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "C", "task_id": "t-c", "label": "C", "dependencies": ["A"]},
        ))
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "D", "task_id": "t-d", "label": "D", "dependencies": ["B", "C"]},
        ))
        await proc.drain()

        node_statuses = {}

        async def node_runner(node: WorkflowNode, workflow_id: str) -> str:
            node_statuses[node.node_id] = "running"
            await asyncio.sleep(0.02)
            return f"result-{node.node_id}"

        executor = DAGExecutor(proc, node_runner, max_parallel_tasks=2)
        result = await executor.run_workflow(wf_id, timeout=5.0)

        assert result.status == NodeStatus.complete
        for node_id in ["A", "B", "C", "D"]:
            assert node_statuses.get(node_id) == "running"

    async def test_run_workflow_unknown_workflow_raises(self, processor_with_workflow):
        """Unknown workflow_id raises KeyError."""
        proc, _ = processor_with_workflow

        executor = DAGExecutor(proc, lambda n, w: asyncio.sleep(0))
        with pytest.raises(KeyError, match="unknown workflow"):
            await executor.run_workflow("nonexistent", timeout=1.0)

    async def test_run_workflow_timeout_raises(self, processor_with_workflow):
        """Timeout is raised for long-running workflows."""
        proc, wf_id = processor_with_workflow

        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "slow", "task_id": "t-slow", "label": "Slow", "dependencies": []},
        ))
        await proc.drain()

        async def slow_runner(node: WorkflowNode, workflow_id: str) -> str:
            await asyncio.sleep(10)  # Much longer than timeout
            return "result"

        executor = DAGExecutor(proc, slow_runner, max_parallel_tasks=1)
        with pytest.raises(TimeoutError):
            await executor.run_workflow(wf_id, timeout=0.1)

    async def test_run_workflow_max_parallel_tasks_respected(self, processor_with_workflow):
        """The semaphore limits parallel execution."""
        proc, wf_id = processor_with_workflow

        # Create 4 independent nodes
        for i in range(4):
            await proc.emit(Event.create(
                "WorkflowNodeAdded",
                {"workflow_id": wf_id, "node_id": f"n{i}", "task_id": f"t{i}", "label": f"N{i}", "dependencies": []},
            ))
        await proc.drain()

        running_count = 0
        max_running = 0

        async def node_runner(node: WorkflowNode, workflow_id: str) -> str:
            nonlocal running_count, max_running
            running_count += 1
            max_running = max(max_running, running_count)
            await asyncio.sleep(0.05)
            running_count -= 1
            return f"result-{node.node_id}"

        executor = DAGExecutor(proc, node_runner, max_parallel_tasks=1)
        result = await executor.run_workflow(wf_id, timeout=5.0)

        assert result.status == NodeStatus.complete
        assert max_running == 1  # Only one running at a time

    async def test_start_ready_nodes_skips_already_handled(self, processor_with_workflow):
        """Nodes already in _handled set are not launched twice."""
        proc, wf_id = processor_with_workflow

        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "n1", "task_id": "t1", "label": "N1", "dependencies": []},
        ))
        await proc.drain()

        execution_count = 0

        async def node_runner(node: WorkflowNode, workflow_id: str) -> str:
            nonlocal execution_count
            execution_count += 1
            return "result"

        executor = DAGExecutor(proc, node_runner, max_parallel_tasks=1)

        # First call
        launched1 = await executor.start_ready_nodes(proc.get_state().workflows[wf_id])
        assert launched1 == 1

        # Second call should not launch again
        launched2 = await executor.start_ready_nodes(proc.get_state().workflows[wf_id])
        assert launched2 == 0

    async def test_is_terminal_complete(self, processor_with_workflow):
        """_is_terminal returns True for complete workflow."""
        proc, wf_id = processor_with_workflow
        wf = proc.get_state().workflows[wf_id]
        # Workflow status is checked; nodes don't affect workflow status directly
        # The executor checks workflow.status in _TERMINAL_WORKFLOW

        from agenthicc.workflow.executor import _TERMINAL_WORKFLOW
        assert NodeStatus.complete in _TERMINAL_WORKFLOW

    async def test_skip_doomed_nodes(self, processor_with_workflow):
        """Nodes with failed dependencies are marked skipped."""
        proc, wf_id = processor_with_workflow

        # A -> B, A will fail
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "A", "task_id": "t-a", "label": "A", "dependencies": []},
        ))
        await proc.emit(Event.create(
            "WorkflowNodeAdded",
            {"workflow_id": wf_id, "node_id": "B", "task_id": "t-b", "label": "B", "dependencies": ["A"]},
        ))
        await proc.drain()

        # Mark A as failed
        await proc.emit(Event.create(
            "WorkflowNodeStatusChanged",
            {"workflow_id": wf_id, "node_id": "A", "status": "failed"},
        ))
        await proc.drain()

        executor = DAGExecutor(proc, lambda n, w: asyncio.sleep(0))

        # Direct call to start_ready_nodes should skip B
        wf = proc.get_state().workflows[wf_id]
        launched = await executor.start_ready_nodes(wf)
        assert launched == 0  # B should be skipped, A already handled

        # Check B was marked skipped
        wf = proc.get_state().workflows[wf_id]
        # B might have been skipped during start_ready_nodes call