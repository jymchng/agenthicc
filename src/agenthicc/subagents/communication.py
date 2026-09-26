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
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from agenthicc.tools.capabilities import tool_control

if TYPE_CHECKING:
    from agenthicc.memory.journal import ConversationJournal

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


@dataclass
class _QuestionRecord:
    question_id: str
    envelope: AgentMessageEnvelope
    status: str = "pending"
    answer: str = ""
    future: asyncio.Future[str] | None = None


@dataclass
class PoolHandle:
    """A resumable pool owned by one session."""

    pool_id: str
    conversation_id: str
    broker: "AgentMessageBroker"
    task: asyncio.Task[object]


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
                return record.answer
            raise CommunicationError(f"question is {record.status}")
        if record.future is None:
            record.future = asyncio.get_running_loop().create_future()
        try:
            answer = await asyncio.wait_for(
                record.future,
                timeout=timeout_s if timeout_s is not None else self._message_timeout_s,
            )
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
                if record is None:
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
            record.acknowledged = True
            if record.state not in {"expired", "rejected"}:
                record.state = "acknowledged"
            self._record(
                "agent_message_acknowledged",
                {"message_id": message_id, "recipient": recipient},
            )
            return {"ok": True, "message_id": message_id, "state": record.state}

    async def wait_for_pending(self) -> list[dict[str, object]]:
        """Wait until at least one parent-facing question is pending."""
        while True:
            pending = self.pending_questions(recipient="main")
            if pending:
                return pending
            if self._closed:
                raise CommunicationError("pool communication is closed")
            await self._pending_event.wait()

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
                    message_record.state = "acknowledged"
                    message_record.acknowledged = True
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


class PoolContinuationRegistry:
    """Session-keyed registry for pools that temporarily await answers."""

    def __init__(self) -> None:
        self._handles: dict[str, PoolHandle] = {}

    def register(self, handle: PoolHandle) -> None:
        self._handles[handle.pool_id] = handle

    def get(self, pool_id: str) -> PoolHandle | None:
        handle = self._handles.get(pool_id)
        if handle is not None and handle.task.done():
            return handle
        return handle

    def remove(self, pool_id: str) -> None:
        self._handles.pop(pool_id, None)

    async def collect(self, pool_id: str) -> object:
        handle = self._handles.get(pool_id)
        if handle is None:
            raise CommunicationError("unknown or expired subagent pool")
        try:
            return await handle.task
        finally:
            self._handles.pop(pool_id, None)


_SESSION_REGISTRIES: dict[str, PoolContinuationRegistry] = {}


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
        existing = vars(conversation_store).get("_subagent_pool_registry")
        if isinstance(existing, PoolContinuationRegistry):
            return existing
        registry = PoolContinuationRegistry()
        setattr(conversation_store, "_subagent_pool_registry", registry)
        return registry
    return _SESSION_REGISTRIES.setdefault(conversation_id, PoolContinuationRegistry())


def make_child_communication_tools(
    broker: AgentMessageBroker,
    worker_id: str,
) -> list[object]:
    """Build tools bound to one worker identity."""
    from lauren_ai._tools import tool  # noqa: PLC0415

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
) -> list[object]:
    """Build tools through which the main agent controls a live pool."""
    from lauren_ai._tools import tool  # noqa: PLC0415

    def _handle(pool_id: str) -> PoolHandle:
        handle = registry.get(pool_id)
        if handle is None or handle.conversation_id != conversation_id:
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
    async def collect_subagent_results(pool_id: str) -> dict[str, object]:
        """Collect a pool that was left running for a clarification answer."""
        _handle(pool_id)
        result = await registry.collect(pool_id)
        from agenthicc.subagents.pool import AggregatedResult  # noqa: PLC0415

        if isinstance(result, AggregatedResult):
            return {
                "ok": result.failed == 0,
                "pool_id": pool_id,
                "total": result.total,
                "succeeded": result.succeeded,
                "failed": result.failed,
                "results": result.text,
            }
        return {"ok": True, "pool_id": pool_id, "results": result}

    return [answer_subagent, send_subagent_message, collect_subagent_results]
