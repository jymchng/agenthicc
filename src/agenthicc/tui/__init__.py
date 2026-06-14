"""agenthicc.tui — Committed-transcript + inline-mode TUI layer."""
from __future__ import annotations

from .terminal import FakeTerminal, Key, Size, Terminal, truncate_to_cols
from .symbols import SPINNER_FRAMES, AGENT_COLORS, MODE_SYMBOLS, MODE_COLORS

__all__ = [
    "FakeTerminal",
    "Key",
    "Size",
    "Terminal",
    "SPINNER_FRAMES",
    "AGENT_COLORS",
    "MODE_SYMBOLS",
    "MODE_COLORS",
    "truncate_to_cols",
]
