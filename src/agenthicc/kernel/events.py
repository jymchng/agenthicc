"""Event and Effect types for the event-sourced kernel (PRD-01)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

__all__ = ["Effect", "EffectType", "Event"]


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    timestamp: float
    payload: dict[str, Any]
    source_agent_id: str | None = None
    tool_call_id: str | None = None

    @classmethod
    def create(
        cls,
        event_type: str,
        payload: dict[str, Any],
        source_agent_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> Event:
        return cls(
            event_id=uuid4().hex,
            event_type=event_type,
            timestamp=time.time(),
            payload=payload,
            source_agent_id=source_agent_id,
            tool_call_id=tool_call_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "source_agent_id": self.source_agent_id,
            "tool_call_id": self.tool_call_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            timestamp=data["timestamp"],
            payload=data["payload"],
            source_agent_id=data.get("source_agent_id"),
            tool_call_id=data.get("tool_call_id"),
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
    payload: dict[str, Any] = field(default_factory=dict)
