"""Client-neutral session service and transport adapters (PRD-150)."""

from .clients import (
    HttpSessionClient,
    IdeSessionAdapter,
    InProcessSessionClient,
    SessionClient,
    WebSessionAdapter,
)
from .models import (
    CommandResult,
    EventDurability,
    SessionCommand,
    SessionError,
    SessionEvent,
    SessionSnapshot,
    SessionState,
)
from .service import SessionEventStore, SessionService, SessionSubscription
from .transport import LocalSessionServer

__all__ = [
    "CommandResult",
    "EventDurability",
    "HttpSessionClient",
    "IdeSessionAdapter",
    "InProcessSessionClient",
    "LocalSessionServer",
    "SessionClient",
    "SessionCommand",
    "SessionError",
    "SessionEvent",
    "SessionEventStore",
    "SessionService",
    "SessionSnapshot",
    "SessionState",
    "SessionSubscription",
    "WebSessionAdapter",
]
