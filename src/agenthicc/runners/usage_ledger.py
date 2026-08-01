"""Session-scoped provider usage accounting (PRD-157).

The ledger is the durable authority for provider token and cost totals.  The
reactive TUI store is only a projection of :class:`UsageSnapshot`; callers may
observe provisional values while a provider call is streaming, but only
completed/reconciled records are written to the session journal.

The module deliberately contains no provider-specific imports.  Lauren-ai
signals and transport ``TokenUsage`` objects are read through the small
attribute-based adapters below so the accounting boundary remains stable when
the transport implementation changes.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.tui.conversation_store import ConversationStore

__all__ = [
    "CostStatus",
    "DurabilityStatus",
    "UsageCall",
    "UsageCategory",
    "UsageLedger",
    "UsageLifecycle",
    "UsageQuality",
    "UsageRecord",
    "UsageSignalSink",
    "UsageSnapshot",
    "UsageSource",
    "UsageValues",
    "UsageRunTracker",
    "summarize_usage_records",
]


class UsageQuality(StrEnum):
    """Quality of the token data represented by an aggregate."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class CostStatus(StrEnum):
    """Provenance of the displayed cost."""

    AUTHORITATIVE = "authoritative"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class DurabilityStatus(StrEnum):
    """Whether completed records have been accepted by the journal."""

    DURABLE = "durable"
    DEGRADED = "degraded"


class UsageSource(StrEnum):
    """Source used to populate a usage record."""

    CHUNK = "chunk"
    MODEL_CALL_COMPLETE = "model_call_complete"
    RUN_COMPLETE = "run_complete"
    RECONCILED = "reconciled"
    LEGACY = "legacy"
    UNKNOWN = "unknown"


class UsageLifecycle(StrEnum):
    """Lifecycle state of one provider call."""

    PROVISIONAL = "provisional"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


class UsageCategory(StrEnum):
    """Billable session activity categories."""

    AGENT = "agent"
    SUBAGENT = "subagent"
    COMPACTION = "compaction"


@dataclass(frozen=True)
class UsageValues:
    """Provider usage values, where ``None`` means unavailable."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    @classmethod
    def from_provider(cls, value: object | None) -> "UsageValues":
        """Adapt a provider ``TokenUsage``-like object safely."""
        if value is None:
            return cls()
        return cls(
            input_tokens=_optional_int(getattr(value, "input_tokens", None)),
            output_tokens=_optional_int(getattr(value, "output_tokens", None)),
            cache_read_tokens=_optional_int(getattr(value, "cache_read_tokens", None)),
            cache_write_tokens=_optional_int(getattr(value, "cache_write_tokens", None)),
        )

    @property
    def known(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
        )

    @property
    def any_tokens(self) -> bool:
        return any((value or 0) > 0 for value in self._token_values())

    def _token_values(self) -> tuple[int | None, ...]:
        return (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        )

    def add(self, other: "UsageValues") -> "UsageValues":
        """Add two values while preserving unknown fields as unknown."""
        return UsageValues(
            input_tokens=_sum_optional(self.input_tokens, other.input_tokens),
            output_tokens=_sum_optional(self.output_tokens, other.output_tokens),
            cache_read_tokens=_sum_optional(self.cache_read_tokens, other.cache_read_tokens),
            cache_write_tokens=_sum_optional(self.cache_write_tokens, other.cache_write_tokens),
        )

    def subtract_known(self, other: "UsageValues") -> "UsageValues":
        """Return the non-negative known residual of two aggregates."""
        return UsageValues(
            input_tokens=_residual(self.input_tokens, other.input_tokens),
            output_tokens=_residual(self.output_tokens, other.output_tokens),
            cache_read_tokens=_residual(self.cache_read_tokens, other.cache_read_tokens),
            cache_write_tokens=_residual(self.cache_write_tokens, other.cache_write_tokens),
        )


@dataclass(frozen=True)
class UsageRecord:
    """One immutable provider-call usage record."""

    record_id: str
    session_id: str
    conversation_id: str
    run_id: str
    agent_id: str
    agent_name: str
    call_index: int
    provider: str
    model: str
    category: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    cost_status: str
    source: str
    lifecycle: str
    created_at: float
    completed_at: float | None

    @property
    def values(self) -> UsageValues:
        return UsageValues(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
        )

    @property
    def is_known(self) -> bool:
        return self.values.known

    @property
    def is_provisional(self) -> bool:
        return self.lifecycle == UsageLifecycle.PROVISIONAL

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-safe journal representation."""
        return {
            "record_id": self.record_id,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "call_index": self.call_index,
            "provider": self.provider,
            "model": self.model,
            "category": self.category,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "cost_status": self.cost_status,
            "source": self.source,
            "lifecycle": self.lifecycle,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "UsageRecord | None":
        """Validate and decode one persisted record; malformed records skip."""
        record_id = value.get("record_id")
        session_id = value.get("session_id")
        if not isinstance(record_id, str) or not record_id:
            return None
        if not isinstance(session_id, str) or not session_id:
            return None
        call_index = _nonnegative_int(value.get("call_index"))
        created_at = _finite_float(value.get("created_at"))
        if created_at is None:
            return None
        completed_at = _finite_float(value.get("completed_at"))
        cost = _finite_float(value.get("cost_usd"))
        return cls(
            record_id=record_id,
            session_id=session_id,
            conversation_id=_text(value.get("conversation_id")),
            run_id=_text(value.get("run_id")),
            agent_id=_text(value.get("agent_id")),
            agent_name=_text(value.get("agent_name")),
            call_index=call_index,
            provider=_text(value.get("provider"), "unknown"),
            model=_text(value.get("model"), "unknown"),
            category=_text(value.get("category"), UsageCategory.AGENT),
            input_tokens=_optional_int(value.get("input_tokens")),
            output_tokens=_optional_int(value.get("output_tokens")),
            cache_read_tokens=_optional_int(value.get("cache_read_tokens")),
            cache_write_tokens=_optional_int(value.get("cache_write_tokens")),
            cost_usd=cost,
            cost_status=_text(value.get("cost_status"), CostStatus.UNAVAILABLE),
            source=_text(value.get("source"), UsageSource.UNKNOWN),
            lifecycle=_text(value.get("lifecycle"), UsageLifecycle.COMPLETED),
            created_at=created_at,
            completed_at=completed_at,
        )


@dataclass(frozen=True)
class UsageSnapshot:
    """Aggregate values used by all UI and session-service projections."""

    session_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    usage_status: str
    cost_status: str
    calls: int
    known_calls: int
    unavailable_calls: int
    provisional_calls: int
    durability_status: str = DurabilityStatus.DURABLE

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class UsageCall:
    """Handle identifying one active provider call."""

    record_id: str
    run_id: str
    call_index: int


class UsageLedger:
    """Durable, idempotent usage ledger for one session."""

    def __init__(
        self,
        session_id: str,
        *,
        journal: "ConversationJournal | None" = None,
        legacy_token_events: Iterable[object] = (),
        conversation_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not session_id or "/" in session_id or "\\" in session_id:
            raise ValueError("session_id must be a non-empty safe identifier")
        self.session_id = session_id
        self.conversation_id = conversation_id or session_id
        if not self.conversation_id or "/" in self.conversation_id or "\\" in self.conversation_id:
            raise ValueError("conversation_id must be a non-empty safe identifier")
        self._journal = journal
        self._clock = clock
        self._records: dict[str, UsageRecord] = {}
        self._active: dict[str, UsageRecord] = {}
        self._next_index: dict[str, int] = {}
        self._subscribers: list[Callable[[UsageSnapshot], None]] = []
        self._projection: Callable[[UsageSnapshot], None] | None = None
        self._persistence_failed = False

        has_canonical_records = False
        if journal is not None:
            for raw in journal.fold_usage_records():
                record = UsageRecord.from_mapping(raw)
                if record is not None and record.session_id == session_id:
                    self._records[record.record_id] = record
                    self._advance_index(record)
                    has_canonical_records = True
        # Canonical journal records supersede rendered legacy token events.
        # Besides preventing double counting, this keeps a modern session from
        # synchronously scanning a potentially enormous conversation.jsonl at
        # startup when the caller supplied a lazy legacy projection.
        if not has_canonical_records:
            self._import_legacy(legacy_token_events)

    @classmethod
    def open(
        cls,
        session_id: str,
        *,
        journal: "ConversationJournal | None" = None,
        legacy_token_events: Iterable[object] = (),
        conversation_id: str | None = None,
    ) -> "UsageLedger":
        """Construct a ledger from the current journal and legacy events."""
        return cls(
            session_id,
            journal=journal,
            legacy_token_events=legacy_token_events,
            conversation_id=conversation_id,
        )

    def subscribe(self, callback: Callable[[UsageSnapshot], None]) -> Callable[[], None]:
        """Subscribe to aggregate changes and return an unsubscribe callback."""
        self._subscribers.append(callback)
        return lambda: self._unsubscribe(callback)

    def bind_conversation_store(self, store: "ConversationStore") -> Callable[[], None]:
        """Project all ledger snapshots into legacy reactive counters."""

        def project(snapshot: UsageSnapshot) -> None:
            store.tokens_in.set(snapshot.input_tokens)
            store.tokens_out.set(snapshot.output_tokens)
            store.cost_usd.set(snapshot.cost_usd)
            usage_status = getattr(store, "usage_status", None)
            if usage_status is not None:
                usage_status.set(snapshot.usage_status)
            cost_status = getattr(store, "cost_status", None)
            if cost_status is not None:
                cost_status.set(snapshot.cost_status)
            usage_calls = getattr(store, "usage_calls", None)
            if usage_calls is not None:
                usage_calls.set(snapshot.calls)

        self._projection = project
        project(self.snapshot())
        return self.subscribe(project)

    def begin_call(
        self,
        *,
        run_id: str,
        provider: str = "unknown",
        model: str = "unknown",
        category: str = UsageCategory.AGENT,
        agent_id: str = "",
        agent_name: str = "",
    ) -> UsageCall:
        """Create a local identity before a provider request is sent."""
        call_index = self._next_index.get(run_id, 0) + 1
        self._next_index[run_id] = call_index
        record_id = f"{run_id}:{call_index}:{uuid.uuid4().hex[:12]}"
        now = self._clock()
        self._active[record_id] = UsageRecord(
            record_id=record_id,
            session_id=self.session_id,
            conversation_id=self.conversation_id,
            run_id=run_id,
            agent_id=agent_id,
            agent_name=agent_name,
            call_index=call_index,
            provider=provider,
            model=model,
            category=category,
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            cost_usd=None,
            cost_status=CostStatus.UNAVAILABLE,
            source=UsageSource.UNKNOWN,
            lifecycle=UsageLifecycle.PROVISIONAL,
            created_at=now,
            completed_at=None,
        )
        self._notify()
        return UsageCall(record_id, run_id, call_index)

    def observe(
        self,
        call: UsageCall,
        usage: object | None,
        *,
        source: str = UsageSource.CHUNK,
        cost_usd: float | None = None,
        cost_status: str | None = None,
    ) -> None:
        """Replace a call's provisional values with the latest observation."""
        current = self._active.get(call.record_id)
        if current is None:
            return
        values = UsageValues.from_provider(usage)
        cost, status = self._cost_for(
            values,
            current.model,
            cost_usd=cost_usd,
            cost_status=cost_status,
        )
        self._active[call.record_id] = replace(
            current,
            input_tokens=values.input_tokens,
            output_tokens=values.output_tokens,
            cache_read_tokens=values.cache_read_tokens,
            cache_write_tokens=values.cache_write_tokens,
            cost_usd=cost,
            cost_status=status,
            source=source,
        )
        self._notify()

    def complete(
        self,
        call: UsageCall,
        usage: object | None = None,
        *,
        source: str = UsageSource.MODEL_CALL_COMPLETE,
        cost_usd: float | None = None,
        cost_status: str | None = None,
        lifecycle: str = UsageLifecycle.COMPLETED,
    ) -> None:
        """Commit or idempotently reconcile one call."""
        # A completed call is immutable from the caller's perspective.  A
        # repeated completion signal therefore becomes a true no-op instead
        # of replacing the record or changing the aggregate.
        current = self._active.pop(call.record_id, None)
        if current is None:
            return
        values = UsageValues.from_provider(usage)
        if not values.known and current.values.known:
            values = current.values
        cost, status = self._cost_for(
            values,
            current.model,
            cost_usd=cost_usd if cost_usd is not None else current.cost_usd,
            cost_status=cost_status if cost_status is not None else current.cost_status,
        )
        now = self._clock()
        updated = replace(
            current,
            input_tokens=values.input_tokens,
            output_tokens=values.output_tokens,
            cache_read_tokens=values.cache_read_tokens,
            cache_write_tokens=values.cache_write_tokens,
            cost_usd=cost,
            cost_status=status,
            source=source,
            lifecycle=lifecycle,
            completed_at=now,
        )
        if self._records.get(call.record_id) == updated:
            self._notify()
            return
        self._records[call.record_id] = updated
        self._persist(updated)
        self._notify()

    def reconcile_run(
        self,
        *,
        run_id: str,
        usage: object | None,
        cost_usd: float | None = None,
    ) -> None:
        """Repair only the residual of a cumulative run-completion signal."""
        target = UsageValues.from_provider(usage)
        run_records = [record for record in self._records.values() if record.run_id == run_id]
        known = _aggregate_values(run_records)
        residual = target.subtract_known(known)
        cost_known = sum(record.cost_usd or 0.0 for record in run_records)
        residual_cost = None
        if cost_usd is not None and math.isfinite(cost_usd):
            residual_cost = max(0.0, cost_usd - cost_known)
        if not residual.any_tokens and not (residual_cost and residual_cost > 0):
            return
        call = self.begin_call(
            run_id=run_id,
            provider="unknown",
            model="unknown",
            category=UsageCategory.AGENT,
        )
        self.complete(
            call,
            residual if residual.known else None,
            source=UsageSource.RUN_COMPLETE,
            cost_usd=residual_cost if residual_cost and residual_cost > 0 else None,
            lifecycle=UsageLifecycle.COMPLETED,
        )

    def finalize_run(self, run_id: str, *, lifecycle: str) -> None:
        """Persist in-flight calls when a run is cancelled or fails."""
        for record in tuple(self._active.values()):
            if record.run_id != run_id:
                continue
            call = UsageCall(record.record_id, record.run_id, record.call_index)
            self.complete(call, source=UsageSource.RECONCILED, lifecycle=lifecycle)

    def snapshot(self) -> UsageSnapshot:
        """Return a consistent aggregate of completed and provisional calls."""
        records = [*self._records.values(), *self._active.values()]
        values = _aggregate_values(records)
        costs = [record for record in records if record.cost_usd is not None]
        unknown = [record for record in records if not record.is_known]
        provisional = [record for record in records if record.is_provisional]
        if not records:
            usage_status = UsageQuality.UNAVAILABLE
        elif unknown or provisional:
            usage_status = UsageQuality.PARTIAL
        else:
            usage_status = UsageQuality.COMPLETE
        if not costs:
            cost_status = CostStatus.UNAVAILABLE
        elif any(record.cost_status == CostStatus.UNAVAILABLE for record in records):
            cost_status = CostStatus.UNAVAILABLE
        elif any(record.cost_status == CostStatus.ESTIMATED for record in costs):
            cost_status = CostStatus.ESTIMATED
        else:
            cost_status = CostStatus.AUTHORITATIVE
        return UsageSnapshot(
            session_id=self.session_id,
            input_tokens=values.input_tokens or 0,
            output_tokens=values.output_tokens or 0,
            cache_read_tokens=values.cache_read_tokens or 0,
            cache_write_tokens=values.cache_write_tokens or 0,
            cost_usd=sum(record.cost_usd or 0.0 for record in costs),
            usage_status=usage_status,
            cost_status=cost_status,
            calls=len(records),
            known_calls=len(records) - len(unknown),
            unavailable_calls=len(unknown),
            provisional_calls=len(provisional),
            durability_status=(
                DurabilityStatus.DEGRADED if self._persistence_failed else DurabilityStatus.DURABLE
            ),
        )

    def records(self, *, include_provisional: bool = False) -> tuple[UsageRecord, ...]:
        """Return immutable records for diagnostics and tests."""
        values = list(self._records.values())
        if include_provisional:
            values.extend(self._active.values())
        return tuple(sorted(values, key=lambda record: (record.created_at, record.record_id)))

    def _cost_for(
        self,
        values: UsageValues,
        model: str,
        *,
        cost_usd: float | None,
        cost_status: str | None,
    ) -> tuple[float | None, str]:
        if cost_usd is not None and math.isfinite(cost_usd) and cost_usd >= 0:
            return cost_usd, cost_status or CostStatus.ESTIMATED
        if not values.known:
            return None, CostStatus.UNAVAILABLE
        # Use the provider's own helper when present only through the original
        # object is impossible here; the caller can pass its estimate. Keeping
        # this explicit prevents the ledger from inventing a price for an
        # unknown model.
        return None, CostStatus.UNAVAILABLE

    def _persist(self, record: UsageRecord) -> None:
        if self._journal is not None:
            try:
                self._journal.append_usage_record(record.to_dict())
            except OSError:
                # The caller still receives the live projection, but the
                # snapshot exposes the record as non-durable through the
                # session's existing error path. Never break the provider loop.
                self._persistence_failed = True

    def _notify(self) -> None:
        snapshot = self.snapshot()
        if self._projection is not None:
            try:
                self._projection(snapshot)
            except Exception:  # noqa: BLE001
                pass
        for subscriber in tuple(self._subscribers):
            try:
                subscriber(snapshot)
            except Exception:  # noqa: BLE001
                pass

    def _advance_index(self, record: UsageRecord) -> None:
        self._next_index[record.run_id] = max(
            self._next_index.get(record.run_id, 0), record.call_index
        )

    def _import_legacy(self, events: Iterable[object]) -> None:
        for event in events:
            event_id = getattr(event, "event_id", None)
            payload = getattr(event, "payload", None)
            if not isinstance(event_id, str) or not isinstance(payload, Mapping):
                continue
            record_id = f"legacy:{event_id}"
            if record_id in self._records:
                continue
            inp = _optional_int(payload.get("input_tokens"))
            out = _optional_int(payload.get("output_tokens"))
            cost = _finite_float(payload.get("cost_usd"))
            self._records[record_id] = UsageRecord(
                record_id=record_id,
                session_id=self.session_id,
                conversation_id=self.conversation_id,
                run_id="legacy",
                agent_id="",
                agent_name="",
                call_index=len(self._records) + 1,
                provider="unknown",
                model="unknown",
                category=UsageCategory.AGENT,
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=None,
                cache_write_tokens=None,
                cost_usd=cost,
                cost_status=CostStatus.ESTIMATED if cost is not None else CostStatus.UNAVAILABLE,
                source=UsageSource.LEGACY,
                lifecycle=UsageLifecycle.COMPLETED,
                created_at=_finite_float(getattr(event, "timestamp", None)) or self._clock(),
                completed_at=_finite_float(getattr(event, "timestamp", None)) or self._clock(),
            )

    def _unsubscribe(self, callback: Callable[[UsageSnapshot], None]) -> None:
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass


class UsageRunTracker:
    """Correlate stream chunks and lauren-ai lifecycle signals for one run."""

    def __init__(
        self,
        ledger: UsageLedger,
        *,
        run_id: str,
        provider: str,
        model: str,
        category: str = UsageCategory.AGENT,
        agent_id: str = "",
        agent_name: str = "",
    ) -> None:
        self.ledger = ledger
        self.run_id = run_id
        self.provider = provider
        self.model = model
        self.category = category
        self.agent_id = agent_id
        self.agent_name = agent_name
        self._current: UsageCall | None = None
        self._awaiting_model_start = False

    @property
    def sink(self) -> "UsageSignalSink":
        return UsageSignalSink(self)

    def ensure_call(self) -> UsageCall:
        if self._current is None:
            self._current = self.ledger.begin_call(
                run_id=self.run_id,
                provider=self.provider,
                model=self.model,
                category=self.category,
                agent_id=self.agent_id,
                agent_name=self.agent_name,
            )
            self._awaiting_model_start = True
        return self._current

    def observe_chunk(self, usage: object) -> None:
        call = self.ensure_call()
        self._awaiting_model_start = False
        cost = _provider_cost(usage, self.model)
        self.ledger.observe(call, usage, source=UsageSource.CHUNK, cost_usd=cost)

    def on_model_started(self) -> None:
        """Open the next call without reopening a completed call on duplicates."""
        if self._current is None:
            self.ensure_call()
            return
        if self._awaiting_model_start:
            self._awaiting_model_start = False
            return
        # A provider can fail to emit ModelCallComplete before beginning its
        # next call. Preserve the observed partial data as one call, then give
        # the new request a distinct identity.
        self.ledger.complete(
            self._current,
            source=UsageSource.RECONCILED,
            lifecycle=UsageLifecycle.PARTIAL,
        )
        self._current = None
        self.ensure_call()

    def on_model_complete(self, signal: object) -> None:
        # A duplicate completion after the call was committed is harmless.
        # The next ModelCallStarted/chunk opens the next call explicitly.
        if self._current is None:
            return
        call = self.ensure_call()
        signal_usage = getattr(signal, "usage", None)
        current = next(
            (
                record
                for record in self.ledger.records(include_provisional=True)
                if record.record_id == call.record_id
            ),
            None,
        )
        observed = current.values if current is not None else UsageValues()
        signal_values = UsageValues.from_provider(signal_usage)
        # Lauren-ai may construct an empty TokenUsage object when the
        # provider omitted usage entirely. Treat an all-zero signal as
        # unavailable unless a chunk already established a known value; this
        # preserves genuine known-zero chunk usage without inventing zero.
        usage = signal_usage if signal_values.any_tokens else None
        reported_cost = _reported_cost(getattr(signal, "cost_usd", None))
        if not observed.known and usage is None:
            self.ledger.complete(call, None, source=UsageSource.MODEL_CALL_COMPLETE)
        else:
            self.ledger.complete(
                call,
                _merge_values(observed, signal_values)
                if usage is not None
                else _values_as_object(observed),
                source=UsageSource.RECONCILED
                if observed.known
                else UsageSource.MODEL_CALL_COMPLETE,
                cost_usd=reported_cost if reported_cost is not None else None,
            )
        self._current = None
        self._awaiting_model_start = False

    def on_run_complete(self, signal: object) -> None:
        usage = getattr(signal, "total_usage", None)
        cost = _finite_float(getattr(signal, "total_cost_usd", None))
        self.ledger.reconcile_run(run_id=self.run_id, usage=usage, cost_usd=cost)

    def finalize(self, lifecycle: str) -> None:
        self.ledger.finalize_run(self.run_id, lifecycle=lifecycle)
        self._current = None


class UsageSignalSink:
    """Lauren-ai ``EventSink`` adapter for :class:`UsageRunTracker`."""

    def __init__(self, tracker: UsageRunTracker) -> None:
        self._tracker = tracker

    async def on_signal(self, signal: object) -> None:
        name = type(signal).__name__
        if name == "ModelCallStarted":
            self._tracker.on_model_started()
        elif name == "ModelCallComplete":
            self._tracker.on_model_complete(signal)
        elif name == "AgentRunComplete":
            self._tracker.on_run_complete(signal)


def summarize_usage_records(records: Iterable[object]) -> dict[str, object]:
    """Return a JSON-safe aggregate for session export/inspection."""
    decoded: list[UsageRecord] = []
    for value in records:
        mapping = value if isinstance(value, Mapping) else None
        if mapping is None:
            continue
        record = UsageRecord.from_mapping(mapping)
        if record is not None:
            decoded.append(record)
    values = _aggregate_values(decoded)
    known = [record for record in decoded if record.is_known]
    costs = [record for record in decoded if record.cost_usd is not None]
    quality = (
        UsageQuality.UNAVAILABLE
        if not decoded
        else (UsageQuality.PARTIAL if len(known) != len(decoded) else UsageQuality.COMPLETE)
    )
    cost_status = (
        CostStatus.UNAVAILABLE
        if not costs or any(record.cost_status == CostStatus.UNAVAILABLE for record in decoded)
        else CostStatus.ESTIMATED
        if any(record.cost_status == CostStatus.ESTIMATED for record in costs)
        else CostStatus.AUTHORITATIVE
    )
    return {
        "input": values.input_tokens or 0,
        "output": values.output_tokens or 0,
        "cache_read": values.cache_read_tokens or 0,
        "cache_write": values.cache_write_tokens or 0,
        "cost_usd": sum(record.cost_usd or 0.0 for record in costs),
        "status": quality,
        "cost_status": cost_status,
        "calls": len(decoded),
        "known_calls": len(known),
        "unavailable_calls": len(decoded) - len(known),
    }


def _aggregate_values(records: Iterable[UsageRecord]) -> UsageValues:
    result = UsageValues()
    for record in records:
        result = result.add(record.values)
    return result


def _provider_cost(usage: object | None, model: str) -> float | None:
    method = getattr(usage, "cost_usd", None)
    if not callable(method):
        return None
    try:
        value = float(method(model))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _reported_cost(value: object) -> float | None:
    """Treat lauren-ai's default ``0.0`` as missing cost information."""
    number = _finite_float(value)
    return number if number is not None and number > 0 else None


def _merge_values(observed: UsageValues, reported: UsageValues) -> UsageValues:
    """Prefer completion fields while retaining fields omitted by a signal."""
    return UsageValues(
        input_tokens=(
            reported.input_tokens if reported.input_tokens is not None else observed.input_tokens
        ),
        output_tokens=(
            reported.output_tokens if reported.output_tokens is not None else observed.output_tokens
        ),
        cache_read_tokens=(
            reported.cache_read_tokens
            if reported.cache_read_tokens is not None
            else observed.cache_read_tokens
        ),
        cache_write_tokens=(
            reported.cache_write_tokens
            if reported.cache_write_tokens is not None
            else observed.cache_write_tokens
        ),
    )


def _values_as_object(values: UsageValues) -> object:
    """Create a tiny adapter for preserving a previously observed value."""
    return values


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _nonnegative_int(value: object) -> int:
    return _optional_int(value) or 0


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return left if right is None and left is not None else right if left is None else None
    return left + right


def _residual(target: int | None, current: int | None) -> int | None:
    if target is None:
        return None
    if current is None:
        return target
    return max(0, target - current)
