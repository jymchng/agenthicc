"""Unit tests for Scheduler — assign_next and release_agent (PRD-03)."""
from __future__ import annotations

import time

import pytest

from agenthicc.kernel import AppState, Event, EventProcessor, NodeStatus, SecurityPolicy, SystemSettings
from agenthicc.kernel.state import Task
from agenthicc.runtime.pool import AgentPool, AgentRecord
from agenthicc.runtime.scheduler import Scheduler

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────


def _make_task(
    task_id: str = "t1",
    status: NodeStatus = NodeStatus.pending,
    assigned_agent_id: str | None = None,
    created_at: float | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        workflow_id="wf-1",
        node_id=f"node-{task_id}",
        description="test task",
        status=status,
        assigned_agent_id=assigned_agent_id,
        created_at=created_at if created_at is not None else time.time(),
    )


def _state_with_tasks(*tasks: Task) -> AppState:
    base = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
    tasks_dict = {t.task_id: t for t in tasks}
    return AppState(
        session_id=base.session_id,
        run_id=base.run_id,
        intents=base.intents,
        workflows=base.workflows,
        tasks=tasks_dict,
        agents=base.agents,
        tools=base.tools,
        hooks=base.hooks,
        snapshot_index=base.snapshot_index,
        settings=base.settings,
        policy=base.policy,
    )


def _empty_state() -> AppState:
    return AppState.create(settings=SystemSettings(), policy=SecurityPolicy())


def _make_processor() -> EventProcessor:
    state = _empty_state()
    return EventProcessor(initial_state=state, persist=False)


# ── assign_next: no tasks ─────────────────────────────────────────────────


class TestAssignNext:
    async def test_assign_next_returns_none_when_no_tasks(self):
        """No tasks in state → assign_next returns None."""
        processor = _make_processor()
        pool = AgentPool()
        scheduler = Scheduler(processor, pool)
        state = _empty_state()
        result = await scheduler.assign_next(state)
        assert result is None

    async def test_assign_next_returns_none_when_no_idle_agents(self):
        """Tasks exist but pool is empty → assign_next returns None."""
        processor = _make_processor()
        pool = AgentPool()
        scheduler = Scheduler(processor, pool)
        state = _state_with_tasks(_make_task("t1", NodeStatus.pending))
        result = await scheduler.assign_next(state)
        assert result is None

    async def test_assign_next_returns_none_when_task_already_assigned(self):
        """Task has assigned_agent_id set → treated as not pending."""
        processor = _make_processor()
        pool = AgentPool()
        record = AgentRecord(agent_id="a1", agent_type="Worker")
        pool.add(record)
        scheduler = Scheduler(processor, pool)
        # Task is pending but already has an assigned agent
        state = _state_with_tasks(_make_task("t1", NodeStatus.pending, assigned_agent_id="a1"))
        result = await scheduler.assign_next(state)
        assert result is None

    async def test_assign_next_returns_none_when_task_is_running(self):
        """A running (not pending) task is not picked up."""
        processor = _make_processor()
        pool = AgentPool()
        record = AgentRecord(agent_id="a1", agent_type="Worker")
        pool.add(record)
        scheduler = Scheduler(processor, pool)
        state = _state_with_tasks(_make_task("t1", NodeStatus.running))
        result = await scheduler.assign_next(state)
        assert result is None

    async def test_assign_next_picks_oldest_pending_task(self):
        """Two pending tasks: the one with the smaller created_at is picked."""
        processor = _make_processor()
        emitted: list[Event] = []

        async def _noop_emit(event: Event) -> None:
            emitted.append(event)

        processor.emit = _noop_emit
        pool = AgentPool()
        record = AgentRecord(agent_id="a1", agent_type="Worker")
        pool.add(record)
        scheduler = Scheduler(processor, pool)

        t_old = _make_task("t-old", NodeStatus.pending, created_at=1000.0)
        t_new = _make_task("t-new", NodeStatus.pending, created_at=2000.0)
        state = _state_with_tasks(t_old, t_new)

        result = await scheduler.assign_next(state)
        assert result is not None
        task_id, agent_id = result
        assert task_id == "t-old"
        assert agent_id == "a1"

    async def test_assign_next_emits_task_assigned_event(self):
        """assign_next emits a TaskAssigned event via the processor."""
        processor = _make_processor()
        emitted: list[Event] = []

        async def _capturing_emit(event: Event) -> None:
            emitted.append(event)

        processor.emit = _capturing_emit
        pool = AgentPool()
        record = AgentRecord(agent_id="a1", agent_type="Worker")
        pool.add(record)
        scheduler = Scheduler(processor, pool)
        state = _state_with_tasks(_make_task("t1", NodeStatus.pending))

        await scheduler.assign_next(state)
        event_types = [e.event_type for e in emitted]
        assert "TaskAssigned" in event_types

    async def test_assign_next_emits_agent_status_busy(self):
        """assign_next emits AgentStatusChanged(busy) after assignment."""
        processor = _make_processor()
        emitted: list[Event] = []

        async def _capturing_emit(event: Event) -> None:
            emitted.append(event)

        processor.emit = _capturing_emit
        pool = AgentPool()
        record = AgentRecord(agent_id="a1", agent_type="Worker")
        pool.add(record)
        scheduler = Scheduler(processor, pool)
        state = _state_with_tasks(_make_task("t1", NodeStatus.pending))

        await scheduler.assign_next(state)
        status_events = [e for e in emitted if e.event_type == "AgentStatusChanged"]
        assert len(status_events) >= 1
        busy_events = [e for e in status_events if e.payload.get("status") == "busy"]
        assert len(busy_events) == 1
        assert busy_events[0].payload["agent_id"] == "a1"

    async def test_assign_next_sets_current_task_id_on_record(self):
        """assign_next sets record.current_task_id on the acquired agent."""
        processor = _make_processor()
        emitted: list[Event] = []

        async def _capturing_emit(event: Event) -> None:
            emitted.append(event)

        processor.emit = _capturing_emit
        pool = AgentPool()
        record = AgentRecord(agent_id="a1", agent_type="Worker")
        pool.add(record)
        scheduler = Scheduler(processor, pool)
        state = _state_with_tasks(_make_task("t1", NodeStatus.pending))

        await scheduler.assign_next(state)
        assert record.current_task_id == "t1"


# ── release_agent ─────────────────────────────────────────────────────────


class TestReleaseAgent:
    async def test_release_agent_emits_agent_status_idle(self):
        """release_agent emits AgentStatusChanged with status=idle."""
        processor = _make_processor()
        emitted: list[Event] = []

        async def _capturing_emit(event: Event) -> None:
            emitted.append(event)

        processor.emit = _capturing_emit
        pool = AgentPool()
        record = AgentRecord(agent_id="a1", agent_type="Worker")
        pool.add(record)
        scheduler = Scheduler(processor, pool)

        # Acquire first to put record in busy state
        await pool.acquire(timeout=0)
        await scheduler.release_agent("a1")

        idle_events = [
            e for e in emitted
            if e.event_type == "AgentStatusChanged" and e.payload.get("status") == "idle"
        ]
        assert len(idle_events) == 1
        assert idle_events[0].payload["agent_id"] == "a1"
        assert idle_events[0].payload["current_task_id"] is None

    async def test_release_agent_returns_agent_to_pool(self):
        """After release_agent, the agent is acquirable again."""
        processor = _make_processor()
        emitted: list[Event] = []

        async def _noop_emit(event: Event) -> None:
            emitted.append(event)

        processor.emit = _noop_emit
        pool = AgentPool()
        record = AgentRecord(agent_id="a1", agent_type="Worker")
        pool.add(record)
        scheduler = Scheduler(processor, pool)

        # Acquire to make busy
        acquired = await pool.acquire(timeout=0)
        assert acquired.agent_id == "a1"
        assert pool.idle_count == 0
        # Release via scheduler
        await scheduler.release_agent("a1")
        # Pool should have agent back
        assert pool.idle_count == 1

    async def test_release_agent_payload_has_correct_task_id_null(self):
        """The released event payload sets current_task_id to None."""
        processor = _make_processor()
        emitted: list[Event] = []

        async def _capturing_emit(event: Event) -> None:
            emitted.append(event)

        processor.emit = _capturing_emit
        pool = AgentPool()
        record = AgentRecord(agent_id="b2", agent_type="Worker", current_task_id="some-task")
        pool.add(record)
        scheduler = Scheduler(processor, pool)

        await pool.acquire(timeout=0)
        await scheduler.release_agent("b2")

        idle_events = [e for e in emitted if e.event_type == "AgentStatusChanged"]
        assert idle_events[-1].payload["current_task_id"] is None
