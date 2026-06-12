"""Sandbox primitives: filesystem workspace view and network guard (PRD-04)."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

__all__ = ["NetworkGuard", "WorkspaceView"]


class WorkspaceView:
    """Filesystem view that enforces a path-prefix boundary.

    Every path argument is resolved with :func:`os.path.realpath` (which
    follows symlinks) and checked against the workspace root before any I/O
    is allowed, so ``..`` traversal, absolute paths outside the root, and
    symlink escapes all raise :class:`PermissionError`.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(os.path.realpath(root))

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str | Path) -> Path:
        """Resolve *path* (relative to the root, or absolute) and verify it
        stays inside the workspace.  Raises PermissionError on escape."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = Path(os.path.realpath(candidate))
        if resolved != self._root and self._root not in resolved.parents:
            raise PermissionError(
                f"Path escape attempt: {str(resolved)!r} is outside "
                f"workspace root {str(self._root)!r}"
            )
        return resolved

    def read_text(self, path: str | Path, encoding: str = "utf-8") -> str:
        return self.resolve(path).read_text(encoding=encoding)

    def write_text(self, path: str | Path, content: str, encoding: str = "utf-8") -> int:
        safe = self.resolve(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        return safe.write_text(content, encoding=encoding)

    def exists(self, path: str | Path) -> bool:
        return self.resolve(path).exists()

    def list_dir(self, path: str | Path = ".") -> list[str]:
        return sorted(os.listdir(self.resolve(path)))


class NetworkGuard:
    """Enforces an allow-list of domains for outbound network calls.

    A host is allowed when it equals an allow-listed domain or is a
    subdomain of one (``api.example.com`` matches ``example.com``).
    """

    def __init__(self, allowed_domains: list[str]) -> None:
        self._allowed = [d.strip().lower().lstrip(".") for d in allowed_domains]

    def check(self, url: str) -> None:
        """Raise :class:`PermissionError` unless *url*'s host is allowed."""
        host = (urlparse(url).hostname or "").lower()
        if not host:
            raise PermissionError(f"Cannot determine host for URL {url!r}")
        for domain in self._allowed:
            if host == domain or host.endswith("." + domain):
                return
        raise PermissionError(
            f"Outbound request to {host!r} is not on the network allow-list."
        )
