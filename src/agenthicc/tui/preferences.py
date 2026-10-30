"""Read the [ui] accessibility preferences from TUI state (AGX-13)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.config import UISettings

__all__ = ["ui_settings"]


def ui_settings(state: object) -> UISettings | None:
    """The state's [ui] preferences, or ``None`` for state without them (test doubles)."""
    from agenthicc.config import UISettings

    try:
        value = object.__getattribute__(state, "ui")
    except AttributeError:
        return None
    current = value() if callable(value) else value
    return current if isinstance(current, UISettings) else None
