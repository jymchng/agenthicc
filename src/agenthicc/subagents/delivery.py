"""Notify the parent that a background subagent pool has finished.

A foreground pool returns its aggregate to the parent as the
``spawn_subagents`` tool result. A pool moved to the background finishes after
that call has returned, so the parent is told at its next logical-turn boundary
instead (SUBAGENT-11 goal 5, "notice, then pull").

The notice is composed entirely by the runtime — the pool id, its terminal
status, the real worker counts and, for a pool stopped by a budget, the fixed
reason the governor recorded — and it contains **no worker-authored text**.
Worker output is untrusted (workers read web pages, files and other tools'
output), and the notice is appended in the user role, the most trusted position
in the parent's context. The results themselves reach the parent only as the
output of ``collect_subagent_results``, a tool the parent calls, so worker text
stays in the tool role exactly as it does for a foreground pool.

Every session runner calls :func:`deliver_background_pool_notices` at its turn
boundary, so the TUI and background sessions deliver identically.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.memory.journaled import JournaledShortTermMemory
    from agenthicc.tui.conversation_store import ConversationStore

__all__ = [
    "background_pool_notice",
    "deliver_background_pool_notices",
]

#: Stop reasons a notice may quote; anything else is reported generically.
_KNOWN_STOP_REASONS = frozenset(
    {
        "max_tokens_exceeded",
        "max_total_time_s_exceeded",
        "max_cost_usd_exceeded",
        "max_tool_calls_exceeded",
        "max_iterations_exceeded",
    }
)


def _count(record: Mapping[str, object], key: str) -> int:
    value = record.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _status_text(status: str, reason: str) -> str:
    if status == "completed":
        return "completed"
    if status == "stopped":
        cause = f": {reason}" if reason in _KNOWN_STOP_REASONS else ""
        return f"was stopped by the pool budget{cause}"
    if status == "interrupted":
        return "was interrupted when the session stopped"
    if status == "cancelled":
        return "was cancelled when the session closed"
    return "finished"


def background_pool_notice(record: Mapping[str, object]) -> str:
    """Return the runtime-authored notice for one terminal background pool.

    *record* is a folded pool lifecycle record from the conversation journal:
    ``pool_id``, ``status`` and, when known, ``total``, ``succeeded``,
    ``failed`` and ``reason``. Its ``text`` is deliberately never read.
    """
    pool_id = str(record.get("pool_id", ""))[:128]
    status = str(record.get("status", "interrupted"))
    reason_value = record.get("reason", "")
    reason = reason_value if isinstance(reason_value, str) else ""
    return (
        f"Background subagent pool {pool_id} {_status_text(status, reason)} "
        f"({_count(record, 'succeeded')}/{_count(record, 'total')} succeeded, "
        f"{_count(record, 'failed')} failed). Its results are not included here: "
        f'call collect_subagent_results(pool_id="{pool_id}") to read them.'
    )


def deliver_background_pool_notices(
    journal: ConversationJournal,
    memory: JournaledShortTermMemory,
    conversation: ConversationStore | None = None,
) -> tuple[str, ...]:
    """Deliver each undelivered terminal background pool exactly once.

    Appends one notice per pool to provider *memory* under a stable message
    id, mirrors it into the reactive *conversation* when one exists, and
    records the delivery in the journal so a resume or compaction never
    delivers it twice. Returns the delivered pool ids.
    """
    delivered: list[str] = []
    for record in journal.completed_background_subagent_pools():
        pool_id = record.get("pool_id")
        if not isinstance(pool_id, str) or not pool_id:
            continue
        message = background_pool_notice(record)
        message_id = f"subagent-completion:{pool_id}"
        memory.add_user_once(message, message_id=message_id)
        if conversation is not None:
            conversation.append_event(
                "user_message",
                {"text": message, "source": "subagent_completion"},
                event_id=message_id,
            )
        journal.subagent_pool_notification_delivered(pool_id=pool_id, message_id=message_id)
        delivered.append(pool_id)
    return tuple(delivered)
