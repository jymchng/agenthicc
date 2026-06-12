"""DAG algorithms over kernel ``Workflow`` / ``WorkflowNode`` (PRD-02).

Pure functions only — no I/O, no event emission. The kernel dataclasses are
frozen, so every function here treats its inputs as read-only.
"""

from __future__ import annotations

from collections import defaultdict, deque

from agenthicc.kernel import NodeStatus, Workflow, WorkflowNode

__all__ = ["CycleError", "detect_cycle", "find_ready_nodes", "topological_sort"]


class CycleError(Exception):
    """Raised when a graph that must be acyclic contains a cycle."""


def find_ready_nodes(workflow: Workflow) -> list[WorkflowNode]:
    """Return every node that may be dispatched right now.

    A node is ready when it is ``pending`` and all of its dependencies are
    ``complete``. Nodes with failed/skipped dependencies are never ready.
    """
    completed = {
        node_id
        for node_id, node in workflow.nodes.items()
        if node.status == NodeStatus.complete
    }
    return [
        node
        for node in workflow.nodes.values()
        if node.status == NodeStatus.pending
        and all(dep in completed for dep in node.dependencies)
    ]


def detect_cycle(nodes: dict[str, WorkflowNode], new_node: WorkflowNode) -> bool:
    """Return ``True`` if adding ``new_node`` to ``nodes`` would create a cycle.

    Uses iterative-friendly three-colour DFS (white/gray/black) over the
    hypothetical graph ``nodes | {new_node}``. Dependencies that reference
    unknown nodes are ignored (dangling edges cannot form cycles).
    """
    graph: dict[str, WorkflowNode] = dict(nodes)
    graph[new_node.node_id] = new_node

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {node_id: WHITE for node_id in graph}

    def dfs(node_id: str) -> bool:
        color[node_id] = GRAY
        for dep in graph[node_id].dependencies:
            if dep not in graph:
                continue  # dangling edge — cannot participate in a cycle
            if color[dep] == GRAY:
                return True  # back edge
            if color[dep] == WHITE and dfs(dep):
                return True
        color[node_id] = BLACK
        return False

    return any(color[node_id] == WHITE and dfs(node_id) for node_id in graph)


def topological_sort(nodes: dict[str, WorkflowNode]) -> list[str]:
    """Return node ids in dependency-first order using Kahn's algorithm.

    Dependencies that reference nodes outside ``nodes`` are ignored.
    Raises :class:`CycleError` if the graph contains a cycle.
    """
    in_degree: dict[str, int] = {node_id: 0 for node_id in nodes}
    dependents: dict[str, list[str]] = defaultdict(list)
    for node_id, node in nodes.items():
        for dep in node.dependencies:
            if dep in nodes:
                in_degree[node_id] += 1
                dependents[dep].append(node_id)

    queue: deque[str] = deque(
        sorted(node_id for node_id, degree in in_degree.items() if degree == 0)
    )
    order: list[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for dependent in dependents[node_id]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(order) != len(nodes):
        raise CycleError("topological_sort: graph contains a cycle")
    return order
