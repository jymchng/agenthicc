"""Unit tests for DAG algorithms: find_ready_nodes, detect_cycle, topological_sort (PRD-02)."""

from __future__ import annotations

import pytest

from agenthicc.kernel import NodeStatus, Workflow, WorkflowNode
from agenthicc.workflows.dag import CycleError, detect_cycle, find_ready_nodes, topological_sort

pytestmark = pytest.mark.unit


# ── helpers ──────────────────────────────────────────────────────────────────


def make_node(
    node_id: str,
    status: NodeStatus = NodeStatus.pending,
    dependencies: frozenset[str] | None = None,
) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        task_id=f"task-{node_id}",
        label=node_id,
        dependencies=dependencies if dependencies is not None else frozenset(),
        status=status,
    )


def make_workflow(nodes: dict[str, WorkflowNode]) -> Workflow:
    return Workflow(
        workflow_id="wf-test",
        intent_id="intent-test",
        nodes=nodes,
        status=NodeStatus.pending,
        created_at=0.0,
    )


# ── find_ready_nodes ──────────────────────────────────────────────────────────


class TestFindReadyNodes:
    def test_no_deps_node_is_ready(self):
        node = make_node("a")
        wf = make_workflow({"a": node})
        ready = find_ready_nodes(wf)
        assert [n.node_id for n in ready] == ["a"]

    def test_dep_not_complete_blocks(self):
        a = make_node("a", status=NodeStatus.pending)
        b = make_node("b", dependencies=frozenset(["a"]))
        wf = make_workflow({"a": a, "b": b})
        ready_ids = {n.node_id for n in find_ready_nodes(wf)}
        assert "a" in ready_ids
        assert "b" not in ready_ids

    def test_dep_complete_unblocks(self):
        a = make_node("a", status=NodeStatus.complete)
        b = make_node("b", dependencies=frozenset(["a"]))
        wf = make_workflow({"a": a, "b": b})
        ready_ids = {n.node_id for n in find_ready_nodes(wf)}
        assert "b" in ready_ids
        assert "a" not in ready_ids  # complete node not in ready list

    def test_running_node_not_in_ready_list(self):
        a = make_node("a", status=NodeStatus.running)
        wf = make_workflow({"a": a})
        assert find_ready_nodes(wf) == []

    def test_complete_node_not_in_ready_list(self):
        a = make_node("a", status=NodeStatus.complete)
        wf = make_workflow({"a": a})
        assert find_ready_nodes(wf) == []

    def test_diamond_dag_only_root_ready_initially(self):
        # root → left, right → sink
        root = make_node("root")
        left = make_node("left", dependencies=frozenset(["root"]))
        right = make_node("right", dependencies=frozenset(["root"]))
        sink = make_node("sink", dependencies=frozenset(["left", "right"]))
        wf = make_workflow({"root": root, "left": left, "right": right, "sink": sink})
        ready_ids = {n.node_id for n in find_ready_nodes(wf)}
        assert ready_ids == {"root"}

    def test_diamond_dag_branches_ready_after_root_complete(self):
        root = make_node("root", status=NodeStatus.complete)
        left = make_node("left", dependencies=frozenset(["root"]))
        right = make_node("right", dependencies=frozenset(["root"]))
        sink = make_node("sink", dependencies=frozenset(["left", "right"]))
        wf = make_workflow({"root": root, "left": left, "right": right, "sink": sink})
        ready_ids = {n.node_id for n in find_ready_nodes(wf)}
        assert ready_ids == {"left", "right"}

    def test_parallel_independent_nodes_all_ready(self):
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(5)}
        wf = make_workflow(nodes)
        ready_ids = {n.node_id for n in find_ready_nodes(wf)}
        assert ready_ids == set(nodes.keys())

    def test_failed_dep_blocks_dependents(self):
        a = make_node("a", status=NodeStatus.failed)
        b = make_node("b", dependencies=frozenset(["a"]))
        wf = make_workflow({"a": a, "b": b})
        ready_ids = {n.node_id for n in find_ready_nodes(wf)}
        # b is pending but dep is not complete, so not ready
        assert "b" not in ready_ids


# ── detect_cycle ──────────────────────────────────────────────────────────────


class TestDetectCycle:
    def test_self_loop(self):
        new_node = make_node("a", dependencies=frozenset(["a"]))
        assert detect_cycle({}, new_node) is True

    def test_direct_cycle_a_to_b_and_b_to_a(self):
        a = make_node("a", dependencies=frozenset(["b"]))
        new_b = make_node("b", dependencies=frozenset(["a"]))
        assert detect_cycle({"a": a}, new_b) is True

    def test_indirect_chain_cycle(self):
        # a -> b -> c, adding c -> a closes the cycle
        a = make_node("a", dependencies=frozenset(["b"]))
        b = make_node("b", dependencies=frozenset(["c"]))
        new_c = make_node("c", dependencies=frozenset(["a"]))
        assert detect_cycle({"a": a, "b": b}, new_c) is True

    def test_no_cycle_linear_chain(self):
        a = make_node("a")
        b = make_node("b", dependencies=frozenset(["a"]))
        new_c = make_node("c", dependencies=frozenset(["b"]))
        assert detect_cycle({"a": a, "b": b}, new_c) is False

    def test_no_cycle_diamond(self):
        root = make_node("root")
        left = make_node("left", dependencies=frozenset(["root"]))
        right = make_node("right", dependencies=frozenset(["root"]))
        new_sink = make_node("sink", dependencies=frozenset(["left", "right"]))
        assert detect_cycle({"root": root, "left": left, "right": right}, new_sink) is False

    def test_dangling_dep_does_not_count_as_cycle(self):
        # new node depends on a node that doesn't exist yet — not a cycle
        new_node = make_node("b", dependencies=frozenset(["missing"]))
        assert detect_cycle({}, new_node) is False

    def test_no_cycle_independent_nodes(self):
        a = make_node("a")
        b = make_node("b")
        new_c = make_node("c")
        assert detect_cycle({"a": a, "b": b}, new_c) is False


# ── topological_sort ──────────────────────────────────────────────────────────


class TestTopologicalSort:
    def test_linear_chain(self):
        a = make_node("a")
        b = make_node("b", dependencies=frozenset(["a"]))
        c = make_node("c", dependencies=frozenset(["b"]))
        order = topological_sort({"a": a, "b": b, "c": c})
        assert order.index("a") < order.index("b") < order.index("c")

    def test_parallel_roots(self):
        a = make_node("a")
        b = make_node("b")
        c = make_node("c", dependencies=frozenset(["a", "b"]))
        order = topological_sort({"a": a, "b": b, "c": c})
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("c")

    def test_diamond_dag(self):
        root = make_node("root")
        left = make_node("left", dependencies=frozenset(["root"]))
        right = make_node("right", dependencies=frozenset(["root"]))
        sink = make_node("sink", dependencies=frozenset(["left", "right"]))
        order = topological_sort({"root": root, "left": left, "right": right, "sink": sink})
        assert order[0] == "root"
        assert order[-1] == "sink"
        assert order.index("left") < order.index("sink")
        assert order.index("right") < order.index("sink")

    def test_raises_cycle_error_on_cyclic_graph(self):
        a = make_node("a", dependencies=frozenset(["b"]))
        b = make_node("b", dependencies=frozenset(["a"]))
        with pytest.raises(CycleError):
            topological_sort({"a": a, "b": b})

    def test_single_node(self):
        node = make_node("only")
        order = topological_sort({"only": node})
        assert order == ["only"]

    def test_dangling_deps_ignored(self):
        # b depends on "missing" which is not in nodes dict — still sortable
        a = make_node("a")
        b = make_node("b", dependencies=frozenset(["a", "missing"]))
        order = topological_sort({"a": a, "b": b})
        assert order.index("a") < order.index("b")
