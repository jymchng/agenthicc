"""Durable, journal-backed idempotency ledger (PRD-129 Phase 3).

Extends the in-memory :class:`~lauren_ai.IdempotencyLedger` so every recorded
tool result is also written to the :class:`~agenthicc.memory.journal.ConversationJournal`.
After a crash, re-driving the same turn constructs a ``DurableIdempotencyLedger``
seeded from the journal's ``tool_recorded`` entries — so completed
(side-effecting) tools are *replayed*, not re-executed, even across a process
restart.  It conforms to the same ``lookup`` / ``record`` / ``promote`` surface
the runner uses, so lauren-ai needs no knowledge of durability.
"""

from __future__ import annotations

from collections import deque

from lauren_ai import IdempotencyLedger, ToolResult
from lauren_ai._tools import canonical_tool_key

from agenthicc.memory.journal import ConversationJournal

__all__ = ["DurableIdempotencyLedger"]


def _serialize(result: ToolResult) -> dict[str, object]:
    return {
        "tool_use_id": result.tool_use_id,
        "content": result.content,
        "is_error": result.is_error,
    }


def _deserialize(payload: object) -> ToolResult:
    if isinstance(payload, dict):
        return ToolResult(
            tool_use_id=str(payload.get("tool_use_id", "")),
            content=payload.get("content", ""),  # type: ignore[arg-type]
            is_error=bool(payload.get("is_error", False)),
        )
    return ToolResult(tool_use_id="", content=str(payload), is_error=False)


class DurableIdempotencyLedger(IdempotencyLedger):
    """An :class:`IdempotencyLedger` whose records are durably journaled."""

    def __init__(
        self,
        journal: ConversationJournal,
        turn_id: str,
        seed_records: list[tuple[str, object]] | None = None,
    ) -> None:
        super().__init__()
        self._journal = journal
        self._turn_id = turn_id
        # Resume: prior-attempt records are already committed (the crash was an
        # implicit rollback) → replayable, in the order they were recorded.
        for key, payload in seed_records or ():
            self._committed.setdefault(key, deque()).append(_deserialize(payload))

    def record(self, name: str, tool_input: dict[str, object] | None, result: ToolResult) -> None:
        key = canonical_tool_key(name, tool_input)
        self._pending.append((key, result))
        self._journal.tool_recorded(self._turn_id, key, _serialize(result))
