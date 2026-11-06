"""TUI workspace package (PRD-60, TUX-02).

The workspace owns the terminal for the application lifetime:
- One always-on live block (never started/stopped per agent turn)
- ScrollBufferAppender writes conversation events to the scroll buffer
- ComposerFrame (around ComposerComponent) and StatusLine render into the
  live block
"""

from agenthicc.tui.workspace.workspace import Workspace
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.workspace.components import (
    ComposerComponent,
    ComposerFrame,
    StatusLine,
)

__all__ = [
    "Workspace",
    "ScrollBufferAppender",
    "StatusLine",
    "ComposerComponent",
    "ComposerFrame",
]
