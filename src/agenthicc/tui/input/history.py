"""HistoryNavigator — up/down history navigation.

Keeps a saved-current snapshot so Down at the end of history restores
whatever the user was typing before navigating up.
"""
from __future__ import annotations


class HistoryNavigator:
    """Wraps a shared history list with index + saved-current state."""

    def __init__(self, history: list[str]) -> None:
        self._history = history
        self._idx: int = len(history)
        self._saved: list[str] = []

    def up(self, current_buf: list[str]) -> list[str] | None:
        """Return the previous history entry, or ``None`` if at the oldest."""
        if self._idx == len(self._history):
            self._saved = list(current_buf)
        if self._idx > 0:
            self._idx -= 1
            return list(self._history[self._idx])
        return None

    def down(self, current_buf: list[str]) -> list[str] | None:
        """Return the next entry, or saved current, or ``None`` if already newest."""
        if self._idx < len(self._history) - 1:
            self._idx += 1
            return list(self._history[self._idx])
        if self._idx == len(self._history) - 1:
            self._idx = len(self._history)
            return list(self._saved)
        return None

    def commit(self, text: str) -> None:
        """Append *text* to history and reset index."""
        if text:
            self._history.append(text)
        self._idx = len(self._history)
        self._saved = []
