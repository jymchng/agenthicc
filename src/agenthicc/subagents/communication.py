"""Typed communication between a parent agent and subagent workers.

The communication boundary is deliberately separate from worker memory.  A
worker receives only bounded, authenticated envelopes through the broker; it
does not receive the parent's transcript and it cannot use a message to change
its tool policy.  One :class:`AgentMessageBroker` belongs to one
``SubagentPool`` and is never process-global routing state.

The broker is intentionally usable without a TUI.  The parent-facing tool
factory is attached to the session-bound ``spawn_subagents`` tool by the agent
turn runner, while the child-facing factory is installed in each worker.
"""

import asyncio
import json
import math
import time
import uuid
import weakref
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from agenthicc.tools.capabilities import tool_control

if TYPE_CHECKING:
    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.subagents.pool import SubagentPool

MessageKind = Literal[
    "status",
    "information",
    "instruction",
    "clarification_request",
    "clarification_response",
    "handoff",
    "error",
    "cancel",
]

MESSAGE_KINDS: frozenset[str] = frozenset(
    {
        "status",
        "information",
        "instruction",
        "clarification_request",
        "clarification_response",
        "handoff",
        "error",
        "cancel",
    }
)

MAX_MESSAGE_BYTES = 16 * 1024
MAX_MAILBOX_MESSAGES = 256
MAX_PENDING_QUESTIONS = 16
DEFAULT_MESSAGE_TIMEOUT_S = 300.0
#: Upper bound for ``collect_subagent_results(wait=True)`` (``subagents.collect_wait_max_s``).
DEFAULT_COLLECT_WAIT_MAX_S = 60.0

__all__ = [
    "AgentMessageBroker",
    "AgentMessageEnvelope",
    "CommunicationError",
    "DEFAULT_MESSAGE_TIMEOUT_S",
    "MAX_MAILBOX_MESSAGES",
    "MAX_MESSAGE_BYTES",
    "MAX_PENDING_QUESTIONS",
    "PoolContinuationRegistry",
    "PoolHandle",
    "make_child_communication_tools",
    "make_parent_communication_tools",
    "registry_for_session",
]


class CommunicationError(ValueError):
    """A caller supplied an invalid or unauthorized communication request."""


@dataclass(frozen=True, slots=True)
class AgentMessageEnvelope:
    """Immutable, bounded routing envelope for one agent message."""

    message_id: str
    conversation_id: str
    parent_run_id: str
    pool_id: str
    sender: str
    recipient: str
    kind: str
    payload: dict[str, object]
    reply_to: str = ""
    correlation_id: str = ""
    sequence: int = 0
    created_at: float = 0.0
    expires_at: float | None = None
    requires_response: bool = False
    policy_revision: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe projection suitable for a tool result/journal."""
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "parent_run_id": self.parent_run_id,
            "pool_id": self.pool_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "kind": self.kind,
            "payload": dict(self.payload),
            "reply_to": self.reply_to,
            "correlation_id": self.correlation_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "requires_response": self.requires_response,
            "policy_revision": self.policy_revision,
        }


@dataclass
class _MessageRecord:
    envelope: AgentMessageEnvelope
    state: str = "created"
    acknowledged: bool = False
    acknowledged_by: set[str] = field(default_factory=set)


@dataclass
class _QuestionRecord:
    question_id: str
    envelope: AgentMessageEnvelope
    status: str = "pending"
    answer: str = ""
    future: asyncio.Future[str] | None = None
    #: The asker has received the answer from :meth:`AgentMessageBroker.wait_for_answer`.
    delivered: bool = False


@dataclass
class PoolHandle:
    """A pool owned by one session, reachable by the parent's control tools."""

    pool_id: str
    conversation_id: str
    broker: "AgentMessageBroker"
    task: asyncio.Task[object]
    #: The pool itself, for stop and retry; ``None`` for a handle without one.
    pool: "SubagentPool | None" = None
    #: Pool ids from the root pool down to this pool's parent. A nested
    #: worker may only control pools whose lineage contains its own pool.
    lineage: tuple[str, ...] = ()
    #: Whether the pool was moved to the background (user or model request).
    backgrounded: bool = False


class AgentMessageBroker:
    """Route bounded messages for one authenticated subagent pool.

    All state transitions are guarded by one lock and keyed by stable message
    IDs.  The broker does not execute provider calls and has no permission to
    modify a worker's policy.  ``ask_parent`` and ``ask_peer`` create a durable
    question and waiters can later resolve them through a correlated answer.
    """

    def __init__(
        self,
        *,
        conversation_id: str,
        parent_run_id: str,
        pool_id: str,
        policy_revision: str = "",
        journal: "ConversationJournal | None" = None,
        clock: Callable[[], float] | None = None,
        message_timeout_s: float = DEFAULT_MESSAGE_TIMEOUT_S,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
        max_mailbox_messages: int = MAX_MAILBOX_MESSAGES,
        max_pending_questions: int = MAX_PENDING_QUESTIONS,
    ) -> None:
        if not conversation_id:
            raise CommunicationError("conversation_id is required")
        if not pool_id:
            raise CommunicationError("pool_id is required")
        if message_timeout_s <= 0 or max_message_bytes <= 0:
            raise CommunicationError("communication limits must be positive")
        self.conversation_id = conversation_id
        self.parent_run_id = parent_run_id
        self.pool_id = pool_id
        self.policy_revision = policy_revision
        self._journal = journal
        self._clock = clock or time.time
        self._message_timeout_s = float(message_timeout_s)
        self._max_message_bytes = int(max_message_bytes)
        self._max_mailbox_messages = int(max_mailbox_messages)
        self._max_pending_questions = int(max_pending_questions)
        self._members: set[str] = {"main"}
        self._messages: dict[str, _MessageRecord] = {}
        self._mailboxes: dict[str, deque[str]] = defaultdict(deque)
        self._questions: dict[str, _QuestionRecord] = {}
        self._sequence: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()
        self._message_event = asyncio.Event()
        self._pending_event = asyncio.Event()
        self._closed = False
        #: When set, questions to the parent are refused with this status.
        self._parent_refusal = ""

    @classmethod
    def rehydrate(
        cls,
        *,
        conversation_id: str,
        parent_run_id: str,
        pool_id: str,
        policy_revision: str = "",
        journal: "ConversationJournal | None" = None,
        events: list[Mapping[str, object]] | None = None,
        active_workers: frozenset[str] = frozenset(),
    ) -> "AgentMessageBroker":
        """Rebuild durable messages/questions for the currently live workers."""
        broker = cls(
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            pool_id=pool_id,
            policy_revision=policy_revision,
            journal=journal,
        )
        raw_events: list[Mapping[str, object]] = events if events is not None else []
        if events is None and journal is not None:
            raw_events = cast(list[Mapping[str, object]], journal.fold_agent_message_events())
        broker._rehydrate_events(raw_events or [])
        broker._members.intersection_update({"main", *active_workers})
        for record in broker._questions.values():
            if record.status == "pending" and (
                record.envelope.sender not in broker._members
                or (
                    record.envelope.recipient != "main"
                    and record.envelope.recipient not in broker._members
                )
            ):
                record.status = "orphaned"
        return broker

    @property
    def members(self) -> frozenset[str]:
        """Return a snapshot of current pool members."""
        return frozenset(self._members)

    def register_worker(self, worker_id: str) -> None:
        """Register a worker before exposing its communication tools."""
        self._validate_identifier(worker_id, "worker_id")
        if worker_id in {"main", "pool"}:
            raise CommunicationError("worker_id is reserved")
        if self._closed:
            raise CommunicationError("pool communication is closed")
        self._members.add(worker_id)
        self._record("agent_pool_member", {"worker_id": worker_id, "state": "registered"})
        # A rehydrated broadcast may have been loaded before workers were
        # registered. Move it into this member's mailbox exactly once.
        for message_id, record in self._messages.items():
            if record.envelope.recipient in {worker_id, "pool"} and record.state == "queued":
                if message_id not in self._mailboxes[worker_id]:
                    self._mailboxes[worker_id].append(message_id)

    def for_retry(self, worker_id: str) -> "AgentMessageBroker":
        """Create a fresh, bounded broker for one retry after pool completion.

        Previous-attempt messages are not replayed: their senders are no longer
        running and cannot answer. The new attempt retains the pool identity and
        communication limits, with only the retried worker registered.
        """
        broker = AgentMessageBroker(
            conversation_id=self.conversation_id,
            parent_run_id=self.parent_run_id,
            pool_id=self.pool_id,
            policy_revision=self.policy_revision,
            journal=self._journal,
            clock=self._clock,
            message_timeout_s=self._message_timeout_s,
            max_message_bytes=self._max_message_bytes,
            max_mailbox_messages=self._max_mailbox_messages,
            max_pending_questions=self._max_pending_questions,
        )
        broker.register_worker(worker_id)
        return broker

    async def unregister_worker(self, worker_id: str, *, reason: str = "worker_lost") -> None:
        """Remove a worker and resolve its unanswered questions as orphaned."""
        async with self._lock:
            self._members.discard(worker_id)
            for question in self._questions.values():
                if question.envelope.sender == worker_id and question.status == "pending":
                    self._finish_question_locked(question, "", "orphaned")
            self._record("agent_message_rejected", {"sender": worker_id, "reason": reason})
            self._record("agent_pool_member", {"worker_id": worker_id, "state": "removed"})

    async def send(
        self,
        *,
        sender: str,
        recipient: str,
        kind: MessageKind | str,
        payload: Mapping[str, object] | str,
        reply_to: str = "",
        correlation_id: str = "",
        requires_response: bool = False,
        ttl_s: float | None = None,
    ) -> AgentMessageEnvelope:
        """Send one validated message and return its immutable envelope."""
        async with self._lock:
            return self._send_locked(
                sender=sender,
                recipient=recipient,
                kind=kind,
                payload=payload,
                reply_to=reply_to,
                correlation_id=correlation_id,
                requires_response=requires_response,
                ttl_s=ttl_s,
            )

    async def open_question(
        self,
        *,
        sender: str,
        recipient: str,
        question: str,
        context: str = "",
        expected_answer: str = "text",
        timeout_s: float = DEFAULT_MESSAGE_TIMEOUT_S,
    ) -> dict[str, object]:
        """Create a parent/peer question without invoking another provider."""
        if not question.strip():
            raise CommunicationError("question must not be empty")
        if timeout_s <= 0:
            raise CommunicationError("question timeout must be positive")
        async with self._lock:
            if recipient == "main" and self._parent_refusal:
                raise CommunicationError(
                    f"question is {self._parent_refusal}: the parent cannot answer while this "
                    "pool runs in the background; continue with your best judgement and say "
                    "what you assumed"
                )
            pending = sum(item.status == "pending" for item in self._questions.values())
            if pending >= self._max_pending_questions:
                raise CommunicationError("communication question quota exceeded")
            envelope = self._send_locked(
                sender=sender,
                recipient=recipient,
                kind="clarification_request",
                payload={
                    "question": question[: self._max_message_bytes // 2],
                    "context": context[: self._max_message_bytes // 2],
                    "expected_answer": expected_answer[:128],
                },
                requires_response=True,
                ttl_s=timeout_s,
            )
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            record = _QuestionRecord(envelope.message_id, envelope, future=future)
            self._questions[envelope.message_id] = record
            self._pending_event.set()
            self._record("question_wait_started", self._question_projection(record))
            return self._question_projection(record)

    async def answer_question(
        self,
        *,
        answerer: str,
        question_id: str,
        answer: str,
    ) -> dict[str, object]:
        """Resolve one pending question, enforcing parent/peer membership."""
        if not answer.strip():
            raise CommunicationError("answer must not be empty")
        async with self._lock:
            record = self._questions.get(question_id)
            if record is None:
                raise CommunicationError("unknown question_id")
            if record.status != "pending":
                return self._question_projection(record)
            expected = record.envelope.recipient
            if answerer != expected and not (expected == "pool" and answerer in self._members):
                raise CommunicationError("answerer is not the question recipient")
            self._finish_question_locked(record, answer[: self._max_message_bytes], "answered")
            self._record(
                "question_answered",
                {**self._question_projection(record), "answer": answer[: self._max_message_bytes]},
            )
            return self._question_projection(record)

    async def refuse_parent_questions(self, status: str) -> int:
        """End every pending question to the parent with *status*; refuse new ones.

        Used when a pool moves to the background under
        ``subagents.on_clarification = "fail"``: nobody will answer, so a worker
        must not wait out the message timeout. Returns how many were ended.
        """
        async with self._lock:
            self._parent_refusal = status[:64]
            ended = 0
            for record in list(self._questions.values()):
                if record.status == "pending" and record.envelope.recipient == "main":
                    self._finish_question_locked(record, "", self._parent_refusal)
                    self._record("question_refused", self._question_projection(record))
                    ended += 1
            return ended

    async def wait_for_answer(
        self,
        question_id: str,
        *,
        timeout_s: float | None = None,
    ) -> str:
        """Wait for an answer, returning an explicit terminal error on expiry."""
        record = self._questions.get(question_id)
        if record is None:
            raise CommunicationError("unknown question_id")
        if record.status != "pending":
            if record.status == "answered":
                record.delivered = True
                return record.answer
            raise CommunicationError(f"question is {record.status}")
        if record.future is None:
            record.future = asyncio.get_running_loop().create_future()
        try:
            answer = await asyncio.wait_for(
                record.future,
                timeout=timeout_s if timeout_s is not None else self._message_timeout_s,
            )
            record.delivered = True
            return answer
        except asyncio.TimeoutError as exc:
            async with self._lock:
                if record.status == "pending":
                    self._finish_question_locked(record, "", "expired")
                    self._record("question_timed_out", self._question_projection(record))
            raise CommunicationError("question timed out") from exc

    async def poll(self, recipient: str, *, limit: int = 16) -> list[dict[str, object]]:
        """Read and mark ordered messages for *recipient* as delivered."""
        if limit < 1:
            raise CommunicationError("limit must be positive")
        async with self._lock:
            self._validate_recipient(recipient)
            mailbox = self._mailboxes[recipient]
            broadcast_mailbox = self._mailboxes["pool"] if recipient != "pool" else deque()
            result: list[dict[str, object]] = []
            while (mailbox or broadcast_mailbox) and len(result) < min(limit, 64):
                # Direct messages have priority; broadcast messages remain
                # available to every member until that member acknowledges or
                # consumes them.
                source = mailbox if mailbox else broadcast_mailbox
                message_id = source.popleft()
                record = self._messages.get(message_id)
                if record is None or recipient in record.acknowledged_by:
                    continue
                if (
                    record.envelope.expires_at is not None
                    and record.envelope.expires_at <= self._clock()
                ):
                    record.state = "expired"
                    continue
                record.state = "delivered"
                result.append(record.envelope.to_dict())
                self._record(
                    "agent_message_delivered",
                    {"message_id": message_id, "recipient": recipient},
                )
            if not mailbox:
                self._message_event.clear()
            return result

    async def acknowledge(self, recipient: str, message_id: str) -> dict[str, object]:
        """Acknowledge a message exactly once."""
        async with self._lock:
            record = self._messages.get(message_id)
            if record is None:
                raise CommunicationError("unknown message_id")
            if record.envelope.recipient not in {recipient, "pool"}:
                raise CommunicationError("recipient does not own this message")
            record.acknowledged_by.add(recipient)
            recipients = (
                self._members
                if record.envelope.recipient == "pool"
                else {record.envelope.recipient}
            )
            record.acknowledged = recipients.issubset(record.acknowledged_by)
            if record.state not in {"expired", "rejected"}:
                record.state = "acknowledged" if record.acknowledged else "delivered"
            self._record(
                "agent_message_acknowledged",
                {"message_id": message_id, "recipient": recipient},
            )
            return {"ok": True, "message_id": message_id, "state": record.state}

    async def undelivered_count(self, recipient: str) -> int:
        """Count live messages this member has not acknowledged."""
        async with self._lock:
            self._validate_recipient(recipient)
            now = self._clock()
            return sum(
                1
                for record in self._messages.values()
                if record.state not in {"expired", "rejected"}
                and (record.envelope.expires_at is None or record.envelope.expires_at > now)
                and (
                    record.envelope.recipient == recipient
                    or (record.envelope.recipient == "pool" and recipient in self._members)
                )
                and recipient not in record.acknowledged_by
            )

    async def wait_for_pending(self) -> list[dict[str, object]]:
        """Wait until at least one parent-facing question is pending."""
        while True:
            pending = self.pending_questions(recipient="main")
            if pending:
                return pending
            if self._closed:
                raise CommunicationError("pool communication is closed")
            await self._pending_event.wait()

    def open_questions(self, *, sender: str) -> list[dict[str, object]]:
        """Questions *sender* asked whose answer it has not received yet.

        That includes a question answered before its asker looked for it: the
        asker still collects that answer with :meth:`wait_for_answer`, so an
        answer that arrives early is not lost.
        """
        now = self._clock()
        return [
            self._question_projection(item)
            for item in self._questions.values()
            if item.envelope.sender == sender
            and (item.status == "pending" or (item.status == "answered" and not item.delivered))
            and (item.envelope.expires_at is None or item.envelope.expires_at > now)
        ]

    def pending_questions(
        self,
        *,
        recipient: str | None = None,
        sender: str | None = None,
    ) -> list[dict[str, object]]:
        """Return bounded pending-question projections without private memory."""
        now = self._clock()
        result: list[dict[str, object]] = []
        for item in self._questions.values():
            if item.status != "pending":
                continue
            if recipient is not None and item.envelope.recipient != recipient:
                continue
            if sender is not None and item.envelope.sender != sender:
                continue
            if item.envelope.expires_at is not None and item.envelope.expires_at <= now:
                continue
            result.append(self._question_projection(item))
        return result

    async def close(self, *, reason: str = "pool_closed") -> None:
        """Close the broker and resolve all outstanding questions."""
        async with self._lock:
            self._closed = True
            for record in self._questions.values():
                if record.status == "pending":
                    self._finish_question_locked(record, "", "cancelled")
                    self._record("question_cancelled", self._question_projection(record))
            self._pending_event.set()
            self._message_event.set()

    def message(self, message_id: str) -> AgentMessageEnvelope | None:
        """Return an envelope for diagnostics/tests without exposing mutable state."""
        record = self._messages.get(message_id)
        return record.envelope if record is not None else None

    def _send_locked(
        self,
        *,
        sender: str,
        recipient: str,
        kind: MessageKind | str,
        payload: Mapping[str, object] | str,
        reply_to: str = "",
        correlation_id: str = "",
        requires_response: bool = False,
        ttl_s: float | None = None,
    ) -> AgentMessageEnvelope:
        self._validate_sender(sender)
        self._validate_recipient(recipient)
        if kind not in MESSAGE_KINDS:
            raise CommunicationError(f"unknown message kind: {kind!r}")
        payload_dict: dict[str, object] = (
            {"text": payload} if isinstance(payload, str) else dict(payload)
        )
        encoded = json.dumps(payload_dict, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) > self._max_message_bytes:
            raise CommunicationError("message payload exceeds the configured byte limit")
        targets = (
            [member for member in self._members if member != sender]
            if recipient == "pool"
            else [recipient]
        )
        if recipient == "pool" and not targets:
            raise CommunicationError("pool has no recipient workers")
        if any(len(self._mailboxes[target]) >= self._max_mailbox_messages for target in targets):
            raise CommunicationError("recipient mailbox is full")
        if correlation_id:
            for record in self._messages.values():
                envelope = record.envelope
                if (
                    envelope.sender == sender
                    and envelope.recipient == recipient
                    and envelope.correlation_id == correlation_id
                    and envelope.kind == str(kind)
                    and envelope.payload == payload_dict
                ):
                    return envelope
        self._sequence[sender] += 1
        now = float(self._clock())
        message_id = uuid.uuid4().hex
        envelope = AgentMessageEnvelope(
            message_id=message_id,
            conversation_id=self.conversation_id,
            parent_run_id=self.parent_run_id,
            pool_id=self.pool_id,
            sender=sender,
            recipient=recipient,
            kind=str(kind),
            payload=payload_dict,
            reply_to=reply_to,
            correlation_id=correlation_id or message_id,
            sequence=self._sequence[sender],
            created_at=now,
            expires_at=now + float(ttl_s if ttl_s is not None else self._message_timeout_s),
            requires_response=requires_response,
            policy_revision=self.policy_revision,
        )
        self._messages[message_id] = _MessageRecord(envelope, state="queued")
        for target in targets:
            self._mailboxes[target].append(message_id)
        self._message_event.set()
        self._record("agent_message_sent", envelope.to_dict())
        return envelope

    def _question_projection(self, record: _QuestionRecord) -> dict[str, object]:
        return {
            "question_id": record.question_id,
            "pool_id": self.pool_id,
            "worker_id": record.envelope.sender,
            "recipient": record.envelope.recipient,
            "status": record.status,
            "question": record.envelope.payload.get("question", ""),
            "context": record.envelope.payload.get("context", ""),
            "expected_answer": record.envelope.payload.get("expected_answer", "text"),
            "answer": record.answer,
            "correlation_id": record.envelope.correlation_id,
            "expires_at": record.envelope.expires_at,
        }

    def _rehydrate_events(self, events: list[Mapping[str, object]]) -> None:
        """Apply journal records without emitting duplicate journal events."""
        for raw in events:
            if str(raw.get("pool_id", "")) != self.pool_id:
                continue
            payload = raw.get("payload")
            if not isinstance(payload, Mapping):
                continue
            kind = str(raw.get("event_kind", ""))
            if kind == "agent_message_sent":
                try:
                    envelope = AgentMessageEnvelope(
                        message_id=str(payload.get("message_id", "")),
                        conversation_id=str(payload.get("conversation_id", "")),
                        parent_run_id=str(payload.get("parent_run_id", "")),
                        pool_id=str(payload.get("pool_id", "")),
                        sender=str(payload.get("sender", "")),
                        recipient=str(payload.get("recipient", "")),
                        kind=str(payload.get("kind", "")),
                        payload=(
                            dict(payload.get("payload", {}))
                            if isinstance(payload.get("payload", {}), Mapping)
                            else {}
                        ),
                        reply_to=str(payload.get("reply_to", "")),
                        correlation_id=str(payload.get("correlation_id", "")),
                        sequence=int(payload.get("sequence", 0)),
                        created_at=float(payload.get("created_at", 0.0)),
                        expires_at=(
                            float(payload["expires_at"])
                            if payload.get("expires_at") is not None
                            else None
                        ),
                        requires_response=bool(payload.get("requires_response", False)),
                        policy_revision=str(payload.get("policy_revision", "")),
                    )
                except (TypeError, ValueError):
                    continue
                if (
                    envelope.conversation_id != self.conversation_id
                    or envelope.parent_run_id != self.parent_run_id
                    or envelope.pool_id != self.pool_id
                    or (envelope.sender not in self._members and envelope.sender != "main")
                    or (envelope.recipient != "pool" and envelope.recipient not in self._members)
                ):
                    continue
                if envelope.message_id in self._messages:
                    continue
                self._messages[envelope.message_id] = _MessageRecord(envelope, state="queued")
                targets = (
                    [member for member in self._members if member != envelope.sender]
                    if envelope.recipient == "pool"
                    else [envelope.recipient]
                )
                for target in targets:
                    self._mailboxes[target].append(envelope.message_id)
                if envelope.recipient == "pool" and not targets:
                    self._mailboxes["pool"].append(envelope.message_id)
                self._sequence[envelope.sender] = max(
                    self._sequence[envelope.sender], envelope.sequence
                )
            elif kind == "agent_pool_member":
                worker_id = str(payload.get("worker_id", ""))
                if worker_id:
                    if payload.get("state") == "removed":
                        self._members.discard(worker_id)
                    else:
                        self._members.add(worker_id)
            elif kind == "question_wait_started":
                question_id = str(payload.get("question_id", ""))
                message = self._messages.get(question_id)
                if message is not None and question_id not in self._questions:
                    self._questions[question_id] = _QuestionRecord(question_id, message.envelope)
            elif kind in {"question_answered", "question_timed_out", "question_cancelled"}:
                question_id = str(payload.get("question_id", ""))
                record = self._questions.get(question_id)
                if record is not None and record.status == "pending":
                    status = {
                        "question_answered": "answered",
                        "question_timed_out": "expired",
                        "question_cancelled": "cancelled",
                    }[kind]
                    record.status = status
                    record.answer = str(payload.get("answer", ""))
            elif kind == "agent_message_delivered":
                message_record = self._messages.get(str(payload.get("message_id", "")))
                if message_record is not None:
                    message_record.state = "delivered"
            elif kind == "agent_message_acknowledged":
                message_record = self._messages.get(str(payload.get("message_id", "")))
                if message_record is not None:
                    recipient = str(payload.get("recipient", ""))
                    if recipient:
                        message_record.acknowledged_by.add(recipient)
                    else:
                        message_record.acknowledged_by.update(self._members)
                    recipients = (
                        self._members
                        if message_record.envelope.recipient == "pool"
                        else {message_record.envelope.recipient}
                    )
                    message_record.acknowledged = recipients.issubset(
                        message_record.acknowledged_by
                    )
                    message_record.state = (
                        "acknowledged" if message_record.acknowledged else "delivered"
                    )
        for message_record in self._messages.values():
            if message_record.state in {"delivered", "acknowledged", "expired", "rejected"}:
                for mailbox in self._mailboxes.values():
                    try:
                        mailbox.remove(message_record.envelope.message_id)
                    except ValueError:
                        continue
        if self.pending_questions(recipient="main"):
            self._pending_event.set()

    def _finish_question_locked(self, record: _QuestionRecord, answer: str, status: str) -> None:
        record.answer = answer
        record.status = status
        if record.future is not None and not record.future.done():
            if status == "answered":
                record.future.set_result(answer)
            else:
                record.future.set_exception(CommunicationError(f"question is {status}"))
        if not self.pending_questions(recipient="main"):
            self._pending_event.clear()

    def _validate_sender(self, sender: str) -> None:
        self._validate_identifier(sender, "sender")
        if sender not in self._members:
            raise CommunicationError("sender is not a member of this pool")

    def _validate_recipient(self, recipient: str) -> None:
        self._validate_identifier(recipient, "recipient")
        if recipient != "pool" and recipient not in self._members:
            raise CommunicationError("recipient is not a member of this pool")

    def _record(self, kind: str, payload: Mapping[str, object]) -> None:
        if self._journal is None:
            return
        try:
            self._journal.agent_message_event(
                pool_id=self.pool_id,
                conversation_id=self.conversation_id,
                parent_run_id=self.parent_run_id,
                policy_revision=self.policy_revision,
                event_kind=kind,
                payload=dict(payload),
            )
        except (OSError, TypeError, ValueError):
            # Communication telemetry must not make a valid agent exchange fail.
            return

    @staticmethod
    def _validate_identifier(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise CommunicationError(f"{field_name} must be a non-empty bounded string")


#: Finished pools a conversation keeps reachable for collect and retry.
MAX_FINISHED_POOL_HANDLES = 32


class PoolContinuationRegistry:
    """Conversation-scoped registry of the pools a parent can reach.

    Every pool is registered when it starts. A finished pool stays reachable —
    for ``collect_subagent_results`` and ``retry_subagent`` — until it is one
    of more than :data:`MAX_FINISHED_POOL_HANDLES` finished pools, when the
    oldest finished ones are dropped. Running pools are never dropped. A pool
    that is no longer held is still collectable from the journal.
    """

    def __init__(self) -> None:
        self._handles: dict[str, PoolHandle] = {}

    def register(self, handle: PoolHandle) -> None:
        self._handles.pop(handle.pool_id, None)
        self._handles[handle.pool_id] = handle
        finished = [pool_id for pool_id, item in self._handles.items() if item.task.done()]
        for pool_id in finished[: max(0, len(finished) - MAX_FINISHED_POOL_HANDLES)]:
            self._handles.pop(pool_id, None)

    def handles(self) -> tuple[PoolHandle, ...]:
        """Every registered handle, oldest first."""
        return tuple(self._handles.values())

    def get(self, pool_id: str) -> PoolHandle | None:
        return self._handles.get(pool_id)

    def remove(self, pool_id: str) -> None:
        self._handles.pop(pool_id, None)


_SESSION_REGISTRIES: dict[str, PoolContinuationRegistry] = {}
#: Registries owned by a conversation store, released with the store.
_STORE_REGISTRIES: weakref.WeakKeyDictionary[object, PoolContinuationRegistry] = (
    weakref.WeakKeyDictionary()
)


def registry_for_session(
    conversation_id: str,
    conversation_store: object | None = None,
) -> PoolContinuationRegistry:
    """Return the session-owned continuation registry.

    A ``ConversationStore`` is preferred because it naturally follows the
    session lifetime.  The keyed fallback supports headless callers that do
    not have a reactive store while avoiding an unkeyed global pool namespace.
    """
    if conversation_store is not None:
        registry = _STORE_REGISTRIES.get(conversation_store)
        if registry is None:
            registry = _STORE_REGISTRIES[conversation_store] = PoolContinuationRegistry()
        return registry
    return _SESSION_REGISTRIES.setdefault(conversation_id, PoolContinuationRegistry())


def make_child_communication_tools(
    broker: AgentMessageBroker,
    worker_id: str,
) -> list[object]:
    """Build tools bound to one worker identity."""
    from lauren_ai._tools import tool

    @tool_control
    @tool()
    async def send_parent_message(message: str, kind: str = "information") -> dict[str, object]:
        """Send a bounded status or finding to the main agent."""
        envelope = await broker.send(
            sender=worker_id,
            recipient="main",
            kind=kind,
            payload={"text": message},
        )
        return {"ok": True, "message_id": envelope.message_id, "status": "sent"}

    @tool_control
    @tool()
    async def ask_parent(
        question: str,
        context: str = "",
        expected_answer: str = "text",
        timeout_s: float = DEFAULT_MESSAGE_TIMEOUT_S,
    ) -> dict[str, object]:
        """Ask the main agent for missing information and await continuation."""
        return await broker.open_question(
            sender=worker_id,
            recipient="main",
            question=question,
            context=context,
            expected_answer=expected_answer,
            timeout_s=timeout_s,
        )

    @tool_control
    @tool()
    async def poll_agent_messages(limit: int = 16) -> dict[str, object]:
        """Read ordered parent and peer messages for this worker."""
        messages = await broker.poll(worker_id, limit=limit)
        return {"ok": True, "messages": messages}

    @tool_control
    @tool()
    async def send_peer_message(
        recipient: str,
        message: str,
        kind: str = "information",
    ) -> dict[str, object]:
        """Send a bounded message to another worker in this pool."""
        envelope = await broker.send(
            sender=worker_id,
            recipient=recipient,
            kind=kind,
            payload={"text": message},
        )
        return {"ok": True, "message_id": envelope.message_id, "status": "sent"}

    @tool_control
    @tool()
    async def ask_peer(
        recipient: str,
        question: str,
        context: str = "",
        timeout_s: float = DEFAULT_MESSAGE_TIMEOUT_S,
    ) -> dict[str, object]:
        """Ask a same-pool peer for information."""
        return await broker.open_question(
            sender=worker_id,
            recipient=recipient,
            question=question,
            context=context,
            timeout_s=timeout_s,
        )

    @tool_control
    @tool()
    async def answer_peer(question_id: str, answer: str) -> dict[str, object]:
        """Answer a question addressed to this worker."""
        result = await broker.answer_question(
            answerer=worker_id,
            question_id=question_id,
            answer=answer,
        )
        return {"ok": True, **result}

    return [
        send_parent_message,
        ask_parent,
        poll_agent_messages,
        send_peer_message,
        ask_peer,
        answer_peer,
    ]


def make_parent_communication_tools(
    registry: PoolContinuationRegistry,
    conversation_id: str,
    journal: "ConversationJournal | None" = None,
    *,
    collect_wait_max_s: float = DEFAULT_COLLECT_WAIT_MAX_S,
    owner: str = "",
) -> list[object]:
    """Build the tools through which an agent controls the pools it spawned.

    *owner* is the spawning worker (``pool_id:task_id``) these tools belong to,
    or ``""`` for the session's main agent. A worker's tools reach only pools
    whose lineage contains it — its own descendants — never a sibling's or
    its parent's pools (SUBAGENT-11 goal 8).
    """
    from lauren_ai._tools import tool

    def _in_scope(handle: PoolHandle) -> bool:
        return handle.conversation_id == conversation_id and (not owner or owner in handle.lineage)

    def _handle(pool_id: str) -> PoolHandle:
        handle = registry.get(pool_id)
        if handle is None or not _in_scope(handle):
            raise CommunicationError("unknown subagent pool for this session")
        return handle

    @tool_control
    @tool()
    async def answer_subagent(
        pool_id: str,
        question_id: str,
        answer: str,
    ) -> dict[str, object]:
        """Answer a clarification raised by one of your subagents."""
        handle = _handle(pool_id)
        result = await handle.broker.answer_question(
            answerer="main",
            question_id=question_id,
            answer=answer,
        )
        return {"ok": True, **result}

    @tool_control
    @tool()
    async def send_subagent_message(
        pool_id: str,
        worker_id: str,
        message: str,
        kind: str = "instruction",
    ) -> dict[str, object]:
        """Send a bounded instruction or clarification to one worker."""
        handle = _handle(pool_id)
        envelope = await handle.broker.send(
            sender="main",
            recipient=worker_id,
            kind=kind,
            payload={"text": message},
        )
        return {"ok": True, "message_id": envelope.message_id, "status": "sent"}

    @tool_control
    @tool()
    async def collect_subagent_results(
        pool_id: str,
        wait: bool = False,
        timeout_s: float | None = None,
    ) -> dict[str, object]:
        """Read a subagent pool's results; it never blocks unless wait is true.

        A running pool returns status "running" with one row per worker and any
        question a worker is waiting on. A finished pool returns its results,
        the same way every time you ask, including after the session resumed.
        wait=true waits up to timeout_s seconds (capped by the configured
        maximum) and, if the pool is still running, returns the running
        snapshot with timed_out=true. Do not poll: you are notified when a
        background pool finishes.
        """
        if not isinstance(wait, bool):
            raise CommunicationError("wait must be true or false")
        bound = collect_wait_max_s
        if timeout_s is not None:
            if (
                isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s)
                or timeout_s < 0
            ):
                raise CommunicationError("timeout_s must be a finite non-negative number")
            bound = min(float(timeout_s), collect_wait_max_s)
        handle = registry.get(pool_id)
        if handle is not None and not _in_scope(handle):
            raise CommunicationError("unknown subagent pool for this session")
        if handle is None:
            # Journal records carry no lineage, so only the main agent may
            # read a pool it no longer holds.
            if owner:
                raise CommunicationError("unknown subagent pool for this session")
            return _journaled_pool_payload(journal, pool_id)
        if wait and not handle.task.done() and bound > 0:
            await asyncio.wait({handle.task}, timeout=bound)
        if not handle.task.done():
            return _running_pool_payload(handle, timed_out=wait)
        return _finished_pool_payload(handle)

    @tool_control
    @tool()
    async def list_subagent_pools() -> dict[str, object]:
        """List the subagent pools you can reach: running, finished and interrupted.

        Each row has the pool id, status, whether it runs in the background,
        worker counts, depth, and whether its completion notice is still to be
        delivered. Use the pool id with collect_subagent_results,
        stop_subagents or retry_subagent.
        """
        rows: dict[str, dict[str, object]] = {}
        if not owner and journal is not None:
            undelivered = {
                str(record.get("pool_id", ""))
                for record in journal.completed_background_subagent_pools()
            }
            for record in journal.fold_subagent_pool_lifecycle():
                pool_id = record.get("pool_id")
                if not isinstance(pool_id, str) or not pool_id:
                    continue
                rows[pool_id] = {
                    "pool_id": pool_id,
                    "status": str(record.get("status", "")),
                    "background": record.get("backgrounded") is True,
                    "total": record.get("total", 0),
                    "succeeded": record.get("succeeded", 0),
                    "failed": record.get("failed", 0),
                    "notice_pending": pool_id in undelivered,
                }
        for handle in registry.handles():
            if not _in_scope(handle):
                continue
            row = rows.setdefault(handle.pool_id, {"pool_id": handle.pool_id})
            row["background"] = bool(row.get("background")) or handle.backgrounded
            if not handle.task.done():
                row["status"] = "running"
            pool = handle.pool
            if pool is not None:
                state = pool.state
                row["total"] = state.total
                row["done"] = state.done
                row["depth"] = state.depth
        return {"ok": True, "pools": list(rows.values())}

    @tool_control
    @tool()
    async def stop_subagents(
        pool_id: str,
        worker_id: str | None = None,
        mode: str | None = None,
        reason: str = "",
    ) -> dict[str, object]:
        """Stop one worker, or every worker of a pool you spawned.

        mode "abort" cancels now and keeps any partial output; "drain" lets
        running workers finish and starts no pending ones. Omitted, it is
        "abort" for one worker and "drain" for a whole pool. Stopping is final
        for that attempt; retry_subagent starts a new one.
        """
        handle = _handle(pool_id)
        pool = handle.pool
        if pool is None or handle.task.done():
            return {"ok": False, "pool_id": pool_id, "error": "the pool is not running"}
        chosen = mode if mode is not None else ("abort" if worker_id else "drain")
        if chosen not in {"abort", "drain"}:
            raise CommunicationError("mode must be 'abort' or 'drain'")
        stop_mode = cast(Literal["abort", "drain"], chosen)
        stop_reason = (reason.strip() or "parent_model_requested")[:256]
        if worker_id is not None:
            request = pool.request_stop(
                worker_id, mode=stop_mode, source="parent_model", reason=stop_reason
            )
            if request is None:
                return {
                    "ok": False,
                    "pool_id": pool_id,
                    "worker_id": worker_id,
                    "error": "the worker is not running or does not exist",
                }
            lifecycle = pool.lifecycle(worker_id)
            return {
                "ok": True,
                "pool_id": pool_id,
                "workers": [
                    {
                        "worker_id": worker_id,
                        "request_id": request.request_id,
                        "mode": request.mode,
                        "phase": lifecycle.phase if lifecycle is not None else "",
                    }
                ],
            }
        # A drain starts no pending worker; say which ones will never start.
        never_started = [
            item.worker_id for item in pool.state.lifecycles if item.phase == "pending"
        ]
        outcomes = pool.request_pool_stop(mode=stop_mode, source="parent_model", reason=stop_reason)
        return {
            "ok": True,
            "pool_id": pool_id,
            "not_started": never_started if stop_mode == "drain" else [],
            "workers": [
                {
                    "worker_id": item.worker_id,
                    "request_id": item.stop_request.request_id if item.stop_request else "",
                    "mode": stop_mode,
                    "phase": item.phase,
                }
                for item in outcomes
            ],
        }

    @tool_control
    @tool()
    async def retry_subagent(pool_id: str, worker_id: str) -> dict[str, object]:
        """Run a new attempt of one failed or cancelled worker of a finished pool.

        Succeeded workers are never rerun. The retry is refused, with the
        reason, while the pool is still running or after its wall-clock limit.
        """
        handle = _handle(pool_id)
        pool = handle.pool
        if pool is None:
            return {"ok": False, "pool_id": pool_id, "error": "the pool cannot be retried"}
        refusal = pool.retry_refusal(worker_id)
        if refusal:
            return {"ok": False, "pool_id": pool_id, "worker_id": worker_id, "error": refusal}
        result = await pool.retry_worker(worker_id)
        if result is None:
            return {
                "ok": False,
                "pool_id": pool_id,
                "worker_id": worker_id,
                "error": pool.retry_refusal(worker_id) or "the retry did not start",
            }
        lifecycle = pool.lifecycle(worker_id)
        return {
            "ok": result.ok,
            "pool_id": pool_id,
            "worker_id": worker_id,
            "attempt": lifecycle.attempt if lifecycle is not None else 1,
            "results": result.text,
            "error": result.error,
        }

    return [
        answer_subagent,
        send_subagent_message,
        collect_subagent_results,
        list_subagent_pools,
        stop_subagents,
        retry_subagent,
    ]


def _running_pool_payload(handle: PoolHandle, *, timed_out: bool) -> dict[str, object]:
    """A non-blocking snapshot of a pool that is still running."""
    from agenthicc.subagents.pool import active_pool

    now = time.time()
    rows: list[dict[str, object]] = []
    pool = active_pool(handle.pool_id)
    if pool is not None:
        for worker in pool.state.workers:
            lifecycle = pool.lifecycle(worker.worker_id)
            started = worker.started_at
            finished = worker.finished_at
            rows.append(
                {
                    "worker_id": worker.worker_id,
                    "label": worker.label,
                    "type": worker.agent_type,
                    "status": worker.status,
                    "attempt": lifecycle.attempt if lifecycle is not None else 1,
                    "elapsed_s": (
                        round((finished or now) - started, 3) if started is not None else 0.0
                    ),
                }
            )
    payload: dict[str, object] = {
        "ok": True,
        "status": "running",
        "pool_id": handle.pool_id,
        "workers": rows,
        "pending_questions": handle.broker.pending_questions(recipient="main"),
    }
    if timed_out:
        payload["timed_out"] = True
    return payload


def _finished_pool_payload(handle: PoolHandle) -> dict[str, object]:
    """The result of a finished pool task; idempotent, the handle is kept."""
    from agenthicc.subagents.pool import AggregatedResult

    task = handle.task
    if task.cancelled():
        return {"ok": False, "status": "cancelled", "pool_id": handle.pool_id, "results": ""}
    error = task.exception()
    if error is not None:
        return {
            "ok": False,
            "status": "failed",
            "pool_id": handle.pool_id,
            "error": f"{type(error).__name__}: {error}"[:1_000],
            "results": "",
        }
    result = task.result()
    if not isinstance(result, AggregatedResult):
        return {"ok": True, "status": "completed", "pool_id": handle.pool_id, "results": result}
    stopped = bool(result.budget_exceeded_reason)
    return {
        "ok": result.failed == 0 and not stopped,
        "status": "stopped" if stopped else "completed",
        "pool_id": handle.pool_id,
        "total": result.total,
        "succeeded": result.succeeded,
        "failed": result.failed,
        "budget_exceeded_reason": result.budget_exceeded_reason,
        "results": result.text,
        "values": [dict(value) for value in result.values],
        "usage": result.usage.to_dict(),
    }


_TERMINAL_POOL_STATUSES = frozenset({"completed", "stopped", "interrupted", "cancelled"})


def _journaled_pool_payload(
    journal: "ConversationJournal | None", pool_id: str
) -> dict[str, object]:
    """Read a pool that has no live handle from this conversation's journal."""
    record = (
        next(
            (
                item
                for item in journal.fold_subagent_pool_lifecycle()
                if item.get("pool_id") == pool_id
            ),
            None,
        )
        if journal is not None
        else None
    )
    if record is None:
        raise CommunicationError("unknown subagent pool for this session")
    status = str(record.get("status", ""))
    if status not in _TERMINAL_POOL_STATUSES:
        return {
            "ok": False,
            "status": "unavailable",
            "pool_id": pool_id,
            "error": (
                "the pool is not running in this process; it is reported as interrupted "
                "when the session is resumed"
            ),
            "results": "",
        }

    def count(key: str) -> int:
        value = record.get(key, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    reason_value = record.get("reason", "")
    reason = reason_value if isinstance(reason_value, str) else ""
    text = record.get("text", "")
    values = record.get("values", [])
    failed = count("failed")
    return {
        "ok": status == "completed" and failed == 0,
        "status": status,
        "pool_id": pool_id,
        "total": count("total"),
        "succeeded": count("succeeded"),
        "failed": failed,
        "budget_exceeded_reason": reason if reason.startswith("max_") else "",
        "results": text if isinstance(text, str) else "",
        "values": [dict(value) for value in values if isinstance(value, Mapping)]
        if isinstance(values, list)
        else [],
    }
