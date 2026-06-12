"""Unit tests for the runtime AgentPool (PRD-03)."""

from __future__ import annotations

import pytest

from agenthicc.runtime import AgentPool, AgentRecord

pytestmark = pytest.mark.unit


def make_record(agent_id: str) -> AgentRecord:
    return AgentRecord(agent_id=agent_id, agent_type="worker")


async def test_acquire_release_round_trip():
    pool = AgentPool()
    pool.add(make_record("a1"))
    assert pool.idle_count == 1
    assert pool.busy_count == 0

    record = await pool.acquire(timeout=1.0)
    assert record.agent_id == "a1"
    assert pool.idle_count == 0
    assert pool.busy_count == 1

    record.current_task_id = "t1"
    pool.release("a1")
    assert pool.idle_count == 1
    assert pool.busy_count == 0
    # release clears the task pointer
    assert record.current_task_id is None

    # the same record comes back on re-acquire
    again = await pool.acquire(timeout=1.0)
    assert again is record


async def test_acquire_times_out_when_pool_empty():
    pool = AgentPool()
    with pytest.raises(TimeoutError):
        await pool.acquire(timeout=0.05)
    # non-blocking poll also raises
    with pytest.raises(TimeoutError):
        await pool.acquire(timeout=0)


async def test_acquire_fifo_fairness():
    pool = AgentPool()
    for name in ("a1", "a2", "a3"):
        pool.add(make_record(name))

    order = [(await pool.acquire(timeout=0)).agent_id for _ in range(3)]
    assert order == ["a1", "a2", "a3"]

    # released agents rejoin at the back of the queue, preserving FIFO
    pool.release("a2")
    pool.release("a1")
    order2 = [(await pool.acquire(timeout=0)).agent_id for _ in range(2)]
    assert order2 == ["a2", "a1"]


async def test_release_unknown_agent_raises():
    pool = AgentPool()
    pool.add(make_record("a1"))
    with pytest.raises(KeyError):
        pool.release("a1")  # registered but idle, not busy
    with pytest.raises(KeyError):
        pool.release("ghost")


async def test_duplicate_add_rejected():
    pool = AgentPool()
    pool.add(make_record("a1"))
    with pytest.raises(ValueError):
        pool.add(make_record("a1"))
    assert pool.total_count == 1
