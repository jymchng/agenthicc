"""Unified Command System (PRD-44, PRD-45)."""

from .command import Command, CommandContext, CommandHandler, MenuFactory
from .registry import UnifiedCommandRegistry
from .dispatcher import CommandDispatcher
from .builtins import build_builtin_registry, BUILTIN_COMMANDS

__all__ = [
    "Command",
    "CommandContext",
    "CommandHandler",
    "MenuFactory",
    "UnifiedCommandRegistry",
    "CommandDispatcher",
    "build_builtin_registry",
    "BUILTIN_COMMANDS",
]
