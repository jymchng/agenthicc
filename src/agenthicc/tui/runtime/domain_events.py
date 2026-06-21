"""Typed domain events and synchronous EventBus (PRD-61 §2)."""
from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Type, TypeVar

E = TypeVar("E", bound="DomainEvent")


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Base event ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DomainEvent:
    event_id:  str   = field(default_factory=_new_id)
    timestamp: float = field(default_factory=time.time)


# ── User input ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MessageSubmitted(DomainEvent):
    text: str = ""


@dataclass(frozen=True)
class InputChanged(DomainEvent):
    buf:             tuple = ()
    cursor:          int   = 0
    paste_condensed: bool  = False
    paste_label:     str   = ""


# ── Agent lifecycle ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentStarted(DomainEvent):
    turn_id: str = ""
    model:   str = ""


@dataclass(frozen=True)
class AgentCompleted(DomainEvent):
    turn_id: str = ""


@dataclass(frozen=True)
class AgentFailed(DomainEvent):
    turn_id: str = ""
    error:   str = ""


@dataclass(frozen=True)
class AgentInterrupted(DomainEvent):
    turn_id: str = ""


# ── Tool lifecycle ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolStarted(DomainEvent):
    tool_use_id: str = ""
    name:        str = ""
    args:        dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCompleted(DomainEvent):
    tool_use_id:  str   = ""
    name:         str   = ""
    success:      bool  = True
    duration_ms:  float | None = None
    output_lines: tuple = ()
    args_str:     str   = ""


# ── LLM streaming ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TextChunk(DomainEvent):
    turn_id: str = ""
    text:    str = ""


@dataclass(frozen=True)
class TextFinalized(DomainEvent):
    turn_id:   str = ""
    full_text: str = ""


@dataclass(frozen=True)
class TokensAccounted(DomainEvent):
    input_tokens:  int   = 0
    output_tokens: int   = 0
    cost_usd:      float = 0.0


# ── Thinking steps ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ThinkingStepEvent(DomainEvent):
    step: str  = ""
    done: bool = False


# ── System ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ResizeDetected(DomainEvent):
    cols: int = 80
    rows: int = 24


@dataclass(frozen=True)
class OverlayRequested(DomainEvent):
    overlay_name: str = ""


@dataclass(frozen=True)
class OverlayClosed(DomainEvent):
    overlay_name: str = ""


@dataclass(frozen=True)
class FileModifiedEvent(DomainEvent):
    path: str = ""


# ── EventBus ──────────────────────────────────────────────────────────────────

class EventBus:
    """Synchronous pub/sub bus.  All handlers run in the calling thread."""

    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable]] = defaultdict(list)

    def subscribe(
        self,
        event_type: Type[E],
        handler: Callable[[E], None],
    ) -> Callable[[], None]:
        """Register *handler* for *event_type*. Returns an unsubscribe callable."""
        self._handlers[event_type].append(handler)
        return lambda: self._safe_remove(event_type, handler)

    def publish(self, event: DomainEvent) -> None:
        for handler in list(self._handlers.get(type(event), [])):
            try:
                handler(event)
            except Exception:       # noqa: BLE001
                import traceback
                traceback.print_exc()

    def _safe_remove(self, event_type: type, handler: Callable) -> None:
        try:
            self._handlers[event_type].remove(handler)
        except ValueError:
            pass
