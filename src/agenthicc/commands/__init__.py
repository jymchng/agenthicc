"""Unified Command System (PRD-44, PRD-45)."""

from .command import (
    BusyPolicy,
    Command,
    CommandContext,
    CommandHandler,
    MenuFactory,
    UsageSnapshot,
)
from .busy_policy import BusyDecision, classify_busy_command
from .registry import UnifiedCommandRegistry
from .dispatcher import CommandDispatcher
from .builtins import build_builtin_registry, BUILTIN_COMMANDS

__all__ = [
    "Command",
    "CommandContext",
    "CommandHandler",
    "BusyPolicy",
    "BusyDecision",
    "UsageSnapshot",
    "classify_busy_command",
    "MenuFactory",
    "UnifiedCommandRegistry",
    "CommandDispatcher",
    "build_builtin_registry",
    "BUILTIN_COMMANDS",
]
