"""AgentPool — idle/busy agent bookkeeping for the runtime (PRD-03).

The pool is a thin asyncio primitive: idle agents wait in a FIFO
:class:`asyncio.Queue`; busy agents live in a dict keyed by ``agent_id``.
All state mutations to ``AppState`` happen elsewhere (via events) — the
pool only tracks *runtime* handles (``AgentRecord``) for scheduling.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
__all__ = ["AgentPool", "AgentRecord"]


@dataclass
class AgentRecord:
    """Runtime handle for one agent managed by the pool.

    ``runner`` is intentionally untyped — it may be a lauren-ai agent
    runner, a coroutine wrapper, or ``None`` for purely event-sourced
    agents in tests.
    """

    agent_id: str
    agent_type: str
    runner: object = None
    current_task_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class AgentPool:
    """FIFO pool of idle agents with O(1) acquire/release."""

    def __init__(self) -> None:
        self._idle: asyncio.Queue[AgentRecord] = asyncio.Queue()
        self._busy: dict[str, AgentRecord] = {}
        self._records: dict[str, AgentRecord] = {}

    # ── registration ─────────────────────────────────────────────────────

    def add(self, record: AgentRecord) -> None:
        """Register *record* and place it on the idle queue."""
        if record.agent_id in self._records:
            raise ValueError(f"agent {record.agent_id!r} already registered")
        self._records[record.agent_id] = record
        self._idle.put_nowait(record)

    def get(self, agent_id: str) -> AgentRecord | None:
        """Return the registered record for *agent_id*, if any."""
        return self._records.get(agent_id)

    # ── acquire / release ────────────────────────────────────────────────

    async def acquire(self, timeout: float | None = None) -> AgentRecord:
        """Take the longest-idle agent and mark it busy.

        Blocks until an agent is available. ``timeout=0`` is a
        non-blocking poll; any timeout expiry raises ``TimeoutError``.
        """
        if timeout is not None and timeout <= 0:
            try:
                record = self._idle.get_nowait()
            except asyncio.QueueEmpty:
                raise TimeoutError("no idle agent available") from None
        elif timeout is not None:
            try:
                record = await asyncio.wait_for(self._idle.get(), timeout)
            except TimeoutError:
                raise TimeoutError(
                    f"no idle agent became available within {timeout}s"
                ) from None
        else:
            record = await self._idle.get()
        self._busy[record.agent_id] = record
        return record

    def release(self, agent_id: str) -> None:
        """Return a busy agent to the back of the idle queue."""
        record = self._busy.pop(agent_id, None)
        if record is None:
            raise KeyError(f"agent {agent_id!r} is not busy")
        record.current_task_id = None
        self._idle.put_nowait(record)

    # ── introspection ────────────────────────────────────────────────────

    @property
    def idle_count(self) -> int:
        return self._idle.qsize()

    @property
    def busy_count(self) -> int:
        return len(self._busy)

    @property
    def total_count(self) -> int:
        return len(self._records)
