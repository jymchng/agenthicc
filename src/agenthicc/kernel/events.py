"""Event and Effect types for the event-sourced kernel (PRD-01)."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

__all__ = [
    "EVENT_SCHEMA_VERSIONS",
    "PROJECTION_EVENT_TYPES",
    "Effect",
    "EffectType",
    "Event",
    "UnknownEventError",
]

#: Event types that notify subscribers but leave kernel state unchanged. The
#: session service streams them to clients and they are persisted with every
#: other event; the reducer accepts them without a handler. Every other event
#: type must have a reducer handler, or the processor rejects it.
PROJECTION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # The processor's own report of an event the reducer refused.
        "KernelEventRejected",
        "McpCatalogPublished",
        "McpServerFailed",
        "McpServerReady",
        "ProviderRecoveryRetryScheduled",
        "SubagentCompleted",
        "SubagentFailed",
        "SubagentPoolCompleted",
        "SubagentPoolStarted",
        "SubagentStarted",
        "TransportRetryScheduled",
        "UIAdUpdate",
        "WorkflowCheckpointSaved",
        "WorkflowPhaseStarted",
    }
)


#: Event types whose payload contract has a version other than the default 1,
#: or that declare theirs explicitly. ``Event.create`` stamps the declared
#: version, so no emit site hard-codes one. The subagent types carry the
#: SUBAGENT-09 record shape (``agenthicc.subagents.events``).
EVENT_SCHEMA_VERSIONS: Mapping[str, int] = {
    "SubagentCompleted": 1,
    "SubagentFailed": 1,
    "SubagentPoolCompleted": 1,
    "SubagentPoolStarted": 1,
    "SubagentStarted": 1,
}


class UnknownEventError(ValueError):
    """Raised by the reducer for an event type the kernel does not declare."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(
            f"unknown kernel event type {event_type!r}: register a reducer handler "
            "or add it to PROJECTION_EVENT_TYPES"
        )


def _payload_str(
    payload: Mapping[str, object],
    key: str,
    *,
    default: str | None = None,
) -> str:
    value = payload.get(key, default)
    if isinstance(value, str):
        return value
    raise ValueError(f"event payload field {key!r} must be a string")


def _payload_optional_str(
    payload: Mapping[str, object],
    key: str,
    *,
    default: str | None = None,
) -> str | None:
    value = payload.get(key, default)
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"event payload field {key!r} must be a string or null")


def _payload_float(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"event payload field {key!r} must be a number")


def _payload_bool(
    payload: Mapping[str, object],
    key: str,
    *,
    default: bool = False,
) -> bool:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"event payload field {key!r} must be a boolean")


def _payload_mapping(
    payload: Mapping[str, object],
    key: str,
    *,
    default: dict[str, object] | None = None,
) -> dict[str, object]:
    value = payload.get(key, default)
    if value is None:
        return {} if default is None else dict(default)
    if isinstance(value, Mapping) and all(isinstance(item_key, str) for item_key in value):
        return {item_key: item_value for item_key, item_value in value.items()}
    raise ValueError(f"event payload field {key!r} must be an object")


def _payload_string_list(
    payload: Mapping[str, object],
    key: str,
    *,
    default: tuple[str, ...] = (),
) -> list[str]:
    value = payload.get(key, default)
    if isinstance(value, (list, tuple, set, frozenset)) and all(
        isinstance(item, str) for item in value
    ):
        return list(value)
    raise ValueError(f"event payload field {key!r} must be a list of strings")


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    timestamp: float
    payload: dict[str, object]
    source_agent_id: str | None = None
    tool_call_id: str | None = None
    #: The payload contract's version (``EVENT_SCHEMA_VERSIONS``, default 1).
    schema_version: int = 1

    @classmethod
    def create(
        cls,
        event_type: str,
        payload: dict[str, object],
        source_agent_id: str | None = None,
        tool_call_id: str | None = None,
        schema_version: int | None = None,
    ) -> Event:
        return cls(
            event_id=uuid4().hex,
            event_type=event_type,
            timestamp=time.time(),
            payload=payload,
            source_agent_id=source_agent_id,
            tool_call_id=tool_call_id,
            schema_version=(
                schema_version
                if schema_version is not None
                else EVENT_SCHEMA_VERSIONS.get(event_type, 1)
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "source_agent_id": self.source_agent_id,
            "tool_call_id": self.tool_call_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Event:
        version = data.get("schema_version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("event field 'schema_version' must be a positive integer")
        return cls(
            event_id=_payload_str(data, "event_id"),
            event_type=_payload_str(data, "event_type"),
            timestamp=_payload_float(data, "timestamp"),
            payload=_payload_mapping(data, "payload"),
            source_agent_id=_payload_optional_str(data, "source_agent_id"),
            tool_call_id=_payload_optional_str(data, "tool_call_id"),
            schema_version=version,
        )


class EffectType(str, Enum):
    spawn_agent = "spawn_agent"
    execute_tool = "execute_tool"
    update_tui = "update_tui"
    persist_snapshot = "persist_snapshot"
    emit_signal = "emit_signal"
    start_workflow_node = "start_workflow_node"
    assign_task = "assign_task"


@dataclass(frozen=True)
class Effect:
    effect_type: EffectType
    payload: dict[str, object] = field(default_factory=dict)
