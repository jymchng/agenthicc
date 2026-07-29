"""TUI reactive runtime — commands and mode manager.

This package implements the reactive runtime layer described in PRD-61 and
PRD-65.
"""

from __future__ import annotations

from agenthicc.tui.runtime.commands import (
    CommandBus,
    Command,
    SendMessageCommand,
    InterruptAgentCommand,
)
from agenthicc.tui.runtime.mode_manager import (
    DEFAULT_MODE_NAME,
    INTERNAL_MODE_NAMES,
    MODE_ALIASES,
    SELECTABLE_MODE_NAMES,
    ModeManager,
    ModeRegistry,
    RuntimeMode,
    UnknownModeError,
    build_safe_mode,
    build_default_registry,
    canonical_mode_name,
)

__all__ = [
    "CommandBus",
    "Command",
    "SendMessageCommand",
    "InterruptAgentCommand",
    "ModeManager",
    "ModeRegistry",
    "RuntimeMode",
    "UnknownModeError",
    "DEFAULT_MODE_NAME",
    "SELECTABLE_MODE_NAMES",
    "INTERNAL_MODE_NAMES",
    "MODE_ALIASES",
    "build_default_registry",
    "build_safe_mode",
    "canonical_mode_name",
]
