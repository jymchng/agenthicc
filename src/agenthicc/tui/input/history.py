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

    def replace_history(self, history: list[str]) -> None:
        """Replace the projection after asynchronous startup loading."""

        self._history = history
        self._idx = len(history)
        self._saved = []

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

    @property
    def entries(self) -> list[str]:
        """The history this navigator walks, oldest first."""
        return self._history


class HistorySearch:
    """Reverse incremental search over prompt history (TUX-04 §2.3).

    Entries match by case-insensitive substring, newest first; a multi-line
    entry matches on any of its lines. Identical entries appear once.
    """

    def __init__(self, history: list[str]) -> None:
        self._history = history
        self.query = ""
        self._position = 0
        self._matches: list[str] = self._find()

    def _find(self) -> list[str]:
        needle = self.query.casefold()
        found: list[str] = []
        seen: set[str] = set()
        for entry in reversed(self._history):
            if entry in seen or needle not in entry.casefold():
                continue
            seen.add(entry)
            found.append(entry)
        return found

    def set_query(self, query: str) -> None:
        """Search for *query*, starting again from the newest match."""
        self.query = query
        self._matches = self._find()
        self._position = 0

    @property
    def matches(self) -> list[str]:
        """Matching entries, newest first."""
        return self._matches

    @property
    def position(self) -> int:
        """Index of the shown match in :attr:`matches`."""
        return self._position

    @property
    def current(self) -> str | None:
        """The shown match, or ``None`` when nothing matches."""
        return self._matches[self._position] if self._matches else None

    def older(self) -> None:
        if self._position + 1 < len(self._matches):
            self._position += 1

    def newer(self) -> None:
        if self._position > 0:
            self._position -= 1
