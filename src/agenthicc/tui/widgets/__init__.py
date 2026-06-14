"""TUI widgets — Claude Code-style layout (PRD-55)."""

from __future__ import annotations

from agenthicc.tui.widgets.command_modals import (
    AgentStatusModal,
    HelpModal,
    HistoryModal,
    ModelsModal,
    SkillsModal,
)
from agenthicc.tui.widgets.dropdown import DropdownWidget
from agenthicc.tui.widgets.footer import Footer
from agenthicc.tui.widgets.header import Header
from agenthicc.tui.widgets.input_panel import InputPanel
from agenthicc.tui.widgets.mode_footer import ModeFooter
from agenthicc.tui.widgets.spinner_panel import SpinnerPanel
from agenthicc.tui.widgets.status_bar import StatusBar
from agenthicc.tui.widgets.transcript_view import TranscriptView
from agenthicc.tui.widgets.trigger_menu import TriggerMenu

__all__ = [
    "AgentStatusModal",
    "DropdownWidget",
    "Footer",
    "Header",
    "HelpModal",
    "HistoryModal",
    "InputPanel",
    "ModelsModal",
    "ModeFooter",
    "SkillsModal",
    "SpinnerPanel",
    "StatusBar",
    "TranscriptView",
    "TriggerMenu",
]
