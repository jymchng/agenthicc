"""Terminal backend abstraction (PRD-106).

Import the platform-independent interface from here:

    from agenthicc.tui.terminal import get_backend, TerminalBackend

``Key`` remains canonical in ``agenthicc.tui.cbreak_reader`` — all existing
importers continue to work unchanged.
"""

from __future__ import annotations

from agenthicc.tui.terminal.backend import TerminalBackend, get_backend

__all__ = ["TerminalBackend", "get_backend"]
