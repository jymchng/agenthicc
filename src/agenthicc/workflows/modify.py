"""WorkflowModifier — validated, event-emitting workflow mutations (PRD-02).

Mutations are expressed as kernel events (``WorkflowNodeAdded`` /
``WorkflowNodeRemoved``); the reducer applies them, so the modifier itself
only performs validation against the current state snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from uuid import uuid4

from agenthicc.kernel import Event, EventProcessor, NodeStatus, WorkflowNode

from agenthicc.workflows.dag import detect_cycle

__all__ = ["ModifyResult", "WorkflowModifier"]


@dataclass(frozen=True)
class ModifyResult:
    ok: bool
    error: str | None = None


class WorkflowModifier:
    """Adds and removes workflow nodes with DAG-integrity validation."""

    def __init__(self, processor: EventProcessor) -> None:
        self._processor = processor

    async def add_node(
        self,
        workflow_id: str,
        node_id: str,
        label: str,
        dependencies: Iterable[str] | None = None,
    ) -> ModifyResult:
        """Validate and add a node. Rejects duplicates and cycles."""
        workflow = self._processor.get_state().workflows.get(workflow_id)
        if workflow is None:
            return ModifyResult(ok=False, error=f"unknown workflow: {workflow_id!r}")
        if node_id in workflow.nodes:
            return ModifyResult(ok=False, error=f"node already exists: {node_id!r}")

        deps = frozenset(dependencies or ())
        candidate = WorkflowNode(
            node_id=node_id,
            task_id=f"task-{uuid4().hex[:8]}",
            label=label,
            dependencies=deps,
            status=NodeStatus.pending,
        )
        if detect_cycle(workflow.nodes, candidate):
            return ModifyResult(
                ok=False,
                error=f"adding node {node_id!r} would introduce a cycle",
            )

        await self._processor.emit(
            Event.create(
                "WorkflowNodeAdded",
                {
                    "workflow_id": workflow_id,
                    "node_id": candidate.node_id,
                    "task_id": candidate.task_id,
                    "label": label,
                    "dependencies": sorted(deps),
                },
            )
        )
        return ModifyResult(ok=True)

    async def remove_node(self, workflow_id: str, node_id: str) -> ModifyResult:
        """Validate and remove a node. Running/complete nodes are protected."""
        workflow = self._processor.get_state().workflows.get(workflow_id)
        if workflow is None:
            return ModifyResult(ok=False, error=f"unknown workflow: {workflow_id!r}")
        node = workflow.nodes.get(node_id)
        if node is None:
            return ModifyResult(ok=False, error=f"unknown node: {node_id!r}")
        if node.status in (NodeStatus.running, NodeStatus.complete):
            return ModifyResult(
                ok=False,
                error=(
                    f"cannot remove node {node_id!r} with status {node.status.value}"
                ),
            )

        await self._processor.emit(
            Event.create(
                "WorkflowNodeRemoved",
                {"workflow_id": workflow_id, "node_id": node_id},
            )
        )
        return ModifyResult(ok=True)
