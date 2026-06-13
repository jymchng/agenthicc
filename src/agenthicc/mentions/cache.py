from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MentionCache"]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        h.update(path.read_bytes())
    except OSError:
        return ""
    return h.hexdigest()


@dataclass
class CacheEntry:
    path: str
    content_hash: str        # SHA-256 of file bytes at injection time
    injected_at_turn: int    # transcript turn index
    chars_used: int


class MentionCache:
    """Per-session cache that tracks which files have been injected and their state."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}  # path → entry
        self._url_cache: dict[str, str] = {}        # url → rendered block (G8)

    def record(
        self,
        path: str,
        resolved: Path,
        chars_used: int,
        turn_index: int,
    ) -> None:
        content_hash = _sha256_file(resolved)
        self._entries[path] = CacheEntry(
            path=path,
            content_hash=content_hash,
            injected_at_turn=turn_index,
            chars_used=chars_used,
        )

    def is_unchanged(self, path: str, resolved: Path) -> bool:
        """True if the file has not changed since it was last injected."""
        entry = self._entries.get(path)
        if entry is None:
            return False
        return _sha256_file(resolved) == entry.content_hash

    def last_turn(self, path: str) -> int | None:
        entry = self._entries.get(path)
        return entry.injected_at_turn if entry else None

    def get_url(self, url: str) -> str | None:
        """Return the cached rendered block for *url*, or None if not cached."""
        return self._url_cache.get(url)

    def set_url(self, url: str, block: str) -> None:
        """Store a rendered URL block in the in-session cache."""
        self._url_cache[url] = block

    def clear(self) -> None:
        self._entries.clear()
        self._url_cache.clear()
