"""Durable, runtime-authored notices for background subagents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.memory.journaled import JournaledShortTermMemory
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.runners.session_conversation import SessionConversation
    from agenthicc.subagents.communication import PoolContinuationRegistry
    from agenthicc.tui.conversation_store import ConversationStore

WAKE_TURN_PREFIX = "subagent-wake:"

__all__ = [
    "NoticeSource",
    "PendingNotice",
    "background_pool_notice",
    "background_question_notice",
    "deliver_background_pool_notices",
    "deliver_background_question_notices",
    "deliver_session_background_notices",
]


@dataclass(frozen=True, slots=True)
class PendingNotice:
    """A runtime-authored message carrying only trusted pool metadata."""

    message_id: str
    kind: Literal["completion", "question"]
    pool_id: str
    text: str
    summary: str = ""
    recorded_at: float = 0.0


class NoticeSource(Protocol):
    """Session-owned source of undelivered notices in journal order."""

    def claim(self) -> tuple[PendingNotice, ...]:
        """Read pending notices. Delivery acknowledges them after persistence."""
        ...

    def has_pending(self) -> bool:
        """Whether a notice is available at a tool boundary."""
        ...


def _count(record: Mapping[str, object], key: str) -> int:
    value = record.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _status_text(status: str) -> str:
    if status == "completed":
        return "completed"
    if status == "interrupted":
        return "was interrupted when the session stopped"
    if status == "cancelled":
        return "was cancelled when the session closed"
    return "finished"


def background_pool_notice(record: Mapping[str, object]) -> str:
    """Compose a completion notice without reading worker output."""
    pool_id = str(record.get("pool_id", ""))[:128]
    status = str(record.get("status", "interrupted"))
    return (
        f"Background subagent pool {pool_id} {_status_text(status)} "
        f"({_count(record, 'succeeded')}/{_count(record, 'total')} succeeded, "
        f"{_count(record, 'failed')} failed). Its results are not included here: "
        f'call collect_subagent_results(pool_id="{pool_id}") to read them.'
    )


def background_question_notice(pool_id: str, worker_id: str, question_id: str) -> str:
    """Compose a question notice without reading the worker's question."""
    return (
        f"A worker ({worker_id[:64]}) of background subagent pool {pool_id[:128]} is waiting "
        f"for your answer to question {question_id[:64]}. Call "
        f'collect_subagent_results(pool_id="{pool_id[:128]}") to read it, then '
        "answer_subagent(pool_id, question_id, answer)."
    )


def _completions(journal: ConversationJournal) -> tuple[PendingNotice, ...]:
    notices: list[PendingNotice] = []
    for record in journal.completed_background_subagent_pools():
        pool_id = record.get("pool_id")
        if not isinstance(pool_id, str) or not pool_id:
            continue
        succeeded = _count(record, "succeeded")
        total = _count(record, "total")
        status = str(record.get("status", "interrupted"))
        label = {
            "interrupted": "Subagents interrupted",
            "cancelled": "Subagents cancelled",
        }.get(status, "Subagents finished")
        at = record.get("at")
        notices.append(
            PendingNotice(
                f"subagent-completion:{pool_id}",
                "completion",
                pool_id,
                background_pool_notice(record),
                f"{label} — pool {pool_id[:8]} · {succeeded}/{total} succeeded",
                float(at) if isinstance(at, (float, int)) else 0.0,
            )
        )
    return tuple(notices)


def _questions(
    registry: PoolContinuationRegistry, journal: ConversationJournal
) -> tuple[PendingNotice, ...]:
    notices: list[PendingNotice] = []
    for handle in registry.handles():
        if not handle.backgrounded or handle.task.done():
            continue
        for question in handle.broker.pending_questions(recipient="main"):
            question_id = question.get("question_id")
            if not isinstance(question_id, str) or not question_id:
                continue
            message_id = f"subagent-question:{question_id}"
            if journal.has_message_id(message_id):
                continue
            worker_id = str(question.get("worker_id", ""))
            envelope = handle.broker.message(question_id)
            notices.append(
                PendingNotice(
                    message_id,
                    "question",
                    handle.pool_id,
                    background_question_notice(handle.pool_id, worker_id, question_id),
                    f"Subagent question — pool {handle.pool_id[:8]} · {worker_id[:64]}",
                    envelope.created_at if envelope is not None else 0.0,
                )
            )
    return tuple(notices)


@dataclass(frozen=True, slots=True)
class JournalNoticeSource:
    """Combine durable terminal pools and still-live worker questions."""

    journal: ConversationJournal
    registry: PoolContinuationRegistry

    def claim(self) -> tuple[PendingNotice, ...]:
        return tuple(
            sorted(
                (*_completions(self.journal), *_questions(self.registry, self.journal)),
                key=lambda notice: notice.recorded_at,
            )
        )

    def has_pending(self) -> bool:
        return bool(self.claim())


def deliver_notices(
    notices: tuple[PendingNotice, ...],
    memory: JournaledShortTermMemory,
    conversation: ConversationStore | None,
) -> None:
    """Append messages before acknowledging delivery, using stable IDs."""
    for notice in notices:
        memory.add_user_once(notice.text, message_id=notice.message_id)
        if conversation is not None:
            conversation.append_event(
                "user_message",
                {
                    "text": notice.text,
                    "source": f"subagent_{notice.kind}",
                    "summary": notice.summary,
                },
                event_id=notice.message_id,
            )
        if notice.kind == "completion":
            memory.journal.subagent_pool_notification_delivered(
                pool_id=notice.pool_id, message_id=notice.message_id
            )
    if notices and conversation is not None:
        pending = memory.journal.completed_background_subagent_pools()
        conversation.subagent_finished_pending.set(len(pending))


def deliver_background_pool_notices(
    journal: ConversationJournal,
    memory: JournaledShortTermMemory,
    conversation: ConversationStore | None = None,
) -> tuple[str, ...]:
    """Deliver undelivered terminal pools and return the pool IDs."""
    notices = _completions(journal)
    deliver_notices(notices, memory, conversation)
    return tuple(notice.pool_id for notice in notices)


def deliver_background_question_notices(
    registry: PoolContinuationRegistry,
    memory: JournaledShortTermMemory,
    conversation: ConversationStore | None = None,
) -> tuple[str, ...]:
    """Deliver pending live worker questions and return the question IDs."""
    notices = _questions(registry, memory.journal)
    deliver_notices(notices, memory, conversation)
    return tuple(notice.message_id.removeprefix("subagent-question:") for notice in notices)


def deliver_session_background_notices(session: SessionContext) -> tuple[str, ...]:
    """Deliver terminal background pools at a non-interactive turn boundary."""
    from agenthicc.memory.journaled import JournaledShortTermMemory

    conversation: SessionConversation | None = session.session_conversation
    if conversation is None or not conversation.tool_history_recovery.provider_ready:
        return ()
    memory = conversation.memory
    if not isinstance(memory, JournaledShortTermMemory):
        return ()
    return deliver_background_pool_notices(
        conversation.journal, memory, session.app_state.conversation
    )
