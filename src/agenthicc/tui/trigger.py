"""Input Trigger System — generic dropdown architecture (PRD-39).

Defines the data structures and protocol that every trigger handler must
implement.  A TriggerRegistry maps single characters to their handlers;
the state machine in mention_input.py consults the registry on every keystroke.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["MatchItem", "TriggerContext", "TriggerHandler", "TriggerRegistry"]


@dataclass
class MatchItem:
    """One row in the dropdown for any trigger type."""

    display: str  # shown in the dropdown left column (e.g. "+ src/auth.py", "/deploy")
    value: str    # inserted into the buffer on selection (may differ from display)
    hint: str = ""  # optional right-column or below-dropdown annotation


@dataclass
class TriggerContext:
    """Read-only runtime context passed to handlers on every call."""

    cwd: Path
    history: list[str] = field(default_factory=list)


@runtime_checkable
class TriggerHandler(Protocol):
    """One handler per trigger character.  All methods are pure (no I/O).

    Implementations must set ``char`` to the single character that activates
    this handler (e.g. ``"@"``, ``"/"``, ``"#"``).
    """

    #: The single character that activates this handler.
    char: str

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        """Return dropdown rows for the current fragment.

        Called on every keystroke after the trigger character.
        *fragment* is everything the user typed AFTER the trigger char.
        Return an empty list to show the "no matches" state.
        """
        ...

    def on_select(
        self,
        item: MatchItem | None,
        fragment: str,
        buf: list[str],
    ) -> list[str]:
        """Return the new buffer after the user confirms a selection.

        *item* is None only when matches is empty and the user pressed Enter.
        Implementations typically insert ``self.char + item.value`` into *buf*.
        """
        ...

    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
        """Return the new buffer when the user presses ESC.

        Typically restores the literal trigger char + fragment so no input is
        lost.
        """
        ...

    def can_activate(self, buf: list[str]) -> bool:
        """Return True if this trigger should open given the current buffer.

        Called immediately before the state machine switches into trigger mode.
        When this returns False the trigger character is appended to the buffer
        as a literal character instead of opening a dropdown.

        The default implementation always returns True (activate unconditionally).
        Override to restrict activation to specific cursor contexts — for example,
        a slash-command handler should only activate on an empty buffer, while an
        @-mention handler should only activate after whitespace or at position 0.
        """
        return True

    def get_hint(self, item: MatchItem | None) -> str | None:
        """Optional one-line hint shown below the dropdown for the highlighted item.

        Return ``None`` to show no hint (default behaviour for most handlers).
        Example: a slash-command handler returns ``"/model [provider] [model]"``
        when ``/model`` is highlighted.
        """
        return None


class TriggerRegistry:
    """Maps trigger characters to their handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, TriggerHandler] = {}

    def register(self, handler: TriggerHandler) -> None:
        """Register *handler* for its declared trigger character.

        Raises:
            ValueError: if ``handler.char`` is not exactly one character.
        """
        if len(handler.char) != 1:
            raise ValueError(
                f"Trigger char must be exactly one character, got {handler.char!r}"
            )
        self._handlers[handler.char] = handler

    def get(self, char: str) -> TriggerHandler | None:
        """Return the handler registered for *char*, or ``None``."""
        return self._handlers.get(char)

    @property
    def chars(self) -> frozenset[str]:
        """The set of characters that have a registered handler."""
        return frozenset(self._handlers)

    def __repr__(self) -> str:
        return f"TriggerRegistry(chars={sorted(self.chars)})"

    def __len__(self) -> int:
        return len(self._handlers)
