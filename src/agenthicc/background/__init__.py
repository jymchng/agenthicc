"""Durable local background-session services (PRD-141).

The background package owns job lifecycle, worker supervision, and the manager
view.  It deliberately delegates session construction and execution to the
existing runners and workflow registry.
"""

from .model import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    BackgroundSession,
    SessionStatus,
    legal_transition,
)
from .store import BackgroundStore, InvalidSessionTransition, SessionNotFound
from .supervisor import BackgroundSupervisor
from .settings import BackgroundSettings, background_enabled, load_background_settings
from .worker import BackgroundInputService

__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "BackgroundSession",
    "BackgroundStore",
    "BackgroundSupervisor",
    "BackgroundInputService",
    "BackgroundSettings",
    "InvalidSessionTransition",
    "SessionNotFound",
    "SessionStatus",
    "background_enabled",
    "load_background_settings",
    "legal_transition",
]
