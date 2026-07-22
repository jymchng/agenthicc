"""TUI workspace package (PRD-60).

The workspace owns the terminal for the application lifetime:
- One always-on Rich Live block (never started/stopped per agent turn)
- ScrollBufferAppender writes conversation events to the scroll buffer
- StatusComponent, ComposerComponent, FooterComponent render into the Live block
"""

from agenthicc.tui.workspace.workspace import Workspace
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.workspace.components import (
    StatusComponent,
    ComposerComponent,
    FooterComponent,
)

__all__ = [
    "Workspace",
    "ScrollBufferAppender",
    "StatusComponent",
    "ComposerComponent",
    "FooterComponent",
]
