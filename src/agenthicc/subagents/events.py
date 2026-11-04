"""The subagent event contract (SUBAGENT-09).

Every subagent emission is one :class:`SubagentEvent`. Each sink projects that
record into its own serialisation instead of defining fields independently:

- the kernel event (``Event.create(row.kernel_type, event.payload())``),
- the conversation-store scroll event (``row.scroll_kind``),
- the worker transcript line (``row.transcript_kinds``, written by
  :class:`~agenthicc.subagents.transcripts.TranscriptWriter` with the same
  ``schema_version``, ``pool_id`` and ``worker_id``),
- the panel row status (``row.panel_statuses``).

:data:`LIFECYCLE_TABLE` maps every transition to what each sink records. A
sink that cannot express a transition says so with ``None`` (or no transcript
kind); no sink invents a state the table does not name.

Field guarantees, for every payload :meth:`SubagentEvent.payload` builds:

- ``schema_version`` (:data:`SUBAGENT_EVENT_SCHEMA_VERSION`), ``event_id``,
  ``at``, ``event`` (the :class:`SubagentEventKind` value) and ``pool_id`` are
  always present; ``worker_id`` and ``attempt`` are present on every
  worker-scoped event; ``session_id`` and ``run_id`` when the pool has them.
- ``text`` and every other free-text field are redacted with the
  session-export redactor *before* they are bounded, and a bounded field
  carries the rule that cut it: ``truncated``/``text_length`` for ``text``,
  ``<key>_truncated``/``<key>_length`` for any other key. Redaction is
  idempotent, so text redacted by the pool and again by the transcript writer
  is unchanged by the second pass.

Result representations (normative; the guide links here):

========================  =======================================  ==================
Representation            Where                                    Size policy
========================  =======================================  ==================
Complete result           ``spawn_subagents`` / ``collect`` tool    unbounded text,
                          result in the parent's tool role          redacted values
Complete journal record   ``subagent_worker_result`` and pool       unbounded, durable
                          lifecycle records in the journal
Bounded preview           kernel and scroll payloads, transcript    redacted, then cut,
                          lines, panel activity                     with markers
========================  =======================================  ==================
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

__all__ = [
    "LIFECYCLE_TABLE",
    "SUBAGENT_EVENT_SCHEMA_VERSION",
    "TRANSCRIPT_MARKER_KINDS",
    "LifecycleRow",
    "SubagentEvent",
    "SubagentEventKind",
    "lifecycle_row",
]

#: The version every subagent kernel, scroll and transcript record carries.
SUBAGENT_EVENT_SCHEMA_VERSION: Final = 1


class SubagentEventKind(StrEnum):
    """Every transition a pool and its workers can undergo."""

    POOL_OPENED = "pool_opened"
    WORKER_SCHEDULED = "worker_scheduled"
    WORKER_STARTED = "worker_started"
    WORKER_TEXT = "worker_text"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    STOP_REQUESTED = "stop_requested"
    MESSAGE_ACKNOWLEDGED = "message_acknowledged"
    WORKER_FINISHED = "worker_finished"
    WORKER_FAILED = "worker_failed"
    POOL_FINISHED = "pool_finished"
    TRANSCRIPT_DEGRADED = "transcript_degraded"


@dataclass(frozen=True, slots=True)
class LifecycleRow:
    """What each sink records for one transition; ``None`` means it records nothing."""

    kind: SubagentEventKind
    kernel_type: str | None
    scroll_kind: str | None
    transcript_kinds: tuple[str, ...]
    #: The panel row statuses the transition can leave (``()`` for none).
    panel_statuses: tuple[str, ...]
    #: Whether the transition belongs to one worker (and so carries ``worker_id``).
    worker_scoped: bool = True


_K = SubagentEventKind

#: The one lifecycle vocabulary, mapped to every sink.
LIFECYCLE_TABLE: Final[tuple[LifecycleRow, ...]] = (
    LifecycleRow(_K.POOL_OPENED, "SubagentPoolStarted", "subagent_pool_started", (), (), False),
    # A retry schedules a new attempt; a first attempt is scheduled with the pool.
    LifecycleRow(_K.WORKER_SCHEDULED, None, None, ("attempt_started",), ("pending",)),
    LifecycleRow(_K.WORKER_STARTED, "SubagentStarted", None, ("prompt",), ("running",)),
    LifecycleRow(_K.WORKER_TEXT, None, None, ("text",), ("running",)),
    LifecycleRow(_K.TOOL_STARTED, None, None, ("tool_started",), ("running",)),
    LifecycleRow(_K.TOOL_FINISHED, None, None, ("tool_finished",), ("running",)),
    LifecycleRow(_K.APPROVAL_REQUESTED, None, None, ("approval_requested",), ("waiting",)),
    LifecycleRow(_K.APPROVAL_RESOLVED, None, None, ("approval_resolved",), ("running",)),
    LifecycleRow(
        _K.STOP_REQUESTED, None, "subagent_worker_stop_requested", ("stop_requested",), ()
    ),
    LifecycleRow(_K.MESSAGE_ACKNOWLEDGED, None, None, ("message_acknowledged",), ()),
    LifecycleRow(
        _K.WORKER_FINISHED, "SubagentCompleted", "subagent_worker_done", ("finished",), ("done",)
    ),
    LifecycleRow(
        _K.WORKER_FAILED,
        "SubagentFailed",
        "subagent_worker_failed",
        ("finished",),
        ("failed", "stopped"),
    ),
    LifecycleRow(_K.POOL_FINISHED, "SubagentPoolCompleted", "subagent_pool_done", (), (), False),
    LifecycleRow(_K.TRANSCRIPT_DEGRADED, None, "subagent_transcript_degraded", (), (), False),
)

#: Transcript lines that mark the file itself rather than a transition.
TRANSCRIPT_MARKER_KINDS: Final = frozenset({"truncated"})

_ROWS: Final[Mapping[SubagentEventKind, LifecycleRow]] = {row.kind: row for row in LIFECYCLE_TABLE}


def lifecycle_row(kind: SubagentEventKind) -> LifecycleRow:
    """The table row for *kind*."""
    return _ROWS[kind]


@dataclass(frozen=True, slots=True)
class SubagentEvent:
    """One subagent emission; every sink serialises a projection of it.

    ``fields`` holds the transition's own payload — already redacted and
    bounded by the pool — and may not redefine an envelope key.
    """

    kind: SubagentEventKind
    pool_id: str
    worker_id: str = ""
    attempt: int = 1
    session_id: str = ""
    run_id: str = ""
    ok: bool | None = None
    fields: Mapping[str, object] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    at: float = field(default_factory=time.time)
    schema_version: int = SUBAGENT_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.pool_id:
            raise ValueError("a subagent event needs a pool_id")
        row = _ROWS[self.kind]
        if row.worker_scoped and not self.worker_id:
            raise ValueError(f"a {self.kind.value} event needs a worker_id")
        clash = _ENVELOPE_KEYS.intersection(self.fields)
        if clash:
            raise ValueError(f"event fields redefine envelope keys: {sorted(clash)}")

    @property
    def row(self) -> LifecycleRow:
        return _ROWS[self.kind]

    def payload(self) -> dict[str, object]:
        """The serialised record, shared by the kernel and scroll projections."""
        record: dict[str, object] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "at": self.at,
            "event": self.kind.value,
            "pool_id": self.pool_id,
        }
        if self.row.worker_scoped:
            record["worker_id"] = self.worker_id
            record["attempt"] = self.attempt
        if self.session_id:
            record["session_id"] = self.session_id
        if self.run_id:
            record["run_id"] = self.run_id
        if self.ok is not None:
            record["ok"] = self.ok
        record.update(self.fields)
        return record


_ENVELOPE_KEYS: Final = frozenset(
    {
        "schema_version",
        "event_id",
        "at",
        "event",
        "pool_id",
        "worker_id",
        "attempt",
        "session_id",
        "run_id",
        "ok",
    }
)
