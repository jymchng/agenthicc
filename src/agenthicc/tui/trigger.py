"""Input Trigger System — generic dropdown architecture (PRD-39, PRD-69).

Defines the data structures and protocol that every trigger handler must
implement.  A TriggerManager maps single characters to their handlers;
unified_session.py consults the manager on every keystroke via resolve().
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from agenthicc.tui.cbreak_reader import Key

__all__ = [
    "MatchItem",
    "TriggerContext",
    "TriggerResult",
    "TriggerHandler",
    "TriggerHandlerBase",
    "TriggerManager",
]


@dataclass
class TriggerResult:
    """Typed result returned by TriggerHandler.on_select.

    Carries the new buffer content and optional post-selection behaviour
    signals so callers never have to inspect raw bytes to decide what to do.
    """

    buffer: list[str]  # new buffer content after selection
    submit: bool = False  # if True, dispatch SendMessageCommand immediately
    cursor: int | None = None  # explicit cursor position; None = end of buffer


@dataclass
class MatchItem:
    """One row in the dropdown for any trigger type."""

    display: str  # computed single-line fallback (backwards compat)
    value: str  # text inserted into buffer on selection
    hint: str = ""  # optional below-dropdown annotation

    # Structured fields (PRD-69) — set by handlers that want the overlay to
    # render a two-column layout with description wrapping.
    label: str = ""  # left column  (e.g. "/commands", "@docs/index.md")
    detail: str = ""  # right column — full, untruncated description/path


@dataclass
class TriggerContext:
    """Read-only runtime context passed to handlers on every call."""

    cwd: Path
    session_id: str = ""  # scope results to a session if needed
    command_registry: object = None  # CommandRegistry, for cross-trigger lookups


@runtime_checkable
class TriggerHandler(Protocol):
    """Type-annotation Protocol for trigger handlers (pure specification).

    Defines the full interface a handler must satisfy.  Contains no default
    implementations — defaults live in TriggerHandlerBase so they are
    actually inherited by in-tree handlers.  External plugins that satisfy
    this Protocol structurally (without explicit inheritance) continue to work.
    """

    char: str  # single activation character
    label: str  # human-readable name ("Mention File", "Command", "Shell", "Agent")

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]: ...
    def on_select(self, item: MatchItem | None, fragment: str, buf: list[str]) -> TriggerResult: ...
    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]: ...
    def can_activate(self, buf: list[str]) -> bool: ...
    def get_hint(self, item: MatchItem | None) -> str | None: ...
    def get_lines(self, item: MatchItem, available_width: int) -> list[str]: ...


class TriggerHandlerBase:
    """Concrete mixin that provides default implementations of optional
    TriggerHandler methods.

    In-tree handlers inherit this so that adding a new optional method with a
    sensible default never breaks existing subclasses.  External plugins that
    satisfy TriggerHandler structurally (no explicit inheritance) are unaffected.

    Required abstract methods (get_matches, on_select, on_cancel) have no
    defaults here — subclasses must implement them.
    """

    char: str = ""
    label: str = ""

    def can_activate(self, buf: list[str]) -> bool:
        """Activate unconditionally by default."""
        return True

    def get_hint(self, item: MatchItem | None) -> str | None:
        """No hint by default."""
        return None

    def get_lines(self, item: MatchItem, available_width: int) -> list[str]:
        """Single-line display, clipped to available_width."""
        return [item.display[:available_width]]


class TriggerManager:
    """Maps trigger characters to their handlers.

    The single source of truth for trigger char → handler resolution, including
    key-enum normalisation (Key.AT → "@").  No other file needs to know about
    specific Key.* values for trigger detection.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, TriggerHandler] = {}

    def register(self, handler: TriggerHandler) -> None:
        """Register *handler* for its declared trigger character."""
        if len(handler.char) != 1:
            raise ValueError(f"Trigger char must be exactly one character, got {handler.char!r}")
        self._handlers[handler.char] = handler

    def unregister(self, char: str) -> None:
        """Remove the handler for *char* (no-op if not registered)."""
        self._handlers.pop(char, None)

    def get(self, char: str) -> TriggerHandler | None:
        """Return the handler registered for *char*, or ``None``."""
        return self._handlers.get(char)

    def resolve(self, key: Key, ch: str) -> str | None:
        """Map a (Key, ch) pair to a registered trigger char, or None.

        This is the single place where key-enum normalisation lives.
        Key.AT → "@" if "@" is registered.
        Any other Key.CHAR ch that is registered maps to itself.
        No other file ever inspects Key enums for trigger detection.
        """
        if key == Key.AT:
            return "@" if "@" in self._handlers else None
        if key == Key.CHAR and ch and ch in self._handlers:
            return ch
        return None

    @property
    def chars(self) -> frozenset[str]:
        """The set of characters that have a registered handler."""
        return frozenset(self._handlers)

    def __repr__(self) -> str:
        return f"TriggerManager(chars={sorted(self.chars)})"

    def __len__(self) -> int:
        return len(self._handlers)
