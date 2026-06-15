"""TUI reactive runtime — events, commands, tasks, mode manager.

This package implements the reactive runtime layer described in PRD-61 and
PRD-65.  It is separate from ``agenthicc.runtime`` (the kernel agent pool).
"""
from __future__ import annotations

from agenthicc.tui.runtime.domain_events import EventBus, DomainEvent
from agenthicc.tui.runtime.commands import CommandBus, Command, SendMessageCommand, InterruptAgentCommand, OpenOverlayCommand, CloseOverlayCommand, RunBuiltinCommand, ClearConversationCommand
from agenthicc.tui.runtime.tasks import TaskHandle, TaskManager
from agenthicc.tui.runtime.mode_manager import ModeManager, ModeRegistry, RuntimeMode

__all__ = [
    "EventBus", "DomainEvent",
    "CommandBus", "Command",
    "SendMessageCommand", "InterruptAgentCommand",
    "OpenOverlayCommand", "CloseOverlayCommand",
    "RunBuiltinCommand", "ClearConversationCommand",
    "TaskHandle", "TaskManager",
    "ModeManager", "ModeRegistry", "RuntimeMode",
]
