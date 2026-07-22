"""BackendRouter — resolve a path to the appropriate FilesystemBackend."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from agenthicc.tools.fs.backend import FilesystemBackend

if TYPE_CHECKING:
    pass

__all__ = [
    "BackendRouter",
    "configure_router",
    "get_router",
    "_detect_default_backend",
]

# Module-level singleton router instance.
_router: BackendRouter | None = None


def _detect_default_backend(root: str = ".") -> FilesystemBackend:
    """Choose the best available backend for the current runtime environment.

    Priority:
    1. Pyodide / Emscripten — try :class:`PyodideFilesystemBackend`.
    2. Windows — try :class:`WindowsFilesystemBackend`.
    3. Everything else — use :class:`LinuxFilesystemBackend`.

    Non-Linux backends are wrapped in ``try/except ImportError`` so missing
    optional dependencies never break the import chain; the function always
    falls back to :class:`LinuxFilesystemBackend`.
    """
    # Pyodide / Emscripten (browser / WASM)
    if hasattr(sys, "_emscripten_info") or "pyodide" in sys.modules:
        try:
            from agenthicc.tools.fs.pyodide import PyodideFilesystemBackend  # type: ignore[import]

            return PyodideFilesystemBackend(root)
        except ImportError:
            pass

    # Windows
    if os.name == "nt":
        try:
            from agenthicc.tools.fs.windows import WindowsFilesystemBackend  # type: ignore[import]

            return WindowsFilesystemBackend(root)
        except ImportError:
            pass

    # POSIX / Linux (always available)
    from agenthicc.tools.fs.linux import LinuxFilesystemBackend

    return LinuxFilesystemBackend(root)


class BackendRouter:
    """Routes a path string to the appropriate :class:`FilesystemBackend`.

    Backends are registered with a path prefix.  When :meth:`resolve` is
    called the first matching prefix wins.  If no prefix matches the
    *default* backend is returned.

    Example::

        router = BackendRouter()
        router.register("/mnt/s3", s3_backend)
        backend = router.resolve("/mnt/s3/data/file.csv")  # -> s3_backend
        backend = router.resolve("/home/user/notes.txt")   # -> default (Linux)
    """

    def __init__(self, default: FilesystemBackend | None = None) -> None:
        self._routes: list[tuple[str, FilesystemBackend]] = []
        self._default: FilesystemBackend = (
            default if default is not None else _detect_default_backend(".")
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, prefix: str, backend: FilesystemBackend) -> None:
        """Register *backend* to handle all paths that start with *prefix*.

        Later registrations do NOT override earlier ones — first match wins.
        """
        self._routes.append((prefix, backend))

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, path: str) -> FilesystemBackend:
        """Return the backend responsible for *path*.

        Iterates registered ``(prefix, backend)`` pairs in insertion order;
        returns the first backend whose prefix is a leading substring of
        *path*.  Falls back to the default backend when no prefix matches.
        """
        for prefix, backend in self._routes:
            if path.startswith(prefix):
                return backend
        return self._default

    # ------------------------------------------------------------------
    # Default accessor
    # ------------------------------------------------------------------

    @property
    def default(self) -> FilesystemBackend:
        """The fallback backend used when no prefix matches."""
        return self._default


# ---------------------------------------------------------------------------
# Module-level helpers used by agent_tools and other callers.
# ---------------------------------------------------------------------------


def configure_router(router: BackendRouter) -> None:
    """Set the module-level singleton *router* used by :func:`get_router`.

    Call this once during application startup to inject a fully configured
    router (e.g. with cloud backends registered).  Subsequent calls to
    :func:`get_router` will return the provided instance.
    """
    global _router
    _router = router


def get_router() -> BackendRouter:
    """Return the module-level singleton :class:`BackendRouter`.

    If :func:`configure_router` has not been called, a fresh router backed
    by :func:`_detect_default_backend` is created on first access.
    """
    global _router
    if _router is None:
        _router = BackendRouter()
    return _router
