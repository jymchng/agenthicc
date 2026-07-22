"""tui.input — reactive input session components."""

from __future__ import annotations

from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.input.completions import CommandSpec, CommandRegistry
from agenthicc.tui.input.history import HistoryNavigator
from agenthicc.tui.input.paste import PasteState
from agenthicc.tui.input.renderer import build_prompt, show_exit_hint

__all__ = [
    "InputBuffer",
    "CommandSpec",
    "CommandRegistry",
    "HistoryNavigator",
    "PasteState",
    "build_prompt",
    "show_exit_hint",
]
