"""Session-wide provider conversation ownership (PRD-156).

The TUI's reactive ``ConversationStore`` is a rendering projection.  This
module owns the provider-ready conversation that must survive direct turns,
mode changes, workflow switches, pauses, and process restarts.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from lauren_ai._memory import ShortTermMemory

from agenthicc.memory.journal import ConversationJournal, journal_path_for
from agenthicc.memory.journaled import JournaledShortTermMemory

__all__ = ["ConversationBusyError", "SessionConversation"]


class ConversationBusyError(RuntimeError):
    """Raised when two agent activities try to mutate one session history."""


@dataclass
class SessionConversation:
    """One session-scoped, journal-backed provider conversation.

    ``conversation_id`` is stable for the session and is passed to lauren-ai's
    agent context on every turn.  The memory object is deliberately shared by
    direct turns and workflows.  The lock is process-local coordination only;
    durable recovery uses the journal and workflow checkpoints.
    """

    conversation_id: str
    journal: ConversationJournal
    memory: JournaledShortTermMemory

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: str | None = None

    @classmethod
    def open(
        cls,
        conversation_id: str,
        *,
        max_tokens: int,
        journal: ConversationJournal | None = None,
        journal_path: Path | None = None,
    ) -> "SessionConversation":
        """Open and fold the session conversation from its durable journal."""
        _validate_identifier(conversation_id, "conversation_id")
        if journal is not None and journal_path is not None:
            raise ValueError("provide journal or journal_path, not both")
        active_journal = journal or ConversationJournal(
            journal_path or journal_path_for(conversation_id)
        )
        return cls(
            conversation_id=conversation_id,
            journal=active_journal,
            memory=JournaledShortTermMemory(active_journal, max_tokens=max_tokens),
        )

    @property
    def cursor(self) -> int:
        """Return the journal cursor used by workflow checkpoints."""
        return self.journal.cursor

    @property
    def messages(self) -> list[object]:
        """Return the current live message buffer without exposing ownership."""
        return list(self.memory._messages)

    @property
    def owner(self) -> str | None:
        """Return the current process-local turn owner, if any."""
        return self._owner

    async def acquire(self, owner_id: str) -> None:
        """Acquire the conversation for one agent activity.

        A second owner is rejected rather than queued implicitly.  The TUI
        queue owns user-facing FIFO behaviour, so hidden concurrent mutation
        would be both unsafe and difficult to recover after cancellation.
        """
        if self._owner is not None and self._owner != owner_id:
            raise ConversationBusyError(
                f"session conversation is owned by {self._owner}; cannot acquire for {owner_id}"
            )
        await self._lock.acquire()
        self._owner = owner_id

    def release(self, owner_id: str) -> None:
        """Release the conversation after an agent activity."""
        if self._owner != owner_id:
            raise RuntimeError(f"conversation owner mismatch: {owner_id!r}")
        self._owner = None
        self._lock.release()

    @asynccontextmanager
    async def owned(self, owner_id: str) -> AsyncIterator[None]:
        """Async context manager for one conversation owner."""
        await self.acquire(owner_id)
        try:
            yield
        finally:
            self.release(owner_id)

    def ensure_valid(self) -> None:
        """Heal any dangling provider tool-call tail before a new turn."""
        self.memory.ensure_valid()

    def close(self) -> None:
        """Close the durable journal handle."""
        self.journal.close()


def _memory_type_check(memory: ShortTermMemory) -> None:
    """Keep the imported lauren type visible to static analyzers."""
    if not isinstance(memory, ShortTermMemory):  # pragma: no cover
        raise TypeError("session conversation memory must be ShortTermMemory")


def _validate_identifier(value: str, label: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or any(separator in value for separator in ("/", "\\"))
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be a non-empty safe identifier")
