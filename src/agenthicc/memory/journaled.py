"""Journaled ShortTermMemory (PRD-129 Phase 2).

A :class:`~lauren_ai._memory.ShortTermMemory` whose every append and reset is
mirrored to a durable :class:`~agenthicc.memory.journal.ConversationJournal`.
This makes the live conversation a crash-recoverable *projection* of the
journal: on construction it folds any existing journal content back into memory
(transparent resume), and thereafter every transition is durably recorded
before the next LLM call.

The append/reset mutation surface and tool-exchange lifecycle are journaled.
``trim_to_fit`` is an in-RAM projection operation and is deliberately not
journaled because the full history remains durable.  Explicit
``ensure_valid_and_persist`` recovery is journaled: a repaired interrupted
tool-call tail is part of the next turn's durable context and must survive a
process restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lauren_ai._memory import ShortTermMemory

if TYPE_CHECKING:
    from lauren_ai._memory import ToolExchange, ToolResultRecord

from agenthicc.memory.journal import ConversationJournal
from agenthicc.memory.tool_history import repair_non_adjacent_tool_history

__all__ = ["JournaledShortTermMemory"]


class JournaledShortTermMemory(ShortTermMemory):
    """``ShortTermMemory`` that durably journals every append and reset."""

    def __init__(self, journal: ConversationJournal, max_tokens: int = 40_000) -> None:
        super().__init__(max_tokens=max_tokens)
        self._journal = journal
        # Resume: fold any existing journal content into memory *without*
        # re-journaling it (these entries are already durable on disk).
        msgs, summary = journal.fold()
        if msgs or summary is not None:
            self._messages = list(msgs)
            self._summary = summary
        # A queued continuation can be journaled before a late result from an
        # interrupted tool task. Lauren-ai correctly rejects that shape as
        # ambiguous; at this session boundary the tool IDs make the repair
        # deterministic. Handle it before the normal missing-tail check.
        repair_non_adjacent_tool_history(self)
        # A crash can also leave an assistant tool batch appended without its
        # result exchange. Repair the in-memory projection before the first
        # resumed provider request and make that repair durable immediately.
        self.ensure_valid_and_persist()

    # ── append surface — record every newly-added message ────────────────────

    def _journal_new(self, before: int) -> None:
        """Journal every message appended since the buffer had *before* items."""
        for msg in self._messages[before:]:
            self._journal.append_message(msg)

    def add_user(self, content: str | list[object]) -> None:
        before = len(self._messages)
        super().add_user(content)
        self._journal_new(before)

    def add_assistant(self, completion: object) -> None:
        before = len(self._messages)
        super().add_assistant(completion)
        self._journal_new(before)

    def add_tool_results(self, results: list[object]) -> None:
        before = len(self._messages)
        super().add_tool_results(results)
        self._journal_new(before)

    # ── reset surface — retry rollback and compaction ────────────────────────

    def restore(self, data: object) -> None:
        super().restore(data)
        self._journal.reset(self._messages, self._summary)

    def commit_tool_exchange(
        self,
        exchange: "ToolExchange",
        results: list[object],
        *,
        on_unresolved: str = "synthesize_error_results",
    ) -> "ToolExchange":
        """Reject a late result from an exchange no longer owned by memory.

        Lauren-ai validates the exchange payload, but 1.5.0 still permits a
        started exchange to commit when ``restore()`` has already cleared the
        active owner. That is unsafe after a cancellation/resume race: the
        stale task would append its result after the queued continuation.
        """
        from lauren_ai import ToolConversationIntegrityError

        current = self.active_tool_exchange
        if current is None or current.exchange_id != exchange.exchange_id:
            expected_count = len(current.call_ids) if current is not None else 0
            raise ToolConversationIntegrityError(
                "Tool exchange does not own the active transaction",
                code="exchange_owner_mismatch",
                expected_count=expected_count,
                observed_count=len(exchange.call_ids),
            )
        return super().commit_tool_exchange(
            exchange,
            results,
            on_unresolved=on_unresolved,
        )

    def journal_reset(self) -> None:
        """Record the current full state.

        Called by the compactor after it replaces ``_messages`` in place (which
        bypasses the append/restore overrides), keeping the journal in sync.
        """
        self._journal.reset(self._messages, self._summary)

    def rollback_to(self, count: int) -> None:
        """Truncate to *count* messages and journal the reset (PRD-129 Phase 3).

        Used on crash-resume to return memory to a turn's pre-turn state before
        re-driving it.
        """
        self._messages = self._messages[:count]
        self._journal.reset(self._messages, self._summary)

    def ensure_valid_and_persist(self) -> bool:
        """Heal an interrupted provider tail and persist the healed state.

        ``ShortTermMemory.ensure_valid()`` inserts synthetic interruption
        results for unanswered tool calls, but the base implementation does
        not journal that in-place repair. That is normally correct for a
        transient pre-send repair. An explicit user cancellation is different:
        the repaired history is the context the next turn must remember, so it
        must survive a process restart as well.
        """
        before = list(self._messages)
        self.ensure_valid()
        changed = self._messages != before
        if changed:
            self._journal.reset(self._messages, self._summary)
        return changed

    def on_tool_exchange_started(self, exchange: ToolExchange) -> None:
        """Persist safe lifecycle metadata for an in-flight exchange."""
        self._journal.tool_exchange_started(
            exchange.exchange_id,
            run_id=exchange.run_id,
            call_count=len(exchange.call_ids),
            call_ids=list(exchange.call_ids),
        )

    def on_tool_exchange_committed(self, exchange: ToolExchange) -> None:
        """Persist the exchange commit after its message projection is durable."""
        synthetic_count = sum(1 for outcome in exchange.outcomes if outcome.synthetic)
        self._journal.tool_exchange_committed(
            exchange.exchange_id,
            call_count=len(exchange.call_ids),
            completed_count=len(exchange.outcomes),
            synthetic_count=synthetic_count,
        )

    def on_tool_exchange_result_recorded(
        self, exchange: ToolExchange, outcome: ToolResultRecord
    ) -> None:
        """Persist one safe result outcome after its message is journaled."""
        self._journal.tool_exchange_result_recorded(
            exchange.exchange_id,
            tool_use_id=outcome.tool_use_id,
            status=outcome.status,
            synthetic=outcome.synthetic,
        )

    def on_tool_exchange_aborted(self, exchange: ToolExchange, *, repaired: bool) -> None:
        """Persist interruption/recovery without exposing tool payloads."""
        self._journal.tool_exchange_aborted(
            exchange.exchange_id,
            call_count=len(exchange.call_ids),
            repaired=repaired,
        )

    @property
    def journal(self) -> ConversationJournal:
        """The underlying durable journal (turn markers, tool records)."""
        return self._journal

    def close(self) -> None:
        """Close the underlying journal file handle."""
        self._journal.close()
