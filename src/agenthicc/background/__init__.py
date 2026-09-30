"""Durable local background-session services (PRD-141).

The background package owns job lifecycle, worker supervision, and the manager
view.  It deliberately delegates session construction and execution to the
existing runners and workflow registry.
"""

from .model import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    BackgroundAttempt,
    BackgroundSession,
    ModeApplicationStatus,
    SessionStatus,
    legal_transition,
)
from .deletion import DeleteFailure, DeleteResult
from .store import BackgroundPage, BackgroundStore, InvalidSessionTransition, SessionNotFound
from .manager_service import BackgroundManagerService, ManagerOperationResult
from .supervisor import BackgroundSupervisor
from .settings import (
    BackgroundManagerSettings,
    BackgroundSettings,
    background_enabled,
    load_background_settings,
)
from .worker import BackgroundInputService
from .terminals import (
    TerminalManager,
    TerminalRecord,
    TerminalState,
    TerminalStore,
    get_current_terminal_manager,
    reset_current_terminal_manager,
    set_current_terminal_manager,
)

__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "BackgroundAttempt",
    "BackgroundSession",
    "ModeApplicationStatus",
    "BackgroundPage",
    "BackgroundManagerService",
    "DeleteFailure",
    "DeleteResult",
    "ManagerOperationResult",
    "BackgroundStore",
    "BackgroundSupervisor",
    "BackgroundInputService",
    "BackgroundSettings",
    "BackgroundManagerSettings",
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
