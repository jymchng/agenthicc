"""Client-neutral session service and transport adapters (PRD-150)."""

import importlib
from typing import Final

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

_LAZY_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    "HttpSessionClient": ("agenthicc.session_service.clients", "HttpSessionClient"),
    "IdeSessionAdapter": ("agenthicc.session_service.clients", "IdeSessionAdapter"),
    "InProcessSessionClient": ("agenthicc.session_service.clients", "InProcessSessionClient"),
    "SessionClient": ("agenthicc.session_service.clients", "SessionClient"),
    "WebSessionAdapter": ("agenthicc.session_service.clients", "WebSessionAdapter"),
    "LocalSessionServer": ("agenthicc.session_service.transport", "LocalSessionServer"),
}


def __getattr__(name: str) -> object:
    """Load HTTP/client adapters only when a caller requests one."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


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
