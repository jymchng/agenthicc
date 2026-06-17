"""Integration tests for WorkflowModifier — state mutations persist through the processor (PRD-02)."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.kernel import (
    AppState,
    Event,
    EventProcessor,
    NodeStatus,
    SecurityPolicy,
    SystemSettings,
)
from agenthicc.workflows.modify import WorkflowModifier

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def proc(tmp_path):
    """Running EventProcessor with no workflows pre-loaded."""
    state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snapshot.json"),
        ),
        policy=SecurityPolicy(),
    )
    p = EventProcessor(initial_state=state, persist=False)
    task = asyncio.create_task(p.run())
    yield p
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _create_workflow(proc: EventProcessor, wf_id: str) -> None:
    """Emit WorkflowCreated event and drain."""
    await proc.emit(Event.create(
        "WorkflowCreated",
        {"workflow_id": wf_id, "intent_id": "intent-integration"},
    ))
    await proc.drain()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_add_node_then_verify_in_state(proc):
    """add_node → drain → node appears in processor.get_state()."""
    wf_id = "wf-integ-add"
    await _create_workflow(proc, wf_id)

    modifier = WorkflowModifier(proc)
    result = await modifier.add_node(wf_id, "step-one", "Step One")
    assert result.ok is True

    await proc.drain()

    state = proc.get_state()
    assert wf_id in state.workflows
    wf = state.workflows[wf_id]
    assert "step-one" in wf.nodes
    assert wf.nodes["step-one"].label == "Step One"
    assert wf.nodes["step-one"].status == NodeStatus.pending


async def test_cycle_guard_prevents_deadlock(proc):
    """Diamond A→(B,C)→D: first three adds succeed, the back-edge is rejected."""
    wf_id = "wf-integ-cycle"
    await _create_workflow(proc, wf_id)

    modifier = WorkflowModifier(proc)

    # A (no deps)
    r_a = await modifier.add_node(wf_id, "A", "Node A")
    assert r_a.ok is True
    await proc.drain()

    # B depends on A
    r_b = await modifier.add_node(wf_id, "B", "Node B", dependencies=["A"])
    assert r_b.ok is True
    await proc.drain()

    # C depends on A
    r_c = await modifier.add_node(wf_id, "C", "Node C", dependencies=["A"])
    assert r_c.ok is True
    await proc.drain()

    # D depends on B and C — still a DAG
    r_d = await modifier.add_node(wf_id, "D", "Node D", dependencies=["B", "C"])
    assert r_d.ok is True
    await proc.drain()

    # Now manually build a state where A has a dep on D, and try to add a new node
    # that creates A→D→...→A cycle. Since A already exists we test a different
    # node: add E that depends on D, and try to create cycle via a node that
    # would make D depend on E (D already exists).
    # Instead, test the simpler back-edge:
    # Try to add a node that already forms a cycle when combined with state.
    # Build a state with A→B already, try to add new-A with dep on B.
    # Because A already exists, let's use a separate fresh processor:
    wf_id2 = "wf-integ-back"
    await proc.emit(Event.create(
        "WorkflowCreated",
        {"workflow_id": wf_id2, "intent_id": "intent-back"},
    ))
    await proc.emit(Event.create(
        "WorkflowNodeAdded",
        {
            "workflow_id": wf_id2,
            "node_id": "X",
            "task_id": "t-x",
            "label": "X",
            "dependencies": ["Y"],  # X depends on Y
        },
    ))
    await proc.drain()

    # Now adding Y with dep on X creates a cycle (X→Y→X)
    r_cycle = await modifier.add_node(wf_id2, "Y", "Y", dependencies=["X"])
    assert r_cycle.ok is False
    assert "cycle" in r_cycle.error.lower()


async def test_remove_node_then_verify_gone(proc):
    """add_node → remove_node → drain → node is absent from state."""
    wf_id = "wf-integ-remove"
    await _create_workflow(proc, wf_id)

    modifier = WorkflowModifier(proc)

    r_add = await modifier.add_node(wf_id, "transient", "Transient Node")
    assert r_add.ok is True
    await proc.drain()

    # Confirm it was added
    wf_before = proc.get_state().workflows[wf_id]
    assert "transient" in wf_before.nodes

    r_rm = await modifier.remove_node(wf_id, "transient")
    assert r_rm.ok is True
    await proc.drain()

    wf_after = proc.get_state().workflows[wf_id]
    assert "transient" not in wf_after.nodes


async def test_add_multiple_nodes_with_chained_deps(proc):
    """A linear chain A→B→C is accepted and all three appear in state."""
    wf_id = "wf-chain"
    await _create_workflow(proc, wf_id)

    modifier = WorkflowModifier(proc)
    assert (await modifier.add_node(wf_id, "A", "A")).ok is True
    await proc.drain()
    assert (await modifier.add_node(wf_id, "B", "B", dependencies=["A"])).ok is True
    await proc.drain()
    assert (await modifier.add_node(wf_id, "C", "C", dependencies=["B"])).ok is True
    await proc.drain()

    wf = proc.get_state().workflows[wf_id]
    assert set(wf.nodes.keys()) == {"A", "B", "C"}
    assert wf.nodes["C"].dependencies == frozenset({"B"})


async def test_add_then_remove_then_readd(proc):
    """Add node, remove it, add it again — final state has the node once."""
    wf_id = "wf-readd"
    await _create_workflow(proc, wf_id)

    modifier = WorkflowModifier(proc)
    await modifier.add_node(wf_id, "toggle", "Toggle")
    await proc.drain()
    await modifier.remove_node(wf_id, "toggle")
    await proc.drain()

    # Node is gone
    assert "toggle" not in proc.get_state().workflows[wf_id].nodes

    # Re-add succeeds
    r = await modifier.add_node(wf_id, "toggle", "Toggle v2")
    assert r.ok is True
    await proc.drain()
    assert "toggle" in proc.get_state().workflows[wf_id].nodes


async def test_remove_from_unknown_workflow_is_safe(proc):
    """remove_node on a nonexistent workflow returns ok=False, no crash."""
    modifier = WorkflowModifier(proc)
    result = await modifier.remove_node("ghost-workflow", "ghost-node")
    assert result.ok is False


async def test_add_node_to_unknown_workflow_is_safe(proc):
    """add_node on a nonexistent workflow returns ok=False, no crash."""
    modifier = WorkflowModifier(proc)
    result = await modifier.add_node("ghost-workflow", "node-x", "X")
    assert result.ok is False
