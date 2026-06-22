"""TUI reactive runtime — commands and mode manager.

This package implements the reactive runtime layer described in PRD-61 and
PRD-65.  It is separate from ``agenthicc.runtime`` (the kernel agent pool).
"""
from __future__ import annotations

from agenthicc.tui.runtime.commands import CommandBus, Command, SendMessageCommand, InterruptAgentCommand
from agenthicc.tui.runtime.mode_manager import ModeManager, ModeRegistry, RuntimeMode

__all__ = [
    "CommandBus", "Command",
    "SendMessageCommand", "InterruptAgentCommand",
    "ModeManager", "ModeRegistry", "RuntimeMode",
]
