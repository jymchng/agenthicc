"""Sandbox primitives: filesystem workspace view and network guard (PRD-04)."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Awaitable, Iterator
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

__all__ = ["NetworkGuard", "ResourceLimits", "ToolSandbox", "WorkspaceView"]

_T = TypeVar("_T")


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
        raise PermissionError(f"Outbound request to {host!r} is not on the network allow-list.")


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Best-effort process resource limits for a tool invocation."""

    cpu_seconds: int | None = None
    memory_mb: int | None = None


class ToolSandbox:
    """Shared filesystem, network, timeout, and resource boundary."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        allowed_paths: list[str | Path] | None = None,
        network_allow_list: list[str] | None = None,
        limits: ResourceLimits | None = None,
    ) -> None:
        paths = list(allowed_paths or [])
        if root is not None and not paths:
            paths = [root]
        self._views = tuple(WorkspaceView(path) for path in paths)
        self._network = NetworkGuard(network_allow_list or [])
        self._limits = limits or ResourceLimits()

    @property
    def workspace(self) -> WorkspaceView | None:
        """Primary workspace view, if a path allow-list was configured."""
        return self._views[0] if self._views else None

    @property
    def network(self) -> NetworkGuard:
        return self._network

    @property
    def limits(self) -> ResourceLimits:
        return self._limits

    def resolve(self, path: str | Path) -> Path:
        """Resolve a path against the configured allow-list."""
        if not self._views:
            return Path(os.path.realpath(path))
        for view in self._views:
            try:
                return view.resolve(path)
            except PermissionError:
                continue
        raise PermissionError(f"Path is outside the tool sandbox: {path!r}")

    def check_url(self, url: str) -> None:
        """Enforce the configured network allow-list."""
        self._network.check(url)

    async def run(self, operation: Awaitable[_T], timeout_s: float = 0.0) -> _T:
        """Run an awaitable with the configured timeout and limits."""
        with self.resource_limits():
            if timeout_s > 0:
                return await asyncio.wait_for(operation, timeout=timeout_s)
            return await operation

    @contextmanager
    def resource_limits(self) -> Iterator[None]:
        """Apply and restore POSIX rlimits when configured.

        Resource limits are process-local.  Production callers should run
        untrusted code in a subprocess; this context still gives subprocess
        and controlled test environments a single enforcement hook.
        """
        try:
            import resource  # noqa: PLC0415
        except ImportError:
            yield
            return

        saved: list[tuple[int, tuple[int, int]]] = []
        try:
            try:
                if self._limits.cpu_seconds is not None:
                    saved.append((resource.RLIMIT_CPU, resource.getrlimit(resource.RLIMIT_CPU)))
                    current = saved[-1][1]
                    hard = current[1]
                    soft = self._limits.cpu_seconds
                    resource.setrlimit(resource.RLIMIT_CPU, (soft, max(soft, hard)))
                if self._limits.memory_mb is not None:
                    saved.append((resource.RLIMIT_AS, resource.getrlimit(resource.RLIMIT_AS)))
                    current = saved[-1][1]
                    hard = current[1]
                    soft = self._limits.memory_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (soft, max(soft, hard)))
            except (OSError, ValueError):
                pass
            yield
        finally:
            for limit, values in reversed(saved):
                try:
                    resource.setrlimit(limit, values)
                except (OSError, ValueError):
                    pass
