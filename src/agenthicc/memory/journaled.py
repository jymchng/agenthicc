"""Journaled ShortTermMemory (PRD-129 Phase 2).

A :class:`~lauren_ai._memory.ShortTermMemory` whose every append and reset is
mirrored to a durable :class:`~agenthicc.memory.journal.ConversationJournal`.
This makes the live conversation a crash-recoverable *projection* of the
journal: on construction it folds any existing journal content back into memory
(transparent resume), and thereafter every transition is durably recorded
before the next LLM call.

Only the append/reset mutation surface is journaled.  ``trim_to_fit`` and
``ensure_valid`` mutate the in-RAM buffer (sliding-window trimming and
tool-call healing) but are deliberately *not* journaled — the journal keeps the
full history, and those operations are deterministically re-derived from it on
the next read, so recording them would be redundant.
"""
from __future__ import annotations

from lauren_ai._memory import ShortTermMemory

from agenthicc.memory.journal import ConversationJournal

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

    @property
    def journal(self) -> ConversationJournal:
        """The underlying durable journal (turn markers, tool records)."""
        return self._journal

    def close(self) -> None:
        """Close the underlying journal file handle."""
        self._journal.close()
