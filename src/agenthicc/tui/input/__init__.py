"""tui.input — idle CBREAK input session components.

Re-exports the main public symbols so existing imports like
``from agenthicc.tui.input.session import run_input_session`` work,
and so ``mention_input.py`` can re-export them for backward compatibility.
"""
from __future__ import annotations

from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.input.history import HistoryNavigator
from agenthicc.tui.input.paste import PasteState
from agenthicc.tui.input.renderer import DropdownState, PromptRenderer, build_prompt, build_footer
from agenthicc.tui.input.session import InputSession, run_input_session

__all__ = [
    "InputBuffer",
    "HistoryNavigator",
    "PasteState",
    "DropdownState",
    "PromptRenderer",
    "build_prompt",
    "build_footer",
    "InputSession",
    "run_input_session",
]
