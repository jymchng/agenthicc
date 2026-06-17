"""DAGExecutor — drives a kernel ``Workflow`` to a terminal state (PRD-02).

The executor never mutates state directly: every transition is an event
emitted through the :class:`~agenthicc.kernel.EventProcessor`, and the
authoritative view of the workflow is always re-read from processor state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from agenthicc.kernel import Event, EventProcessor, NodeStatus, Workflow, WorkflowNode

from agenthicc.workflows.dag import find_ready_nodes

__all__ = ["DAGExecutor", "NodeRunner"]

logger = logging.getLogger(__name__)

#: Executes one node and returns its result string. May raise to fail the node.
NodeRunner = Callable[[WorkflowNode, str], Awaitable[str]]

_TERMINAL_WORKFLOW = frozenset({NodeStatus.complete, NodeStatus.failed})
_DOOMED_DEP = frozenset({NodeStatus.failed, NodeStatus.skipped})


class DAGExecutor:
    """Concurrent, event-driven executor for workflow DAGs.

    * Ready nodes are launched as independent ``asyncio.Task``s.
    * Live concurrency is capped by ``asyncio.Semaphore(max_parallel_tasks)``.
    * Launched/handled node ids are tracked in a set so a node is never
      dispatched twice, even while its status events are still in flight.
    * Nodes whose dependencies failed (or were skipped) are marked
      ``skipped`` so the workflow can still reach a terminal state.
    """

    def __init__(
        self,
        processor: EventProcessor,
        node_runner: NodeRunner,
        max_parallel_tasks: int = 4,
    ) -> None:
        self._processor = processor
        self._node_runner = node_runner
        self._semaphore = asyncio.Semaphore(max_parallel_tasks)
        self._handled: set[str] = set()  # node ids launched or skipped
        self._tasks: set[asyncio.Task[None]] = set()

    # ── dispatch ─────────────────────────────────────────────────────────

    async def start_ready_nodes(self, workflow: Workflow) -> int:
        """Launch a task for every ready node not yet dispatched.

        Also marks pending nodes that can never run (a dependency failed or
        was skipped) as ``skipped``. Returns the number of nodes launched.
        """
        launched = 0
        for node in find_ready_nodes(workflow):
            if node.node_id in self._handled:
                continue
            self._handled.add(node.node_id)
            task = asyncio.create_task(
                self._run_node(node, workflow.workflow_id),
                name=f"workflow-node-{node.node_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            launched += 1

        await self._skip_doomed_nodes(workflow)
        return launched

    async def _skip_doomed_nodes(self, workflow: Workflow) -> None:
        for node in workflow.nodes.values():
            if node.status != NodeStatus.pending or node.node_id in self._handled:
                continue
            if any(
                workflow.nodes[dep].status in _DOOMED_DEP
                for dep in node.dependencies
                if dep in workflow.nodes
            ):
                self._handled.add(node.node_id)
                await self._emit_status(
                    workflow.workflow_id,
                    node.node_id,
                    NodeStatus.skipped,
                    error="skipped: upstream dependency failed",
                )

    async def _run_node(self, node: WorkflowNode, workflow_id: str) -> None:
        async with self._semaphore:
            await self._emit_status(workflow_id, node.node_id, NodeStatus.running)
            try:
                result = await self._node_runner(node, workflow_id)
            except Exception as exc:
                logger.exception("node %s failed", node.node_id)
                await self._emit_status(
                    workflow_id, node.node_id, NodeStatus.failed, error=str(exc)
                )
            else:
                await self._emit_status(
                    workflow_id, node.node_id, NodeStatus.complete, result=result
                )

    async def _emit_status(
        self,
        workflow_id: str,
        node_id: str,
        status: NodeStatus,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "workflow_id": workflow_id,
            "node_id": node_id,
            "status": status.value,
        }
        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error
        await self._processor.emit(Event.create("WorkflowNodeStatusChanged", payload))

    # ── main loop ────────────────────────────────────────────────────────

    async def run_workflow(self, workflow_id: str, timeout: float = 30.0) -> Workflow:
        """Drive ``workflow_id`` until its status is terminal.

        Subscribes to processor state updates and re-evaluates the ready set
        after every applied event (this is what picks up nodes added
        dynamically mid-run). Returns the terminal :class:`Workflow`.
        Raises ``TimeoutError`` if the workflow does not finish in time.
        """
        queue = self._processor.subscribe()
        try:
            async with asyncio.timeout(timeout):
                workflow = self._processor.get_state().workflows.get(workflow_id)
                if workflow is None:
                    raise KeyError(f"unknown workflow: {workflow_id!r}")
                if self._is_terminal(workflow):
                    return workflow
                await self.start_ready_nodes(workflow)

                while True:
                    state = await queue.get()
                    workflow = state.workflows.get(workflow_id)
                    if workflow is None:
                        continue
                    if self._is_terminal(workflow):
                        return workflow
                    await self.start_ready_nodes(workflow)
        finally:
            self._processor.unsubscribe(queue)

    @staticmethod
    def _is_terminal(workflow: Workflow) -> bool:
        return workflow.status in _TERMINAL_WORKFLOW or not workflow.nodes
