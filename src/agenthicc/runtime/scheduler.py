"""Scheduler — matches pending tasks to idle agents (PRD-03).

The scheduler never mutates ``AppState`` directly: assignment is
expressed as ``TaskAssigned`` + ``AgentStatusChanged`` events; the kernel
reducer applies them. The :class:`~agenthicc.runtime.pool.AgentPool`
holds the runtime handles used for non-blocking idle-agent lookup.
"""

from __future__ import annotations

from agenthicc.kernel import AppState, Event, EventProcessor, NodeStatus

from .pool import AgentPool

__all__ = ["Scheduler"]


class Scheduler:
    """Assigns the oldest pending task to the longest-idle agent."""

    def __init__(self, processor: EventProcessor, pool: AgentPool) -> None:
        self._processor = processor
        self._pool = pool

    async def assign_next(self, state: AppState) -> tuple[str, str] | None:
        """One scheduling step.

        Picks the oldest pending, unassigned task and a non-blocking idle
        agent from the pool. Emits ``TaskAssigned`` and
        ``AgentStatusChanged(busy)``; returns ``(task_id, agent_id)`` or
        ``None`` when there is nothing to do.
        """
        pending = [
            task
            for task in state.tasks.values()
            if task.status == NodeStatus.pending and task.assigned_agent_id is None
        ]
        if not pending:
            return None
        task = min(pending, key=lambda t: t.created_at)

        try:
            record = await self._pool.acquire(timeout=0)
        except TimeoutError:
            return None

        record.current_task_id = task.task_id
        await self._processor.emit(
            Event.create(
                "TaskAssigned",
                {"task_id": task.task_id, "agent_id": record.agent_id},
            )
        )
        await self._processor.emit(
            Event.create(
                "AgentStatusChanged",
                {
                    "agent_id": record.agent_id,
                    "status": "busy",
                    "current_task_id": task.task_id,
                },
            )
        )
        return task.task_id, record.agent_id

    async def release_agent(self, agent_id: str) -> None:
        """Return an agent to the idle pool and emit the status change."""
        self._pool.release(agent_id)
        await self._processor.emit(
            Event.create(
                "AgentStatusChanged",
                {"agent_id": agent_id, "status": "idle", "current_task_id": None},
            )
        )
