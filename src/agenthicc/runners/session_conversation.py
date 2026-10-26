"""Session-wide provider conversation ownership (PRD-156).

The TUI's reactive ``ConversationStore`` is a rendering projection.  This
module owns the provider-ready conversation that must survive direct turns,
mode changes, workflow switches, pauses, and process restarts.

The journal opens with the session, but folding it into provider memory needs
lauren-ai and, on resume, a read of the whole journal.  Neither belongs in front
of the first TUI frame (AGX-02), so the memory is materialized on first use,
or ahead of time by :meth:`SessionConversation.load` on a worker thread.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from agenthicc.memory.journal import ConversationJournal, journal_path_for

if TYPE_CHECKING:
    from agenthicc.memory.journaled import JournaledShortTermMemory
    from agenthicc.memory.tool_history import ToolHistoryRecoveryState

__all__ = ["ConversationBusyError", "SessionConversation"]


class ConversationBusyError(RuntimeError):
    """Raised when two agent activities try to mutate one session history."""


class SessionConversation:
    """One session-scoped, journal-backed provider conversation.

    ``conversation_id`` is stable for the session and is passed to lauren-ai's
    agent context on every turn.  The memory object is deliberately shared by
    direct turns and workflows.  The lock is process-local coordination only;
    durable recovery uses the journal and workflow checkpoints.
    """

    def __init__(
        self,
        conversation_id: str,
        journal: ConversationJournal,
        *,
        max_tokens: Callable[[], int],
    ) -> None:
        self.conversation_id = conversation_id
        self.journal = journal
        self._max_tokens = max_tokens
        self._memory: JournaledShortTermMemory | None = None
        # Guards the one-time fold: a worker thread may be loading while the
        # event loop touches ``memory``; the loser waits for the same object.
        self._memory_lock = threading.Lock()
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
        conversation = cls.open_deferred(
            conversation_id,
            max_tokens=lambda: max_tokens,
            journal=journal,
            journal_path=journal_path,
        )
        conversation.load_memory()
        return conversation

    @classmethod
    def open_deferred(
        cls,
        conversation_id: str,
        *,
        max_tokens: Callable[[], int],
        journal: ConversationJournal | None = None,
        journal_path: Path | None = None,
    ) -> "SessionConversation":
        """Open the durable journal now and fold provider memory on first use.

        *max_tokens* is evaluated when the memory is materialized, because the
        live-window budget comes from lauren-ai's model registry.
        """
        _validate_identifier(conversation_id, "conversation_id")
        if journal is not None and journal_path is not None:
            raise ValueError("provide journal or journal_path, not both")
        active_journal = journal or ConversationJournal(
            journal_path or journal_path_for(conversation_id)
        )
        return cls(conversation_id, active_journal, max_tokens=max_tokens)

    @property
    def memory(self) -> JournaledShortTermMemory:
        """Return the provider memory, folding the journal on first access."""
        memory = self._memory
        return memory if memory is not None else self.load_memory()

    @property
    def memory_loaded(self) -> bool:
        """Whether the journal has already been folded into provider memory."""
        return self._memory is not None

    def load_memory(self) -> JournaledShortTermMemory:
        """Fold the journal into provider memory exactly once (blocking)."""
        with self._memory_lock:
            if self._memory is None:
                from agenthicc.memory.journaled import JournaledShortTermMemory

                self._memory = JournaledShortTermMemory(self.journal, max_tokens=self._max_tokens())
            return self._memory

    async def load(self) -> JournaledShortTermMemory:
        """Fold the journal on a worker thread so the event loop stays responsive."""
        memory = self._memory
        if memory is not None:
            return memory
        return await asyncio.to_thread(self.load_memory)

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

    @property
    def tool_history_recovery(self) -> ToolHistoryRecoveryState:
        """Return whether the persisted provider conversation is safe to use."""
        return self.memory.tool_history_recovery

    def retry_tool_history_recovery(self) -> ToolHistoryRecoveryState:
        """Retry deterministic local history repair without provider I/O."""
        return self.memory.retry_tool_history_recovery()

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
        """Close the durable journal handle once no fold is in flight."""
        with self._memory_lock:
            self.journal.close()


def _validate_identifier(value: str, label: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or any(separator in value for separator in ("/", "\\"))
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be a non-empty safe identifier")
