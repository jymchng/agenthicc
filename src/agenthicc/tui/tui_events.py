"""TUI event dataclasses and EventBus.

Every state change in the system is represented as an explicit, typed Event
object.  Components subscribe to the event types they care about and are
notified synchronously on the calling thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Type, TypeVar

T = TypeVar("T")


# ── Base event ────────────────────────────────────────────────────────────────

@dataclass
class Event:
    """Base class for all TUI events."""


# ── Transcript events ─────────────────────────────────────────────────────────

@dataclass
class UserMessageEvent(Event):
    text: str


@dataclass
class AssistantStartEvent(Event):
    agent_id: str
    model_short: str


@dataclass
class AssistantChunkEvent(Event):
    agent_id: str
    chunk: str


@dataclass
class AssistantCompleteEvent(Event):
    agent_id: str


@dataclass
class ThinkingStepEvent(Event):
    step: str
    done: bool = False


@dataclass
class ToolStartEvent(Event):
    tool_use_id: str
    name: str
    args: dict = field(default_factory=dict)


@dataclass
class ToolProgressEvent(Event):
    tool_use_id: str
    text: str


@dataclass
class ToolCompleteEvent(Event):
    tool_use_id: str
    name: str
    success: bool
    duration_ms: float | None = None
    error: str | None = None
    diff: str | None = None


@dataclass
class FileModifiedEvent(Event):
    path: str


@dataclass
class ApprovalRequestEvent(Event):
    prompt: str
    command: str


@dataclass
class ApprovalResponseEvent(Event):
    approved: bool


@dataclass
class ErrorEvent(Event):
    message: str
    detail: str = ""


# ── Agent / session events ────────────────────────────────────────────────────

@dataclass
class AgentStateChangeEvent(Event):
    state: str          # "idle" | "thinking" | "running" | "approval" | "error" | "complete"
    tool: str | None = None
    progress: str | None = None


@dataclass
class TokenUpdateEvent(Event):
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class SessionSummaryEvent(Event):
    session_id: str
    completed_agents: int


# ── Input events ──────────────────────────────────────────────────────────────

@dataclass
class InputChangedEvent(Event):
    """Fired by the input loop so the LivePanel redraws the input bar."""
    buf: list[str]
    cursor: int
    paste_condensed: bool = False
    paste_label: str = ""


@dataclass
class InputSubmittedEvent(Event):
    text: str


@dataclass
class ModeChangedEvent(Event):
    name: str
    badge: str


@dataclass
class NotificationEvent(Event):
    """Transient text shown in the footer (e.g. 'Press Ctrl+C again')."""
    text: str | None   # None clears the notification


# ── EventBus ──────────────────────────────────────────────────────────────────

class EventBus:
    """Synchronous pub/sub bus.  Subscribers receive events on the calling thread."""

    def __init__(self) -> None:
        self._subs: dict[type, list[Callable[[Any], None]]] = {}

    def subscribe(self, event_type: Type[T], handler: Callable[[T], None]) -> None:
        self._subs.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: Type[T], handler: Callable[[T], None]) -> None:
        lst = self._subs.get(event_type, [])
        try:
            lst.remove(handler)
        except ValueError:
            pass

    def publish(self, event: Event) -> None:
        for handler in self._subs.get(type(event), []):
            try:
                handler(event)
            except Exception:  # noqa: BLE001
                pass
