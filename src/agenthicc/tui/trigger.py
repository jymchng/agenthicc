"""Trigger system — @mention and /command completion triggers."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "MatchItem",
    "TriggerContext",
    "TriggerHandler",
    "TriggerRegistry",
]


@dataclass
class MatchItem:
    display: str = ""
    value: str = ""
    hint: str = ""
    icon: str = ""
    description: str = ""
    # legacy field alias
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.display
        if not self.display:
            self.display = self.label
        # Keep description and hint in sync
        if not self.description and self.hint:
            self.description = self.hint
        elif not self.hint and self.description:
            self.hint = self.description


@dataclass
class TriggerContext:
    """Context for trigger handlers.

    Supports both old-style (cwd/history) and new-style (text/cursor/fragment) fields.
    """
    # New-style fields
    text: str = ""
    cursor: int = 0
    fragment: str = ""
    # Old-style fields used by tests
    cwd: Any = field(default_factory=lambda: Path("."))
    history: list[str] = field(default_factory=list)


@runtime_checkable
class TriggerHandler(Protocol):
    """Protocol for trigger handlers.

    The char-based interface (used by TriggerRegistry tests):
      - char: str — single character that activates this trigger
      - get_matches(fragment, ctx) -> list[MatchItem]
      - on_select(item, fragment, buf) -> list[str]
      - on_cancel(fragment, buf) -> list[str]
      - get_hint(item) -> str | None

    The context-based interface (new-style):
      - can_trigger(ctx) -> bool
      - get_matches(ctx) -> list[MatchItem]
      - apply(ctx, item) -> str
    """
    char: str

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        ...

    def on_select(self, item: Any, fragment: str, buf: list[str]) -> list[str]:
        ...

    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
        ...

    def get_hint(self, item: Any) -> str | None:
        ...


class TriggerRegistry:
    """Registry mapping single trigger characters to TriggerHandler instances."""

    def __init__(self) -> None:
        self._char_handlers: dict[str, Any] = {}
        self._ctx_handlers: list[Any] = []  # new-style protocol handlers

    def register(self, handler: Any) -> None:
        char = getattr(handler, "char", None)
        if char is None or not isinstance(char, str) or len(char) != 1:
            raise ValueError(
                f"TriggerHandler.char must be exactly one character, got {char!r}"
            )
        self._char_handlers[char] = handler

    def get(self, char: str) -> Any | None:
        return self._char_handlers.get(char)

    # New-style protocol API
    def get_active(self, ctx: TriggerContext) -> Any | None:
        for h in self._ctx_handlers:
            if hasattr(h, "can_trigger") and h.can_trigger(ctx):
                return h
        return None

    def get_matches(self, ctx: TriggerContext) -> list[MatchItem]:
        handler = self.get_active(ctx)
        if handler is None:
            return []
        if hasattr(handler, "get_matches"):
            return handler.get_matches(ctx)
        return []

    @property
    def chars(self) -> frozenset[str]:
        return frozenset(self._char_handlers.keys())

    def __len__(self) -> int:
        return len(self._char_handlers)

    def __repr__(self) -> str:
        chars = ", ".join(sorted(self._char_handlers.keys()))
        return f"TriggerRegistry(chars={{{chars}}})"
