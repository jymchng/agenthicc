"""Durable local background-session services (PRD-141).

The background package owns job lifecycle, worker supervision, and the agents
hub's service layer.  It deliberately delegates session construction and execution to the
existing runners and workflow registry.

Public names resolve lazily (AGX-01): ``from agenthicc.background import
BackgroundStore`` loads the store, not the worker, terminal manager and
notification delivery.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .deletion import DeleteFailure, DeleteResult
    from .inbox import BackgroundInboxItem, BackgroundInteractionInbox, InboxConflict
    from .index import SessionFilter, SessionOrder
    from .agents_service import AgentsService, ManagerOperationResult
    from .model import (
        ACTIVE_STATUSES,
        TERMINAL_STATUSES,
        BackgroundAttempt,
        BackgroundSession,
        ModeApplicationStatus,
        PriorityChange,
        QueueDispatchState,
        QueuePriority,
        SessionStatus,
        legal_transition,
    )
    from .settings import (
        AgentsHubSettings,
        BackgroundSettings,
        background_enabled,
        load_background_settings,
    )
    from .store import BackgroundWindow, BackgroundStore, InvalidSessionTransition, SessionNotFound
    from .supervisor import BackgroundSupervisor, QueueFullError
    from .terminals import (
        TerminalManager,
        TerminalRecord,
        TerminalState,
        TerminalStore,
        get_current_terminal_manager,
        reset_current_terminal_manager,
        set_current_terminal_manager,
    )
    from .worker import BackgroundInputService

__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "BackgroundAttempt",
    "BackgroundSession",
    "ModeApplicationStatus",
    "PriorityChange",
    "QueueDispatchState",
    "QueuePriority",
    "BackgroundWindow",
    "SessionFilter",
    "SessionOrder",
    "AgentsService",
    "BackgroundInboxItem",
    "BackgroundInteractionInbox",
    "InboxConflict",
    "DeleteFailure",
    "DeleteResult",
    "ManagerOperationResult",
    "BackgroundStore",
    "BackgroundSupervisor",
    "QueueFullError",
    "BackgroundInputService",
    "BackgroundSettings",
    "AgentsHubSettings",
    "InvalidSessionTransition",
    "SessionNotFound",
    "SessionStatus",
    "background_enabled",
    "load_background_settings",
    "TerminalManager",
    "TerminalRecord",
    "TerminalState",
    "TerminalStore",
    "get_current_terminal_manager",
    "reset_current_terminal_manager",
    "set_current_terminal_manager",
    "legal_transition",
]

_EXPORTS: dict[str, str] = {
    "ACTIVE_STATUSES": "model",
    "TERMINAL_STATUSES": "model",
    "BackgroundAttempt": "model",
    "BackgroundSession": "model",
    "ModeApplicationStatus": "model",
    "PriorityChange": "model",
    "QueueDispatchState": "model",
    "QueuePriority": "model",
    "SessionStatus": "model",
    "legal_transition": "model",
    "DeleteFailure": "deletion",
    "DeleteResult": "deletion",
    "BackgroundInboxItem": "inbox",
    "BackgroundInteractionInbox": "inbox",
    "InboxConflict": "inbox",
    "BackgroundWindow": "store",
    "SessionFilter": "index",
    "SessionOrder": "index",
    "BackgroundStore": "store",
    "InvalidSessionTransition": "store",
    "SessionNotFound": "store",
    "AgentsService": "agents_service",
    "ManagerOperationResult": "agents_service",
    "BackgroundSupervisor": "supervisor",
    "QueueFullError": "supervisor",
    "AgentsHubSettings": "settings",
    "BackgroundSettings": "settings",
    "background_enabled": "settings",
    "load_background_settings": "settings",
    "BackgroundInputService": "worker",
    "TerminalManager": "terminals",
    "TerminalRecord": "terminals",
    "TerminalState": "terminals",
    "TerminalStore": "terminals",
    "get_current_terminal_manager": "terminals",
    "reset_current_terminal_manager": "terminals",
    "set_current_terminal_manager": "terminals",
}
_SUBMODULES = frozenset(
    {
        "coverage_gate",
        "deletion",
        "inbox",
        "input_inbox",
        "integration",
        "agents_service",
        "model",
        "notifications",
        "queue",
        "recovery",
        "settings",
        "store",
        "supervisor",
        "terminals",
        "worker",
    }
)


def __getattr__(name: str) -> object:
    if name in _SUBMODULES:
        return importlib.import_module(f".{name}", __name__)
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value: object = vars(importlib.import_module(f".{module}", __name__))[name]
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__, *_SUBMODULES})
